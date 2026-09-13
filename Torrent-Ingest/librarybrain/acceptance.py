"""The DB-driven acceptance gate, as a module BOTH repos can run.

This file exists because the gate could only be reached down one path. `db_acceptance`
lived in `searcher.py` and took a `.torrent`'s bytes, so it ran only where a real
`.torrent` had been downloaded -- and once every `.torrent` cache began serving truncated
files, 100% of drops became `.magnet` and the fleet's authoritative "should I download
this?" check went dark for four days without a single line of complaint (§4.120).

The gate's input was never really a `.torrent`; it is a FILE LIST. A magnet has no file
list at drop time, but Torrent-Ingest holds one the moment qBittorrent resolves the
magnet's metadata -- before it has committed to downloading the content. So the check
moves here, to a module with no dependency on either repo's `config`, and both callers
hand it the file list they have:

  * Torrent-Searcher, from a downloaded `.torrent`'s info dict, at drop time;
  * Torrent-Ingest, from qBittorrent, after magnet metadata resolves and before the
    download is admitted.

**Why this module imports almost nothing.** Torrent-Ingest and Torrent-Searcher both ship
`config.py` and `ingest.py`, so a module Torrent-Ingest path-loads must never do a bare
`import config` -- that resolves to whichever repo got onto `sys.path` first and was how
the ingest daemon once crashed at start (§4.102). This module therefore imports only
`librarydb` and `parse`, the two searcher modules that are stdlib-only and whose names
collide with nothing, and takes everything else -- the media root's episode titles, the AI
mapper -- as arguments. `item_map` is deliberately NOT imported: it needs the searcher's
`config` and `discovery`, so the AI mapper is INJECTED by the caller that can supply one.

**Three verdicts, not two.** `db_acceptance` returned a bool, so "every file is already
owned" and "I could not read this file list" were the same answer. On the drop path that
conflation was survivable: both meant "don't drop it", and the next sweep would look
again. On the ingest path they are opposite facts about a torrent that is already IN the
queue -- one is a decision, the other is the absence of one -- and treating a
cannot-determine as a refusal would delete content that no later sweep will re-offer.
So the verdict is ACCEPT / REFUSE / UNKNOWN, and UNKNOWN is the caller's problem to
declare rather than something this module quietly rounds off. §7: a fallback for "I cannot
verify this" must not be "accept it" -- and it must not be "destroy it" either.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import librarydb
import parse

# --- verdicts ----------------------------------------------------------------

ACCEPT = "accept"
REFUSE = "refuse"
UNKNOWN = "unknown"


@dataclass
class Verdict:
    """The gate's answer, and enough of its working to log and to persist.

    `decision` is ACCEPT / REFUSE / UNKNOWN. `mapped` counts the files the mapper read
    CONFIDENTLY (anything that became a real item type); it is what separates a REFUSE
    that means "all of this is owned" from an UNKNOWN that means "I read nothing".
    """
    decision: str
    reason: str
    new_count: int = 0
    upgrade_count: int = 0
    mapped: int = 0
    statuses: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    used_ai_mapper: bool = False

    @property
    def accepted(self) -> bool:
        return self.decision == ACCEPT

    @property
    def refused(self) -> bool:
        return self.decision == REFUSE

    @property
    def unknown(self) -> bool:
        return self.decision == UNKNOWN


# --- the deterministic filename->item mapper ---------------------------------
# Moved here from `item_map`, unchanged, because these three functions need only `parse`
# while the rest of that module needs the searcher's `config` and the AI provider chain.
# `item_map` re-exports them, so every existing caller still works.

_STRAY_SEASON_MAX = 12          # rows, absolute
_STRAY_SEASON_FRACTION = 0.02   # of the show


def _dominant_season(library_episodes: list[dict] | None) -> int | None:
    """The one non-special season an absolute-numbered show files everything under,
    tolerating a SMALL stray minority under another season number, else None.

    Requiring literally one season made this fail for the largest show in the library:
    `One Piece` holds 1,156 episodes under Season 01 and 8 under a "Season 02" that does
    not exist on disk (only `Season 00` and `Season 01` are there). Those 8 rows switched
    absolute detection off for One Piece, which is precisely the show the guard that reads
    it was written to protect — so the release-season re-slicing check and the fansub
    episode reading were both inert for it, and 522-file One Piece packs came back every
    sweep with no verdict at all. A stray handful must not veto a 1,156-episode run, but a
    real second season must, so the minority is bounded BOTH ways: at most 12 rows and at
    most 2% of the show.
    """
    eps = [e for e in (library_episodes or []) if isinstance(e, dict)]
    counts: dict[int, int] = {}
    for e in eps:
        if e.get("number") or 0:
            s = int(e.get("season") or 1)
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return None
    dom = max(counts, key=lambda s: counts[s])
    stray = sum(n for s, n in counts.items() if s != dom)
    if stray and (stray > _STRAY_SEASON_MAX
                  or stray > _STRAY_SEASON_FRACTION * sum(counts.values())):
        return None
    return dom


def looks_absolute(library_episodes: list[dict] | None) -> bool:
    """True when `library_episodes` describe an ABSOLUTE-numbered show: a long contiguous
    run filed under one DOMINANT non-special season (Bleach's 406 eps under "Season 01",
    Re:Zero's 66), allowing the small stray minority `_dominant_season` bounds. The
    deterministic fallback consults this so it does not re-introduce the "release Season 3
    > our max Season 1 -> new" repeat-download bug."""
    dom = _dominant_season(library_episodes)
    if dom is None:
        return False
    eps = [e for e in (library_episodes or []) if isinstance(e, dict)]
    nums = sorted(int(e.get("number") or 0) for e in eps
                  if int(e.get("number") or 0) and int(e.get("season") or 1) == dom)
    if not nums:
        return False
    span = max(nums) - min(nums) + 1
    if len(nums) >= 30:
        return span <= len(nums) * 1.15 + 2
    # Sparse absolute run (Attack on Titan holding 10 of 88 episodes) — see
    # inventory.is_absolute_numbered for the rationale.
    return max(nums) >= 30 and len(nums) < span * 0.6


def abs_season(library_episodes: list[dict] | None) -> int | None:
    """The single season an absolute-numbered show files everything under, else None.

    Centralises "what is the one true season for this absolute show" so both the
    deterministic fallback and the AI-mapper acceptance gate refuse a mapped season that
    disagrees with it (a per-release season label is NOT our numbering)."""
    if not looks_absolute(library_episodes):
        return None
    # The DOMINANT season, not the first row's: a stray minority is tolerated by
    # `_dominant_season`, so reading the season off whichever row happens to come first
    # would return the stray one for exactly the shows that tolerance exists for.
    return _dominant_season(library_episodes)


_FN_BRACKETS = re.compile(r"\[[^\[\]]*\]|\([^()]*\)|\{[^{}]*\}")
_FN_EXT = re.compile(r"\.[A-Za-z0-9]{2,4}$")
_FN_FIELD = re.compile(r"\s-\s(\d{1,4})(?:v\d{1,2})?(?=\s-\s|$)")
_FN_TRAIL = re.compile(r"(?:^|\s)(\d{1,4})(?:v\d{1,2})?$")
_FN_RANGE = re.compile(r"\s-\s\d{1,4}\s*-\s*\d{1,4}\s*$")
_FN_SEASON_TAIL = re.compile(r"(?:\bS\d{1,2}\b|\bSeason\s+\d{1,2}\b|\s\d{1,2})\s*$", re.I)
_FN_NONEP_TAIL = re.compile(
    r"\b(movie|film|ova|oav|ona|special|specials|sp|extra|picture drama)\b[\s\-~]*$", re.I)


def episode_from_filename(base: str) -> int | None:
    """Episode number read from a FANSUB-STYLE FILENAME, or None when ambiguous.

    `parse.parse` reads release TITLES and only understands `SxxExx`, so it returns no
    episode at all for the fansub file forms that dominate the queue — measured, it read
    0 of 522 One Piece files and 0 of 30 Coalgirls files. A file list it cannot read is
    what makes the gate abstain, and an abstention on the drop path is how a repeat gets
    dropped again (§5 item 4). This reads the two UNAMBIGUOUS fansub fields:
    `Title - NN` (optionally `Title - NN - Episode Title`) and a bare trailing `Title NN`.

    Group tags, CRC32 stamps and resolution markers are stripped FIRST, because
    `[12345678]` and `1920x1080` are digit runs that are not episode numbers.

    It ABSTAINS on the three forms that MEASURED as mis-mappings over the 10,335-filename
    corpus of the journal plus the live queue, because each would let a file vote a
    torrent away on a false claim:
      * a season marker just before the number (`Spy x Family S2 - 09`, `White Album 2 -
        09`) — that is episode 9 OF THAT SEASON, and calling it episode 9 of the library's
        season is the "release season is a re-slicing" bug one layer down;
      * a movie/OVA/special marker (`Dragon Ball Z - Movie 09`) — that is movie 9, and
        calling it episode 9 makes a movie pack read as an owned season;
      * a range (`Show - 01 - 12`) — that is a span, not one episode.
    Those are 221 of 3,615 extractions (6%); abstaining keeps the other 94% and no
    abstention can refuse anything (§7 — a file that cannot speak does not disagree).
    """
    stem = _FN_EXT.sub("", base)
    prev = None
    while prev != stem:                        # nested groups: "[a [b]]"
        prev = stem
        stem = _FN_BRACKETS.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem.replace("_", " ")).strip(" -~")
    if not stem or _FN_RANGE.search(stem):
        return None
    m = _FN_FIELD.search(stem)
    if m:
        pre = stem[:m.start()]
    else:
        m = _FN_TRAIL.search(stem)
        if not m:
            return None
        # A bare trailing 4-digit YEAR is part of a title ("Some Movie 1999"). Episode
        # fields that long are One Piece-style and always carry the " - NNNN - " form
        # handled above, so this costs nothing.
        if 1900 <= int(m.group(1)) <= 2099:
            return None
        pre = stem[:m.start()]
    if _FN_NONEP_TAIL.search(pre) or _FN_SEASON_TAIL.search(pre):
        return None
    return int(m.group(1))


