"""Marry a release's ARCS to the provider's SEASONS -- arithmetic, not judgement.

WHY THIS EXISTS. `_release_structure_block` already tells the run how the release is
laid out, and `_provider_season_block` already tells it what seasons the show has. The
step between them -- *which arc belongs to which season* -- was still left to the model,
and that is the exact step that kept failing. Measured on 2026-09-12, the third run of
`[MTBB] Monogatari Series (BD 1080p)`: Season 03 held four different arcs, Season 05 two,
Season 06 two. Every episode resolved to a real title and a real plot. They were simply
another arc's.

The trap is that the counts line up. Season 03 held exactly 23 files and the provider's
Season 03 is exactly 23 episodes -- from entirely the wrong arcs. Nothing but a census
back to the source arc can see that, which is why nothing caught it for three runs.

WHAT IS ACTUALLY COMPUTABLE. Two facts the harness already holds:

  * the release's own UNITS. Not folders -- a unit is one filename LABEL whose episode
    numbers form a single run. Monogatari's five folders `05 - Nekomonogatari (White)`
    through `10 - Koimonogatari` all name their files
    `Monogatari Series Second Season - 01..23`, so they are ONE unit of 23, which is
    precisely what the provider calls Season 03. Folders would have split it five ways.
  * the provider's season SIZES, from `epguide.season_shape`.

Marrying them is an exact-cover search, and on a real release the answer is unique. For
Monogatari there are 11 units and 6 provider seasons, and exactly ONE assignment covers
the most files:

    S01 <- Bakemonogatari (15)          S04 <- Owarimonogatari S1 (13)
    S02 <- Nisemonogatari (11)          S05 <- Zoku Owarimonogatari (6)
    S03 <- Monogatari Series Second Season (23)

leaving Kizumonogatari (the films) and the five arcs the provider carries as specials.
That is the right answer, and no model had to find it.

THE THREE RULES THE SEARCH ENFORCES, each of which is a real-world fact:

  1. EXACT. A season is filled exactly or not at all. A season half-filled by one arc and
     half by another is the failure this module exists to stop.
  2. MONOTONE. Releases are ordered; the provider's seasons are ordered. A later season
     never draws on an earlier unit than an earlier season did. This is what makes the
     answer unique -- without it Monogatari has several exact covers.
  3. PARTIAL. A season may be left EMPTY. A release need not hold the whole show, and
     demanding every season be filled would fail on every single-cour drop. Coverage is
     maximised instead, and an assignment is only stated as fact when the maximum is
     reached by exactly one of them.

WHAT IT DELIBERATELY DOES NOT DO. It says nothing about the units it could not place.
Those are films, specials, or an entry the provider carries separately -- three different
answers that need judgement, and judgement is what the model is for. The block says so
outright rather than forcing them into the nearest season number, which is how
Nekomonogatari (Black) ended up in Season 03 in the first place.

Fails soft in every direction, like `epguide`: an unknown show, an unparseable release or
a search that finds nothing returns None and the caller's prompt is exactly what it was.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# Ceilings on the search. A release with more units than this is not the multi-arc case
# this module is for, and an exact-cover search over it could run long; returning None
# costs nothing because the prompt then reads exactly as it did before.
MAX_UNITS = 40
MAX_SEASONS = 30
MAX_NODES = 200_000


class _Budget:
    """Node budget for the search, so a pathological release cannot hang an ingest."""

    def __init__(self, limit=MAX_NODES):
        self.left = limit

    def spend(self):
        self.left -= 1
        return self.left > 0


class Unit:
    """One release unit: a filename LABEL whose episode numbers form a single run.

    `folders` is every top-level folder the unit's files live in -- often several, which
    is the whole point: the five folders carrying `Monogatari Series Second Season` are
    one unit, and treating them as five is what put four arcs in one season.
    """

    __slots__ = ("label", "numbers", "paths", "folders", "order")

    def __init__(self, label, numbers, paths, folders, order):
        self.label = label
        self.numbers = numbers            # sorted episode numbers as the FILENAMES say
        self.paths = paths                # release-relative paths, in number order
        self.folders = folders            # top-level folders, in release order
        self.order = order                # position in the release

    @property
    def size(self):
        return len(self.paths)

    @property
    def contiguous(self):
        """Whether the filename numbers form one unbroken run."""
        return bool(self.numbers) and (
            self.numbers == list(range(self.numbers[0], self.numbers[0] + len(self.numbers))))

    def __repr__(self):                                          # pragma: no cover
        return f"<Unit {self.label!r} x{self.size}>"


def strip_release_root(paths):
    """`paths` with the one top-level folder they ALL share removed.

    THE BUG THIS FIXES, found 2026-09-12 and older than it looks. A chunked torrent's
    file list comes from qBittorrent, where every entry is named from the torrent root:

        [MTBB] Monogatari Series (BD 1080p)/01 - Bakemonogatari/[MTBB] Bakemonogatari - 01v2 [346DABB1].mkv

    The non-chunked path walks the content directory instead and gets paths relative to
    it (`01 - Bakemonogatari/...`). Both are handed to `_release_structure_block` as "the
    release's relative paths", and it reads `parts[0]` as the arc folder -- so on the
    CHUNKED path it saw one folder, `[MTBB] Monogatari Series (BD 1080p)`, for all 103
    files, and returned "" because a single-folder release "tells the model nothing it
    cannot already see."

    That block is the one that names the split/numbering conflict. It was written for
    Monogatari, and on Monogatari -- a chunked pack, every time -- it rendered nothing at
    all. Which is why the conflict kept being invisible after it shipped.

    Only strips when EVERY path shares the same first component AND there is a level
    below it, so a release that genuinely has one arc folder is left alone.
    """
    paths = [str(p).replace("\\", "/").lstrip("/") for p in paths or () if str(p).strip()]
    while paths:
        if len({p.split("/", 1)[0] for p in paths}) != 1:
            break                          # several top-level entries: nothing to strip
        if any("/" not in p for p in paths):
            break                          # a loose file at this level: not a wrapper
        below = [p.split("/", 1)[1] for p in paths]
        if all("/" not in p for p in below):
            break                          # that folder IS the structure; keep it
        paths = below
    return paths


def units(release_files, parse=None):
    """The release's units, in release order. `[]` when nothing parses.

    `parse` is `identify._parse_release_name` by default and is injectable so the tests
    can exercise this module without importing the whole identify stack.
    """
    if parse is None:
        from identify import _parse_release_name as parse          # noqa: PLC0415

    by_label = {}
    for rel_s in sorted(strip_release_root(release_files)):
        rel = PurePosixPath(rel_s.replace("\\", "/"))
        hit = parse(rel.stem)
        if not hit:
            continue
        label, num = hit
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        rec = by_label.setdefault(label, {"nums": [], "paths": [], "folders": []})
        rec["nums"].append(num)
        rec["paths"].append(rel_s)
        if top not in rec["folders"]:
            rec["folders"].append(top)

    out = []
    for label, rec in by_label.items():
        pairs = sorted(zip(rec["nums"], rec["paths"]))
        out.append(Unit(label, [n for n, _ in pairs], [p for _, p in pairs],
                        rec["folders"], 0))
    # Release order is the order the first folder of each unit appears in -- which is what
    # the release itself numbers ("01 - Bakemonogatari", "02 - Kizumonogatari", ...).
    out.sort(key=lambda u: (u.folders[0] if u.folders else "", u.paths[0]))
    for i, u in enumerate(out):
        u.order = i
    return out


def _cover(sizes, season_list, budget):
    """Every maximal monotone exact cover of `season_list` by the units in `sizes`.

    Returns `(best_coverage, [assignment, ...])` where an assignment is
    `[(season, [unit_index, ...]), ...]`. Only assignments AT the best coverage come
    back, because a smaller one is never the answer -- it is the same answer with a
    season left out.
    """
    best = [-1]
    found = []

    def rec(si, ui, acc, cov):
        if not budget.spend():
            raise _Overrun
        if si == len(season_list):
            if cov > best[0]:
                best[0] = cov
                found.clear()
            if cov == best[0] and acc:
                found.append(list(acc))
            return
        season, need = season_list[si]
        rec(si + 1, ui, acc, cov)                      # leave this season unfilled
        if need <= 0:
            return

        def pick(start, chosen, tot):
            if tot == need and chosen:
                rec(si + 1, chosen[-1] + 1, acc + [(season, list(chosen))], cov + tot)
                return
            if tot >= need:
                return
            for k in range(start, len(sizes)):
                if tot + sizes[k] <= need:
                    pick(k + 1, chosen + [k], tot + sizes[k])

        pick(ui, [], 0)

    rec(0, 0, [], 0)
    return best[0], found


class _Overrun(Exception):
    """The search exceeded its node budget; the caller reports 'no proposal'."""


class Proposal:
    """The harness's arc->season answer, or its honest absence."""

    __slots__ = ("units", "mapping", "leftover", "ambiguous", "shape", "why")

    def __init__(self, units_, mapping, leftover, ambiguous, shape, why=""):
        self.units = units_
        self.mapping = mapping            # {season: [Unit, ...]} in release order
        self.leftover = leftover          # [Unit, ...] no numbered season fits
        self.ambiguous = ambiguous        # several covers tie -> state none as fact
        self.shape = shape                # {season: episode count} from the provider
        self.why = why

    @property
    def settled(self):
        """Whether this is an answer the prompt may state as FACT."""
        return bool(self.mapping) and not self.ambiguous

    def season_of(self, path):
        """The proposed season for one release-relative path, or None."""
        for season, us in self.mapping.items():
            for u in us:
                if path in u.paths:
                    return season
        return None

    def episode_of(self, path):
        """The proposed episode number for one release-relative path, or None.

        A season filled by SEVERAL units numbers them in release order, running on across
        the units -- which is exactly what the release's own filenames already say when
        the units share a label, and the only coherent reading when they do not.
        """
        for season, us in self.mapping.items():
            n = 0
            for u in us:
                for i, p in enumerate(u.paths):
                    if p == path:
                        return n + i + 1
                n += u.size
        return None


