#!/usr/bin/env python3
"""Regression test: the iCloud DirectIngest bridge moves, never clobbers, never loses
(2026-09-13).

THE CASE. `iCloud Drive/Torrents/DirectIngest/` is a drop point: anything in it is
bridged onto the local `~/Downloads/DirectIngest/`, where `direct_ingest.py` files it.
The move crosses volumes and crosses a sync layer, so the failure modes are specific:

  * a drop iCloud has not finished handing over must be left alone (never half-copied,
    never deleted);
  * a crash between copy and rename must never leave a half-file where the ingest
    daemon would file it -- hence staging + `os.replace` + verify;
  * a same-named local file must never be clobbered: identical content means the iCloud
    copy is a leftover and is removed; different content is uniquified;
  * only ingestible media is touched, and a placeholder is seen for what it stands for.

BOTH DIRECTIONS (§4.5):
  Part 1 -- `find_drops` picks up exactly the ingestible entries, placeholders included.
  Part 2 -- a moved file/folder lands locally, the iCloud source is removed, and no
            staging leftovers remain.
  Part 3 -- collisions: identical -> iCloud duplicate removed; different -> `.1`.
  Part 4 -- a failed copy leaves the iCloud source untouched and stages nothing.
  Part 5 -- dry-run reports and changes nothing (not even a materialization).

    python3 scripts/test_direct_ingest_bridge.py

Writes only inside a temp dir. Exit 0 means every check passed.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import direct_ingest_bridge as bridge                                   # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


tmp = Path(tempfile.mkdtemp(prefix="direct-ingest-bridge-")).resolve()
source = tmp / "iCloud" / "Torrents" / "DirectIngest"
dest = tmp / "Downloads" / "DirectIngest"
stage = dest / ".icloud-bridge"
saved = (bridge.SOURCE_DIR, bridge.DEST_DIR, bridge.STAGE_DIR, bridge.LOG_FILE,
         bridge.materialize, bridge._copy_verified)
real_copy_verified = bridge._copy_verified
real_materialize = bridge.materialize


def reset():
    shutil.rmtree(tmp / "iCloud", ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    stage.mkdir(parents=True)


try:
    bridge.SOURCE_DIR = source
    bridge.DEST_DIR = dest
    bridge.STAGE_DIR = stage
    bridge.LOG_FILE = tmp / "bridge.log"
    bridge.materialize = lambda p, wait_sec=600: True

    # ----------------------------------------------------------------------
    print("Part 1 -- discovery: media, folders, placeholders; junk excluded")
    reset()
    (source / "movie.mkv").write_bytes(b"video")
    (source / "book.epub").write_bytes(b"book")
    (source / "notes.txt").write_bytes(b"text")
    (source / ".hidden.mkv").write_bytes(b"hidden")
    (source / "._junk.mkv").write_bytes(b"appledouble")
    (source / ".DS_Store").write_bytes(b"finder")
    (source / ".Placeholder.cbz.icloud").write_bytes(b"placeholder")
    (source / "failed").mkdir()
    (source / "failed" / "old.cbz").write_bytes(b"old")
    (source / "season").mkdir()
    (source / "season" / "Show.S01E01.mkv").write_bytes(b"video")
    (source / "clutter").mkdir()
    (source / "clutter" / "readme.txt").write_bytes(b"nope")
    got = sorted(p.name for p in bridge.find_drops())
    check("exactly the ingestible drops are found",
          got, sorted(["movie.mkv", "book.epub", "Placeholder.cbz", "season"]))

    # placeholder resolution, both a top-level drop and a file inside a folder
    check("a top-level placeholder resolves to its real path",
          bridge._placeholders(source / "Placeholder.cbz"), [str(source / "Placeholder.cbz")])
    inside = source / "season"
    (inside / ".Show.S01E02.mkv.icloud").write_bytes(b"placeholder")
    check("a placeholder inside a folder resolves too",
          bridge._placeholders(inside), [str(inside / "Show.S01E02.mkv")])
    (inside / ".Show.S01E02.mkv.icloud").unlink()
    # neither bytes nor placeholder -> materialize must not block a pass
    check("materialize gives up at once when iCloud has surfaced nothing",
          real_materialize(source / "never-uploaded.mkv", wait_sec=1), False)

    # ----------------------------------------------------------------------
    print("\nPart 2 -- a settled file and a settled folder are moved off iCloud")
    reset()
    f = source / "Some Film (2024).mkv"
    f.write_bytes(b"m" * 4096)
    check("scan_once reports one move", bridge.scan_once(), 1)
    check("the file is local", (dest / "Some Film (2024).mkv").exists(), True)
    check("its bytes are intact", (dest / "Some Film (2024).mkv").read_bytes(), b"m" * 4096)
    check("the iCloud copy is gone", f.exists(), False)
    check("no staging leftovers", list(stage.glob(".staging-*")), [])

    d = source / "Some Show Season 1"
    d.mkdir()
    (d / "Show.S01E01.mkv").write_bytes(b"e" * 100)
    (d / "Show.S01E01.srt").write_bytes(b"s" * 10)
    check("a folder drop moves as one unit", bridge.scan_once(), 1)
    check("the folder is local",
          (dest / "Some Show Season 1" / "Show.S01E01.mkv").read_bytes(), b"e" * 100)
    check("the iCloud folder is gone", d.exists(), False)

    # ----------------------------------------------------------------------
    print("\nPart 3 -- collisions never clobber")
    reset()
    src = source / "same.mkv"
    src.write_bytes(b"identical")
    (dest / "same.mkv").write_bytes(b"identical")
    check("an identical local copy makes the iCloud copy redundant", bridge.scan_once(), 1)
    check("the iCloud duplicate is removed", src.exists(), False)
    check("the local copy is untouched", (dest / "same.mkv").read_bytes(), b"identical")
    check("no `.1` was created", (dest / "same.1.mkv").exists(), False)

    reset()
    src = source / "conflict.mkv"
    src.write_bytes(b"new version")
    (dest / "conflict.mkv").write_bytes(b"old version")
    check("a different local file still ingests", bridge.scan_once(), 1)
    check("the new file was uniquified",
          (dest / "conflict.1.mkv").read_bytes(), b"new version")
    check("the old local file is untouched", (dest / "conflict.mkv").read_bytes(),
          b"old version")
    check("the iCloud copy is gone", src.exists(), False)

    # ----------------------------------------------------------------------
    print("\nPart 4 -- a failed copy leaves iCloud untouched and stages nothing")
    reset()
    src = source / "big.mkv"
    src.write_bytes(b"bytes")
    bridge._copy_verified = lambda s, d: False
    check("the move reports failure", bridge.scan_once(), 0)
    check("the iCloud source is still there", src.exists(), True)
    check("nothing landed locally", (dest / "big.mkv").exists(), False)
    check("nothing is left staged", list(stage.glob(".staging-*")), [])
    bridge._copy_verified = real_copy_verified

    # ----------------------------------------------------------------------
    print("\nPart 5 -- dry-run reports and touches nothing")
    reset()
    src = source / "dry.mkv"
    src.write_bytes(b"dry")
    bridge.materialize = lambda p, wait_sec=600: (_ for _ in ()).throw(
        AssertionError("dry-run must not materialize"))
    check("dry-run reports the drop", bridge.scan_once(dry_run=True), 1)
    check("the iCloud source stays", src.exists(), True)
    check("nothing landed locally", (dest / "dry.mkv").exists(), False)
    bridge.materialize = lambda p, wait_sec=600: True

finally:
    (bridge.SOURCE_DIR, bridge.DEST_DIR, bridge.STAGE_DIR, bridge.LOG_FILE,
     bridge.materialize, bridge._copy_verified) = saved
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for x in failures:
        print(f"  - {x}")
    sys.exit(1)
print("direct ingest bridge moves safely: all checks passed.")