def fallback_map_files(series_name: str, kind: str, filenames: list[str],
                       library_episodes: list[dict] | None = None) -> list[dict]:
    """Deterministic filename→item mapping, used when the AI mapper is unavailable.

    Reads season/episode/volume/chapter straight out of each filename with the SAME
    parsers the cull/dedupe path already trusts. It is deliberately less smart than the AI
    (it cannot reconcile a per-release season number against absolute library numbering by
    title), so it errs conservative: a file it cannot read confidently becomes "other"
    (counted by neither side of the gate) rather than a guess. For an absolute-numbered
    show a file whose parsed season is NOT the library's one season is refused ("other"),
    so the "release Season 3 == our Season 1" repeat is not re-admitted by the fallback.

    The point is to stop ONE LLM hiccup from failing the acceptance gate closed for the
    whole sweep: the common SxxExx / Vol.N / Ch.N case is fully decidable without the model,
    and everything ambiguous still fails safe.
    """
    single_season = abs_season(library_episodes)

    # A fansub episode number is only trusted when the FILE LIST corroborates that it is
    # reading an episode RUN at all. `Eureka Seven Hi-Evolution 1/2/3` is a film trilogy
    # filed under Movies/, and it has exactly the shape of
    # `[Coalgirls]_Ao_no_Exorcist_01/02/03` — a title then a trailing number. Nothing in
    # the NAMES separates them, and a count of three does not either, so the run has to be
    # long enough that a film set cannot produce it: a trilogy or an OVA pair tops out
    # around four entries, while every pack this reading exists to judge carries twenty or
    # more (Dragon Ball 638, One Piece 522, Ao no Exorcist 25, Aquarion 26). Measured on
    # the counterfactual replay, a threshold of 3 still refused that trilogy — 3 of 3
    # "already owned" — and would have thrown the movies away (§7: a claim must be
    # corroborated, especially one your own parser made).
    fansub_eps: dict[str, int] = {}
    if single_season is not None and kind not in _DETERMINISTIC_KINDS and kind != "movie":
        for name in filenames:
            base = name.rsplit("/", 1)[-1]
            p = parse.parse(base)
            if p.season is not None and p.ep_min is not None:
                continue
            ep = episode_from_filename(base)
            if ep is not None:
                fansub_eps[base] = ep
    if len(set(fansub_eps.values())) < MIN_FANSUB_RUN:
        fansub_eps = {}

    items: list[dict] = []
    for name in filenames:
        base = name.rsplit("/", 1)[-1]
        if kind in ("manga", "comic"):
            form, num, _colored = parse.manga_kind(base)
            if form == "volume" and num is not None:
                items.append({"type": "volume", "number": num})
            elif form == "chapter" and num is not None:
                items.append({"type": "chapter", "number": num})
            else:
                items.append({"type": "other"})
        elif kind == "lightnovel":
            form, num = parse.lightnovel_kind(base)
            if form == "volume" and num is not None:
                items.append({"type": "volume", "number": num})
            else:
                items.append({"type": "other"})
        elif kind == "movie":
            items.append({"type": "movie"})
        else:  # anime / tv
            p = parse.parse(base)
            if p.season is not None and p.ep_min is not None:
                if single_season is not None and p.season != single_season:
                    items.append({"type": "other"})
                else:
                    items.append({"type": "episode", "season": p.season, "number": p.ep_min})
                continue
            # No SxxExx. A fansub filename still carries an episode number, and for an
            # ABSOLUTE-numbered show the season it belongs to is not a guess: the library
            # files everything under exactly one season, and `evaluate` has already
            # refused the case where the RELEASE advertises a different one. Restricting
            # the inheritance to `single_season` is what keeps this from re-deriving the
            # reverted per-season fix: where the season is genuinely unknowable (a
            # multi-season library and a seasonless file list) the file still abstains.
            ep = fansub_eps.get(base)
            if ep is not None:
                items.append({"type": "episode", "season": single_season, "number": ep})
            else:
                items.append({"type": "other"})
    return items