def propose(release_files, shape, parse=None):
    """`Proposal` for this release against `shape`, or None when there is nothing to say.

    `shape` is `{season: episode_count}` -- `epguide.season_shape`'s return exactly.
    """
    if not shape or not release_files:
        return None
    us = units(release_files, parse=parse)
    if not us or len(us) > MAX_UNITS:
        return None
    seasons = sorted(s for s in shape if isinstance(s, int) and s > 0)
    if not seasons or len(seasons) > MAX_SEASONS:
        return None
    # A release whose units do not each carry one unbroken run is not the case this
    # module reasons about; its numbering needs resolving before its arcs can be married
    # to anything, and `_release_structure_block` is what says so.
    if any(not u.contiguous for u in us):
        return Proposal(us, {}, list(us), False, shape,
                        "a release unit's own episode numbers are not one unbroken run")

    season_list = [(s, shape[s]) for s in seasons]
    sizes = [u.size for u in us]
    try:
        best, covers = _cover(sizes, season_list, _Budget())
    except _Overrun:
        return Proposal(us, {}, list(us), False, shape,
                        "the arc/season search exceeded its budget")
    if best <= 0 or not covers:
        return Proposal(us, {}, list(us), False, shape,
                        "no set of whole arcs fills any of this show's seasons exactly")

    # Several distinct covers at the same coverage is a genuine ambiguity. Say so; do not
    # pick one and present it as arithmetic.
    distinct = {tuple((s, tuple(ix)) for s, ix in c) for c in covers}
    if len(distinct) > 1:
        return Proposal(us, {}, list(us), True, shape,
                        f"{len(distinct)} different arc->season assignments cover the "
                        f"same {best} files")

    mapping = {}
    used = set()
    for season, idx in covers[0]:
        mapping[season] = [us[i] for i in idx]
        used.update(idx)
    leftover = [u for i, u in enumerate(us) if i not in used]
    return Proposal(us, mapping, leftover, False, shape)


