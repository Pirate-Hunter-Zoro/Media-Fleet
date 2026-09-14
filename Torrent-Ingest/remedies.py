"""The CLOSED registry of things the fleet is allowed to fix by itself.

`fleet_doctor` reads `fleet_health.json` and looks each finding up here. What it finds is
a remedy that was written, reviewed and tested in advance -- never an action composed at
runtime. That is the whole design, and it is the §4.4 rule (the model proposes, the
harness disposes) applied to self-healing: a free model is allowed to choose WHICH of
these runs, and is never allowed to say what one does.

WHY A REGISTRY AND NOT "LET THE AI FIX IT"
------------------------------------------
This fleet moves and deletes real media, and its own history is a list of plausible
actions that destroyed things: a sweeper that removed directories that "looked empty on
the mount" queued 1,205 files of KEPT content; a queue line pointed at a re-downloaded
volume would have destroyed it three separate times. Every one of those would have been
approved by a competent reader of the symptom. So the safety property here cannot be "the
model is careful"; it has to be "the model cannot express a destructive action at all",
and that is what a closed registry of pre-written remedies gives.

THE FOUR RULES EVERY REMEDY OBEYS
---------------------------------
1. **It re-checks before acting.** A finding is a snapshot from up to five minutes ago;
   `detect()` re-establishes it NOW. A remedy that fires on a stale finding is how a fixed
   problem gets "fixed" again.
2. **It proves it worked.** `verify()` re-reads the world and returns what actually
   changed. §4.20 was paid for by a repair tool that REPORTED 250 repairs it never made,
   so a remedy may never report a count it did not confirm.
3. **It is idempotent.** Running it twice is running it once. The doctor loops.
4. **It is non-destructive, structurally.** No remedy deletes media, writes `~/Media`,
   touches the deletion queue, or restarts the reaper. `test_remedies.py` asserts this by
   reading the source of every remedy in the registry, so a future remedy that reaches for
   `rm -rf` fails the build rather than the library.

Anything that does not fit those rules is an OWNER remedy: the doctor writes precise
instructions and does nothing. That is a real outcome, not a failure -- "only you can do
this" is frequently the correct and honest answer, and saying it plainly beats inventing
an automated action that half-works.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config
import yacreader_db
import yacreader_index

DIRECT_INGEST_DIR = Path.home() / "Downloads" / "DirectIngest"
MEDIA_ROOT = Path.home() / "Media"

#: Labels the doctor may restart. `torrentreap` is DELIBERATELY ABSENT and must stay
#: absent: a restart throws away the MEGA account probe that is the purge's whole cost,
#: and a drain has run for days at a time. `ship-fleet.sh` learned this the hard way
#: (§4.24); a self-healing loop that could kick it would learn it every night.
#:
#: `mediasync` is absent for a DIFFERENT reason and must also stay absent: it carries no
#: `KeepAlive` on purpose, because the reaper kills it while purging and a relaunch would
#: fight that mid-purge. Its own watchdog (`mediasync_watchdog`) restarts it only when the
#: reaper's pause marker is absent, which is a distinction this doctor cannot make.
RESTARTABLE_LABELS = (
    "com.mikeyferguson.torrentingest",
    "com.mikeyferguson.directingest",
    "com.mikeyferguson.driveingest",
    "com.mikeyferguson.fleethealth",
    "com.mikeyferguson.mediadoctor",
    "com.mikeyferguson.librarysupervisor",
    "com.mikeyferguson.megasupervisor",
)

#: Where a launchd agent's plist lives once installed.
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
#: The repos whose `com.mikeyferguson.*.plist` files define the expected fleet.
DEV_ROOT = Path.home() / "Developer" / "Media-Fleet"

#: Labels that legitimately are NOT loaded in the user domain, with the reason. Anything
#: else with a plist in a repo is expected to be loaded, and a missing one is a fault.
NOT_EXPECTED = {
    "com.mikeyferguson.splittunnel":
        "an OPT-IN root LaunchDaemon in /Library/LaunchDaemons, deliberately not "
        "auto-installed and outside the user domain entirely",
}


def expected_labels() -> set[str]:
    """Every label the fleet should have loaded, DERIVED from the repos' own plists.

    Derived rather than hand-listed on purpose. A hand-maintained roster is a second copy
    of the truth, and the fleet's recurring failure is exactly that shape: two records of
    one fact drifting apart while both look healthy. Adding a plist to a repo makes it
    supervised automatically; nobody has to remember to update a list here.
    """
    found = set()
    try:
        for p in DEV_ROOT.glob("*/com.mikeyferguson.*.plist"):
            found.add(p.stem)
    except OSError:
        return set()
    return found - set(NOT_EXPECTED)


def loaded_labels() -> tuple[set[str], dict[str, str]]:
    """`(labels launchd has loaded, {label: pid_field})`. Empty on any failure."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set(), {}
    labels, pids = set(), {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("com.mikeyferguson."):
            labels.add(parts[2])
            pids[parts[2]] = parts[0]
    return labels, pids

#: Never restarted by the doctor, with the reason, so a future reader does not "fix" the
#: omission. Asserted by `test_remedies.py`.
NEVER_RESTART = {
    "com.mikeyferguson.torrentreap":
        "a restart discards the in-flight MEGA account probe and can throw away a "
        "multi-day drain (§4.6, §4.24)",
}
# `torrentsearcher` is not here, and not in RESTARTABLE_LABELS, because it no longer
# exists: torrent and comic DISCOVERY was removed from the fleet on 2026-09-10 (see
# README "Acquisition"). Nothing searches, nothing drops, and the owner hand-drops every
# `.torrent`. A future reader adding an entry for it would be re-creating the subsystem.


@dataclass(frozen=True)
class Remedy:
    id: str
    #: `fleet_health` check names this remedy answers. Matching is on the CHECK, never on
    #: the message prose -- the prose is written for a phone screen and gets reworded.
    answers: tuple[str, ...]
    title: str
    #: "auto" -- the doctor may apply it. "owner" -- the doctor only writes instructions.
    safety: str
    #: `() -> (still_true, evidence)`. Re-establishes the fault NOW.
    detect: Callable[[], tuple[bool, str]]
    #: `() -> (changed, what_changed)`. Only called for safety == "auto".
    apply: Callable[[], tuple[bool, str]] | None = None
    #: `() -> (ok, evidence)`. Proves the change landed. Required for "auto".
    verify: Callable[[], tuple[bool, str]] | None = None
    owner_instruction: str = ""



# --- remedy: a daemon that should be running is not --------------------------

def _dead_labels() -> list[str]:
    """Loaded, but with no running process."""
    _labels, pids = loaded_labels()
    return sorted(l for l in RESTARTABLE_LABELS if pids.get(l) == "-")


# --- remedy: a daemon that VANISHED from launchd entirely --------------------
#
# `KeepAlive` only restarts a job launchd still has. It does nothing for a label that was
# booted out, whose plist was removed from ~/Library/LaunchAgents, or that never got
# installed after a fresh checkout -- and that failure is completely silent, because
# `launchctl list` simply stops mentioning it. It is also not hypothetical: the searcher
# was removed exactly this way, and nothing in the fleet noticed for four days.

def _missing_labels() -> list[str]:
    """Expected by the repos, absent from launchd. The reaper is excluded -- see below."""
    missing = expected_labels() - loaded_labels()[0]
    return sorted(l for l in missing if l != "com.mikeyferguson.torrentreap")


def _detect_missing_daemon() -> tuple[bool, str]:
    missing = _missing_labels()
    if not missing:
        return False, f"all {len(expected_labels())} expected agents are loaded"
    return True, "not loaded at all: " + ", ".join(m.split(".")[-1] for m in missing)


def _apply_missing_daemon() -> tuple[bool, str]:
    done, failed = [], []
    for label in _missing_labels():
        plist = LAUNCH_AGENTS / f"{label}.plist"
        if not plist.exists():
            # Install from the repo copy first; a label cannot be bootstrapped without one.
            src = next(DEV_ROOT.glob(f"*/{label}.plist"), None)
            if src is None:
                failed.append(f"{label} (no plist in any repo)")
                continue
            try:
                LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
                plist.write_bytes(src.read_bytes())
            except OSError as exc:
                failed.append(f"{label} ({exc})")
                continue
        try:
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                           capture_output=True, text=True, timeout=60)
            done.append(label)
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append(f"{label} ({exc})")
    detail = ("bootstrapped " + ", ".join(d.split(".")[-1] for d in done)) if done else ""
    if failed:
        detail = (detail + "; could not: " + ", ".join(failed)).lstrip("; ")
    return bool(done), detail or "nothing to bootstrap"


