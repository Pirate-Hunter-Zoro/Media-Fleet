#!/usr/bin/env python3
"""`verify_arc_mapping.py` must census a season, never sample it.

HANDOFF §6 carried this as an accepted limit: the tool sampled ONE file per season, and on
the real Monogatari pack it reported Season 01 as coming from `09 - Onimonogatari` when all
15 of Season 01's files came from `01 - Bakemonogatari`. A tool that is confidently wrong
about the exact failure it exists to detect is worse than no tool, because its output reads
like a verdict.

The fix is not subtle -- report every arc a season drew on -- so the test is not subtle
either: build a season that MIXES four arcs the way the three failed runs did, and assert
the report names all four. A sampling implementation names one and passes nothing here.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config                                                        # noqa: E402
import verify_arc_mapping as vam                                     # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


# The Season 03 census from HANDOFF §1 -- the shape of the failure, exactly as measured:
# 23 files that looked perfect (the provider's Season 03 is 23 episodes) and held four arcs.
MIXED_SEASON = {}
for i, e in enumerate(range(1, 14)):
    MIXED_SEASON[(3, e)] = f"04 - Owarimonogatari S1/[MTBB] Owarimonogatari - {e:02d}.mkv"
for i, e in enumerate(range(14, 20)):
    MIXED_SEASON[(3, e)] = f"07 - Owarimonogatari S2/[MTBB] Owarimonogatari S2 - {i + 1:02d}.mkv"
for i, e in enumerate(range(20, 23)):
    MIXED_SEASON[(3, e)] = f"08 - Tsukimonogatari/[MTBB] Tsukimonogatari - {i + 1:02d}.mkv"
MIXED_SEASON[(3, 23)] = "05 - Nekomonogatari (Black)/[MTBB] Nekomonogatari Black - 01.mkv"

# A correct season for contrast: ONE arc, filed cleanly.
CLEAN_SEASON = {(1, e): f"01 - Bakemonogatari/[MTBB] Bakemonogatari - {e:02d}.mkv"
                for e in range(1, 16)}


def run_report(sources: dict) -> str:
    """Drive main() with the journal and Jellyfin both stubbed out."""
    saved_sources = vam.filed_sources
    saved_jf = vam._jf
    saved_argv = sys.argv
    vam.filed_sources = lambda show, info_hash: dict(sources)

    def fake_jf(path, params):
        if params.get("IncludeItemTypes") == "Series":
            return {"Items": [{"Name": "Monogatari Series (2009)", "Id": "series-1"}]}
        # Every slot resolves to a real-looking title, which is the point: a wrong
        # boundary resolves perfectly and this is what makes it invisible.
        items = []
        for (s, e) in sources:
            items.append({"ParentIndexNumber": s, "IndexNumber": e,
                          "Name": f"Tsubasa Tiger ({e})",
                          "Overview": "A real synopsis belonging to whichever arc the "
                                      "provider keeps at this coordinate."})
        return {"Items": items}

    vam._jf = fake_jf
    sys.argv = ["verify_arc_mapping.py", "--show", "Monogatari Series (2009)"]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            vam.main()
    finally:
        vam.filed_sources = saved_sources
        vam._jf = saved_jf
        sys.argv = saved_argv
    return buf.getvalue()


print("=== verify_arc_mapping: census, not sample ===")

out = run_report(MIXED_SEASON)

for arc in ("04 - Owarimonogatari S1", "07 - Owarimonogatari S2",
            "08 - Tsukimonogatari", "05 - Nekomonogatari (Black)"):
    check(f"mixed season names {arc!r}", arc in out)

check("mixed season is reported as 4 source arcs", "4 source arc(s)" in out)
check("mixed season raises the multi-arc HINT", "HINT" in out and "MORE THAN ONE" in out)
check("the hint points at the pass/fail census tool", "audit_arc_placement.py" in out)
check("per-arc file counts are shown (13 of one arc)", "13 file(s)" in out)

# The contrast case: one arc must NOT trip the hint, or the signal is worthless.
out_clean = run_report(CLEAN_SEASON)
check("clean season names its single arc", "01 - Bakemonogatari" in out_clean)
check("clean season is reported as 1 source arc", "1 source arc(s)" in out_clean)
check("clean season raises NO multi-arc hint", "MORE THAN ONE" not in out_clean)

# The regression that started all this: the old code's `sorted(...)[0]` sample.
src = (Path(__file__).resolve().parent / "verify_arc_mapping.py").read_text()
check("the single-sample line is gone from the source",
      "first_e, first_src = sorted(per_season[s])[0]" not in src)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("arc-mapping census: all checks passed")