# --- the prompt block ---------------------------------------------------------------

def block(proposal, series_title=""):
    """The prompt text for a proposal. `""` when there is nothing worth saying.

    Deliberately written as a CLAIM the run is asked to confirm or reject, not as an
    order. A harness that proposes and a model that checks inverts the failure this
    module exists for: a wrong mapping becomes visible instead of silent. But the
    arithmetic is stated as arithmetic, because it is.
    """
    if proposal is None or not proposal.settled:
        return ""
    title = f" FOR {series_title!r}" if series_title else ""
    lines = [
        "======================================================================",
        f"ARC -> SEASON, COMPUTED BY THE HARNESS{title}",
        "======================================================================",
        "The harness matched this release's ARCS to the provider's SEASONS by size. An",
        "arc here is one filename LABEL whose episode numbers form a single run -- which",
        "is often SEVERAL folders (see the release structure above), because a release",
        "splits a broadcast season into named arc folders while numbering the files",
        "straight through them.",
        "",
        "Each season below is filled EXACTLY by the arcs listed, the arcs are taken in",
        "release order, and this is the ONLY assignment that does so. It is arithmetic,",
        "not a guess:",
        "",
    ]
    for season in sorted(proposal.mapping):
        us = proposal.mapping[season]
        total = sum(u.size for u in us)
        lines.append(f"  Season {season:02d}  ({proposal.shape.get(season)} episodes per "
                     f"the provider; {total} file(s) here)")
        n = 0
        for u in us:
            lo, hi = n + 1, n + u.size
            folders = ", ".join(u.folders[:4]) + ("..." if len(u.folders) > 4 else "")
            lines.append(f"      {u.label[:40]:42s} {u.size:3d} file(s) -> "
                         f"E{lo:02d}-E{hi:02d}   [{folders}]")
            n += u.size
    if proposal.leftover:
        lines += [
            "",
            "  These arcs fit NO season of this show, and the harness does not guess at",
            "  them. Each is a film, a special (Season 00), or an entry the provider",
            "  carries as a separate series -- say which in your rationale. Do NOT force",
            "  one into the nearest season number; that is the mistake this block exists",
            "  to prevent, and it is how a whole arc ends up wearing another arc's",
            "  titles:",
            "",
        ]
        for u in proposal.leftover:
            folders = ", ".join(u.folders[:4]) + ("..." if len(u.folders) > 4 else "")
            lines.append(f"      {u.label[:40]:42s} {u.size:3d} file(s)          "
                         f"[{folders}]")
    lines += [
        "",
        "  If you believe this mapping is wrong, say so explicitly in your rationale and",
        "  state which arc belongs to which season instead. Silently filing against a",
        "  different mapping is the one answer to avoid: the harness re-checks the plan",
        "  against these arcs and will reject a season that draws on two of them.",
    ]
    return "\n".join(lines) + "\n"