# --- the gate ----------------------------------------------------------------

_ITEM_TYPES = ("episode", "volume", "chapter", "movie")

# Kinds whose numbering is FULLY deterministic (Vol.N / Ch.N), so the AI filename→item
# mapper adds nothing and is not worth a free-model call.
_DETERMINISTIC_KINDS = ("manga", "comic", "lightnovel")

# Content files. Subtitles are deliberately NOT here: a subtitle carries no episode
# number, so it cannot corroborate a mapping and must not dilute one either -- a file that
# cannot speak abstains, it does not disagree (§7).
_CONTENT_EXTS = frozenset({
    ".mkv", ".mp4", ".avi", ".m4v", ".mov",                  # video
    ".cbz", ".cbr", ".cbt", ".cb7", ".pdf", ".epub", ".zip",  # comics / novels
})

# How much of a torrent the mapper must actually have READ before "everything here is
# already owned" is allowed to be a terminal refusal, and how little collapsing onto a
# single item key it may have done. Both were set by measuring the live queue -- see
# `_refusal_is_corroborated`.
MIN_MAPPED_FRACTION = 0.8
MIN_DISTINCT_FRACTION = 0.5

# How many DISTINCT fansub episode numbers a file list must yield before
# `episode_from_filename`'s reading is trusted at all. A film trilogy is shape-identical to
# three fansub episodes, so the run has to be longer than any film set can be -- see
# `fallback_map_files`, where the counterfactual replay set this number.
MIN_FANSUB_RUN = 6


