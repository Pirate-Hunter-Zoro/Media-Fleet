#!/usr/bin/env python3
"""The self-healer must not be able to destroy anything. Asserted, not promised.

`fleet_doctor` runs unattended and can be steered, for unrecognised findings, by a free
model's one-word answer. The safety story therefore cannot be "the remedies are careful" --
it has to be that a destructive remedy CANNOT BE ADDED without failing the build. This
reads the source of every remedy in the registry and refuses the ones that reach for a
destructive operation.

Why source inspection rather than trusting review: this fleet's own history is a list of
plausible, well-reviewed actions that destroyed things -- a sweeper that removed
directories that "looked empty on the mount" queued 1,205 files of KEPT content. The
review was not the missing part.

Both directions throughout (§4.5): the scanner is shown a deliberately destructive
function and must FAIL it, or "no remedy is destructive" is a sentence that would pass
against a scanner that can no longer see anything.

Read-only: it inspects source and calls only `detect()`, which every remedy documents as
side-effect free.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import remedies                                                      # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


# Operations no remedy may contain. Deliberately blunt: a remedy that legitimately needs
# one of these is a remedy that should not be automatic, so a false positive here is the
# system working. `unlink`/`rmtree`/`rm ` cover the filesystem, the media roots cover
# placement, and the last group covers this fleet's specific irreversible paths.
# Destructive primitives. Banned in EVERY callable a remedy has, including `detect` --
# detect runs on every pass, including --dry-run, so it must be as safe as it claims.
FORBIDDEN = (
    "rmtree", "unlink(", "os.remove", "shutil.move", "rm -rf", "rm -f",
    "apply_plan", "mediafs_deletions", "reap.py",
    "DROP TABLE", "DELETE FROM", "purge_batch", "bootout",
)

# Banned only in the ACTING halves (`apply`/`verify`). A remedy may legitimately NAME the
# reaper in `detect` -- asking "is torrentreap loaded?" is a read, and `reaper_not_loaded`
# exists precisely to report that without starting it. What must never appear is the
# reaper inside the half that runs commands.
#
# The blunt version of this rule banned the bare token everywhere and failed a remedy whose
# entire purpose is to NOT touch the reaper. A guard that cannot tell "mentions" from
# "acts on" pushes the next person to weaken it, which is how a real guard gets lost.
FORBIDDEN_IN_ACTIONS = ("torrentreap", "torrentsearcher")

# Paths a remedy may never write. `~/Media` is the library; a deletion is only real through
# the mount, so both spellings are barred.
FORBIDDEN_PATHS = ("MediaLibrary", "/Media/", "Media\"", "SSD_LIBRARY_ROOT")


def _src(fn) -> str:
    if fn is None:
        return ""
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""                       # a lambda in a comprehension; nothing to read


def _remedy_source(r) -> str:
    """Every callable a remedy can reach, concatenated."""
    return "\n".join(_src(fn) for fn in (r.detect, r.apply, r.verify))


def test_no_destructive_remedies() -> None:
    print("no remedy can destroy anything")
    for r in remedies.REGISTRY:
        src = _remedy_source(r)
        hits = [tok for tok in FORBIDDEN if tok in src]
        check(f"{r.id}: no destructive operation", hits, [])

        # The acting halves may not so much as name the reaper or the quarantined searcher.
        actions = _src(r.apply) + "\n" + _src(r.verify)
        act_hits = [tok for tok in FORBIDDEN_IN_ACTIONS if tok in actions]
        check(f"{r.id}: does not act on the reaper/searcher", act_hits, [])

        path_hits = [tok for tok in FORBIDDEN_PATHS if tok in src]
        # `remedies` reads ~/Media/Comics to see whether a comic ARRIVED, which is a read.
        # The bar is that it must not WRITE there, so a read-only rglob is allowed through
        # explicitly rather than by weakening the scanner for everyone.
        check(f"{r.id}: does not write a media root", path_hits, [])


def test_bootstrap_excludes_the_reaper() -> None:
    """The one auto remedy that STARTS daemons must never start the reaper.

    Asserted behaviourally, not by reading source: `_missing_labels()` is what the apply
    step iterates, so if the reaper ever fell out of that filter this fails even though
    the source scan above would still pass.
    """
    print("\nthe daemon bootstrapper cannot start the reaper")
    missing = remedies._missing_labels()
    check("torrentreap is never in the bootstrap set",
          "com.mikeyferguson.torrentreap" in missing, False)
    # And prove the filter is real: with the reaper forced absent, it still must not appear.
    real = remedies.loaded_labels
    try:
        remedies.loaded_labels = lambda: (
            {l for l in real()[0] if "torrentreap" not in l}, real()[1])
        forced = remedies._missing_labels()
        check("...even when the reaper IS genuinely absent (control)",
              "com.mikeyferguson.torrentreap" in forced, False)
        check("...while the control proves absence is detectable",
              "com.mikeyferguson.torrentreap" in remedies.expected_labels(), True)
    finally:
        remedies.loaded_labels = real


def test_scanner_can_fail() -> None:
    """The control. Without this, every assertion above is unfalsifiable."""
    print("\nthe scanner can actually fail something (control)")

    def _destructive():
        import shutil
        shutil.rmtree("/tmp/anything")

    src = inspect.getsource(_destructive)
    check("a deliberately destructive function is caught",
          [t for t in FORBIDDEN if t in src], ["rmtree"])


def test_auto_remedies_are_complete() -> None:
    print("\nevery auto remedy can prove its work")
    for r in remedies.REGISTRY:
        if r.safety != "auto":
            continue
        check(f"{r.id}: has an apply step", r.apply is not None, True)
        # §4.20: a repair must PROVE it changed something, never report a count.
        check(f"{r.id}: has a verify step", r.verify is not None, True)
    for r in remedies.REGISTRY:
        check(f"{r.id}: safety is a known value", r.safety in ("auto", "owner"), True)
        check(f"{r.id}: answers at least one check", len(r.answers) >= 1, True)
    check("remedy ids are unique",
          len(remedies.ids()), len(set(remedies.ids())))


def test_owner_remedies_explain() -> None:
    print("\nevery owner remedy says what to do")
    for r in remedies.REGISTRY:
        if r.safety != "owner":
            continue
        check(f"{r.id}: has an instruction",
              len(r.owner_instruction.strip()) > 40, True)


def test_reaper_is_untouchable() -> None:
    """The reaper must never be restartable. §4.6/§4.24: `ship-fleet.sh` carried it in its
    restart list the entire time the hand-off said DO NOT RESTART THE REAPER in capitals,
    one invocation from discarding a three-day drain. A nightly self-healer would have
    found that gap far faster than a human deploy did."""
    print("\nthe reaper and the quarantined searcher are unrestartable")
    check("torrentreap is not restartable",
          "com.mikeyferguson.torrentreap" in remedies.RESTARTABLE_LABELS, False)
    # Discovery was removed from the fleet on 2026-09-10, so `torrentsearcher` must not
    # appear anywhere in the registry -- a remedy naming it would re-create the subsystem.
    check("torrentsearcher is not restartable (it no longer exists)",
          "com.mikeyferguson.torrentsearcher" in remedies.RESTARTABLE_LABELS, False)
    check("torrentreap is named in NEVER_RESTART with a reason",
          len(remedies.NEVER_RESTART.get("com.mikeyferguson.torrentreap", "")) > 20, True)
    check("only the reaper is in NEVER_RESTART",
          sorted(remedies.NEVER_RESTART), ["com.mikeyferguson.torrentreap"])
    overlap = set(remedies.RESTARTABLE_LABELS) & set(remedies.NEVER_RESTART)
    check("nothing is both restartable and never-restart", overlap, set())


def test_detect_is_side_effect_free() -> None:
    """`detect()` runs on every pass for every finding, including in --dry-run, so it must
    be safe to call. Calling each one is the only honest way to assert it does not blow up
    on this machine's real state."""
    print("\nevery detect() runs clean against live state")
    for r in remedies.REGISTRY:
        try:
            still, evidence = r.detect()
            check(f"{r.id}: detect() returns (bool, str)",
                  isinstance(still, bool) and isinstance(evidence, str), True)
        except Exception as exc:                                      # noqa: BLE001
            check(f"{r.id}: detect() runs", f"raised {exc!r}", "no exception")


def test_ai_can_only_pick_registered_ids() -> None:
    """The model's answer is a LOOKUP, not an instruction. Anything that is not a
    registered id must resolve to None -- that is the whole containment."""
    print("\na model answer can only ever name a registered repair")
    for bad in ("rm -rf ~/Media", "delete everything", "NONE", "make_up_an_id",
                "restart_dead_daemon; rm -rf /", ""):
        check(f"{bad[:32]!r} is not a remedy", remedies.by_id(bad), None)
    check("a real id resolves", remedies.by_id("restart_dead_daemon") is not None,
          True)


def main() -> int:
    test_no_destructive_remedies()
    test_bootstrap_excludes_the_reaper()
    test_scanner_can_fail()
    test_auto_remedies_are_complete()
    test_owner_remedies_explain()
    test_reaper_is_untouchable()
    test_detect_is_side_effect_free()
    test_ai_can_only_pick_registered_ids()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("remedies: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