# --- the metadata the run would otherwise go looking for -----------------------------

_PART = re.compile(r"\s*(?:-\s*)?(?:part|pt\.?)\s*\d+\s*$|\s*\(\d+\)\s*$", re.IGNORECASE)

# Below this, a "plot" is not a plot. The block is squeezed when a provider's ceiling
# demands it, and the synopses are the first thing to give -- but they end up in a LOCKED
# sidecar, so there is a floor under how far they may be cut before they must be dropped
# outright instead.
_MIN_USEFUL_PLOT = 60


def _stem(name):
    """The ARC a special's title belongs to.

    Two shapes, and both occur in one show. Most arcs are parts of one title
    (`Tsubasa Family - Part 3`), but `Koyomimonogatari` gives each of its twelve episodes
    its own name (`Koyomimonogatari - Koyomi Stone`) -- so stripping only the part marker
    leaves twelve groups of one and the arc never matches its twelve files. Taking what
    precedes the first ` - ` handles both, and leaves a title with no separator
    (`A Cruel Fairy Tale: The Beautiful Princess`) as its own group.
    """
    name = (name or "").strip()
    head = name.split(" - ")[0].strip()
    if head and head != name:
        return head
    return _PART.sub("", name).strip(" -:")


def special_groups(specials):
    """The provider's specials grouped into ARCS, in air order.

    TVMaze numbers a show's regular run and leaves its specials unnumbered, so the only
    structure in the list is that the parts of one arc share a title stem and air
    together. `[(stem, [entry, ...]), ...]`, consecutive runs only -- two arcs that happen
    to share a stem years apart stay separate, which is what lets a size match mean
    something.
    """
    out = []
    for e in specials or ():
        st = _stem(e.get("name") or "")
        if out and out[-1][0] == st:
            out[-1][1].append(e)
        else:
            out.append((st, [e]))
    return out


