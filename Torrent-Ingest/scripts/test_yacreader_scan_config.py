#!/usr/bin/env python3
"""The YacReader scan-at-startup flags are an enforced invariant, not a memory.

The 2026-09-14 ElfQuest report: every file filed, on the mount, in the pool, and
INVISIBLE in the reader, because both `UPDATE_LIBRARIES_*` flags in YacReader's own ini
read `false`. The library supervisor now patches them before every start and bounces a
running app whose flags drift; `record_plan` drops a refresh marker when comics are
filed. This asserts the ini surgery is exact and safe:

  * the two flags are set, everything else in the file survives byte-for-byte;
  * a correct file is left untouched (no rewrite churn under the lock);
  * a missing key is appended inside `[libraryConfig]`, not into another section;
  * a missing file is created usable;
  * the app's own exit-rewrite cannot be raced because callers hold the index lock
    (asserted as a source contract, since a unit test cannot freeze the app).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                        # noqa: E402
import yacreader_db                                                  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    if not cond:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


TMP = Path(tempfile.mkdtemp(prefix="yac-scan-config-"))

ORIGINAL = """[General]
LIBRARIES=@ByteArray(Comics)

[libraryConfig]
UPDATE_LIBRARIES_AT_CERTAIN_TIME_TIME=00:00
UPDATE_LIBRARIES_AT_STARTUP=false
UPDATE_LIBRARIES_PERIODICALLY=false
USE_OPEN_GL=2
"""

print("=== YacReader scan settings ===")

ini = TMP / "YACReaderLibrary.ini"
ini.write_text(ORIGINAL, encoding="utf-8")

check("drift is detected before any fix", yacreader_db.scan_settings_ok(ini) is False)
check("both flags read as false",
      yacreader_db.read_scan_settings(ini)
      == {"UPDATE_LIBRARIES_AT_STARTUP": "false",
          "UPDATE_LIBRARIES_PERIODICALLY": "false"})

changed = yacreader_db.ensure_scan_settings(ini)
text = ini.read_text(encoding="utf-8")
check("ensure reports it changed the file", changed is True)
check("startup flag is now true", "UPDATE_LIBRARIES_AT_STARTUP=true" in text)
check("periodic flag is now true", "UPDATE_LIBRARIES_PERIODICALLY=true" in text)
check("now reads ok", yacreader_db.scan_settings_ok(ini) is True)
check("the registered library path survived",
      "LIBRARIES=@ByteArray(Comics)" in text)
check("unrelated libraryConfig keys survived", "USE_OPEN_GL=2" in text)
check("unrelated sections survived", "[General]" in text)
backups = list(TMP.glob("YACReaderLibrary.ini.bak-scanfix-*"))
check("one timestamped backup was taken", len(backups) == 1)
check("the backup holds the pre-fix flags",
      "UPDATE_LIBRARIES_AT_STARTUP=false" in backups[0].read_text(encoding="utf-8"))

before = ini.read_text(encoding="utf-8")
check("a second ensure is a no-op", yacreader_db.ensure_scan_settings(ini) is False)
check("a no-op does not rewrite the file", ini.read_text(encoding="utf-8") == before)

print()
print("=== partial and hostile shapes ===")

# A key living in the WRONG section must not count; the patch belongs in [libraryConfig].
ini2 = TMP / "wrong-section.ini"
ini2.write_text("[libraryConfig]\nUSE_OPEN_GL=2\n\n[other]\n"
                "UPDATE_LIBRARIES_AT_STARTUP=true\nUPDATE_LIBRARIES_PERIODICALLY=true\n",
                encoding="utf-8")
check("flags in another section do not read as ok",
      yacreader_db.scan_settings_ok(ini2) is False)
yacreader_db.ensure_scan_settings(ini2)
text2 = ini2.read_text(encoding="utf-8")
check("the patch went into [libraryConfig]",
      text2.index("[libraryConfig]")
      < text2.index("UPDATE_LIBRARIES_AT_STARTUP=true") < text2.index("[other]"))
check("the decoy section is untouched", "[other]\nUPDATE_LIBRARIES_AT_STARTUP=true" in text2)

# Size limits: a brand-new file must come out usable.
ini3 = TMP / "fresh.ini"
check("a missing note reads as drift", yacreader_db.scan_settings_ok(ini3) is False)
check("ensure creates a usable file", yacreader_db.ensure_scan_settings(ini3) is True)
check("the created file passes", yacreader_db.scan_settings_ok(ini3) is True)
check("no backup for a file that never existed",
      not list(TMP.glob("fresh.ini.bak-scanfix-*")))

# The lock contract: patching while the app is up races its exit rewrite, so the two
# production callers must patch in a stopped window.
sup_src = (Path(__file__).resolve().parent.parent / "library_supervisor.py").read_text()
check("the supervisor enforces the flags", "ensure_scan_settings()" in sup_src)
check("the supervisor detects drift while up", "scan_settings_ok()" in sup_src)
check("the supervisor consumes the refresh marker",
      "YACREADER_REFRESH_MARKER" in sup_src)
rescan_src = (Path(__file__).resolve().parent / "yacreader_rescan.py").read_text()
check("the rescan tool patches under the index lock", "db_lock(" in rescan_src)
check("the rescan tool writes the refresh marker",
      "YACREADER_REFRESH_MARKER" in rescan_src)
check("config owns the expected flag values",
      config.YACREADER_SCAN_SETTINGS.get("UPDATE_LIBRARIES_AT_STARTUP") == "true")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("YacReader scan config: all checks passed")