def _verify_missing_daemon() -> tuple[bool, str]:
    still = _missing_labels()
    if still:
        return False, "still not loaded: " + ", ".join(s.split(".")[-1] for s in still)
    return True, f"all {len(expected_labels())} expected agents are loaded"


MISSING_DAEMON = Remedy(
    id="bootstrap_missing_daemon",
    answers=("check_daemons_loaded",),
    title="Load a daemon that has vanished from launchd entirely",
    safety="auto",
    detect=_detect_missing_daemon,
    apply=_apply_missing_daemon,
    verify=_verify_missing_daemon,
)


REAPER_MISSING = Remedy(
    id="reaper_not_loaded",
    answers=("check_reaper_loaded",),
    title="The reaper is not loaded — deliberately NOT started automatically",
    safety="owner",
    detect=lambda: (
        "com.mikeyferguson.torrentreap" not in loaded_labels()[0],
        "torrentreap is absent from launchd"),
    owner_instruction=(
        "This one is not auto-repaired, and the asymmetry is deliberate. Not running the "
        "reaper only means deletions queue up on disk, which is harmless and reversible. "
        "Starting it at the wrong moment is not: it drains `mediafs_deletions.jsonl`, "
        "which is the ONLY path that purges a MEGA copy, and that is irreversible.\n"
        "    If you do want it: launchctl bootstrap gui/501 "
        "~/Library/LaunchAgents/com.mikeyferguson.torrentreap.plist\n"
        "    Check first that no drain is already in flight: "
        "pgrep -f 'Torrent-Ingest/reap.py'"),
)