def match_arcs_to_specials(leftover, groups):
    """`{unit.order: group index}` for the left-over arcs, or `{}` when it is not certain.

    SIZE ALONE IS NOT ENOUGH, and the counter-example is in this very release. Matching
    each arc to the unique specials group of the same size pairs `Kizumonogatari` -- three
    FILMS -- with `Owarimonogatari`'s three 2017 specials, because that is the only group
    of three. It would then have printed three confident, entirely wrong titles. Meanwhile
    `Nekomonogatari (Black)` and `Tsukimonogatari` are both four files against two groups
    of four, so size alone declines to match either, and both are easy.

    ORDER settles all three. A release lists its arcs in order and a provider lists its
    specials in air order, so the assignment must be MONOTONE -- the same property that
    makes the season cover unique in `_cover`. Maximising covered files under that
    constraint pairs Tsubasa Family, Suruga Devil, Yotsugi Doll and Koyomimonogatari
    correctly and leaves Kizumonogatari unmatched, which is the honest answer: the films
    are not specials at all.

    Returns `{}` unless the best assignment is unique, so a tie asserts nothing.
    """
    leftover = list(leftover or ())
    groups = list(groups or ())
    if not leftover or not groups:
        return {}
    best = [-1]
    found = []

    def rec(ui, gi, acc, cov):
        if ui == len(leftover):
            if cov > best[0]:
                best[0] = cov
                found.clear()
            if cov == best[0]:
                found.append(dict(acc))
            return
        unit = leftover[ui]
        rec(ui + 1, gi, acc, cov)                       # leave this arc unmatched
        for k in range(gi, len(groups)):
            if len(groups[k][1]) == unit.size:
                acc[unit.order] = k
                rec(ui + 1, k + 1, acc, cov + unit.size)
                del acc[unit.order]

    rec(0, 0, {}, 0)
    if best[0] <= 0:
        return {}
    distinct = {tuple(sorted(m.items())) for m in found}
    return found[0] if len(distinct) == 1 else {}


def _named_matches(leftover, groups, matched):
    """`{unit.order: group index}` for arcs the provider knows by NAME but not by size.

    The release splits `Owarimonogatari S2` into 7 files; TVMaze carries the same 2017 run
    as 3 grouped entries. There is no file-to-entry mapping to assert -- but the provider
    plainly knows the arc, which is enough to say it is a SPECIAL and to give it Season-00
    numbers. Unique-or-nothing, like every other match here: `Kizumonogatari` matches two
    different groups and so matches none, which is correct, because it is three films.
    """
    taken = set(matched.values())
    out = {}
    for unit in leftover:
        if unit.order in matched:
            continue
        low = unit.label.lower()
        hits = [i for i, (st, _es) in enumerate(groups)
                if i not in taken and st and low.startswith(st.lower())]
        if len(hits) == 1:
            out[unit.order] = hits[0]
    return out


def match_arcs_to_tmdb_specials(leftover, runs):
    """`{unit.order: run index}` pairing left-over arcs with TMDB Season-0 runs.

    TMDB groups this show's specials by a name prefix and numbers them, and its grouping
    is CLOSER to the release than TVMaze's: Owarimonogatari's 2017 run is 7 entries in
    TMDB and 7 files in the release, where TVMaze carries it as 3. Requiring an exact size
    match AND a shared word keeps that from becoming a guess.
    """
    out, taken = {}, set()
    for unit in leftover or ():
        low = {w for w in re.split(r"[^a-z0-9]+", unit.label.lower()) if w}
        hits = []
        for i, (stem, eps) in enumerate(runs or ()):
            if i in taken or len(eps) != unit.size:
                continue
            theirs = {w for w in re.split(r"[^a-z0-9]+", (stem or "").lower()) if w}
            if low & theirs:
                hits.append(i)
        if len(hits) == 1:
            out[unit.order] = hits[0]
            taken.add(hits[0])
    return out


