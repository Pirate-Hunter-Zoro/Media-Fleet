"""Search adapters for nyaa, 1337x, The Pirate Bay, and the Internet Archive.

One function per source, all returning a uniform list of `Result`. Stdlib-only. Each
adapter is written to be correct against its site's real markup/API; a network/parse
failure on one source is logged and non-fatal -- the others still run.

Three of the four are torrent sources (nyaa/1337x serve `.torrent` files, TPB exposes
magnets). The Internet Archive is the direct-download source: public-domain books, scans
and media with plain `https://archive.org/download/<id>/<file>` URLs, so a `.pdf` copy
can be fetched without a torrent client at all.
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import config


@dataclass
class Result:
    title: str
    source: str                 # "nyaa" | "1337x" | "tpb" | "archive" | "libgen"
    kind: str = "torrent"       # "torrent" | "magnet" | "direct"
    magnet: str | None = None
    torrent_url: str | None = None
    infohash: str | None = None
    direct_url: str | None = None
    filename: str | None = None
    fmt: str | None = None      # "pdf" | "epub" | "mp4" | ... (direct downloads)
    md5: str | None = None      # libgen file identity
    base: str | None = None     # libgen mirror base (for the ads->get download hop)
    seeders: int = 0
    leechers: int = 0
    size_bytes: int | None = None
    score: int = 0

    def key(self) -> str:
        return (self.infohash or self.magnet or self.direct_url
                or self.torrent_url or self.title).strip()


_SIMPLE_EXTENSIONS = {
    "book": (".pdf", ".epub", ".mobi", ".djvu"),
    "manga": (".pdf", ".cbz", ".epub", ".djvu"),
    "comic": (".pdf", ".cbz", ".cbr", ".epub"),
    "movie": (".mp4", ".mkv", ".avi", ".m4v"),
    "tv": (".mp4", ".mkv", ".avi", ".m4v"),
    "anime": (".mp4", ".mkv", ".avi", ".m4v"),
    "audiobook": (".mp3", ".m4b", ".flac", ".ogg"),
    "other": (".pdf", ".epub", ".mp4", ".mp3"),
}


def normalize(title: str) -> str:
    """Loose key for matching a result title to the requested title.

    Lowercases, folds accents to ASCII, drops a leading [Group] tag and a trailing year
    in parentheses, and collapses non-alphanumerics to single spaces.
    """
    import unicodedata
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"^\s*[\[\(][^\]\)]{1,40}[\]\)]", " ", t)
    t = re.sub(r"\(\s*(19|20)\d{2}\s*\)", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4,
               "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def parse_size(text: str) -> int | None:
    """'56.7 GiB' -> bytes; None if unreadable."""
    m = re.match(r"\s*([\d.,]+)\s*([a-z]+)\s*", (text or "").strip().lower())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    unit = _SIZE_UNITS.get(m.group(2))
    return int(num * unit) if unit else None


def _open(url: str, timeout: int = config.HTTP_TIMEOUT, headers: dict | None = None):
    hdr = {"User-Agent": config.USER_AGENT, "Accept": "*/*"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


# --- nyaa.si -----------------------------------------------------------------

def search_nyaa(query: str, category: str = "0_0") -> list[Result]:
    out: list[Result] = []
    seen: set[str] = set()
    # nyaa RSS paginates with `&p=N`. Walk pages until MAX_RESULTS_PER_QUERY distinct
    # results so a rare release buried past page 1 is still reached (the same
    # result-depth fix the Torrent-Searcher got in §5 item 0).
    for page in range(1, 11):
        url = config.NYAA_RSS.format(q=urllib.parse.quote(query), c=category) \
            + f"&p={page}"
        try:
            with _open(url) as resp:
                root = ET.fromstring(resp.read())
        except (urllib.error.URLError, ET.ParseError, OSError) as exc:
            print(f"  [nyaa] search failed for {query!r}: {exc}")
            return out

        items = root.findall(".//item")
        if not items:
            break
        for item in items:
            def local(name: str) -> str:
                el = item.find(name)
                if el is None:
                    el = item.find(f"{{https://nyaa.si/xmlns/nyaa}}{name}")
                return (el.text or "").strip() if el is not None else ""

            title = local("title")
            if not title:
                continue
            info_hash = local("infoHash")
            key = info_hash or local("link")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(Result(
                title=html.unescape(title),
                source="nyaa",
                kind="torrent",
                torrent_url=local("link") or None,
                infohash=info_hash or None,
                seeders=int(local("seeders") or 0),
                leechers=int(local("leechers") or 0),
                size_bytes=parse_size(local("size")),
            ))
        if len(out) >= config.MAX_RESULTS_PER_QUERY or len(items) < 50:
            break
    return out


# --- 1337x.to ----------------------------------------------------------------

_ROW_RE = re.compile(
    r"<td class=\"coll-1 name\">.*?<a href=\"(/torrent/\d+/[^\"]+)\">(.*?)</a>"
    r".*?<td class=\"coll-2 seeds\">(\d+)</td>"
    r".*?<td class=\"coll-3 leeches\">(\d+)</td>",
    re.DOTALL,
)
_MAGNET_RE = re.compile(r"href=\"(magnet:\?[^\"]+)\"")
_INFOHASH_RE = re.compile(r"urn:btih:([0-9a-fA-F]{40})")


def search_1337x(query: str, page: int = 1) -> list[Result]:
    out: list[Result] = []
    seen: set[str] = set()
    # Walk search pages until MAX_RESULTS_PER_QUERY results, so a rare release past page 1
    # is still reached (§5 item 0 result-depth).
    for p in range(page, page + 6):
        url = config.X1337_SEARCH.format(q=urllib.parse.quote(query), page=p)
        try:
            with _open(url) as resp:
                doc = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            print(f"  [1337x] search failed for {query!r}: {exc}")
            return out

        page_new = 0
        for m in _ROW_RE.finditer(doc):
            href, title, seeds, leech = m.group(1), m.group(2), m.group(3), m.group(4)
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            if not title:
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append(Result(
                title=title,
                source="1337x",
                kind="torrent",
                torrent_url=config.X1337_BASE + href,
                seeders=int(seeds or 0),
                leechers=int(leech or 0),
            ))
            page_new += 1
        if len(out) >= config.MAX_RESULTS_PER_QUERY or page_new == 0:
            break
    return out


def fetch_1337x_page(result: Result) -> None:
    """Fill magnet/infohash for a 1337x result by reading its torrent page."""
    if not result.torrent_url:
        return
    try:
        with _open(result.torrent_url) as resp:
            doc = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print(f"  [1337x] page fetch failed: {exc}")
        return
    mm = _MAGNET_RE.search(doc)
    if mm:
        result.magnet = html.unescape(mm.group(1))
        ih = _INFOHASH_RE.search(result.magnet)
        if ih:
            result.infohash = ih.group(1).lower()


# --- The Pirate Bay (via apibay.org) -----------------------------------------

_TPB_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
)


def search_tpb(query: str, cats: tuple[int, ...]) -> list[Result]:
    out: list[Result] = []
    for cat in cats:
        url = config.APIBAY_SEARCH.format(q=urllib.parse.quote(query), c=cat)
        try:
            with _open(url) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print(f"  [tpb] search failed for {query!r} (cat {cat}): {exc}")
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            if not isinstance(e, dict) or e.get("name") in (None, "No results returned"):
                continue
            ih = (e.get("info_hash") or "").lower()
            title = html.unescape(str(e.get("name") or "")).strip()
            magnet = f"magnet:?xt=urn:btih:{ih}&dn={urllib.parse.quote(title)}" if ih else None
            if ih:
                magnet += "&tr=" + "&tr=".join(_TPB_TRACKERS)
            out.append(Result(
                title=title,
                source="tpb",
                kind="magnet",
                magnet=magnet,
                infohash=ih or None,
                seeders=int(e.get("seeders") or 0),
                leechers=int(e.get("leechers") or 0),
                size_bytes=int(e.get("size") or 0) or None,
            ))
        time.sleep(config.REQUEST_DELAY_SEC)
    return out


# --- Internet Archive (direct download) --------------------------------------

def search_archive(query: str, mediatype: str | None = None,
                   exts: tuple[str, ...] = (".pdf",)) -> list[Result]:
    q = query
    if mediatype:
        q = f"({query}) AND mediatype:({mediatype})"
    params = {
        "q": q,
        "fl[]": ["identifier", "title", "creator", "mediatype", "downloads"],
        "rows": str(config.ARCHIVE_MAX_ITEMS),
        "page": "1",
        "output": "json",
    }
    url = config.ARCHIVE_ADV_SEARCH + "?" + urllib.parse.urlencode(params, doseq=True)
    try:
        with _open(url) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"  [archive] search failed for {query!r}: {exc}")
        return []

    docs = ((data.get("response") or {}).get("docs")) or []
    out: list[Result] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        ident = doc.get("identifier")
        if not ident:
            continue
        out.extend(_archive_files(ident, str(doc.get("title") or ""), exts))
        time.sleep(config.REQUEST_DELAY_SEC)
    return out


def _archive_files(ident: str, title: str, exts: tuple[str, ...]) -> list[Result]:
    """List a single archive.org item's directly-downloadable files matching `exts`."""
    url = config.ARCHIVE_METADATA.format(ident=ident)
    try:
        with _open(url) as resp:
            meta = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError, OSError):
        return []
    out: list[Result] = []
    for f in meta.get("files") or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name") or ""
        if not name.lower().endswith(exts):
            continue
        fmt = (f.get("format") or "").lower()
        # Lending-library items expose "ACS Encrypted"/"Encrypted ... PDF" files that are
        # not directly downloadable without a loan; skip them so we only ever offer a URL
        # that will actually serve the file.
        if "encrypted" in fmt or "acs" in fmt:
            continue
        size = f.get("size")
        size_bytes = int(size) if isinstance(size, int) or (isinstance(size, str) and size.isdigit()) else None
        out.append(Result(
            title=title or name,
            source="archive",
            kind="direct",
            direct_url=config.ARCHIVE_DOWNLOAD.format(
                ident=ident, name=urllib.parse.quote(name, safe="/")),
            filename=name.rsplit("/", 1)[-1],
            fmt=name.rsplit(".", 1)[-1].lower() if "." in name else None,
            size_bytes=size_bytes,
        ))
    return out


