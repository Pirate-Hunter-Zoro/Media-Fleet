#!/usr/bin/env python3
"""The doctor's repair loop must verify itself and remember what it proved.

Two report classes that used to churn forever, neither tied to any one series:

  1. An episode image adopted from the provider that is STILL not a real still.
     The provider re-uses one image across every episode it has no distinct still
     for (classic serials are the shape; the rule names no series), so the
     per-pass re-adopt rewrote the same bytes, the shape check re-fired, and the
     report listed a problem whose only repair had already been attempted. The
     repair now records which provider answer it applied (`art_still_tried`) and,
     when the shape persists, remembers a per-image refusal for
     `ART_NO_STILL_TTL_SEC` -- the same memory the no-still case already had. A
     provider LOOKUP failure is never remembered (a TMDB blip must not hide an
     image for 30 days), and a NEW provider still is adopted immediately.
  2. An `[auto]` problem that survives `STUCK_AFTER_PASSES`. The counters were
     advanced inside `write_report`, which ran AFTER `_save(STATE_FILE, state)`,
     so they were never persisted and the report kept saying `[auto]` forever.
     The save now follows the report, a line that stops being reported is
     forgotten, and a healthy pass resets the show's counters.

Both directions, fakes only (no network, no live library):

    python3 scripts/test_media_doctor_repair.py

Exit 0 = all checks passed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media_doctor as md                                             # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


HOUR = 3600.0
NOW = 2_000_000_000.0
SHOW = "Some Show (1999)"
BAD = f"/library/Shows/{SHOW}/Season 01/{SHOW} - S01E01-thumb.jpg"
OTHER = f"/library/Shows/{SHOW}/Season 01/{SHOW} - S01E02-thumb.jpg"
REASON = "identical to 2 other episode image(s)"

print("Part 1 -- the provider's answer decides: adopt / already-tried / no-still")
check("no url at all -> no-still", md._art_next_action(None, None) == "no-still")
check("a url never tried -> adopt", md._art_next_action("http://i/a.jpg", None) == "adopt")
check("the same url already applied -> already-tried",
      md._art_next_action("http://i/a.jpg", "http://i/a.jpg") == "already-tried")
check("a NEW best url -> adopt",
      md._art_next_action("http://i/b.jpg", "http://i/a.jpg") == "adopt")

print("Part 2 -- a refusal is remembered per image, and only while fresh")
raw = [(BAD, REASON)]
check("no refusals -> kept", md._drop_refused_art(raw, {}) == raw)
check("a fresh refusal -> dropped",
      md._drop_refused_art(raw, {"art_no_still": {BAD: {"ts": NOW, "reason": "no-still"}}},
                           now=NOW) == [])
check("an EXPIRED refusal -> kept",
      md._drop_refused_art(raw, {"art_no_still": {BAD: NOW - md.ART_NO_STILL_TTL_SEC - 1}},
                           now=NOW) == raw)
check("the pre-upgrade bare-timestamp shape is honoured",
      md._drop_refused_art(raw, {"art_no_still": {BAD: NOW - HOUR}}, now=NOW) == [])
check("one image's refusal does not hide its neighbour",
      md._drop_refused_art(raw + [(OTHER, REASON)],
                           {"art_no_still": {BAD: {"ts": NOW}}}, now=NOW) == [(OTHER, REASON)])

print("Part 3 -- the live repair loop: adopt once, refuse after, never churn")


class FakeJf:
    def __init__(self) -> None:
        self.adopts: list[str] = []
        self.url: str | None = "http://images/x.jpg"
        self.raise_lookup = False

    def episodes(self, _sid):
        return [{"Id": "e1",
                 "Path": f"/library/Shows/{SHOW}/Season 01/{SHOW} - S01E01.mkv"}]

    def best_remote_still(self, _eid):
        if self.raise_lookup:
            raise RuntimeError("tmdb unreachable")
        return self.url

    def adopt_remote_image(self, _eid, _image_type, url):
        self.adopts.append(url)


def probs():
    return {"show": SHOW, "path": f"/library/Shows/{SHOW}", "sid": "s1", "sig": "1",
            "age": 1e9,
            "problems": [{"kind": "artwork_bogus", "detail": "1 ...", "auto": True,
                          "sev": 2, "art_bad": [(BAD, REASON)]}]}


state: dict = {}
jf = FakeJf()
acted = md.apply_auto_fixes(probs(), jf, state, dry_run=False, cycle_budget={"art": 40})
check("pass 1 adopts the provider's best answer", jf.adopts == [jf.url])
check("pass 1 records which answer it applied",
      state[SHOW]["art_still_tried"].get(BAD) == jf.url)
check("pass 1 records no refusal", not state[SHOW].get("art_no_still"))
check("pass 1 reports the re-adopt", any("re-adopt 1" in a for a in acted))

acted = md.apply_auto_fixes(probs(), jf, state, dry_run=False, cycle_budget={"art": 40})
check("pass 2 does NOT re-adopt the same bytes", jf.adopts == [jf.url])
check("pass 2 records the refusal with its reason",
      state[SHOW]["art_no_still"][BAD]["reason"] == "already-tried")
check("pass 2 reports it left as-is", any("left as-is" in a for a in acted))
check("the detection side now drops it",
      md._drop_refused_art(probs()["problems"][0]["art_bad"], state[SHOW],
                           now=time.time()) == [])

state2: dict = {}
jf2 = FakeJf()
jf2.raise_lookup = True
md.apply_auto_fixes(probs(), jf2, state2, dry_run=False, cycle_budget={"art": 40})
check("a transport blip records NO refusal", not state2[SHOW].get("art_no_still"))
check("a transport blip adopts nothing", jf2.adopts == [])

state3 = {SHOW: {"sig": "1",
                 "art_no_still": {BAD: {"ts": time.time(), "reason": "already-tried"}},
                 "art_still_tried": {BAD: "http://images/old.jpg"}}}
jf3 = FakeJf()
jf3.url = "http://images/new.jpg"
md.apply_auto_fixes(probs(), jf3, state3, dry_run=False, cycle_budget={"art": 40})
check("a new provider answer is adopted at once", jf3.adopts == ["http://images/new.jpg"])
check("adopting a new answer clears the stale refusal",
      BAD not in (state3[SHOW].get("art_no_still") or {}))

state4: dict = {}
jf4 = FakeJf()
md.apply_auto_fixes(probs(), jf4, state4, dry_run=True, cycle_budget={"art": 40})
check("dry run adopts nothing", jf4.adopts == [])
check("dry run records no attempt",
      not (state4.get(SHOW) or {}).get("art_still_tried"))

print("Part 4 -- a stuck [auto] line is persisted, demoted, and forgotten")
tmp = tempfile.TemporaryDirectory()
saved_report = md.REPORT_FILE
try:
    md.REPORT_FILE = Path(tmp.name) / "library_health.txt"
    st: dict = {}
    prob = {"kind": "some_auto_fault",
            "detail": "3 episode image(s) are not real stills (x)", "auto": True, "sev": 2}
    show_probs = [{"show": "Fixture Show", "problems": [prob]}]
    text = ""
    for _ in range(md.STUCK_AFTER_PASSES):
        md.write_report(show_probs, {}, state=st)
        text = md.REPORT_FILE.read_text()
    check("the count is persisted in state",
          st["Fixture Show"]["stuck"].get(md._stuck_key(prob)) == md.STUCK_AFTER_PASSES)
    check("the 4th pass demotes the line to NEEDS REVIEW", "[NEEDS REVIEW]" in text)
    check("and says how long it has been stuck",
          f"unchanged for {md.STUCK_AFTER_PASSES} passes" in text)

    md.write_report([{"show": "Fixture Show",
                      "problems": [{"kind": "other", "detail": "z", "auto": True,
                                    "sev": 1}]}], {}, state=st)
    check("a line that stops being reported is forgotten",
          md._stuck_key(prob) not in st["Fixture Show"]["stuck"])

    md._note_problem_pass(st, "Fixture Show", [])
    check("a healthy pass clears the counters", st["Fixture Show"]["stuck"] == {})

    # The first pass of a NEW problem is not already stuck.
    st2: dict = {}
    md.write_report(show_probs, {}, state=st2)
    check("a fresh problem starts at [auto]",
          "[auto]" in md.REPORT_FILE.read_text())
finally:
    md.REPORT_FILE = saved_report
    tmp.cleanup()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("all checks passed")
