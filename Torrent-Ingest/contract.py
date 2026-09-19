"""The plan API's contract, asserted at startup instead of discovered at runtime.

`library.validate_plan` / `apply_plan` / `verify_applied` stopped being private the moment
a second repo started calling them. `~/Developer/Media-Orchestrator/YouTube-Downloader` builds plans and
hands them to those functions directly, which means a change here can break that repo
SILENTLY: its plans simply start failing validation, hours later, on a machine nobody is
watching, and the log blames the plan rather than the schema change underneath it.

Prose in a README does not prevent that. This does: it exercises the actual contract
against the real code, cheaply enough to run on every daemon start, so a breaking change
surfaces at the boundary that broke rather than at the next ingest.

What it deliberately does NOT do:
  * touch the library -- no file is created, moved, or deleted. `validate_plan` never
    writes, so a plan naming an obviously-fake destination under the real MEDIA_ROOT is
    completely inert. `apply_plan` is never called.
  * mutate module state -- no monkeypatching of `config.MEDIA_ROOT`, so it is safe to run
    from inside a live daemon.
  * assert anything about a plan's CONTENT. It checks the shapes both ingests rely on,
    and the guards that must stay guards.

    python3 contract.py          # report; exit 1 if the contract is broken
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import config
import library

# A destination that cannot collide with anything real. Never written -- validate_plan
# resolves and confines paths but does not create them.
_FAKE_SHOW = "Shows/__contract_check__ (1970)/Season 01/__contract_check__ (1970) - S01E01 - X.mkv"
_FAKE_MOVIE = "Movies/__contract_check__ (1970).mkv"

# Everything the YouTube ingest imports from this repo. Listed explicitly so deleting or
# renaming one is caught here rather than by an ImportError in the other repo.
REQUIRED_CONFIG_ATTRS = (
    "MEDIA_ROOT", "SHOWS_ROOT", "MOVIES_ROOT", "DOWNLOADS_DIR", "MIN_FREE_BYTES",
    "EXTRA_PATH", "AI_BIN", "AI_MODEL", "VIDEO_EXTENSIONS", "STAGING_DIRNAME",
    "IDENTIFY_TRANSIENT_SIGNATURES", "MEDIAFS_MOUNT", "log_stamp",
    "IDENTIFY_UNAVAILABLE_SIGNATURES", "identify_unavailable", "ai_env",
)
REQUIRED_LIBRARY_ATTRS = (
    "build_library_digest", "validate_plan", "apply_plan", "verify_applied",
    "PlanError", "_atomic_write", "_owned_movie_nfo_xml",
)
REQUIRED_PLAYLIST_ATTRS = ("LIBRARY_ROOT", "MOVIES_DIR", "_parse_span", "resolve_items",
                           "manifest_path_for", "insert_movie_item", "manifest_has_movie")


def _accepts(plan, root, label, problems):
    try:
        library.validate_plan(plan, root)
    except Exception as exc:                                          # noqa: BLE001
        problems.append(f"{label}: should VALIDATE but was rejected -- {exc}")
        return None
    return plan


def _rejects(plan, root, label, problems):
    try:
        library.validate_plan(plan, root)
    except library.PlanError:
        return
    except Exception as exc:                                          # noqa: BLE001
        problems.append(f"{label}: rejected, but with {type(exc).__name__} rather than "
                        f"PlanError -- {exc}")
        return
    problems.append(f"{label}: should be REJECTED but validated. A guard has been lost.")


def check() -> list:
    """Run every contract assertion. Returns a list of human-readable problems ([] = ok)."""
    problems: list[str] = []

    for name, attrs, mod in (("config", REQUIRED_CONFIG_ATTRS, config),
                             ("library", REQUIRED_LIBRARY_ATTRS, library)):
        for attr in attrs:
            if not hasattr(mod, attr):
                problems.append(f"{name}.{attr} is missing -- the YouTube ingest imports it")

    # The AI runtime, checked as a SHAPE rather than a call: every judgment step in both
    # repos spawns `[*config.AI_BIN, ...]`, so a string here instead of a sequence would
    # splat into single characters and every run would fail with an unreadable execve
    # error hours later. No request is made -- a dead credential is already handled
    # correctly at run time (identify_unavailable -> defer), and a network call has no
    # business in a check that runs on every daemon start.
    ai_bin = getattr(config, "AI_BIN", None)
    if isinstance(ai_bin, str) or not isinstance(ai_bin, (list, tuple)) or not ai_bin:
        problems.append("config.AI_BIN must be a non-empty list (interpreter + runner "
                        "path); every call site spawns it with [*config.AI_BIN, ...]")
    elif not Path(ai_bin[-1]).exists():
        problems.append(f"config.AI_BIN points at {ai_bin[-1]}, which does not exist")

    try:
        import playlist
        for attr in REQUIRED_PLAYLIST_ATTRS:
            if not hasattr(playlist, attr):
                problems.append(f"playlist.{attr} is missing -- the playlist curator "
                                f"and/or the YouTube ingest import it")
        # The eviction trap: under the virtual library a film's payload is evicted from the
        # SSD lower, so a movie index built from config.MOVIES_ROOT sees almost nothing.
        # It must be built from the MOUNT. This silently blocked every {"movie": ...} token
        # once already.
        if getattr(playlist, "MOVIES_DIR", None) == getattr(config, "MOVIES_ROOT", None):
            problems.append(
                "playlist.MOVIES_DIR equals config.MOVIES_ROOT (the SSD lower). It must "
                "resolve against the mediafs MOUNT, or evicted films vanish from the "
                "index and every movie playlist token silently fails to resolve.")
    except ImportError as exc:
        problems.append(f"playlist module will not import: {exc}")

    try:
        import playlist_watch
        if not hasattr(playlist_watch, "consider_new_episodes"):
            problems.append("playlist_watch.consider_new_episodes is missing -- both "
                            "ingests call it after a placement")
    except ImportError as exc:
        problems.append(f"playlist_watch will not import: {exc}")

    if problems:
        return problems           # no point exercising behaviour with the API broken

    with tempfile.TemporaryDirectory(prefix="contract-") as td:
        root = Path(td).resolve()
        src = root / "sample.mkv"
        src.write_bytes(b"\0" * 1024)
        s = str(src)

        # 1. An OWNED SHOW episode: what the YouTube ingest produces for every
        #    playlist-derived series, and what One Pace produces. Requires season,
        #    episode, episode_title and plot, and must come back with the apply-time
        #    bookkeeping filled in.
        plan = _accepts({
            "media_type": "show", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": _FAKE_SHOW, "season": 1, "episode": 1,
                "episode_title": "T", "plot": "P"}],
        }, str(root), "owned show episode", problems)
        if plan is not None:
            f = plan["files"][0]
            for key in ("_src_abs", "_dst_abs", "_src_size"):
                if key not in f:
                    problems.append(f"validate_plan no longer sets {key}; apply_plan "
                                    f"requires it and both ingests rely on validate "
                                    f"populating it")

        # 2. An OWNED MOVIE with NO tmdb_id. This branch exists specifically so the
        #    YouTube ingest can file a long-form video as a standalone film -- nothing on
        #    YouTube has a TMDB entry. If it regresses, every `route: "movie"` fails.
        _accepts({
            "media_type": "movie", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": _FAKE_MOVIE, "owned": True,
                "movie_title": "X", "plot": "P", "year": 1970}],
        }, str(root), "owned movie without a tmdb_id", problems)

        # 3. The guards that must REMAIN guards. Each of these, if it lapsed, would let a
        #    permanently-blank title into the library, or would.
        _rejects({
            "media_type": "movie", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": _FAKE_MOVIE, "owned": True, "movie_title": "X"}],
        }, str(root), "owned movie with NO plot (locks a blank film)", problems)

        _rejects({
            "media_type": "movie", "title": "X", "files": [{
                "src": s, "dst_rel": _FAKE_MOVIE}],
        }, str(root), "un-owned movie with no tmdb_id (ships blank/mis-matched)", problems)

        _rejects({
            "media_type": "show", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": _FAKE_SHOW, "season": 1, "episode": 1,
                "episode_title": "T"}],
        }, str(root), "owned episode with NO plot (locks a blank episode)", problems)

        _rejects({
            "media_type": "show", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": "Music/nope.mkv", "season": 1, "episode": 1,
                "episode_title": "T", "plot": "P"}],
        }, str(root), "destination outside Shows/Movies/Comics", problems)

        _rejects({
            "media_type": "show", "owned": True, "title": "X", "files": [{
                "src": s, "dst_rel": "Shows/../../escape.mkv", "season": 1, "episode": 1,
                "episode_title": "T", "plot": "P"}],
        }, str(root), "destination escaping the media root", problems)

        _rejects({
            "media_type": "show", "owned": True, "title": "X", "files": [{
                "src": str(root.parent / "outside.mkv"), "dst_rel": _FAKE_SHOW,
                "season": 1, "episode": 1, "episode_title": "T", "plot": "P"}],
        }, str(root), "source outside the downloaded content", problems)

    return problems


def assert_ok(log_fn=None) -> bool:
    """Check, and report. Returns True if the contract holds.

    Callers decide how loud to be: the YouTube ingest refuses to place anything when this
    fails (its plans would be rejected anyway, so proceeding just burns bandwidth and
    fills the ledger with failures), while this repo's own daemons log and continue --
    a broken cross-repo contract must not stop torrents from ingesting.
    """
    problems = check()
    if not problems:
        return True
    say = log_fn or print
    say("PLAN API CONTRACT BROKEN -- library.validate_plan no longer behaves as the "
        "ingests require:")
    for p in problems:
        say(f"  * {p}")
    say("See Torrent-Ingest README, 'YouTube ingest', for what depends on this.")
    return False


def main() -> int:
    problems = check()
    if not problems:
        print("plan API contract: OK")
        return 0
    print("plan API contract: BROKEN")
    for p in problems:
        print(f"  * {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