# --- Library Genesis (direct download for textbooks/ebooks) ------------------

def search_libgen(query: str) -> list[Result]:
    """Search Library Genesis, failing over across mirrors until one responds.

    Returns the first non-empty result set; an unreachable/down mirror is skipped. A
    short timeout keeps the failover quick even when a mirror just hangs.
    """
    for base in config.LIBGEN_MIRRORS:
        url = (f"{base}/index.php?req={urllib.parse.quote(query)}"
               f"&columns%5B%5D=t&columns%5B%5D=a&objects%5B%5D=f&topics%5B%5D=l"
               f"&res={config.MAX_RESULTS_PER_QUERY}")
        try:
            with _open(url, timeout=config.LIBGEN_TIMEOUT_SEC) as resp:
                doc = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            print(f"  [libgen:{base}] unreachable: {exc}")
            continue
        if "md5=" not in doc:
            continue
        results = _parse_libgen(doc, base)
        if results:
            return results
    return []


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _parse_libgen(doc: str, base: str) -> list[Result]:
    """Parse LibGen search-result rows into Results. Each row carries a 32-hex `md5`
    (the file identity), plus title / author / year / size / extension cells."""
    out: list[Result] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", doc, re.DOTALL):
        m = re.search(r"md5=([0-9a-f]{32})", row)
        if not m:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)

        def cell(i: int) -> str:
            return _strip_tags(tds[i]) if i < len(tds) else ""

        title = cell(0)
        tm = re.search(r'edition\.php\?id=\d+">(.*?)</a>', row, re.DOTALL)
        if tm:
            title = _strip_tags(tm.group(1))
        if not title:
            continue
        ext = cell(7).strip().lower()
        out.append(Result(
            title=title,
            source="libgen",
            kind="direct",
            direct_url=f"{base}/ads.php?md5={m.group(1)}",
            md5=m.group(1),
            base=base,
            fmt=ext or None,
            size_bytes=parse_size(cell(6)),
        ))
    return out


