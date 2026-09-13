"""Startup assertions for the couplings this repo cannot survive without.

This repo depends on two others (Torrent-Ingest for placement, Media-Syncer for the VPN
split-tunnel), and every one of those dependencies fails in the same unhelpful
way: quietly, hours later, on a machine nobody is watching, with a log that blames the
wrong layer. Documenting them in a README helps a human reading the README; it does
nothing for a 3am cycle.

So each one is asserted here at cycle start, and the check names the ACTUAL fix rather
than the symptom.

Three classes, in order of how badly they mislead when unchecked:

  1. MODULE SHADOWING. Torrent-Ingest's modules all do a plain `import config` and must
     get THEIRS. This repo's own settings therefore live in `ytconfig.py`. If a
     `config.py` ever appears here it wins that import -- the entry script's directory
     leads `sys.path` -- and `library` comes up holding a module with none of the
     attributes it needs. The failure is an AttributeError deep inside placement, which
     looks like a Torrent-Ingest bug and is not.
  2. THE PLAN API CONTRACT. Delegated to Torrent-Ingest's own `contract` module, which
     owns those functions and exercises them for real (see its docstring).
   3. THE INSTALLED SPLIT-TUNNEL BEING STALE. `route -n get` proves whether routes are in
      force right now, which `youtube_sync` already gates on -- but it cannot notice that
      `/usr/local/bin/split_tunnel.sh` is an OLD copy whose routes happen to
      still be up. Updating it needs `sudo`, so it is the step most often skipped after a
      change, and the consequence only shows up at the next reboot.
"""
from __future__ import annotations

import datetime
import filecmp
import re
import subprocess
from pathlib import Path

import discover
import ytconfig

INSTALLED_SPLIT_TUNNEL = Path("/usr/local/bin/split_tunnel.sh")
REPO_SPLIT_TUNNEL = (Path.home() / "Developer" / "Media-Fleet" / "Media-Syncer" / "scripts"
                     / "split_tunnel.sh")

# Everything this repo imports out of Torrent-Ingest. Checked by name so a rename over
# there is reported here, at the boundary, instead of raising mid-placement.
_BORROWED = {
    "library": ("build_library_digest", "validate_plan", "apply_plan", "verify_applied",
                "PlanError", "_atomic_write"),
    "playlist": ("LIBRARY_ROOT", "MOVIES_DIR", "_parse_span"),
}


def _check_no_config_shadow(problems: list) -> None:
    stray = ytconfig.PROJECT_ROOT / "config.py"
    if stray.exists():
        problems.append(
            f"FATAL: {stray} exists. Torrent-Ingest's modules do a plain `import config` "
            f"and would get THIS file instead of theirs, so placement breaks with a "
            f"confusing AttributeError. This repo's settings belong in ytconfig.py -- "
            f"delete or rename {stray.name}.")
    # A stale __pycache__ entry can shadow just as effectively as the source file.
    for cached in (ytconfig.PROJECT_ROOT / "__pycache__").glob("config.*.pyc"):
        problems.append(
            f"FATAL: stale bytecode {cached} can shadow Torrent-Ingest's config. "
            f"Delete {ytconfig.PROJECT_ROOT / '__pycache__'}.")


def _check_borrowed_modules(problems: list) -> None:
    import config as ti_config
    # Confirm `import config` really resolved to the sibling repo and not to something on
    # sys.path that happens to be called config.
    resolved = Path(getattr(ti_config, "__file__", "") or "").resolve()
    expected_dir = ytconfig.TORRENT_INGEST_DIR.resolve()
    if expected_dir not in resolved.parents:
        problems.append(
            f"FATAL: `import config` resolved to {resolved}, which is not inside "
            f"{expected_dir}. Something on sys.path is shadowing Torrent-Ingest's "
            f"config.py; placement will misbehave in ways that look like its bug.")
        return
    for mod_name, attrs in _BORROWED.items():
        try:
            mod = __import__(mod_name)
        except ImportError as exc:
            problems.append(f"FATAL: cannot import Torrent-Ingest's {mod_name}: {exc}")
            continue
        for attr in attrs:
            if not hasattr(mod, attr):
                problems.append(
                    f"FATAL: {mod_name}.{attr} no longer exists in Torrent-Ingest. This "
                    f"repo calls it directly; see that repo's README under 'YouTube "
                    f"ingest' for what is load-bearing.")


def _check_plan_contract(problems: list) -> None:
    """Delegate to Torrent-Ingest's own contract test -- it owns those functions."""
    try:
        import contract
    except ImportError as exc:
        problems.append(f"FATAL: Torrent-Ingest's contract module will not import "
                        f"({exc}); the plan API cannot be verified.")
        return
    for p in contract.check():
        problems.append(f"FATAL: plan API contract -- {p}")