def _content_files(names) -> list[str]:
    """The content files in a torrent's file list (video/comic/novel, not subtitles,
    samples, images or `.nfo` sidecars)."""
    out = []
    for n in names:
        base = n.rsplit("/", 1)[-1].lower()
        dot = base.rfind(".")
        if dot > 0 and base[dot:] in _CONTENT_EXTS:
            out.append(n)
    return out


def _refusal_is_corroborated(names, statuses, items, mapped) -> tuple[bool, str]:
    """Whether "every item is already owned" is backed by a mapping that actually READ
    this torrent -- the test a refusal must pass wherever refusing is TERMINAL.

    §4.121's lesson, one layer up: a claim must be corroborated before it is acted on,
    especially a claim your own parser made. Here the parser's claim is "I looked at this
    torrent and we own all of it", and measuring the live queue showed three ways that
    claim can be true of the mapping and false of the torrent:

      * **A sliver of a megapack.** "Dragon Ball Complete Collection DB DBZ Z GT Super
        Daima Movies" holds 750 files; the mapper read 20 of them (the `Daima - S01Exx`
        run, which collides with Dragon Ball Z's own S01 numbering) and every one was
        owned. Refusing on 2.7% of a pack says nothing about the other 97%.
      * **Only the season we happen to file.** "Dragon Ball Z Complete" holds 323 files
        and mapped 39 -- an absolute-numbered show skips every file whose season is not
        the one filed season, so the gate read 12% of the pack and called it owned.
      * **Wholesale collapse onto one key.** "Digimon Adventure 1999 S01" holds 339
        episode files, and the ledger pointed at a `movie`-kind series row carrying one
        item, so the deterministic mapper typed all 339 files `movie` -- one key, owned,
        and a 339-episode series refused. "Unknown" is not "one" (§7).

    So a terminal refusal requires the mapping to be BROAD (most content files read) and
    NOT COLLAPSED (many files did not become one key). Failing either is an UNKNOWN, which
    is admitted, counted and reported -- never a silent pass and never a deletion.
    """
    content = _content_files(names)
    if not content:
        return False, "no content files to corroborate against"
    frac = mapped / len(content)
    if frac < MIN_MAPPED_FRACTION:
        return False, (f"only {mapped} of {len(content)} content file(s) "
                       f"({frac:.0%}) could be mapped")
    keys = {(it.get("type"), it.get("season"), it.get("number"))
            for st, it in zip(statuses, items) if st != "skip"}
    if mapped and len(keys) / mapped < MIN_DISTINCT_FRACTION:
        return False, (f"{mapped} mapped file(s) collapsed onto {len(keys)} distinct "
                       f"item(s) -- the mapping is degenerate")
    # A refusal may not rest ENTIRELY on specials. Season 0 is where release numbering and
    # library numbering diverge most -- the one false refusal in the replay over 747
    # completed records was `Heaven.Officials.Blessing.S00E05`, which this library files as
    # S00E04, so the release's own number matched a DIFFERENT special we already held and
    # the file read as owned. Reconciling that needs the episode-TITLE match only the AI
    # mapper does; without it, "we already own this special" is too weak to be terminal.
    if not any(k[0] != "episode" or (k[1] or 0) != 0 for k in keys):
        return False, (f"every mapped item is a special (season 0), whose numbering is "
                       f"not comparable across releases")
    return True, f"{mapped} of {len(content)} content file(s) mapped to {len(keys)} item(s)"