def metadata_block(proposal, specials, wave_paths=None, max_plot=220, tmdb_runs=None):
    """Per-file titles and plots for the arcs this run must place as SPECIALS.

    WHY THIS IS IN THE PROMPT AND NOT LEFT TO THE RUN. `validate_plan` requires a real
    `episode_title` AND a real `plot` on every Season-0 file -- correctly, because the
    harness LOCKS those sidecars and a blank one would freeze a blank title into the
    library permanently. The run's only way to satisfy that was a web search.

    Measured 2026-09-12: a confirm-mode run on groq spent all 19 of its turns
    web-searching for exactly this, hit the loop breaker, wrote no plan, and burned
    195,693 of the provider's 200,000 daily tokens. The fleet already had the answer on
    disk, cached, from the same lookup that produced the season shape.

    A leftover arc is matched to a specials group by SIZE, and only when exactly one group
    of that size is unmatched -- so a wrong title is never asserted. An arc that cannot be
    matched is simply absent from the block and the run looks it up as before.
    """
    if proposal is None or not proposal.leftover or not specials:
        return ""
    wave = None
    if wave_paths is not None:
        wave = {PurePosixPath(str(p).replace("\\", "/")).name for p in wave_paths}

    groups = special_groups(specials)
    matched = match_arcs_to_specials(proposal.leftover, groups)
    # TMDB's Season-0 list, when available, is BOTH better grouped and authoritative about
    # the episode NUMBERS Jellyfin expects. Preferred over the size-and-order match above
    # wherever it lines up exactly; the TVMaze match stays as the fallback.
    tmdb_match = match_arcs_to_tmdb_specials(proposal.leftover, tmdb_runs or [])

    # Season-00 NUMBERS, proposed here for the same reason the season mapping is: it is
    # arithmetic, and leaving it to the run is asking for a collision. Thirty-two files
    # across five arcs have to get thirty-two distinct S00 numbers, and `validate_plan`
    # rejects the whole plan when two files land on one destination -- so one slip costs
    # the pack, not one file.
    #
    # Numbered in RELEASE order over the arcs the provider actually evidences as specials
    # (matched by size, or by name for one it groups differently). An arc with no such
    # evidence -- Kizumonogatari, which is three FILMS -- is deliberately left out rather
    # than given a number it would have to abandon on its way to Movies/.
    # PER-ARC SOURCE, best first. TMDB where it lines up exactly (its Season-0 numbers are
    # the ones Jellyfin expects, and its grouping matches the release more closely than
    # TVMaze's); the TVMaze size-and-order match otherwise; a hedge when the provider
    # groups what the release splits.
    plan = {}
    nxt = 1
    for unit in proposal.leftover:
        if unit.order in tmdb_match:
            stem, eps = (tmdb_runs or [])[tmdb_match[unit.order]]
            # TITLES and PLOTS from TMDB -- NOT its episode numbers. Using TMDB's Season-0
            # numbers was tried on 2026-09-12 and reverted before shipping: waves 1 and 2
            # had already filed specials sequentially from S00E01, so switching schemes
            # mid-pack would have put Hanamonogatari at S00E13-17 while its own files sat
            # at S00E05-09. A Season 00 numbered two different ways is worse than one
            # numbered arbitrarily but consistently. There is no upside either: every
            # Season-0 sidecar is LOCKED with the fleet's own title, so the provider's
            # numbering buys no rendering benefit -- only a collision risk.
            plan[unit.order] = ("tmdb", stem,
                                [{"name": e["name"], "summary": e["overview"]} for e in eps],
                                None)
        elif unit.order in matched:
            stem, eps = groups[matched[unit.order]]
            plan[unit.order] = ("tvmaze", stem, eps, None)
        elif unit.order in _named_matches(proposal.leftover, groups, matched):
            idx = _named_matches(proposal.leftover, groups, matched)[unit.order]
            stem, eps = groups[idx]
            plan[unit.order] = ("hedged", stem, eps, None)
    # Season-0 numbers: TMDB's own where known, otherwise packed around them so nothing
    # collides. Two files at one destination fails the WHOLE plan, not one file.
    taken = {n for v in plan.values() if v[3] for n in v[3]}
    s00 = {}
    for unit in proposal.leftover:
        rec = plan.get(unit.order)
        if not rec:
            continue
        if rec[3]:
            s00[unit.order] = rec[3][0]
            continue
        while any(nxt + i in taken for i in range(unit.size)):
            nxt += 1
        s00[unit.order] = nxt
        taken.update(range(nxt, nxt + unit.size))
        nxt += unit.size

    rows, hedged = [], []
    for unit in proposal.leftover:
        rec = plan.get(unit.order)
        if not rec:
            continue
        kind, stem, entries, numbers = rec
        if wave is not None and not any(PurePosixPath(x).name in wave for x in unit.paths):
            continue
        if kind == "hedged":
            lo = s00.get(unit.order)
            span = (f" They are Season 00 E{lo:02d}-E{lo + unit.size - 1:02d} in the "
                    f"numbering above." if lo else "")
            hedged.append(f"  {unit.label}  ->  the provider knows this arc as {stem!r} but "
                          f"lists only {len(entries)} entr(y/ies) for the release's "
                          f"{unit.size} file(s), so it groups what the release splits. The "
                          f"titles are below; work out which file is which part yourself, "
                          f"and say so in your rationale.{span}")
            for e in entries:
                hedged.append(f"        {e['name']}")
            continue
        lines = []
        for i, (path, entry) in enumerate(zip(unit.paths, entries)):
            base = PurePosixPath(path).name
            if wave is not None and base not in wave:
                continue
            plot = (entry.get("summary") or "").strip()
            if max_plot < _MIN_USEFUL_PLOT:
                plot = ""
            elif len(plot) > max_plot:
                plot = plot[:max_plot].rsplit(" ", 1)[0] + "..."
            num = numbers[i] if numbers else s00[unit.order] + i
            lines.append(f"      {base}  ->  S00E{num:02d}")
            lines.append(f"        title: {entry['name']}")
            if plot:
                lines.append(f"        plot : {plot}")
        if lines:
            src = ("TMDB, which is what Jellyfin scrapes -- use these numbers"
                   if kind == "tmdb" else "the episode guide")
            rows.append(f"  {unit.label}  ->  {len(entries)} SPECIAL(s) per {src}, {stem!r}:")
            rows.extend(lines)

    if hedged:
        rows += [""] + hedged

    if not rows:
        return ""
    return "\n".join([
        "======================================================================",
        "TITLES AND PLOTS FOR THE SPECIALS (from the fleet's own episode guide)",
        "======================================================================",
        "Every Season-00 file MUST carry a real title and a real plot -- the harness locks",
        "those sidecars, so a blank one is permanent. Here they are. Use them verbatim;",
        "there is nothing to look up. The arc was matched to the provider's specials by",
        "episode count, so if a title plainly does not fit its file, say so rather than",
        "filing it.",
        "",
        "The S00E numbers are proposed too, running across the arcs in RELEASE order so",
        "that no two files collide -- two files at one destination is a hard error that",
        "fails the WHOLE plan, not one file. Use them unless you have a reason not to; if",
        "you decide some other arc is also a special, number it AFTER these.",
        "",
    ] + rows) + "\n"


