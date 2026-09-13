# Title-Scout

Find one specific title anywhere, and start its download.

Drop a single title into `find.txt` in the iCloud Torrents folder — "Project Hail Mary",
"Foundation by Isaac Asimov", "Dune Frank Herbert" — and Title-Scout finds the exact work
and downloads it. It is a **one-shot finder**, not a library-builder: no watchlist, no
franchise expansion, no "keep looking for new episodes". One title in, one download out.

It is deliberately **separate from the fleet's media pipeline**. It never reads the
library, never writes `.torrent` files for Torrent-Ingest, and never places anything into
`~/Media`. Its only outputs are a download landing in `Torrents/Scouted` and a record under
`state/found.json`. It was built by extracting the search/verify/download guts of
Torrent-Searcher and re-pointing them at a single-title inbox — it shares none of that
repo's state and writes to none of its folders.

## What it does, concretely

For the title in `find.txt`, one pass:

1. **Interpret** (one DeepSeek call) — turn the raw text into `{title, author, kind,
   format, queries}`. `kind` is `book` / `audiobook` / `manga` / `comic` / `movie` /
   `tv` / `anime` / `other`, and it picks which tracker category and archive.org
   mediatype to search. Queries are the exact title, then title+author and alternate
   spellings — tight, no franchise expansion. `format` is the preferred file format —
   `epub` for a novel, `pdf` for a textbook/reference, `audio`/`video` for those kinds —
   and it biases both the ranking and the final choice.
2. **Search** seven sources for every query:
   - **nyaa.si** — RSS (paginated); `.torrent` (Literature for books/manga, Audio for
     audiobooks).
   - **1337x.to** — HTML scrape (paginated); `.torrent` via magnet (Cloudflare may 403;
     non-fatal).
   - **The Pirate Bay** — apibay.org JSON; magnets (E-books 601, Comics 602,
     Audio books 102, etc.).
   - **eztv** — the TV-focused indexer (plain JSON, no Cloudflare); searched for
     `anime`/`tv`/`movie` requests, where nyaa and 1337x cover western TV poorly.
   - **torrentdownloads** — a general HTML indexer serving `.torrent` files; searched for
     `anime`/`tv`/`movie` requests.
   - **Internet Archive** — advancedsearch + metadata; direct-download `.pdf` / `.epub` /
     `.mp4` / `.mp3` files. This is the "a `.pdf` copy online will do" path: no torrent
     client involved.
   - **Library Genesis** — the standard source for textbooks/papers/ebooks, searched for
     `book`/`manga`/`comic`/`audiobook` requests. Mirrors are tried in order (fork mirrors
     first, since the main `.is`/`.rs`/`.st` mirrors are often down); download is a
     two-hop `ads.php` → `get.php` chain with a session cookie jar.
   - **Anna's Archive** — a *broader hunt*, only when the primary sources above came up
     empty or matched nothing. Its search sits behind a DDoS-Guard JavaScript challenge
     that plain HTTP can't pass, so it is driven through a real headless browser
     (`annas.js` + system Chrome over the DevTools Protocol). Book-like kinds only.
3. **Verify** (one DeepSeek call) — given the candidates, pick the single one that is
   genuinely the *same work* (same title **and** same author where given), not a
   same-name different thing. Only a `high`/`medium`-confidence match is downloaded.
4. **Download** — a direct file (`.pdf`/`.epub`/...) is streamed into `Torrents/Scouted`
   with a sanitized name. A torrent is added to qBittorrent (Web API on `127.0.0.1:8090`,
   the same client the fleet uses) staging into `~/Downloads/.title-scout` on the local
   fast disk — never iCloud, so partial-file writes don't fight the sync — and moved into
   `Torrents/Scouted` the moment qBittorrent reports it complete.

`find.txt` holds one title per line (it is a queue, not a single blob). A title is purged
from the file **only once it is found and downloaded**; a title nothing matched (or a
transient failure) is kept and retried on an increasing backoff (5 min → 10 → 20 → …
capped at 1 h), so an outage never eats your request and a genuinely-unfindable title just
gets re-checked at most once an hour. Each line's outcome is appended to
`state/found.json`.

## Running it

```bash
# one-shot: find whatever is currently in find.txt
python3 scout.py --once

# report what it would download, without downloading
python3 scout.py --once --dry-run

# find a title directly, without touching find.txt
python3 scout.py --title "Project Hail Mary"

# install the launchd daemon (watches find.txt every minute)
./startup.sh

# stop it
./cancel_scout.sh
```

Env knobs: `POLL_INTERVAL_SEC` (default 60), `DEEPSEEK_API_KEY` (defaults to
`~/.config/api-keys/deepseek_key`, the same file the fleet uses), and `ANNAS_ENABLED`
(default on; set `0` to skip the Anna's Archive fallback, or pass `--no-annas`).

## Layout

| file | purpose |
|------|---------|
| `config.py` | paths, sources, DeepSeek, qBittorrent, state-file names |
| `ai.py` | the two DeepSeek calls: `interpret_title` and `verify_match` |
| `sources.py` | nyaa / 1337x / TPB / eztv / torrentdownloads / Internet Archive / Library Genesis / Anna's Archive adapters + download helpers |
| `annas.js` | headless-Chrome (DevTools Protocol) driver for Anna's Archive's DDoS-Guard challenge |
| `scout.py` | the daemon loop, matching, and the actual download (qBittorrent / direct) |

Stdlib-only, like the rest of the fleet: the sibling repos run under different conda
envs, so a module-level `import requests` (or `qbittorrentapi`) would move an ImportError
to daemon start. qBittorrent is driven through its Web API with raw `urllib` multipart.
The only non-stdlib pieces are external *binaries*, not Python deps: qBittorrent's Web
API, and `annas.js` (system Chrome + Node's built-in WebSocket, no npm packages).

## State (gitignored)

All under `state/`:

- `found.json` — the append-only history of every request and its outcome
  (`downloaded` / `unmatched` / `no_candidates` / `failed`).
- `seen.json` — candidate key → first downloaded; re-requesting a title you already
  grabbed is recognised and skipped rather than downloaded twice.
- `.last_find_txt` — safety copy of `find.txt` before any found titles are purged from it.
- `.retry.json` — transient-failure backoff (keeps `find.txt` from being hammered).

## Sources and the Cloudflare caveat

nyaa, the Internet Archive, Library Genesis, and apibay (TPB) are plain-HTTP-friendly.
1337x sits behind Cloudflare, so datacenter/VPN egress gets `403`; when blocked it logs
and continues — the other sources still run. Failures on any one source are non-fatal.

Anna's Archive is a **fallback**, not an always-on source: its search sits behind a
DDoS-Guard JavaScript fingerprint challenge that returns no results without executing a
real browser. It is only searched when the primary sources come up empty (or match
nothing), and only for book-like requests, via `annas.js` which drives the system
Chrome in *headed* mode over the DevTools Protocol. Its `.org`/`.se`/`.li`/`.gs` domains
are suspended or parked as of 2026 — the live mirrors (`annas-archive.pk/.gd/.gl`) are
tried in order. Anna's Archive records that come from Library Genesis share LibGen's
md5, so they download through the same `ads.php` → `get.php` chain; records that only
exist on Anna's Archive's own `nexusstc`/`upload` servers are surfaced as candidates but
not auto-downloaded (their slow partner servers require a browser, a waitlist and their
own verification). If the box is egressing through the Tailscale/Mullvad exit node and
DDoS-Guard refuses to verify, drop the exit node (`tailscale set --exit-node=`) for the
hunt and restore it afterwards.