def _check_split_tunnel_fresh(warnings: list) -> None:
    """A WARNING, not fatal: stale routes that are currently up still work. It matters at
    the next reboot, and it needs sudo to fix, so it must be said out loud rather than
    inferred from the egress probe (which cannot see staleness)."""
    if not REPO_SPLIT_TUNNEL.exists():
        warnings.append(f"Media-Syncer's split-tunnel script is missing at "
                        f"{REPO_SPLIT_TUNNEL}; Google cannot be pinned around the VPN.")
        return
    if not INSTALLED_SPLIT_TUNNEL.exists():
        warnings.append(
            f"The split-tunnel daemon is NOT installed ({INSTALLED_SPLIT_TUNNEL} absent). "
            f"Until it is, every cycle defers -- see Media-Syncer's README for the sudo "
            f"install steps.")
        return
    try:
        same = filecmp.cmp(INSTALLED_SPLIT_TUNNEL, REPO_SPLIT_TUNNEL, shallow=False)
    except OSError:
        return
    if not same:
        warnings.append(
            f"The INSTALLED split-tunnel is a stale copy of "
            f"{REPO_SPLIT_TUNNEL.name}. Current routes may still be up, so this is not "
            f"breaking anything right now -- but the installed copy is what runs at the "
            f"next boot. Fix needs sudo:\n"
            f"    sudo cp {REPO_SPLIT_TUNNEL} {INSTALLED_SPLIT_TUNNEL}")


def _check_free_space_floor(warnings: list) -> None:
    """Keep this repo's disk floor within one bounded dip of Media-Syncer's.

    The Downloads volume and the library are the same physical disk, so whichever floor is
    LOWER is the one that actually binds -- and if ours is far below, the YouTube ingest
    quietly eats the headroom Media-Syncer reserves for torrent staging and the predictive
    cache, while every check here still reports healthy. Read from Media-Syncer's own config
    so the two cannot drift apart silently; if it moves its floor, this says so.

    But they must NOT be equal either. Media-Syncer's tiering drives the disk down toward its
    own floor by design, so a floor level with it leaves `free - MIN_FREE_BYTES` at roughly
    zero permanently and nothing here ever downloads (measured 2026-08-07: 485 MB of headroom,
    19 hours of deferred waves). So the check is a band, not a minimum: ours must be below
    theirs by exactly the deliberate FLOOR_DIP_BYTES, and never below Torrent-Ingest's own
    20 GiB OS headroom.
    """
    ms_cfg = Path.home() / "Developer" / "Media-Fleet" / "Media-Syncer" / "scripts" / "config.py"
    try:
        text = ms_cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    m = re.search(r"^SSD_MIN_FREE_BYTES\s*=\s*(\d+)\s*\*\s*1024\*\*3", text, re.M)
    if not m:
        return
    theirs = int(m.group(1)) * 1024 ** 3
    expected = theirs - ytconfig.FLOOR_DIP_BYTES
    gib = lambda b: f"{b // 1024**3} GiB"
    if ytconfig.MIN_FREE_BYTES < expected:
        warnings.append(
            f"Disk floor dips too far below Media-Syncer's: ours {gib(ytconfig.MIN_FREE_BYTES)} "
            f"vs its SSD_MIN_FREE_BYTES {gib(theirs)}, on the same physical volume, a dip of "
            f"{gib(theirs - ytconfig.MIN_FREE_BYTES)} where {gib(ytconfig.FLOOR_DIP_BYTES)} is "
            f"intended. The lower floor is the one that binds, so downloads here can eat the "
            f"headroom it reserves for torrent staging and the predictive cache. Raise "
            f"ytconfig.MIN_FREE_BYTES to {gib(expected)}.")
    elif ytconfig.MIN_FREE_BYTES >= theirs:
        warnings.append(
            f"Disk floor is level with or above Media-Syncer's: ours "
            f"{gib(ytconfig.MIN_FREE_BYTES)} vs its SSD_MIN_FREE_BYTES {gib(theirs)}. That "
            f"looks conservative and is actually a stall -- its tiering drives the disk down "
            f"toward its own floor, so the headroom left here settles at roughly zero and no "
            f"wave ever fits. Lower ytconfig.MIN_FREE_BYTES to {gib(expected)}.")


# How old a yt-dlp build may be before it is reported. Releases land roughly monthly and
# YouTube changes its player continuously, so a build much past this starts failing the
# MEDIA fetch while metadata still works.
YT_DLP_STALE_DAYS = 30