def ownership_block(proposal, tmdb_id, serves=None, same_show=None):
    """Which proposed slots Jellyfin's own provider cannot render, so the run OWNS those.

    The owner's instruction, verbatim (2026-09-12): *"let TMDB still work for all the
    individual episodes/files it will work for - only own what is necessary."*

    The fleet's guide (TVMaze) decides the arc->season mapping; TMDB is what Jellyfin
    actually scrapes for titles. They mostly agree. Where they do not, an un-owned file
    renders blank or, worse, wears another season's titles -- and the fix is not to change
    the mapping, it is to lock the fleet's own metadata onto exactly those files.

    Measured for Monogatari: 10 files of 103. Naming all 103 owned would work too, and
    would be wrong -- it would freeze the fleet's word over 93 files TMDB renders better
    than we ever will, and Jellyfin could never improve on them.

    `""` whenever TMDB cannot be asked, which leaves the run exactly as it was.
    """
    if proposal is None or not proposal.settled or not tmdb_id:
        return ""
    if serves is None and same_show is None:
        try:
            import tmdbguide
            if not tmdbguide.season_shape(tmdb_id):
                return ""
            serves = lambda s, e: tmdbguide.serves(tmdb_id, s, e)      # noqa: E731
            same_show = lambda s, labs: tmdbguide.season_identity_matches(  # noqa: E731
                tmdb_id, s, labs)
        except Exception:                                              # noqa: BLE001
            return ""
    rows, whole = [], set()
    for season in sorted(proposal.mapping):
        labels = [u.label for u in proposal.mapping[season]]
        size = sum(u.size for u in proposal.mapping[season])
        # TWO different failures, and the second is the nastier one.
        #   * TMDB has no such SLOT -> the episode renders blank. Obvious when you see it.
        #   * TMDB has the slot but the season is a DIFFERENT SHOW -> the episode renders a
        #     perfectly plausible title belonging to something else. Monogatari's TMDB
        #     Season 05 is the 2024 *OFF & MONSTER Season*, 15 episodes; the fleet files
        #     Zoku Owarimonogatari (6, 2019) there. `serves()` says yes to all six.
        if same_show is not None and not same_show(season, labels):
            whole.add(season)
            rows.append((season, list(range(1, size + 1)), "different show"))
            continue
        # The "TMDB has no such SLOT" half used to be flagged here too. It was REMOVED on
        # 2026-09-12 after being measured against the live library, and the measurement is
        # worth keeping because the premise was wrong in a way that is easy to repeat:
        #
        #   TMDB's Bakemonogatari season really does have 12 episodes (checked against the
        #   season endpoint, not just the summary count). The fleet filed 15. The prediction
        #   was that E13-15 would render blank. They did not -- Jellyfin rendered all three
        #   with real overviews and romanised titles ("Tsubasa kyatto sono san"). Jellyfin
        #   does not scrape TMDB alone; the series carries a TVDB id as well, and its
        #   provider chain filled what TMDB lacked.
        #
        # So a missing TMDB slot does not predict a blank episode, and owning on that basis
        # would freeze the fleet's text over a provider that was about to do fine -- exactly
        # what the owner asked not to happen. It is also the RECOVERABLE case: a genuinely
        # blank episode is found after the fact by `media_doctor`'s `plot_blank` check and
        # repaired then, with evidence instead of a guess.
        #
        # The identity mismatch above stays, because it is the opposite on both counts:
        # reliable (a season whose NAME is a different show is not a near-miss) and
        # UNRECOVERABLE (nothing downstream can tell a plausible wrong title from a right
        # one -- that is precisely why it is dangerous).
    if not rows:
        return ""
    lines = [
        "======================================================================",
        "FILES THAT MUST BE `owned` (the harness checked Jellyfin's own provider)",
        "======================================================================",
        "Jellyfin scrapes TMDB among other providers. For the season(s) below, TMDB's",
        "season is a DIFFERENT SHOW from the arc being filed there -- so those episodes",
        "would render titles belonging to something else entirely. That is worse than a",
        "blank: a blank is obviously wrong, a plausible wrong title is not, and nothing",
        "downstream can detect it.",
        "",
        "Put `\"owned\": true` on THESE FILES ONLY, each with a real `episode_title` and",
        "`plot`. Do NOT set the plan-level `owned` flag, and do not own anything else:",
        "every other file is served fine by Jellyfin's own providers, and owning one",
        "freezes our text over it forever.",
        "",
    ]
    for season, bad, why in rows:
        span = (f"E01-E{bad[-1]:02d} (ALL of them)" if season in whole
                else ", ".join(f"E{b:02d}" for b in bad))
        note = ("TMDB's Season is a DIFFERENT SHOW -- these would render plausible but "
                "wrong titles" if why == "different show"
                else "TMDB has no such episode -- these would render blank")
        lines.append(f"  Season {season:02d}:  {span}")
        lines.append(f"               {note}")
    lines += [
        "",
        "  Everything else in this plan stays un-owned and Jellyfin scrapes it as usual.",
    ]
    return "\n".join(lines) + "\n"


# --- what validate_plan needs -------------------------------------------------------

_NUM = re.compile(r"(\d{1,4})")


def unit_of_source(us, src_path):
    """The `Unit` a plan file's SOURCE path belongs to, or None.

    Matched on the basename, because a plan's `src` is an absolute path into the
    download while a unit's paths are release-relative.
    """
    base = PurePosixPath(str(src_path).replace("\\", "/")).name
    for u in us:
        for p in u.paths:
            if PurePosixPath(p).name == base:
                return u
    return None