def _detect_dead_daemon() -> tuple[bool, str]:
    dead = _dead_labels()
    if not dead:
        return False, "every restartable daemon has a pid"
    return True, "not running: " + ", ".join(dead)


def _apply_dead_daemon() -> tuple[bool, str]:
    done = []
    for label in _dead_labels():
        if label in NEVER_RESTART:                    # belt and braces; see the constant
            continue
        try:
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                           capture_output=True, text=True, timeout=60)
            done.append(label)
        except (OSError, subprocess.SubprocessError):
            continue
    return bool(done), ("restarted " + ", ".join(done)) if done else "nothing restarted"


def _verify_dead_daemon() -> tuple[bool, str]:
    """Prove each one came back with a real pid, rather than reporting the kickstart."""
    still = _dead_labels()
    if still:
        return False, "still not running: " + ", ".join(still)
    return True, "every restartable daemon has a pid"


DEAD_DAEMON = Remedy(
    id="restart_dead_daemon",
    answers=("check_daemons",),
    title="Restart a daemon that is loaded but not running",
    safety="auto",
    detect=_detect_dead_daemon,
    apply=_apply_dead_daemon,
    verify=_verify_dead_daemon,
)


# --- remedy: the reader is not picking up the shelf --------------------------
#
# WHY THIS IS A REMEDY AND NOT JUST A SUPERVISOR. The supervisor owns the normal path
# (patch the flags, bounce the app, consume the filing marker). But it can only act on
# state it polls; when it was itself the thing that needed upgrading, the reader sat with
# both auto-update flags `false` while every ElfQuest file was on the shelf -- invisible.
# `fleet_health` now detects that state from the outside and this remedy repairs it with
# the same reviewed tool a human would run, so the loop closes even when the supervisor
# is the broken half. `yacreader_rescan.py --apply` is idempotent and non-destructive:
# flags patched under the index lock, marker dropped, app restarted by the supervisor.

def _detect_yacreader_refresh() -> tuple[bool, str]:
    if not config.YACREADER_DB.exists():
        return False, "no YacReader index exists yet"
    if not yacreader_db.scan_settings_ok():
        return True, "the auto-update flags are off"
    if not yacreader_db.app_running():
        return False, "the reader is down; its next start scans with the flags on"
    if yacreader_db.update_in_progress():
        return False, "an update is already running"
    missing = yacreader_index.unindexed_files(
        config.YACREADER_DB, config.MEDIA_SYNCER_INVENTORY,
        config.MEDIAFS_MOUNT / "Comics", include_mount=False)
    if missing:
        quiet = yacreader_db.index_quiet_sec()
        if quiet is not None and quiet < config.SUPERVISOR_YAC_STALE_ACTION_SEC:
            return False, ("the index changed recently; a scan may already be starting")
        return True, f"{len(missing)} shelf comic file(s) are not in the index"
    return False, "the reader is current and its flags are on"