def _check_yt_dlp_fresh(warnings: list) -> None:
    """Report a yt-dlp too old to still speak YouTube's current player protocol.

    This is the dependency that rots fastest and the only one that used to fail with no
    fingerprint of its own. A stale build extracts metadata fine and then 403s the media
    fetch -- which is indistinguishable, in the log, from the PO-token gating that
    `PLAYER_CLIENTS_FALLBACK` exists to route around. So the chain dutifully retries every
    client, every client 403s for the real reason (the build, not the client), and four
    cycles later the video is parked as if YouTube had refused it.

    Measured 2026-09-05: yt-dlp 2026.07.04 403'd two tracks on `default`, `web_embedded`
    and `web_music` alike, on every attempt; 2026.08.19 downloaded both immediately, same
    machine, same route, same client. Six weeks of drift was enough.

    A warning, not a failure: an old build still places most things, and stopping the whole
    ingest over a fetch that might succeed would be the worse trade. The point is that the
    log names the cause the first time instead of the fifth.
    """
    try:
        # discover.build_env(), not os.environ: launchd hands this daemon a bare PATH,
        # and the check must resolve the SAME yt-dlp the download passes will run.
        out = subprocess.run([ytconfig.YT_DLP, "--version"], capture_output=True,
                             text=True, timeout=30, env=discover.build_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"Cannot run {ytconfig.YT_DLP} ({exc}). Nothing will download this "
                        f"cycle. Install it with `brew install yt-dlp`.")
        return
    ver = (out.stdout or "").strip().splitlines()[0].strip() if out.stdout else ""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", ver)
    if not m:
        return                      # a nightly or a git build; nothing honest to say
    built = datetime.date(*(int(g) for g in m.groups()))
    age = (datetime.date.today() - built).days
    if age <= YT_DLP_STALE_DAYS:
        return
    warnings.append(
        f"yt-dlp is {age} days old (build {ver}). YouTube changes its player faster than "
        f"that, and a stale build 403s the MEDIA fetch while metadata still succeeds -- "
        f"which looks exactly like the PO-token gating PLAYER_CLIENTS_FALLBACK routes "
        f"around, so every client in the chain fails for a reason no client can fix and "
        f"the video parks after four cycles. Fix: `brew upgrade yt-dlp`.")


def _check_audio_dirs(warnings: list) -> None:
    """Warn when an audio destination's cloud tree is not mounted.

    Both music folders live in a File Provider tree (iCloud Drive, Google Drive), and an
    unmounted tree looks exactly like a path that does not exist. `soundtracks.file_track`
    refuses to create one for that reason -- so an unmounted Drive turns into a run of
    "failed" ledger entries whose cause is nowhere near this repo. Saying it here, once, at
    cycle start names the actual fix.

    A warning rather than fatal on purpose: it stops ONE playlist, and the library side of
    the cycle is unaffected. Refusing to run at all because a music folder is offline would
    be a much worse trade than filing everything else and retrying the tracks next cycle.
    """
    for pid, d in ytconfig.AUDIO_PLAYLIST_DIRS.items():
        if d.is_dir() or d.parent.is_dir():
            continue
        warnings.append(
            f"Audio destination for playlist {pid} is unreachable: {d} (its parent "
            f"{d.parent} does not exist). The cloud folder is probably not mounted -- open "
            f"it in Finder, or check the account is still signed in. Tracks from that "
            f"playlist will be recorded as failed and retried next cycle; nothing else in "
            f"the cycle is affected.")


def check() -> tuple[list, list]:
    """Returns (fatal_problems, warnings). Empty fatal list means it is safe to ingest."""
    problems: list[str] = []
    warnings: list[str] = []
    _check_no_config_shadow(problems)
    if not problems:                  # a shadowed config makes everything below noise
        _check_borrowed_modules(problems)
    if not problems:
        _check_plan_contract(problems)
    _check_split_tunnel_fresh(warnings)
    _check_free_space_floor(warnings)
    _check_yt_dlp_fresh(warnings)
    _check_audio_dirs(warnings)
    return problems, warnings


def assert_ok(log_fn=print) -> bool:
    """Report, and say whether ingesting is safe.

    A fatal problem means placement WOULD fail anyway -- every plan would be rejected --
    so refusing up front just avoids burning bandwidth and filling the ledger with
    failures that blame the wrong thing.
    """
    problems, warnings = check()
    for w in warnings:
        log_fn("! " + w.replace("\n", "\n! "))
    if not problems:
        return True
    log_fn("=" * 70)
    log_fn("PREFLIGHT FAILED -- a cross-repo dependency is broken, so nothing will be "
           "placed this cycle:")
    for p in problems:
        log_fn(f"  * {p}")
    log_fn("=" * 70)
    return False


def main() -> int:
    problems, warnings = check()
    for w in warnings:
        print(f"WARN: {w}")
    if problems:
        print("preflight: FAILED")
        for p in problems:
            print(f"  * {p}")
        return 1
    print("preflight: OK" + ("  (with warnings)" if warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