def evaluate(conn, series_id: int, series_name: str, kind: str,
             names: list[str], release_title: str = "",
             episode_titles: dict | None = None,
             ai_mapper=None, log=None,
             refusal_is_terminal: bool = False) -> Verdict:
    """Does this FILE LIST contain any library item we do not already own at
    equal-or-better quality?

    Maps each filename to a library item, then compares it against `librarydb.owned_items`:

      * item key absent                    -> (a) NEW
      * item key present, strictly better  -> (b) UPGRADE (higher res / dual-audio)
      * otherwise                          -> owned

    Candidate quality comes from the RELEASE TITLE (resolution tier, dual-audio marker),
    not the file list. Returns a `Verdict`; the caller decides what an UNKNOWN costs.

    `ai_mapper`, when given, is called as
    `ai_mapper(series_name, kind, names, episodes) -> list[dict] | None` and is used only
    for anime/tv that already have owned episodes -- the one case where a release's season
    numbering may be a re-slicing of the library's absolute count and needs a title match.
    When it is absent or returns None, the deterministic mapper runs instead: a verdict
    from it is never LESS safe, it is just less clever.

    `refusal_is_terminal` says what a REFUSE costs the caller, and the caller is the only
    one who knows. On the searcher's drop path a refusal is free and reversible — nothing
    has been acquired, and the next sweep looks again — so the gate refuses on the balance
    of evidence. On ingest's magnet path the torrent is already queued and a refusal
    retires it for good, so the refusal must additionally be CORROBORATED by a mapping
    that demonstrably read the torrent (`_refusal_is_corroborated`); an uncorroborated
    "everything is owned" degrades to UNKNOWN rather than destroying content.
    """
    if conn is None:
        return Verdict(UNKNOWN, "library DB unavailable")
    if not series_id:
        return Verdict(UNKNOWN, "series not resolved in the library DB")
    if not names:
        return Verdict(UNKNOWN, "no readable file list")

    # An umbrella row ("Digimon") owns nothing while the library holds the content under
    # specific names ("Digimon Ghost Game"), so asking it makes every file read as NEW.
    # Re-ask the row this release's own title names. Both callers reach the judgment here,
    # so the drop path and the magnet path cannot drift on it.
    governing_id = librarydb.series_id_for_release(conn, series_id, release_title)
    if governing_id != series_id:
        if log:
            dest = (librarydb.series_by_id(conn, governing_id) or {}).get("name")
            log(f"acceptance: {series_name!r} owns nothing and is a franchise umbrella; "
                f"judging this release against {dest!r}, which the release title names")
        series_id = governing_id

    episodes = librarydb.library_episode_list(conn, series_id)

    # Reconcile a per-release season number to OUR numbering by episode TITLE. An
    # ingest-written row can hold the FILENAME, which is truthy and would beat the real
    # title, so a filename counts as no title at all (§4.118).
    if episode_titles:
        for e in episodes:
            if librarydb.looks_like_filename(e.get("title")):
                e["title"] = episode_titles.get((e["season"], e["number"])) or e.get("title")

    single_season = abs_season(episodes)
    release_p = parse.parse(release_title or "")

    # Fail-closed guard (§4.11): an absolute-numbered show files everything under ONE
    # season, so a release advertising a different season is a per-arc re-slicing whose
    # episode numbers cannot be reconciled to our absolute count. Counting those "new"
    # re-drops content we own in full (the Bleach/Gintama/One Piece floods).
    if single_season is not None and release_p.season is not None \
            and release_p.season != single_season:
        # The evidence here is the release TITLE and nothing else. That is enough to
        # decline a drop (free, and re-examined next sweep) but not to retire a queued
        # torrent for good: the same title shape covers a genuinely new season we do not
        # hold. Where the refusal is terminal this is a cannot-verify, not a decision.
        if refusal_is_terminal:
            return Verdict(UNKNOWN, "absolute-numbered show; release season may be a "
                                    "re-slicing, and the title is the only evidence")
        return Verdict(REFUSE, "absolute-numbered show; release season is a re-slicing",
                       mapped=len(names))

    # Manga/comic/light-novel numbering is deterministic, and a BRAND-NEW show has no
    # library numbering to reconcile against -- its own SxxExx IS the library numbering.
    # Only an already-owned anime/tv show needs the AI title match.
    used_ai = False
    if kind in _DETERMINISTIC_KINDS or not episodes or ai_mapper is None:
        items = fallback_map_files(series_name, kind, names, episodes)
    else:
        items = ai_mapper(series_name, kind, names, episodes)
        if items is None:
            # De-flake: the AI mapper is a long LLM call on the HOT path and its occasional
            # timeout must not fail the gate closed for every genuinely-new episode.
            items = fallback_map_files(series_name, kind, names, episodes)
            if log:
                log(f"AI filename mapper unavailable; using the deterministic fallback "
                    f"for {series_name} ({len(names)} file(s))")
        else:
            used_ai = True

    owned = librarydb.owned_items(conn, series_id)
    cand_res = parse.resolution_rank(release_p.resolution)
    cand_dual = parse.has_dual_audio(release_title or "")

    new_count = upgrade_count = mapped = 0
    statuses: list[str] = []
    for it in items:
        t = it.get("type")
        if t not in _ITEM_TYPES:
            statuses.append("skip")
            continue
        if t == "episode" and single_season is not None \
                and int(it.get("season") or 1) != single_season:
            statuses.append("skip")
            continue
        # This file was read as a real library item. Only these count toward `mapped`,
        # which is what lets the caller tell "everything here is owned" (a decision) from
        # "I could not read any of this" (the absence of one).
        mapped += 1
        key = librarydb.item_key(t, it.get("season"), it.get("number"))
        cur = owned.get(key)
        if cur is None:
            statuses.append("new")
            new_count += 1
            continue
        # Upgrade: strictly better on at least one axis, worse on none. Both axes are
        # compared ONLY when the OWNED copy's value is KNOWN: the inventory scan never
        # records a show episode's resolution or audio, so every owned item carries 0
        # ("unknown", not "SD"). Treating a 1080p candidate as "better than 0" is what
        # re-downloaded whole series to fill a handful of gaps (§4.23).
        better = worse = False
        if cur["resolution"] > 0:
            if cand_res > cur["resolution"]:
                better = True
            elif cand_res < cur["resolution"]:
                worse = True
        if cur["dual_audio"]:
            if cand_dual > cur["dual_audio"]:
                better = True
            elif cand_dual < cur["dual_audio"]:
                worse = True
        if better and not worse:
            statuses.append("upgrade")
            upgrade_count += 1
        else:
            statuses.append("owned")

    if not mapped:
        # Nothing in this file list could be read as a library item. That is not a
        # refusal -- it is the gate failing to reach a verdict, and saying so is the whole
        # reason this module has three answers instead of two.
        return Verdict(UNKNOWN, f"no library item could be read from {len(names)} file(s)",
                       statuses=statuses, items=items, used_ai_mapper=used_ai)

    if not new_count and not upgrade_count:
        if refusal_is_terminal:
            ok, why = _refusal_is_corroborated(names, statuses, items, mapped)
            if not ok:
                return Verdict(UNKNOWN,
                               f"every mapped item is owned, but the mapping is not "
                               f"corroborated: {why}",
                               mapped=mapped, statuses=statuses, items=items,
                               used_ai_mapper=used_ai)
            return Verdict(REFUSE,
                           f"all items already owned at equal-or-better quality ({why})",
                           mapped=mapped, statuses=statuses, items=items,
                           used_ai_mapper=used_ai)
        return Verdict(REFUSE, "all items already owned at equal-or-better quality",
                       mapped=mapped, statuses=statuses, items=items,
                       used_ai_mapper=used_ai)

    reason = (f"{new_count} new item(s)" if new_count
              else f"{upgrade_count} upgrade item(s)")
    return Verdict(ACCEPT, reason, new_count=new_count, upgrade_count=upgrade_count,
                   mapped=mapped, statuses=statuses, items=items, used_ai_mapper=used_ai)


