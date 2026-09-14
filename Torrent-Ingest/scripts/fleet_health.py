#!/usr/bin/env python3
"""fleet_health.py -- the human-attention watchdog.

The daemons are self-healing for everything they can fix, but a handful of failures
are human actions the AI can never take: re-issuing a missing OpenRouter key, adding a
MEGA account, freeing a full disk, or a dead drive. This daemon watches for exactly
those, and writes a phone-glanceable report to the iCloud Torrents folder
(`fleet_health.txt`) so the human sees "something needs you" without ever opening a log.

It deliberately does NOT fix anything and never fails: it is a detector + a notifier.

The AI credential case is simple now that the fleet is free-only. A missing OpenRouter
key must not fail any pipeline -- every consumer already DEFERS on `AIUnavailable`
(identify leaves the torrent at DOWNLOADED, escalation skips, the searcher's calls are
best-effort) -- so nothing breaks while the key is absent. This daemon checks the key
file every cycle and flips `fleet_health.txt` to a loud "ADD KEY" notice while it is
missing, then back to ALL CLEAR the moment it returns; the deferred work resumes on its
own the next cycle. Same for an empty key file.

Runs under launchd (`com.mikeyferguson.fleethealth`), looping every CYCLE_SEC (5 min);
also runnable by hand: `python3 scripts/fleet_health.py --once`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config      # noqa: E402
import yacreader_db       # noqa: E402
import yacreader_index    # noqa: E402


# --- tunables ----------------------------------------------------------------

CYCLE_SEC = int(os.environ.get("FLEET_HEALTH_CYCLE_SEC", "300"))   # 5 min
REPORT_FILE = config.TORRENTS_DIR / "fleet_health.txt"
# The same findings, machine-readable, for `fleet_doctor`. Written from the SAME
# collection pass as the report above so the two cannot disagree.
MACHINE_REPORT_FILE = config.STATE_DIR / "fleet_health.json"

OPENROUTER_KEY_FILE = Path.home() / ".config" / "api-keys" / "openrouter_key"

MEDIA_SYNCER_DIR = Path.home() / "Developer" / "Media-Fleet" / "Media-Syncer"
FREE_SPACE_FILE = MEDIA_SYNCER_DIR / "free_space.json"

# Flag a volume once its free space drops below this (the ingest chunk budget needs
# room on Downloads; the library SSD needs room to place files).
DISK_WARN_BYTES = int(os.environ.get("FLEET_HEALTH_DISK_WARN_GB", "50")) * 1024 ** 3
# Flag the MEGA free-space cache once it is this stale (Media-Syncer refreshes it every
# sync cycle, so a stale copy means the sync loop has stopped).
MEGA_STALE_SEC = int(os.environ.get("FLEET_HEALTH_MEGA_STALE_SEC", str(6 * 3600)))

# Google Drive (light novels) low-space threshold. Books are tiny, so the floor is low,
# but a Drive that fills up means novel placements silently fail — a human must free
# space (or buy more), so this is an ACTION, not a WARN.
GDRIVE_LOW_BYTES = int(os.environ.get("FLEET_HEALTH_GDRIVE_LOW_GB", "1")) * 1024 ** 3

# Extra known mirror domains probed deterministically on top of the free model's
# suggestions.
# 1337x's canonical domain sits behind Cloudflare and 403s datacenter/VPN egress, so its
# mirrors (which live on a different CDN) are exactly what a web search sometimes misses
# but a shape-probe cannot. Probed whenever discovery runs; a mirror that verifies is
# adopted even if the free model's WebSearch returns nothing useful.
MIRROR_CANDIDATES = {
    "1337x": (
        "https://1377x.to", "https://www.1377x.to", "https://1337x.st",
        "https://1337x.is", "https://1337x.bz", "https://x1337x.cc",
    ),
}
TRACKER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TRACKER_WARN_STREAK = int(os.environ.get("FLEET_HEALTH_TRACKER_WARN_STREAK", "6"))      # ~30 min
TRACKER_ACTION_STREAK = int(os.environ.get("FLEET_HEALTH_TRACKER_ACTION_STREAK", "72"))  # ~6 h
# How long after a failed auto-discovery to try again while the source stays down. A domain
# move is a slow event, but discovery must not be a one-shot: a mirror that appears next
# week is found because discovery keeps retrying.
DISCOVERY_RETRY_COOLDOWN_SEC = int(os.environ.get(
    "FLEET_HEALTH_DISCOVERY_RETRY_SEC", str(24 * 3600)))

# How long the acceptance gate may go without recording a verdict, WHILE DROPS ARE STILL
# ARRIVING, before that is an ACTION. §4.120's gate was dark for four days in a fleet with
# a health report, a verification script and a hand-off document, and none of them noticed.
GATE_DARK_SEC = int(os.environ.get("FLEET_HEALTH_GATE_DARK_SEC", str(24 * 3600)))

STATE_FILE = config.STATE_DIR / "fleet_health_state.json"





def _log(msg: str) -> None:
    print(f"[fleet_health] {msg}", flush=True)


def _human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


# --- the checks (each returns a list of (severity, message)) -----------------

def check_free_provider() -> list[tuple[str, str]]:
    """The fleet's AI is free-only, so the only credential that matters is the OpenRouter
    key's presence. A missing/empty key is an ACTION -- every AI consumer is paused until
    the human drops one in."""
    try:
        key = OPENROUTER_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return [("ACTION", "OpenRouter key file missing -- every AI consumer is paused")]
    if not key:
        return [("ACTION", "OpenRouter key file is empty -- every AI consumer is paused")]
    return []


def check_disk() -> list[tuple[str, str]]:
    """Flag a volume that is running low or has vanished (a dead drive)."""
    out: list[tuple[str, str]] = []
    for label, path in (("library SSD", config.MEDIA_ROOT),
                        ("downloads", Path.home() / "Downloads")):
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            out.append(("ACTION", f"{label} volume not accessible at {path} -- is the "
                                  f"drive mounted?"))
            continue
        if usage.free < DISK_WARN_BYTES:
            out.append(("ACTION", f"{label} low: {_human(usage.free)} free "
                                  f"(below {_human(DISK_WARN_BYTES)})"))
    return out


def _reaper_is_draining() -> bool:
    """True if the reaper process is alive right now.

    `pgrep` on the engine path, not `launchctl list`: the job can be loaded with no
    process, and what matters here is whether something is actively holding Media-Syncer
    down.
    """
    r = subprocess.run(["/usr/bin/pgrep", "-f", "Torrent-Ingest/reap.py"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def check_mega() -> list[tuple[str, str]]:
    """A stale free-space cache means Media-Syncer's sync loop has stopped.

    ...EXCEPT when the reaper is deliberately holding it down. The reaper kills
    `media_sync` for the duration of a purge and stamps `REAP_PAUSED_MARKER`; a drain has
    run for five days at a time, so the cache going stale is the EXPECTED, correct
    consequence of a healthy purge, not a fault. Reporting it as one trained the owner to
    ignore this line -- and a warning that is always there is a warning nobody reads.

    So the pause is subtracted, and the two things it could be hiding are asserted
    instead:

      * marker present, reaper ALIVE  -- expected; the drain explains the staleness.
      * marker present, reaper GONE   -- a LEAKED pause. `mediasync` has no KeepAlive and
        its watchdog deliberately will not relaunch while the marker exists, so nothing
        resumes it and replication stays stopped indefinitely. That is a worse fault than
        the one this check was written for, and it had no detector at all.
      * no marker at all              -- the original fault: the sync loop stopped on its
        own (a dead or unprovisioned MEGA account, a crashed daemon).
    """
    try:
        if not FREE_SPACE_FILE.exists():
            return [("WARN", "MEGA free-space cache missing -- Media-Syncer may be down")]
        age = time.time() - FREE_SPACE_FILE.stat().st_mtime
    except OSError:
        return [("WARN", "MEGA free-space cache unreadable")]

    paused = False
    try:
        paused = Path(config.REAP_PAUSED_MARKER).exists()
    except OSError:
        paused = False

    if paused and not _reaper_is_draining():
        return [("ACTION", "Media-Syncer is PAUSED but the reaper is not running -- the "
                           "pause marker leaked. Nothing will resume replication on its "
                           "own (mediasync has no KeepAlive and its watchdog stands down "
                           "while the marker exists). Remove "
                           f"{Path(config.REAP_PAUSED_MARKER).name} and kickstart "
                           "com.mikeyferguson.mediasync.")]
    if paused:
        return []                       # the drain explains it; this is healthy

    if age > MEGA_STALE_SEC:
        return [("WARN", f"MEGA free-space cache is {int(age // 3600)}h old -- Media-Syncer "
                         f"may be stuck; check media_sync.log")]
    return []


def check_gdrive() -> list[tuple[str, str]]:
    """Flag the Google Drive (light-novel) volume when it is low on space or unreachable.
    Books are tiny, so a low Drive is almost always an unexpected fill-up — a human must
    free space, hence an ACTION rather than a WARN."""
    try:
        usage = shutil.disk_usage(config.NOVELS_ROOT)
    except OSError:
        return [("ACTION", f"Google Drive Novels folder not accessible at "
                           f"{config.NOVELS_ROOT} -- is the Google Drive app running?")]
    if usage.free < GDRIVE_LOW_BYTES:
        return [("ACTION", f"Google Drive low on space: {_human(usage.free)} free "
                           f"(below {_human(GDRIVE_LOW_BYTES)}). Light-novel placements "
                           f"will fail until you free space or buy more.")]
    return []






def _launchctl_labels() -> tuple[set, dict]:
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


def _expected_labels() -> set:
    """Every agent the repos say should exist. Mirrors `remedies.expected_labels()`."""
    found = set()
    try:
        for p in (Path.home() / "Developer" / "Media-Fleet").glob("*/com.mikeyferguson.*.plist"):
            found.add(p.stem)
    except OSError:
        return set()
    return found - {"com.mikeyferguson.splittunnel"}   # opt-in root LaunchDaemon


def check_daemons() -> list[tuple[str, str]]:
    """A daemon that launchd has loaded but that is not running.

    Deliberately does NOT report `torrentreap` (frequently mid-drain; a restart throws away
    its MEGA probe) or `mediasync` (no KeepAlive by design -- the reaper kills it while
    purging, and its own watchdog restarts it only when the pause marker is absent).
    Reporting either would invite exactly the repair that must never happen.
    """
    _labels, pids = _launchctl_labels()
    watched = {
        "com.mikeyferguson.torrentingest", "com.mikeyferguson.directingest",
        "com.mikeyferguson.driveingest", "com.mikeyferguson.fleethealth",
        "com.mikeyferguson.mediadoctor", "com.mikeyferguson.librarysupervisor",
        "com.mikeyferguson.megasupervisor", "com.mikeyferguson.fleetdoctor",
    }
    dead = sorted(l.rsplit(".", 1)[-1] for l in watched if pids.get(l) == "-")
    if not dead:
        return []
    return [("WARN", f"{len(dead)} daemon(s) loaded but not running: "
                     f"{', '.join(dead)}. fleet_doctor restarts these automatically.")]


def check_daemons_loaded() -> list[tuple[str, str]]:
    """An agent that has VANISHED from launchd entirely.

    `KeepAlive` only restarts a job launchd still knows about. It does nothing for a label
    that was booted out, whose plist was deleted from ~/Library/LaunchAgents, or that was
    never installed after a fresh checkout -- and that failure is silent, because
    `launchctl list` simply stops mentioning it. Not hypothetical: the searcher was removed
    exactly this way and nothing noticed for four days.

    The expected set is DERIVED from the repos' own plists, so adding a daemon supervises
    it automatically and no second list can drift out of step.
    """
    expected = _expected_labels()
    if not expected:
        return []                       # cannot read the repos; do not invent an alarm
    loaded, _pids = _launchctl_labels()
    missing = sorted(expected - loaded)
    out: list[tuple[str, str]] = []

    reaper = "com.mikeyferguson.torrentreap"
    if reaper in missing:
        missing.remove(reaper)
        out.append(("ACTION", "the reaper (torrentreap) is NOT LOADED. Deletions will "
                              "queue up harmlessly until it is back -- this is NOT "
                              "auto-started, because starting it at the wrong moment "
                              "purges MEGA copies irreversibly. See fleet_doctor.txt."))
    if missing:
        out.append(("WARN", f"{len(missing)} agent(s) not loaded at all: "
                            f"{', '.join(m.rsplit('.', 1)[-1] for m in missing)}. "
                            f"fleet_doctor bootstraps these automatically."))
    return out


def check_library() -> list[tuple[str, str]]:
    """Surface how many library items media_doctor has flagged for human review, so the
    phone report shows them without the human opening library_health.txt."""
    try:
        report = config.TORRENTS_DIR / "library_health.txt"
        if not report.exists():
            return []
        text = report.read_text(encoding="utf-8", errors="replace")
        need_review = text.count("NEEDS REVIEW")
        if need_review:
            return [("WARN", f"{need_review} library item(s) need review "
                             f"(see library_health.txt)")]
    except OSError:
        pass
    return []


def _yacreader_integrity() -> str:
    try:
        con = sqlite3.connect(f"file:{config.YACREADER_DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"unopenable: {exc}"
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return f"malformed: {exc}"
    finally:
        con.close()


def check_yacreader() -> list[tuple[str, str]]:
    """Is the reader indexing what the shelf holds?

    YACReader never notices the filesystem on its own: a filed comic is invisible until
    the APP runs a library update, and on 2026-09-14 every ElfQuest file sat on the shelf
    and in the pool with BOTH auto-update flags `false` -- the owner's report was simply
    "it's not showing up". `library_supervisor` owns the repair; this check is the fleet's
    detector for the states that outlive it:

      * a folder row that SIGSEGVs the app on its next reload (`FolderModel::createModelData`
        dereferences a missing parent -- scripts/yacreader_index_repair.py),
      * a damaged index (restore the newest backup that PASSES integrity_check),
      * the app UP but idle with no library open (a crash restore leaves no window, so
        `LibrariesUpdateCoordinator::init()` never runs and nothing scans),
      * auto-update flags off,
      * shelf files the index does not know about.

    The inventory-only freshness check deliberately skips the FUSE walk (this runs every
    5 minutes); it can under-report a file that was filed seconds ago and not yet
    uploaded, never over-report one that is missing.
    """
    db = config.YACREADER_DB
    if not db.exists():
        return [("ACTION", "no YacReader index exists; the reader cannot show comics "
                           "(a full rescan rebuilds it)")]

    out: list[tuple[str, str]] = []
    faults = yacreader_index.load_order_faults(db)
    if any(f["kind"] == "unreadable" for f in faults):
        detail = next(f["detail"] for f in faults if f["kind"] == "unreadable")
        return [("ACTION", f"the YacReader index cannot be read ({detail}); restore the "
                           f"newest backup that passes integrity_check "
                           f"(scripts/yacreader_index_health.py)")]
    if faults:
        out.append(("ACTION", f"{len(faults)} YacReader folder row(s) will crash the "
                              f"reader on its next reload -- FolderModel::createModelData "
                              f"dereferences a missing parent; run "
                              f"scripts/yacreader_index_repair.py --apply"))

    integ = _yacreader_integrity()
    if integ != "ok":
        out.append(("ACTION", f"the YacReader index is damaged ({integ.splitlines()[0]}); "
                              f"restore the newest backup that PASSES integrity_check "
                              f"(scripts/yacreader_index_health.py)"))

    running = yacreader_db.app_running()
    busy = yacreader_db.update_in_progress() if running else False

    if not yacreader_db.scan_settings_ok():
        out.append(("ACTION", "YacReader's auto-update flags are OFF, so new comics are "
                              "never indexed (scripts/yacreader_rescan.py --apply)"))

    missing = yacreader_index.unindexed_files(
        db, config.MEDIA_SYNCER_INVENTORY, config.MEDIAFS_MOUNT / "Comics",
        include_mount=False)
    if missing:
        if busy:
            out.append(("WARN", f"{len(missing)} comic file(s) on the shelf are not in "
                                f"the YacReader index yet; an update is running"))
        elif running:
            quiet = yacreader_db.index_quiet_sec()
            if quiet is None or quiet >= config.SUPERVISOR_YAC_STALE_ACTION_SEC:
                out.append(("ACTION", f"{len(missing)} comic file(s) are on the shelf but "
                                      f"not in the YacReader index and the reader has "
                                      f"been idle; run "
                                      f"scripts/yacreader_rescan.py --apply"))
            else:
                out.append(("WARN", f"{len(missing)} comic file(s) on the shelf are not "
                                    f"in the YacReader index yet; the index changed "
                                    f"{int(quiet // 60)}m ago, so a scan may be starting"))
        else:
            out.append(("WARN", f"{len(missing)} comic file(s) are on the shelf but not "
                                f"in the YacReader index; the reader is down (the "
                                f"supervisor starts it with the mount)"))
    return out










def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def check_vpn() -> list[tuple[str, str]]:
    """The VPN, read from the tailscale watchdog's heartbeat.

    THE FLEET'S HEALTH REPORT SAID NOTHING ABOUT ITS SINGLE POINT OF FAILURE. Every MEGA
    transfer, every mediafs read and every torrent rides the Tailscale exit node, and
    Jellyfin serves its library THROUGH that mount -- so on 2026-09-01 a dead exit node took
    all four down for five hours and this report, the fleet's one human-readable summary,
    did not mention it. The watchdog was repairing the whole time; nothing was reporting.

    An ACTION, not a WARN, for the two states that stop the fleet dead: no traffic, and a
    STALE heartbeat. Stale is the important one -- it means the watchdog itself is not
    running, so nothing is supervising the tunnel at all, and no self-report can ever tell
    you that on its own behalf.
    """
    try:
        sys.path.insert(0, str(Path.home() / "Developer" / "Media-Fleet" / "Media-Syncer"))
        from scripts import tailscale_watchdog as tsw           # noqa: PLC0415
    except Exception as exc:                                    # noqa: BLE001
        return [("WARN", f"cannot read the tailscale watchdog heartbeat: {exc}")]
    d = tsw.read_status()
    line = tsw.status_line()
    if not d:
        return [("ACTION", line)]
    if d.get("stale"):
        return [("ACTION", line)]
    if d.get("traffic_ok") is False:
        return [("ACTION", line)]
    if not d.get("address") or not d.get("exit_node"):
        return [("WARN", line)]
    return []


def _newest_drop_age() -> float | None:
    """Seconds since the most recent drop appeared in the watch folder, or None if empty.

    This is the "is the pipeline even acquiring?" qualifier. A quiet gate is only a fault
    if there is work it should have judged -- otherwise a fleet with nothing to fetch would
    report its own idleness as a broken guard, and an alarm that cries wolf gets ignored
    exactly when it matters.
    """
    newest = None
    for d in (config.TORRENTS_DIR, config.QUEUED_DIR, config.INGESTING_DIR):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() not in (".torrent", ".magnet"):
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return None if newest is None else max(0.0, time.time() - newest)


def check_acceptance_gate() -> list[tuple[str, str]]:
    """Is the fleet's authoritative acceptance gate still being REACHED? (§4.120)

    THIS IS THE CHECK WHOSE ABSENCE COST THE MOST. `db_acceptance` decides whether a
    torrent is worth downloading at all, and it existed on only one of the two drop paths.
    When every `.torrent` fetch started failing and 100% of drops became magnets, the gate
    simply stopped being called -- it did not fail, it was never reached -- and it stayed
    that way for four days while the queue filled with repeats and nothing said a word.

    A guard that fails soft needs a heartbeat or it is indistinguishable from a guard that
    was deleted (§7). So the gate now records every verdict it reaches, and this compares
    "when did the gate last speak?" against "is the pipeline still taking in work?". Those
    two facts together are the only way to tell a correctly idle gate from a bypassed one.
    """
    try:
        sys.path.insert(0, str(Path.home() / "Developer" / "Media-Fleet" / "Torrent-Ingest"))
        import acceptance_gate                                  # noqa: PLC0415
    except Exception as exc:                                    # noqa: BLE001
        return [("WARN", f"cannot read the acceptance-gate heartbeat: {exc}")]
    try:
        live = acceptance_gate.liveness()
    except Exception as exc:                                    # noqa: BLE001
        return [("WARN", f"acceptance-gate heartbeat unreadable: {exc}")]

    drop_age = _newest_drop_age()
    counts = live.get("counts") or {}
    tally = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no verdicts"
    age = live.get("age_sec")

    if age is None:
        if drop_age is not None and drop_age < GATE_DARK_SEC:
            return [("ACTION",
                     "the acceptance gate has NEVER recorded a verdict, yet drops are "
                     f"still arriving (newest {int(drop_age // 60)}m ago) — the fleet is "
                     "acquiring content that nothing is judging (§4.120)")]
        return [("WARN", "the acceptance gate has no heartbeat yet (no drops either)")]

    if age > GATE_DARK_SEC and drop_age is not None and drop_age < GATE_DARK_SEC:
        return [("ACTION",
                 f"the acceptance gate has been silent for {age / 3600:.1f}h while drops "
                 f"kept arriving (newest {int(drop_age // 60)}m ago) — it is dark, "
                 f"whatever the queue looks like [{tally}]")]

    if age > GATE_DARK_SEC:
        return [("WARN", f"acceptance gate quiet for {age / 3600:.1f}h, but no recent "
                         f"drops either [{tally}]")]
    return []


def collect(state: dict) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    for check in (check_vpn, check_acceptance_gate, check_free_provider, check_disk,
                  check_mega, check_gdrive, check_library, check_yacreader,
                  check_daemons, check_daemons_loaded):
        try:
            issues.extend(check())
        except Exception as exc:                                      # noqa: BLE001
            issues.append(("WARN", f"{check.__name__} failed: {exc}"))
    return issues


def collect_tagged(state: dict) -> list[tuple[str, str, str]]:
    """`collect`, but each issue carries the NAME OF THE CHECK that raised it.

    `fleet_doctor` needs a stable key to look a remedy up by, and the only stable key here
    is which check fired -- the message is prose written for a phone screen and is edited
    whenever it reads badly. Matching a remedy on that prose would make every wording
    change a silent behaviour change, which is §4.26's fault exactly: reading the label
    instead of the thing.

    `collect()` is kept as-is because `write_report` and the daemon loop only ever needed
    (severity, message), and widening their tuple to carry a field they ignore is how the
    report and the machine-readable file drift into two different truths. Both are built
    from THIS function -- see `main` -- so there is one collection pass, not two.
    """
    issues: list[tuple[str, str, str]] = []
    for check in (check_vpn, check_acceptance_gate, check_free_provider, check_disk,
                  check_mega, check_gdrive, check_library, check_yacreader,
                  check_daemons, check_daemons_loaded):
        try:
            for sev, msg in check():
                issues.append((sev, msg, check.__name__))
        except Exception as exc:                                      # noqa: BLE001
            issues.append(("WARN", f"{check.__name__} failed: {exc}", "check_failed"))
    return issues


def write_machine_report(tagged: list[tuple[str, str, str]]) -> None:
    """The same findings as `fleet_health.txt`, as JSON, for `fleet_doctor`.

    Written beside the human report and from the same pass, so the two can never disagree
    about what is wrong. `check` is the stable key; `message` is the prose and is NOT to be
    matched on.
    """
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_ts": time.time(),
        "findings": [{"severity": sev, "check": check, "message": msg}
                     for sev, msg, check in tagged],
    }
    try:
        MACHINE_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MACHINE_REPORT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(MACHINE_REPORT_FILE)
    except OSError:
        pass


def write_report(issues: list[tuple[str, str]]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"FLEET HEALTH  {now}", ""]
    actions = [m for sev, m in issues if sev == "ACTION"]
    warns = [m for sev, m in issues if sev == "WARN"]
    if not issues:
        lines.append("ALL CLEAR -- nothing needs you.")
    else:
        lines.append(f"{len(actions)} action(s), {len(warns)} warning(s):")
        lines.append("")
        for m in actions:
            lines.append(f"  [ACTION] {m}")
        for m in warns:
            lines.append(f"  [warn]   {m}")
        lines.append("")
        lines.append("Everything else keeps running; AI work that is paused resumes on its "
                     "own once the action above is resolved.")
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Fleet human-attention watchdog.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    args = ap.parse_args()

    _log("fleet_health up (cycle %ds)" % CYCLE_SEC)
    state = _load_state()
    while True:
        tagged = collect_tagged(state)
        issues = [(sev, msg) for sev, msg, _check in tagged]
        _save_state(state)
        write_report(issues)
        write_machine_report(tagged)
        if issues:
            _log(f"{len(issues)} issue(s): "
                 + "; ".join(m for _s, m in issues[:3]))
        else:
            _log("all clear")
        if args.once:
            return 0
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