def _apply_yacreader_refresh() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent / "scripts" / "yacreader_rescan.py"),
             "--apply"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run yacreader_rescan.py: {exc}"
    if proc.returncode != 0:
        return False, (f"yacreader_rescan.py rc={proc.returncode}: "
                       f"{(proc.stderr or proc.stdout or '').strip()[-200:]}")
    return True, "checked the scan flags and requested a refresh"


def _verify_yacreader_refresh() -> tuple[bool, str]:
    if not yacreader_db.scan_settings_ok():
        return False, "the auto-update flags are still not on"
    if config.YACREADER_REFRESH_MARKER.exists():
        return True, ("flags are on; the refresh marker is pending and the supervisor "
                      "will bounce the reader")
    return True, ("flags are on; the refresh marker was already consumed, so the "
                  "supervisor is restarting the reader now")


REFRESH_YACREADER = Remedy(
    id="refresh_yacreader",
    answers=("check_yacreader",),
    title="Put YacReader's auto-update flags on and request a library rescan",
    safety="auto",
    detect=_detect_yacreader_refresh,
    apply=_apply_yacreader_refresh,
    verify=_verify_yacreader_refresh,
)


# --- remedy: the reader's own index will crash it ----------------------------
#
# `FolderModel::createModelData` dereferences the parent it looks up
# `ORDER BY parentId,name` with NO null check, so a dangling/cyclic/rootless folder row is
# a SIGSEGV on the next reload -- the 2026-09-13 crash, which left the app up for ten
# hours with a stale index and new comics invisible. `yacreader_index_repair.py --apply`
# fixes the tree under the index lock, with a verified backup and an integrity check.

def _detect_yacreader_crash_rows() -> tuple[bool, str]:
    if not config.YACREADER_DB.exists():
        return False, "no YacReader index exists yet"
    faults = [f for f in yacreader_index.load_order_faults(config.YACREADER_DB)
              if f["kind"] != "unreadable"]
    if not faults:
        return False, "no row can crash the loader"
    return True, (f"{len(faults)} row(s) would crash the loader: "
                  + "; ".join(f["detail"] for f in faults[:3]))


def _apply_yacreader_crash_rows() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent / "scripts" / "yacreader_index_repair.py"),
             "--apply"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run yacreader_index_repair.py: {exc}"
    if proc.returncode != 0:
        return False, (f"yacreader_index_repair.py rc={proc.returncode}: "
                       f"{(proc.stderr or proc.stdout or '').strip()[-200:]}")
    return True, "repaired the folder tree under the index lock"


def _verify_yacreader_crash_rows() -> tuple[bool, str]:
    faults = [f for f in yacreader_index.load_order_faults(config.YACREADER_DB)
              if f["kind"] != "unreadable"]
    if faults:
        return False, f"{len(faults)} crash row(s) remain"
    return True, "the loader's parent-order invariant holds"


REPAIR_YACREADER_INDEX = Remedy(
    id="repair_yacreader_index",
    answers=("check_yacreader",),
    title="Repair folder rows that would crash YacReader's loader",
    safety="auto",
    detect=_detect_yacreader_crash_rows,
    apply=_apply_yacreader_crash_rows,
    verify=_verify_yacreader_crash_rows,
)


# --- remedies the doctor must NEVER automate ---------------------------------
#
# Present in the registry ON PURPOSE. A finding with no entry at all gets a generic "a
# person needs to look at this"; these get the specific reason automation is wrong, which
# is the part a future session (or a future model) most needs to be told.

GATE_DARK = Remedy(
    id="acceptance_gate_dark",
    answers=("check_acceptance_gate",),
    title="The acceptance gate has recorded no verdict while drops kept arriving",
    safety="owner",
    detect=lambda: (True, "reported by fleet_health's own liveness check"),
    owner_instruction=(
        "DO NOT clear this by writing a heartbeat. A heartbeat is cleared by real work or "
        "not at all (§4.22): the flag stays lit until a drop is actually admitted through "
        "the gate, and faking it removes the only signal that acquisition has stopped.\n"
        "    Diagnose with: python3 scripts/gate_status.py -- it prints the verdict tally "
        "and the drop mix, which is what says whether the gate is unreached or merely idle."),
)