def _libgen_download_md5(base: str, md5: str, dest_path) -> int:
    """Download a LibGen file by md5: `ads.php` page -> `get.php?...&key=...` -> bytes.

    The `key` is session-bound, so both hops share one cookie jar. Returns bytes written.
    Raises on any hop that fails (caller fails over to the next mirror).
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    hdr = {"User-Agent": config.USER_AGENT, "Accept": "*/*"}

    req = urllib.request.Request(f"{base}/ads.php?md5={md5}", headers=hdr)
    with opener.open(req, timeout=config.HTTP_TIMEOUT) as resp:  # noqa: S310
        doc = resp.read().decode("utf-8", errors="replace")
    link = re.search(r"get\.php\?md5=[0-9a-f]+&key=[A-Za-z0-9]+", doc)
    if not link:
        raise ValueError("no download link on libgen ads page")
    url = f"{base}/{link.group(0)}"

    written = 0
    req2 = urllib.request.Request(url, headers=hdr)
    with opener.open(req2, timeout=config.HTTP_TIMEOUT) as resp:  # noqa: S310
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
    return written


def libgen_download(result: Result, dest_path) -> int:
    """Download a LibGen result (the original ads->get chain), via its md5."""
    return _libgen_download_md5(result.base or config.LIBGEN_MIRRORS[0],
                                result.md5, dest_path)


# --- Anna's Archive (headless-browser broader hunt) --------------------------

def search_annas_archive(query: str) -> list[Result]:
    """Broader hunt via Anna's Archive, driven by a real headless browser.

    Anna's Archive search is behind a DDoS-Guard JS challenge that plain HTTP cannot
    pass; `annas.js` drives the system Chrome over CDP, waits out the challenge, and
    returns the rendered results as JSON. Mirrors are tried in order (the .org/.se/.li
    /.gs domains are gone and the .pk/.gd/.gl set rotates).
    """
    for domain in config.ANNAS_DOMAINS:
        out = _annas_search_domain(query, domain)
        if out:
            return out
    return []


def _annas_search_domain(query: str, domain: str) -> list[Result]:
    env = dict(os.environ, AA_DOMAIN=domain)
    try:
        proc = subprocess.run(
            [config.NODE_BIN, str(config.ANNAS_JS), "search", query],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [annas:{domain}] search failed for {query!r}: {exc}")
        return []
    if proc.returncode != 0:
        print(f"  [annas:{domain}] annas.js exited {proc.returncode}: "
              f"{proc.stderr.strip()[:200]}")
        return []
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        print(f"  [annas:{domain}] unparseable output from annas.js")
        return []

    out: list[Result] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict) or not r.get("md5"):
            continue
        out.append(Result(
            title=str(r.get("title") or "").strip(),
            source="annas",
            kind="direct",
            md5=str(r["md5"]),
            direct_url=f"https://{domain}/md5/{r['md5']}",
            fmt=str(r.get("ext") or "").lower() or None,
            size_bytes=parse_size(str(r.get("size") or "")),
        ))
    return out


def _annas_detail(md5: str) -> dict:
    """Ask annas.js for a record page's download links (slow partners, libgen, ipfs)."""
    env = dict(os.environ, AA_DOMAIN=config.ANNAS_DOMAINS[0])
    try:
        proc = subprocess.run(
            [config.NODE_BIN, str(config.ANNAS_JS), "slow", md5],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [annas] detail lookup failed for {md5}: {exc}")
        return {}
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


def annas_download(result: Result, dest_path) -> int:
    """Download an Anna's Archive result. Returns bytes written, or 0 on failure.

    Anna's Archive aggregates Library Genesis (lgli/lgrs/zlib); those records share
    LibGen's md5, so the existing ads->get chain downloads them directly. Non-LibGen
    records (nexusstc/upload-only) are not auto-downloaded: their slow partner servers
    require a browser, a waitlist, and their own verification.
    """
    for base in config.LIBGEN_MIRRORS:
        try:
            n = _libgen_download_md5(base, result.md5, dest_path)
            if n > 0:
                return n
        except Exception:  # noqa: BLE001 -- mirror down; try the next
            continue

    # Final fallback: a LibGen file link straight off the record page (libgen.li
    # `file.php?id=...` etc.), which some mirrors serve without the ads->get hop.
    for url in _annas_detail(result.md5).get("libgen_urls") or []:
        if not url.startswith("http"):
            continue
        try:
            n = download_direct(url, dest_path)
            if n > 0:
                return n
        except Exception:  # noqa: BLE001
            continue
    return 0


# --- shared ------------------------------------------------------------------

def search_eztv(query: str) -> list[Result]:
    """eztv JSON API: plain JSON, no Cloudflare. The cheap win for western TV."""
    out: list[Result] = []
    url = config.EZTV_SEARCH.format(q=urllib.parse.quote(query),
                                    n=config.MAX_RESULTS_PER_QUERY)
    try:
        with _open(url) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"  [eztv] search failed for {query!r}: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    for e in data.get("torrents") or []:
        if not isinstance(e, dict):
            continue
        title = html.unescape(str(e.get("title") or "")).strip()
        if not title:
            continue
        ih = (e.get("info_hash") or "").lower()
        magnet = e.get("magnet_url") or ""
        out.append(Result(
            title=title,
            source="eztv",
            kind="magnet" if magnet else "torrent",
            magnet=magnet or None,
            infohash=ih or None,
            torrent_url=e.get("torrent_url") or None,
            seeders=int(e.get("seeds") or 0),
            leechers=int(e.get("peers") or 0),
            size_bytes=int(e.get("size_bytes") or 0) or None,
        ))
    return out


_TD_ROW_RE = re.compile(
    r"<a href=\"(/torrent/\d+/[^\"]+)\"[^>]*>(.*?)</a>", re.DOTALL)
_TD_MAGNET_RE = re.compile(r"href=\"(magnet:\?[^\"]+)\"", re.IGNORECASE)


def search_torrentdownloads(query: str) -> list[Result]:
    """torrentdownloads HTML scrape: general indexer that serves `.torrent` files."""
    url = config.TORRENTDOWNLOADS_SEARCH.format(q=urllib.parse.quote(query))
    try:
        with _open(url) as resp:
            doc = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [torrentdownloads] search failed for {query!r}: {exc}")
        return []
    out: list[Result] = []
    seen: set[str] = set()
    for m in _TD_ROW_RE.finditer(doc):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        title = html.unescape(title).strip()
        if not title or href in seen:
            continue
        seen.add(href)
        out.append(Result(
            title=title,
            source="torrentdownloads",
            kind="torrent",
            torrent_url=config.TORRENTDOWNLOADS_BASE + href.replace("/torrent/",
                                                                    "/download/"),
            seeders=0, leechers=0,
        ))
        if len(out) >= config.MAX_RESULTS_PER_QUERY:
            break
    return out


def search_all(query: str, kind: str) -> list[Result]:
    """Search every reachable source for a query. Failures are logged, not fatal."""
    nyaa_cat = config.NYAA_CATEGORY_BY_KIND.get(kind, "0_0")
    tpb_cats = config.TPB_CATS_BY_KIND.get(kind, (601, 602))
    mediatype = config.ARCHIVE_MEDIATYPE_BY_KIND.get(kind)
    exts = _SIMPLE_EXTENSIONS.get(kind, (".pdf", ".epub"))

    results: list[Result] = []
    results.extend(search_nyaa(query, nyaa_cat))
    time.sleep(config.REQUEST_DELAY_SEC)
    results.extend(search_1337x(query))
    time.sleep(config.REQUEST_DELAY_SEC)
    results.extend(search_tpb(query, tpb_cats))
    # eztv + torrentdownloads carry western TV/movies, the gap nyaa/1337x cover poorly;
    # searched for every video kind (anime/tv/movie), matching the Torrent-Searcher.
    if kind in ("anime", "tv", "movie"):
        time.sleep(config.REQUEST_DELAY_SEC)
        results.extend(search_eztv(query))
        time.sleep(config.REQUEST_DELAY_SEC)
        results.extend(search_torrentdownloads(query))
    time.sleep(config.REQUEST_DELAY_SEC)
    results.extend(search_archive(query, mediatype, exts))
    if kind in config.LIBGEN_KINDS:
        time.sleep(config.REQUEST_DELAY_SEC)
        results.extend(search_libgen(query))
    return results


def download_torrent_bytes(url: str, referer: str | None = None) -> bytes:
    """Fetch a `.torrent` file's bytes from a direct URL."""
    headers = {"Referer": referer} if referer else {}
    with _open(url, headers=headers) as resp:
        data = resp.read()
    if not data or data[:20].lstrip().startswith(b"<!DOCTYPE") or data[:5] == b"<html":
        raise ValueError(f"torrent URL returned HTML, not a torrent: {url}")
    return data


def _bdecode(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"d":
        d, i = {}, i + 1
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            d[k] = v
        return d, i + 1
    if c == b"l":
        l, i = [], i + 1
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            l.append(v)
        return l, i + 1
    if c == b"i":
        e = data.index(b"e", i)
        return int(data[i + 1:e]), e + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        s = colon + 1
        return data[s:s + n], s + n
    raise ValueError("bad bencode")


def infohash_of(torrent_bytes: bytes) -> str | None:
    """SHA1 of the raw bencoded 'info' dict -- the torrent's identity."""
    if not torrent_bytes or torrent_bytes[:1] != b"d":
        return None
    i = 1
    while i < len(torrent_bytes) and torrent_bytes[i:i + 1] != b"e":
        k, i = _bdecode(torrent_bytes, i)
        vstart = i
        _, i = _bdecode(torrent_bytes, i)
        if k == b"info":
            import hashlib
            return hashlib.sha1(torrent_bytes[vstart:i]).hexdigest()
    return None


def magnet_infohash(magnet: str) -> str | None:
    """Extract the 40-hex infohash from a magnet URI, if present."""
    m = re.search(r"urn:btih:([0-9a-fA-F]{40})", magnet or "")
    return m.group(1).lower() if m else None


def download_direct(url: str, dest_path) -> int:
    """Stream a direct URL to `dest_path`, returning bytes written."""
    written = 0
    with _open(url) as resp:
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
    return written
