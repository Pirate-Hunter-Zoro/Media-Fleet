"""Turn a torrent title into structured data: season, episode coverage, resolution.

The one hard, load-bearing job in this repo. Torrent titles are free-form uploader
strings, and everything downstream -- "do we already own this?", "is this a new season?",
"how good is this copy?" -- hinges on reading the numbers out of them reliably.

We deliberately err toward "parse nothing" over "parse wrong": a title we cannot read
comes back as a Parsed object with `None` fields, and callers treat `None` as "not new
enough to act on", never as "new". A false positive wastes a download; a false negative
only costs a re-check next sweep.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Parsed:
    title: str
    group: str | None = None            # leading [Group] or (Group)
    season: int | None = None           # explicit season number, if stated
    ep_min: int | None = None           # lowest episode number mentioned
    ep_max: int | None = None           # highest episode number mentioned
    season_range: tuple[int, int] | None = None  # a MULTI-season pack ("Season 1-8")
    resolution: str | None = None       # "2160p"/"1080p"/"720p"/"480p"/"DVD"
    is_batch: bool = False              # looks like a multi-episode pack
    keywords: list = field(default_factory=list)

    @property
    def episode_count(self) -> int | None:
        if self.ep_min is None or self.ep_max is None:
            return None
        return self.ep_max - self.ep_min + 1


_SEASON_RE = [
    # "S01E05", and also the CONCATENATED multi-episode form "S01E01E02" / "S01E23E24E25".
    # `\b` cannot close this: in "S01E01E02" the character after the episode digits is
    # "E", a word character, so no boundary exists there and the whole pattern failed --
    # taking the SEASON with it. A show that airs two 11-minute segments per slot (Big
    # Hero 6: The Series, Dawn of the Croods, Croods Family Tree) names nearly every file
    # this way, so those releases parsed as having no season AND no episodes at all.
    re.compile(r"\bS(?P<s>\d{1,2})\s*E\d{1,4}(?!\d)", re.IGNORECASE),
    re.compile(r"\bS(?P<s>\d{1,2})\b", re.IGNORECASE),
    # "Season 1" / "Season 10", but NOT "Season 10-bit": a bit-depth marker ("10-bit",
    # "8-bit", "Hi10P", "Ma10p") must never be read as a season number. `(?![\d-])` is the
    # guard: after the digits, the next char must be neither another digit (which would
    # just be a longer number) nor a hyphen (the start of "-bit"). Without rejecting digits
    # too, "Season 10-bit" would backtrack and capture "Season 1".
    # The separator may be whitespace OR the scene-standard dot/underscore:
    # "Season.01" and "Season_01" are as common as "Season 01", and reading them as NO
    # season at all is worse than a miss -- a season release then looks like a
    # complete-series pack, wins the series' single "pack" slot, and beats the real
    # S01-S08 packs (a 480p "That.70s.Show.Season.01" did exactly that).
    # `\b` is not usable on the left here: "_" is a word character, so "Show_Season_03"
    # has no boundary before "Season". An explicit not-alphanumeric lookbehind matches the
    # dot, underscore and space forms alike without matching mid-word.
    re.compile(r"(?<![A-Za-z0-9])Season[\s._]*(?P<s>\d{1,2})(?![\d-])", re.IGNORECASE),
    # Ordinal seasons: "3rd Season", "2nd Season", "1st Season". The bit-depth guard is
    # implicit — "10-bit" has no "th/nd/rd/st" suffix so it cannot match here.
    re.compile(r"\b(?P<s>\d{1,2})(?:st|nd|rd|th)[\s._]+Season\b", re.IGNORECASE),
]

# Ordered most-specific first: a bare "01-52" is a season batch, but "E01-E13" is an
# episode range inside a season. Match the explicit ones first and only fall back to the
# bare dash-range if nothing more specific fired.
_RANGE_RES = [
    # "S01E01E02" / "S01E23E24E25" -- a single file holding consecutive episodes, the
    # standard naming for cartoons that air two or three segments in one slot. Must come
    # FIRST: the single-episode rule below would otherwise never fire on these (its
    # trailing `\b` fails against the next "E") and the release would parse as nothing,
    # so its content key degraded to a title-derived string and no two encodes of the
    # same episodes ever collapsed.
    re.compile(r"\bS\d{1,2}E(?P<a>\d{1,4})(?:E\d{1,4})*E(?P<b>\d{1,4})(?![\dEe])",
               re.IGNORECASE),
    # "S01E01-E52" / "S01E102-E111" / "S01E01-S01E52" -- season-prefixed episode ranges.
    re.compile(r"\bS\d{1,2}\s*E(?P<a>\d{1,4})\s*[-~–]\s*(?:(?:S\d{1,2}\s*)?E)?(?P<b>\d{1,4})\b",
               re.IGNORECASE),
    re.compile(r"\bE(?P<a>\d{1,4})\s*[-~–]\s*E?(?P<b>\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bEp(?:isodes|s)?\.?\s*(?P<a>\d{1,4})\s*[-~–]\s*(?P<b>\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bS\d{1,2}E(?P<a>\d{1,4})(?!\d)", re.IGNORECASE),  # single episode marker
    re.compile(r"\(\s*(?P<a>\d{1,4})\s*[-~–]\s*(?P<b>\d{1,4})\s*\)"),   # "(01-52)"
    re.compile(r"\b(?P<a>\d{1,4})\s*~\s*(?P<b>\d{1,4})\b"),             # "01 ~ 52"
    re.compile(r"\b(?P<a>\d{1,4})\s*[-–]\s*(?P<b>\d{1,4})(?:\s*END)?\b", re.IGNORECASE),
]

_RESOLUTION = re.compile(r"\b(2160p|1080p|720p|480p|576p|1080i)\b", re.IGNORECASE)
_DVD = re.compile(r"\b(DVDRip|DVD-Rip|DVD|BDMV)\b", re.IGNORECASE)
_GROUP = re.compile(r"^\s*[\[\(]([^\]\)]{1,40})[\]\)]")

# A MULTI-SEASON pack advertises a run of seasons, not a run of episodes inside one
# season: "Season 1-8", "Seasons 1 to 8", "S01-S08", "S01 thru S05", and the
# space-separated lists some groups use ("Season 1 2 3 4 5 6 7 8", "S01 S02 S03 S04").
# These are the releases that cover a whole show or a big chunk of it ("That 70s Show
# Season 1-8"). Detecting them up front stops the generic range parser from misreading
# "Season 1-8" as "Season 1, episodes 1-8" -- which is what made a complete-series pack
# read as an already-owned slice and get skipped.
_SEASON_RANGE_RES = [
    re.compile(r"(?<![A-Za-z0-9])Seasons?[\s._]*(?P<a>\d{1,2})[\s._]*"
               r"(?:[-–~]|to|thru|through)[\s._]*(?P<b>\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bS(?P<a>\d{1,2})[\s._]*[-–~][\s._]*S(?P<b>\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bS(?P<a>\d{1,2})[\s._]*(?:to|thru|through)[\s._]*S?(?P<b>\d{1,2})\b",
               re.IGNORECASE),
]

# "Season 1 2 3 4 5 6 7 8" and "S01 S02 S03 S04 S05 S06 S07 S08" — a bare run of THREE
# or more season numbers separated by spaces. Three-plus is the bar so a stray "Season 1"
# followed by an unrelated number is never read as a season list.
_SEASON_LIST_RES = [
    re.compile(r"(?<![A-Za-z0-9])Seasons?[\s._]+((?:\d{1,2}[\s._]+){2,}\d{1,2})\b",
               re.IGNORECASE),
    re.compile(r"\b(S\d{1,2}(?:[\s._]+S\d{1,2}){2,})\b", re.IGNORECASE),
]


def _match_season_range(title: str) -> tuple[int, int] | None:
    for rx in _SEASON_RANGE_RES:
        m = rx.search(title)
        if m:
            a, b = int(m.group("a")), int(m.group("b"))
            if 1 <= a <= 40 and 1 <= b <= 40 and a != b:
                return (min(a, b), max(a, b))
    for rx in _SEASON_LIST_RES:
        m = rx.search(title)
        if m:
            nums = [int(x) for x in re.findall(r"\d{1,2}", m.group(1)) if 1 <= int(x) <= 40]
            if len(nums) >= 3 and max(nums) != min(nums):
                return (min(nums), max(nums))
    return None


def _first_group(m: re.Match) -> str:
    return m.groupdict().get("s") or ""


def parse(title: str) -> Parsed:
    """Parse one torrent title into a Parsed."""
    p = Parsed(title=title.strip())

    g = _GROUP.match(p.title)
    if g:
        p.group = g.group(1).strip()
    else:
        g = re.match(r"^\s*([^\s\-]+)", p.title)  # a bare "AnimeRG.xyz" style prefix
        if g and len(g.group(1)) <= 24 and re.search(r"[\._]", g.group(1)):
            p.group = g.group(1)

    sr = _match_season_range(p.title)
    if sr is not None:
        p.season_range = sr

    for rx in _SEASON_RE:
        m = rx.search(p.title)
        if m and m.group("s"):
            p.season = int(m.group("s"))
            break

    # A multi-season pack's "1-8" is a season run, not an episode span: skip the generic
    # episode-range parser so "Season 1-8" never reads as "Season 1, episodes 1-8".
    if p.season_range is None:
        for rx in _RANGE_RES:
            m = rx.search(p.title)
            if m:
                gd = m.groupdict()
                a = gd.get("a")
                if a is None:
                    continue
                b = gd.get("b")
                lo = hi = int(a)
                if b is not None:
                    lo, hi = min(int(a), int(b)), max(int(a), int(b))
                # Ignore ranges that are obviously not episode counts (years, sizes, IDs).
                if lo < 1 or hi > 4000 or (hi - lo) > 3000:
                    continue
                # A range whose LEFT number is a 4-digit year ("Season 1 (1998 - 99)",
                # "(2010 - 2011)") is a season's air date, never an episode span -- a real
                # episode range starts with a small number (no show reaches episode 1900).
                if 1900 <= int(a) <= 2100:
                    continue
                p.ep_min, p.ep_max = lo, hi
                p.is_batch = lo != hi
                break

    m = _RESOLUTION.search(p.title)
    if m:
        p.resolution = m.group(1).lower()
    elif _DVD.search(p.title):
        p.resolution = "dvd"

    low = p.title.lower()
    for kw in ("batch", "complete", "dual audio", "dual-audio", "remux", "bdrip", "bluray"):
        if kw in low:
            p.keywords.append(kw)

    return p


# Tokens that are too generic to identify a release on their own. Used to (a) drop
# loose one-word search queries ("Fate" on its own matches "Star Wars ... Holocrons of
# Fate") and (b) decide whether a result title is actually about the series we asked for.
_GENERIC_TOKENS = {
    # franchise roots / common words that a bare query turns into a garbage magnet pile
    "fate",
    # English stopwords (function words + pronouns + common verbs). A result must not be
    # considered "the same series" merely because it shares a word like "that" or "show".
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "it", "as", "by", "with", "from",
    "that", "this", "these", "those", "there", "their", "they", "them", "then", "than",
    "not", "no", "nor", "so", "but", "if", "be", "been", "being", "am",
    "have", "has", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "shall", "just", "only", "also", "very",
    "more", "most", "some", "any", "each", "every", "own", "same", "other", "another",
    "we", "you", "he", "she", "i", "me", "my", "your", "his", "her", "its", "our", "their",
    "when", "where", "who", "whom", "whose", "which", "why", "how",
    # generic media qualifiers that never identify a specific show
    "anime", "tv", "movie", "movies", "ova", "season", "seasons", "series",
    "show", "shows", "part", "special", "specials", "complete", "batch", "dual", "audio",
    "sub", "subs", "subbed", "dub", "dubbed", "multi", "all", "eng", "jpn",
    "ep", "eps", "episode", "episodes", "film", "films",
}


def meaningful_tokens(text: str) -> set[str]:
    """The tokens of `text` that actually identify a title: normalized words minus
    numbers and generic qualifiers. A release title must share at least one of these
    with the series it claims to be."""
    toks = set()
    for t in normalize(text).split():
        if t in _GENERIC_TOKENS or t.isdigit():
            continue
        toks.add(t)
    return toks


def normalize(title: str) -> str:
    """A loose key for matching a torrent title to a library series folder name.

    Lowercases, drops a leading group tag and a trailing year in parentheses, folds
    accented characters to ASCII, and collapses non-alphanumerics to single spaces.
    "Pokémon Horizons: The Series (2023)" and "[AnimeRG] Pokemon Horizons" both fall out
    as "pokemon horizons the series".
    """
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = _GROUP.sub(" ", t)
    t = re.sub(r"\(\s*(19|20)\d{2}\s*\)", " ", t)      # drop "(2007)"
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Sortable resolution tier. Higher is "better definition". Used to decide when a later
# release is an upgrade worth re-queuing over one we already dropped.
_RES_RANK = {"2160p": 5, "4k": 5, "uhd": 5, "1080p": 4, "1080i": 4,
             "720p": 3, "480p": 2, "576p": 2, "dvd": 1}


def resolution_rank(resolution: str | None) -> int:
    """Map a Parsed.resolution (or raw token) to a numeric quality tier."""
    if not resolution:
        return 0
    return _RES_RANK.get(resolution.lower(), 0)


def has_dual_audio(title: str) -> bool:
    """True when a release carries two or more audio tracks (the fleet's anime
    requirement). "dual audio" is the common phrasing; "multi-audio"/"multi audio" mean
    the same thing. A bare "dual" (e.g. "dual episode") or a plain "dub" (usually
    English-only) is deliberately NOT enough."""
    low = title.lower()
    return (
        "dual audio" in low or "dual-audio" in low or "dual_audio" in low
        or "multi-audio" in low or "multi audio" in low
    )


# --- manga release classification ---------------------------------------------
#
# Manga is the one content class the searcher must treat as a HIERARCHY rather than a
# flat "episode range": a colored volume beats a black-and-white volume, which beats an
# individual chapter. Reading which form a release is — and whether it is English — is a
# filename problem, and it decides both what we drop and how the drop is scored.

_COLOR_MARKERS = ("colored", "full color", "full-color", "color edition", "colour")

_VOL_WORD_RE = re.compile(r"\bvol(?:ume)?\.?\s*(\d{1,4})\b", re.IGNORECASE)
_V_BARE_RE = re.compile(r"\bv\.?\s*(\d{1,4})\b", re.IGNORECASE)
_CHAPTER_WORD_RE = re.compile(r"\b(?:chapter|chap|ch)\.?\s*(\d{1,4})\b", re.IGNORECASE)
_C_BARE_RE = re.compile(r"\bc\.?\s*(\d{2,4})\b", re.IGNORECASE)
# Western-comic issue marker: "Star Wars #42". A bare "#N" is unambiguous (manga rarely
# uses it), so it is folded into the same "chapter" tier the manga hierarchy already has.
_HASH_ISSUE_RE = re.compile(r"#\s*(\d{1,4})\b")

# A collected edition marker -- "The Complete ElfQuest Vol. 1-4", an Omnibus, a
# Compendium, an "Epic/Complete Collection". Such a title is ONE big collection, NOT its
# first volume, so `manga_kind` must classify it as a collection rather than reading
# "Vol. 1" and mistaking the whole thing for a single already-held volume (§5 item 1a).
_COLLECTION_MARKER_RE = re.compile(
    r"\b(?:omnibus|omnibuses|compendium|compendiums|complete\s+collection|"
    r"complete\s+series|epic\s+collection|complete|collection|collections)\b",
    re.IGNORECASE)

# The "everything" pack among collections: a complete/omnibus/compendium that covers the
# whole run, vs a plain TPB/trade. Only the former may supersede the individual
# volumes/issues it contains (the comic analogue of the torrent complete-pack priority).
_COMPLETE_COLLECTION_RE = re.compile(
    r"\b(?:omnibus|omnibuses|compendium|compendiums|complete)\b", re.IGNORECASE)


def looks_colored(title: str) -> bool:
    """Whether a manga release advertises a COLORED edition by name."""
    low = (title or "").lower()
    return any(m in low for m in _COLOR_MARKERS)


# Best-definition markers for comics. "Digital HD" is the GetComics convention; "HD"/
# "hi-res"/"1080p" cover the torrent-side naming. SD is only ever a DOWNGRADE, so an
# explicit SD marker scores below an untagged release (which is treated as default, not SD).
_COMIC_HD_MARKERS = ("digital hd", "digital-hd", " hi-res", "hi-res", " high res",
                     "high resolution", "1080p", " hd ", "-hd ", "hd scan",
                     # Scanner tags that state quality outright, seen in this library.
                     "digital hq", "digital-hq", "hq scan", "hdcomic", "webrip-hd")
_COMIC_SD_MARKERS = ("digital sd", "digital-sd", " sd scan", "low-res", "low res",
                     "480p", "compressed", "sd quality",
                     # `(SDdigital)` -- no separator, so the two-word forms above miss it.
                     # It is how "Frankenstein Junji Ito Story Collection (2018)
                     # (SDdigital) (tunafan)" scored 0 while being an explicit SD scan.
                     "sddigital", "digital sd)", "(sd)", "sd-digital")

# A scan's stated pixel dimension: `(Digital-1920)`, `[Digital-2048]`, `2560px`. This is
# the only quality signal in a comic filename that is a MEASUREMENT rather than an
# adjective, and it is the one the owner was asking for -- "3 onwards have higher res
# versions. THESE should be what we grab for ElfQuest". A coarse HD/SD flag cannot express
# "2048 beats 1920"; a number can.
# ONLY the forms that actually name a scan dimension. A first version allowed a bare
# 3-4 digit number and, measured over 5876 real titles, matched 3084 of them -- video
# resolutions (`720p`, `1080p`, `2160p`) and JAV product codes (`SSNI-618`, `HND-630`),
# almost none of them scan dimensions. A number only means "pixels" when something in the
# title says so.
_COMIC_PIXELS_RE = re.compile(
    r"(?:digital[\s._-]*(\d{3,4})(?![0-9])|(\d{3,4})\s*px(?![a-z0-9]))", re.IGNORECASE)
# Below this a number in a comic title is a year, an issue number or a release-group id,
# not a scan dimension. Above it, no scan is that big.
_COMIC_PIXELS_MIN = 600
_COMIC_PIXELS_MAX = 4000


def comic_quality(title: str) -> int:
    """+1 for an explicit HD/high-resolution marker, -1 for an explicit SD/compressed
    marker, 0 otherwise. Feeds `manga_score` so a same-volume HD release outranks an SD
    one; it never changes the drop decision on its own.

    Coarse on purpose -- it is a scoring NUDGE, and its callers weight it at ±100 against a
    seeder term worth up to 999. For deciding whether one scan actually supersedes another,
    use `comic_quality_rank`, which is finer.
    """
    low = (title or "").lower()
    if any(m in low for m in _COMIC_HD_MARKERS):
        return 1
    if any(m in low for m in _COMIC_SD_MARKERS):
        return -1
    return 0


def comic_pixels(title: str):
    """The scan's stated pixel dimension (`(Digital-1920)`, `2048px`), or None.

    Years are excluded by the `_COMIC_PIXELS_MIN`/`MAX` window plus an explicit 19xx/20xx
    guard -- `(2013)` is a publication year on nearly every comic release, and reading it
    as a 2013-pixel scan would rank every old book above every new one.
    """
    best = None
    for m in _COMIC_PIXELS_RE.finditer(title or ""):
        n = int(m.group(1) or m.group(2))
        if not (_COMIC_PIXELS_MIN <= n <= _COMIC_PIXELS_MAX):
            continue
        # No year guard is needed any more: the pattern itself requires a `digital-`
        # prefix or a `px` suffix, and a publication year never carries either.
        best = n if best is None else max(best, n)
    return best


def comic_quality_rank(title: str) -> tuple:
    """A comparable quality rank for one comic scan: `(coarse, pixels)`.

    Ordering, best last: an explicit SD marker < no marker < an explicit HD marker, and
    within any of those a larger stated pixel dimension wins. Comparing tuples means a
    release that states 2048 beats one that states 1920 even though both read as "HD",
    which is exactly what "take the higher res version" requires and what a bare -1/0/+1
    could never express.
    """
    return (comic_quality(title), comic_pixels(title) or 0)


def manga_kind(title: str) -> tuple[str, int | None, bool]:
    """Classify a manga/comic release title.

    Returns `(form, number, colored)`:
      * form is `"volume"`, `"chapter"`, or `"collection"` (no volume/chapter marker).
      * number is the volume/chapter/issue number, or None for a collection.
      * colored is whether the release is a colored edition.

    Recognises manga markers (`Vol. N`, `vN`, `Ch. N`, `cNNN`) AND the western-comic
    issue marker (`#N`), so a western single issue classifies as a chapter and a western
    TPB/`Vol. N` as a volume — the same tiers, applied to both traditions.

    A COLLECTED-EDITION marker (Omnibus / Compendium / "Complete" / "Collection") is
    checked FIRST and wins: "The Complete ElfQuest Vol. 1-4" is one big collection, not
    "volume 1" — otherwise the searcher misreads it as an already-held single volume and
    never grabs the collection (§5 item 1a).
    """
    colored = looks_colored(title)
    if _COLLECTION_MARKER_RE.search(title):
        return "collection", None, colored
    m = _VOL_WORD_RE.search(title) or _V_BARE_RE.search(title)
    if m:
        return "volume", int(m.group(1)), colored
    m = _CHAPTER_WORD_RE.search(title) or _C_BARE_RE.search(title) or _HASH_ISSUE_RE.search(title)
    if m:
        return "chapter", int(m.group(1)), colored
    return "collection", None, colored


def is_complete_collection(title: str) -> bool:
    """True when a comic release is the "everything" pack — a Complete/Omnibus/Compendium
    collection covering the whole run, vs a plain TPB/trade or a single volume. Only the
    former may supersede the individual volumes/issues it contains."""
    return bool(_COMPLETE_COLLECTION_RE.search(title or ""))


# English is a HARD requirement for manga: a raw/foreign-language release is never
# dropped. nyaa manga is English by default, so an untagged release is accepted; the
# filter only rejects releases that plainly advertise a different language or "raw"
# (untranslated Japanese). Explicit "english"/"eng" always wins.
_ENGLISH_RE = re.compile(r"\b(?:english|eng)\b", re.IGNORECASE)
_FOREIGN_LANG_RE = re.compile(
    # A bracketed OR parenthesised language tag. It was `\[` only, so the equally common
    # `(JPN)` / `(RAW)` shape went unmatched and "Dragon Ball Full Color Manga v01-42
    # (JPN)" read as English -- a release that announces its language in the title.
    r"[\[(](?:raw|jp|jpn|jap|es|es-la|pt|pt-br|ptbr|fr|it|de|ru|cn|kr|vn|th|id|ar|tr|pl)\b"
    r"|\b(?:raw|jpn|japanese|japones|japonés|español|espanol|spanish|portuguese|"
    r"português|portugues|français|francais|french|italiano|italian|ita|deutsch|german|"
    r"russian|русский|chinese|中文|korean|한국어|vietnamese|tiếng việt|indonesian|"
    r"bahasa|thai|ไทย|arabic|العربية|turkish|türkçe|polish|polski|"
    r"napisy|napisy pl|sub ita|sub esp|sub fra|sub ger|sub por|ger sub)\b",
    re.IGNORECASE,
)


# A run of CYRILLIC letters in a comic/manga title. Unlike CJK -- which appears constantly
# in perfectly good English scanlations as a dual title ("進撃の巨人 Attack on Titan v01") --
# Cyrillic in a comic title means a RUSSIAN edition, always. Two Russian ElfQuest PDFs
# ("ElfQuest 1. Изгнание огнем.") were queued from libgen because the foreign check matched
# only the WORD "русский" and never the script itself.
_CYRILLIC_RUN_RE = re.compile(r"[\u0400-\u04FF]{3,}")


# A Japanese EDITION qualifier. Bare CJK is deliberately not disqualifying (see above), but
# these are not series names -- they are the words a JAPANESE-market edition is sold under,
# and an English scanlation of the same book says "Colored Edition" / "Perfect Edition" in
# English instead. Without this, "ONE PIECE カラー版 01-86 [One Piece Colored Edition 01-86]"
# read as English on the strength of its own bracketed gloss, and an English-only library
# would have taken three volumes of Japanese raws.
_JP_EDITION_RE = re.compile(r"カラー版|完全版|新装版|愛蔵版|文庫版|総集編|単行本")


def manga_is_english(title: str) -> bool:
    """Whether a manga release is English (or untagged — nyaa's English default).
    False only for a release that advertises a foreign language or a raw Japanese scan."""
    low = (title or "").lower()
    if _CYRILLIC_RUN_RE.search(title or ""):
        return False
    # Checked BEFORE the English marker: these releases carry a Latin gloss of their own
    # Japanese title ("[One Piece Colored Edition 01-86]") and it would otherwise redeem
    # them. A dual-TITLE scanlation ("進撃の巨人 Attack on Titan v01") names no edition and
    # is untouched.
    if _JP_EDITION_RE.search(title or ""):
        return False
    if _ENGLISH_RE.search(low):
        return True
    return not _FOREIGN_LANG_RE.search(low)


# --- video language gate -------------------------------------------------------
#
# A VOSTFR ("version originale sous-titrée français" — French-subbed), German-dub,
# Italian-dub, or Russian-only release of an anime is NOT what the owner wants even when
# it fills an episode gap: the fleet's audio policy is English subs or dual-audio, never a
# single foreign language. This gate rejects a video release that plainly advertises a
# foreign language AND carries no English/dual/multi marker to redeem it. "dual audio" and
# "multi-subs" releases (which include English) pass.
#
# The English-OK side runs FIRST: any dual/multi/English marker redeems a release, so the
# foreign side only ever rejects a release with NO English indication at all. The "multi"
# patterns tolerate the "Multi37-Subs" / "Multi10-Dubs" shape (a number slipped between the
# word and its qualifier) that used to read as foreign-only.
# The separator class includes `.` because scene naming is dot-delimited: `Dual.Audio` and
# `Multi.Subs` are as common as the spaced forms, and with `[ _-]` alone they did not count
# as an English marker at all -- so a dot-named dual-audio release was judged on its
# foreign half only ("Show.S01.GERMAN.Dual.Audio.1080p" read as German-only).
_VIDEO_ENGLISH_OK = re.compile(
    r"dual[ _.-]?audio|multi[ _.-]?audio|"
    r"multi(?:[ _.-]*\d+)?[ _.-]*(?:subs?|dubs?)|"
    r"\beng\b|\benglish\b|english[ _.-]subbed|english[ _.-]dubbed", re.IGNORECASE)
# A foreign-language tag that is NOT paired with an English/dual marker. Covers the dub
# languages (Ukranian, Russian, German/French/Italian/Spanish/Portuguese), the sub-tag
# language codes ("SUB ITA", "Sub Esp", "SUB FRA", "CHT"/"CHS", "Ukr", the Russian "DVO"),
# and the raw/VOSTFR/VF set.
_VIDEO_FOREIGN = re.compile(
    r"\bvostfr\b|\bvosti\b|\bvf\b|\bvfi\b|\braw\b|简繁|"
    r"(?:german|french|italian|spanish|portuguese)[ _-]dub|"
    # The ABBREVIATED dub tags, and `+` as a separator. "Nisekoi Staffel 1 ... Ger+Jap Dub"
    # passed every check: `Ger` is not the word `german`, and `+` is not in the `[ _-]`
    # separator class -- so two German releases sat in the queue as English ones.
    r"\b(?:ger|fre|fra|ita|spa|esp|por|pt|rus|ukr|pol|tur|hun|cze|dan|swe|nor|fin|gre|heb)"
    r"[ _+.-]*(?:jap|jpn|ja|eng)?[ _+.-]*dub\b|"
    # `Staffel` (German), `Temporada` (Spanish/Portuguese), `Saison` (French), `Stagione`
    # (Italian), `Sezon`. A release that numbers its seasons in another language IS in that
    # language -- a far stronger signal than any audio tag, and it is what these releases
    # led with. Note this also stops the season parser reading them as a bare `pack`.
    r"\b(?:staffel|temporada|saison|stagione|sezon|sezona|seizoen)\b|"
    r"\[rus\]|\brus[ _-]?dub\b|dub[ _-]?rus\b|\[rusdub\]|\brus\b|"
    r"chinese[ _-]?sub|\bchsub\b|\bcht\b|\bchs\b|"
    r"\bukr(?:ainian)?\b|\bdvo\b|"
    r"\bnapisy\b|"
    # A bare "Ita" is the Italian audio tag ("[H264 - Ita AC3]", "[Manga Ita Cbr]"). It is
    # only reached AFTER `_VIDEO_ENGLISH_OK`, so the very common dual "[Ita Eng]" / "iTA.ENG"
    # releases still pass on their English marker; what this catches is the Italian-ONLY
    # release, which is what the owner kept seeing queued.
    r"\bita\b|"
    # A bare scene "MULTi" tag means several audio tracks of which English is not
    # promised (typically original + French). It is only foreign-flagged HERE, after
    # `_VIDEO_ENGLISH_OK` has already claimed "multi-audio" / "multi-subs" / "MULTi ENG",
    # and it is not matched when the next token makes it a different word ("Multi Season",
    # "Multi.Audio"), so a genuine multi-language-WITH-English pack still passes.
    r"\bmulti\b(?![ _.-]*(?:audio|subs?|dubs?|lang|season|s\d))|"
    r"subs?[ _.-]*(?:ita|fra|esp|ger|por|ara|pol|tur|tha|vie|ind)\b|"
    r"\b(?:ita|fra|esp|ger|por)[ _.-]*(?:subs?|dub)\b|"
    # Nordic/Dutch/Central-European dub-and-sub markers. "SWESUB" (Swedish subs) is a
    # common TV-rip tag the old list missed, so Swedish/Norwegian/Danish/Finnish/Dutch
    # releases slipped through as English-appropriate. Compound words ("swesub") and the
    # full language words with a sub/dub marker; a BARE language code is only matched when
    # it is paired with a sub/dub token (never a bare "fin"/"nor" that might be English).
    r"\b(?:swesub|dansub|norsub|finsub|dutsub|nlsub|svsub|dksub|czsub|hunsub)\b|"
    r"\b(?:swedish|norwegian|danish|finnish|dutch)[ _-]?(?:subs?|dub)\b|"
    r"\bsubs?[ _.-]*(?:swe|nor|dan|fin|dut|nld|nl|sv|dk|cz|hun|rom|svk|pl|cs)\b|"
    r"\b(?:swe|nor|dan|fin|dut|nld|nl|sv|dk|cz|hun|rom|svk|pl|cs)[ _.-]*(?:subs?|dub)\b",
    re.IGNORECASE)


# A BARE scene language tag: the full language word with no `dub`/`sub` suffix to pair it
# with. "One Piece 001-100 FRENCH" and "Yu-gi-oh! Duel Monsters S02 FRENCH 480p WEB x264"
# both read as English-appropriate without this, because `_VIDEO_FOREIGN` matched the full
# language words only as `french[ _-]dub`, and scene releases tag the language alone.
#
# It must be POSITIONAL, not a bare word match: the language word is also an ordinary
# English title word -- "The Italian Job", "The Spanish Princess", "The French Dispatch" --
# and matching it anywhere would refuse those on their own names. A release TAG sits where
# tags sit: immediately before another release token (resolution, source, codec, group), or
# at the very end of the title. A title word is followed by more title words.
_REL_TOKEN = (
    r"\d{3,4}p|web|webrip|web-?dl|bluray|blu-ray|bdrip|bdmux|brrip|bdremux|hdtv|hdrip|"
    r"dvdrip|dvdscr|dvd|remux|x26[45]|h\.?26[45]|xvid|divx|hevc|avc|aac\d?|ac3|eac3|dts|"
    r"ddp?\d|flac|opus|mp3|complete|integrale|multi|dual|repack|proper|extended|uncut|"
    r"s\d{1,2}|e\d{1,3}|saison|staffel|batch|vf|vff|vostfr|10bit|8bit|hdr|sdr"
)
_VIDEO_BARE_LANG = re.compile(
    r"\b(?:truefrench|french|german|italian|spanish|espanol|castellano|latino|portuguese|"
    r"brazilian|russian|ukrainian|polish|turkish|hungarian|czech|slovak|romanian|dutch|"
    r"flemish|swedish|norwegian|danish|finnish|greek|hebrew|arabic|thai|vietnamese|"
    r"indonesian|korean|mandarin|cantonese|japanese)\b"
    r"(?=[ _.\-\[\]()+]*(?:" + _REL_TOKEN + r")\b|[ _.\-\[\]()]*$)",
    re.IGNORECASE,
)


def video_is_english(title: str) -> bool:
    """True when a video release is English-appropriate: it advertises English/dual/multi,
    or it advertises no foreign language at all. False only when it advertises a foreign
    language (VOSTFR, a foreign dub, a bare scene language tag,
    Russian/Chinese/Ukrainian/Italian/…) with no English/dual/multi marker."""
    low = (title or "").lower()
    if _VIDEO_ENGLISH_OK.search(low):
        return True
    return not (_VIDEO_FOREIGN.search(low) or _VIDEO_BARE_LANG.search(low))


# --- light-novel classification -------------------------------------------------
#
# Light novels are e-books (`.epub`/`.pdf`) that live in the Google Drive Novels tree,
# NOT in the YACReader comics library. The searcher treats them as their own kind with
# the SAME English-only rule as manga (an untagged nyaa Literature release is English by
# default), and reads a volume/book number out of the title the same way it does a manga
# volume — the release's "Volume NN" / "vNN" / a bare leading "NN -" book number.

# A release title that plainly marks a MANGA/COMIC archive (or the word "manga"/"comic"),
# or a plain `.zip` (an unpackable archive the pipeline cannot place as an e-book), is not
# a light novel, so a loose Literature search that also surfaces the manga never drops it
# as a novel.
_NOVEL_ARCHIVE_RE = re.compile(r"\.(?:cbz|cbr|cbt|cb7|zip)\b|\bmanga\b|\bcomic\b", re.IGNORECASE)


def lightnovel_is_english(title: str) -> bool:
    """English-only for light novels, identical to the manga rule."""
    return manga_is_english(title)


def lightnovel_kind(title: str) -> tuple[str, int | None]:
    """Classify a light-novel release title as ("volume", number) or ("collection", None).

    Reuses the manga volume markers (a light novel ships "Volume NN" / "vNN" just like a
    manga volume), and additionally recognises a bare leading "NN - " book number that
    the Novels folder already uses (`3 - The Prisoner of Azkaban.epub`).
    """
    colored = looks_colored(title)          # ignored for novels, but reused for parsing
    m = _VOL_WORD_RE.search(title) or _V_BARE_RE.search(title)
    if m:
        return "volume", int(m.group(1))
    m = re.search(r"^\s*(\d{1,4})\s*[-–—]", title)
    if m:
        return "volume", int(m.group(1))
    return "collection", None


def lightnovel_looks_manga(title: str) -> bool:
    """True when a Literature result is a manga/comic, not a light novel (so a light-novel
    search never drops the manga counterpart by accident)."""
    return bool(_NOVEL_ARCHIVE_RE.search((title or "").lower()))


# A VIDEO/anime release that leaked into a light-novel search: it advertises a resolution
# (1080p/720p/480p), a video source (BD/WEB/BluRay/BDRip), a fansub group (HorribleSubs,
# SubsPlease, ...), an episode marker, or a video container. A real light novel is an
# .epub/.pdf — it never carries these. Rejecting it here keeps "Re:Zero kara Hajimeru
# Isekai Seikatsu - 35 [1080p]" (an anime episode) from being dropped as a novel.
_NOVEL_VIDEO_RE = re.compile(
    r"\b(?:2160p|1080p|720p|480p|576p|1080i|bdrip|bluray|blu-ray|bd\b|web-dl|webdl|"
    r"webrip|web\b|hdtv|remux|hevc|x264|x265|10bit|10-bit|8bit|8-bit|hi10p|flac|aac|"
    r"\.mkv\b|\.mp4\b|\.avi\b|\.m4v\b|episode|s\d{1,2}e\d{1,4})\b",
    re.IGNORECASE,
)


def lightnovel_looks_video(title: str) -> bool:
    """True when a Literature result is actually a VIDEO release (an anime episode that
    surfaced in a light-novel search), not an e-book."""
    return bool(_NOVEL_VIDEO_RE.search((title or "").lower()))