LIBRARY_REVIEW = Remedy(
    id="library_needs_review",
    answers=("check_library",),
    title="Library items flagged for review",
    safety="owner",
    detect=lambda: (True, "reported by media_doctor"),
    owner_instruction=(
        "This is a CURATION list, not a bug list, and it is not safe to action "
        "automatically: 82 of the 86 items on it were misnumbered file sets whose .nfo "
        "names the right episode, and deleting per the report would have destroyed real "
        "episodes (§4.26).\n"
        "    Read library_health.txt in the Torrents folder. Believe the .nfo over the "
        "filename."),
)

DISK_LOW = Remedy(
    id="disk_low",
    answers=("check_disk",),
    title="A volume is running low on space",
    safety="owner",
    detect=lambda: (True, "reported by fleet_health"),
    owner_instruction=(
        "Nothing here deletes to free space, deliberately -- every automatic disk-freeing "
        "path this fleet has had ended up queueing kept content for destruction.\n"
        "    Check staging first: du -sh ~/Downloads/.torrent-ingest. If identify is "
        "capped, staged bytes cannot be filed and will keep growing; that is a reason to "
        "wait for the daily reset, not to delete."),
)

AI_KEY_MISSING = Remedy(
    id="ai_key_missing",
    answers=("check_free_provider",),
    title="The OpenRouter key is missing or empty",
    safety="owner",
    detect=lambda: (True, "reported by fleet_health"),
    owner_instruction=(
        "Put a free OpenRouter key in ~/.config/api-keys/openrouter_key.\n"
        "    Nothing breaks while it is absent -- every consumer defers rather than "
        "failing -- and the paused work resumes on its own once the key is back.\n"
        "    A PAID key is forbidden (§4.3), including the paid slug a 404 offers you."),
)


def _detect_yacreader_damaged() -> tuple[bool, str]:
    if not config.YACREADER_DB.exists():
        return False, "no YacReader index exists yet"
    try:
        con = sqlite3.connect(f"file:{config.YACREADER_DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return True, f"the index is unopenable: {exc}"
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return True, f"the index is malformed: {exc}"
    finally:
        con.close()
    return (integ != "ok"), f"integrity_check says {str(integ).splitlines()[0]!r}"


YACREADER_INDEX_DAMAGED = Remedy(
    id="yacreader_index_damaged",
    answers=("check_yacreader",),
    title="The YacReader index is damaged",
    safety="owner",
    detect=_detect_yacreader_damaged,
    owner_instruction=(
        "Do NOT restore the NEWEST backup -- restore the newest one that PASSES "
        "integrity_check. A backup taken on the way into a repair is a backup of the "
        "damage, and restoring it restores the corruption (§4.185).\n"
        "    python3 scripts/yacreader_index_health.py   # prints the per-backup census\n"
        "    cp -p '<the newest CLEAN backup>' "
        "~/Media/Comics/.yacreaderlibrary/library.ydb\n"
        "    Then let the supervisor restart YacReader (or run "
        "scripts/yacreader_rescan.py --apply). Do not run comic_shelf_audit --apply "
        "against a damaged index; it refuses one precisely so a repair cannot snapshot "
        "the damage."),
)


REGISTRY: tuple[Remedy, ...] = (
    DEAD_DAEMON,
    MISSING_DAEMON,
    REAPER_MISSING,
    REFRESH_YACREADER,
    REPAIR_YACREADER_INDEX,
    YACREADER_INDEX_DAMAGED,
    GATE_DARK,
    LIBRARY_REVIEW,
    DISK_LOW,
    AI_KEY_MISSING,
)


def for_check(check: str) -> list[Remedy]:
    """Remedies registered against a `fleet_health` check name, autos first."""
    hits = [r for r in REGISTRY if check in r.answers]
    return sorted(hits, key=lambda r: 0 if r.safety == "auto" else 1)


def by_id(remedy_id: str) -> Remedy | None:
    return next((r for r in REGISTRY if r.id == remedy_id), None)


def ids() -> list[str]:
    return [r.id for r in REGISTRY]


__all__ = ["Remedy", "REGISTRY", "RESTARTABLE_LABELS", "NEVER_RESTART",
           "for_check", "by_id", "ids"]