def plan_for(series_name: str, kind: str, series_id: int,
             names: list[str], verdict: Verdict) -> dict:
    """The settled per-file torrent→item map, shaped for `librarydb.save_torrent_plan`.

    Each file carries its own new|upgrade|owned|skip verdict so a chunked ingest can skip
    the already-owned files without downloading them (§6.3.2)."""
    return {"series": series_name, "kind": kind, "series_id": series_id,
            "files": [{"src": n, "status": st, **it}
                      for n, st, it in zip(names, verdict.statuses, verdict.items)]}


# --- liveness ----------------------------------------------------------------
# §4.120's real lesson is not that the gate was bypassed; it is that a load-bearing check
# went dark for FOUR DAYS in a fleet that has a health report, a verification script and a
# hand-off document, and not one of them noticed. The gate did not fail — it stopped being
# REACHED, and nothing can report that on its own behalf except a heartbeat. This is the
# same shape as the Tailscale heartbeat (§4.126), and the FRESHNESS of the file is the
# load-bearing part: a stale heartbeat is itself the alarm.
#
# One file PER WRITER. The gate now runs in two daemons (the searcher's drop path and
# ingest's magnet path) and a single shared JSON would be a read-modify-write race between
# them. Each writer owns its own file exclusively, so there is no race at all, and the
# reader takes the newest across all of them.

HEARTBEAT_DIRNAME = "gate_heartbeat"


def record_verdict(state_dir, source: str, decision: str) -> None:
    """Note that the gate REACHED a verdict, for `<state_dir>/gate_heartbeat/<source>.json`.

    Records the last time this source produced any verdict plus a running tally per
    decision. Called on every verdict, accept or refuse or unknown: what is being tracked
    is that the check ran, not what it decided.

    Can never raise. A heartbeat that can break the thing it observes is worse than no
    heartbeat, and this sits directly on the drop and admission paths.
    """
    try:
        d = Path(state_dir) / HEARTBEAT_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{source}.json"
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                cur = {}
        counts = cur.get("counts") if isinstance(cur.get("counts"), dict) else {}
        counts[decision] = int(counts.get(decision) or 0) + 1
        now = time.time()
        payload = {
            "source": source,
            "last_verdict_at": now,
            "last_verdict_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "last_decision": decision,
            "counts": counts,
            "first_seen_at": cur.get("first_seen_at") or now,
        }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, p)                      # atomic: a reader never sees a half file
    except Exception:                            # noqa: BLE001 -- never break the caller
        pass


def liveness(state_dir) -> dict:
    """What is known about whether the acceptance gate is still being reached.

    Returns `{"sources": {...}, "last_verdict_at": float|None, "age_sec": float|None,
    "counts": {...}}`, taking the NEWEST verdict across every writer. `last_verdict_at`
    of None means no writer has ever recorded one — which, on a fleet that is dropping
    torrents, is the §4.120 fault itself.
    """
    out: dict = {"sources": {}, "last_verdict_at": None, "age_sec": None, "counts": {}}
    d = Path(state_dir) / HEARTBEAT_DIRNAME
    if not d.is_dir():
        return out
    newest = None
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        src = data.get("source") or p.stem
        out["sources"][src] = data
        for k, v in (data.get("counts") or {}).items():
            out["counts"][k] = int(out["counts"].get(k) or 0) + int(v or 0)
        ts = data.get("last_verdict_at")
        if isinstance(ts, (int, float)) and (newest is None or ts > newest):
            newest = float(ts)
    if newest is not None:
        out["last_verdict_at"] = newest
        out["age_sec"] = max(0.0, time.time() - newest)
    return out
