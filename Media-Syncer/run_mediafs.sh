#!/bin/bash
# Launcher for the mediafs virtual-library mount, invoked by launchd.
#
# mediafs runs in the FOREGROUND and launchd KeepAlive restarts it if it dies.
# The one failure mode that needs care is a stale mount left behind by a crash
# ("Transport endpoint not connected") -- so before mounting we force-clean any
# leftover mount at the mountpoint. That pairing (force-clean + KeepAlive) is the
# watchdog: a dead mount is torn down and remounted automatically.
set -uo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$HOME/.local/bin"

# The FUSE layer is fuse-t, never macFUSE. fusepy resolves its dylib by NAME
# (`ctypes.util.find_library('fuse')`), which finds macFUSE's
# /usr/local/lib/libfuse.dylib if that antique is installed -- and on macOS 27
# macFUSE 5.0.6's mount_macfuse refuses to serve ("the file system is not
# available (2)"), so every KeepAlive respawn crash-looped. FUSE_LIBRARY_PATH is
# fusepy's supported override; point it at fuse-t's libfuse compatibility
# library so the mount rides the layer Open-Code-Doctor keeps current.
FUSE_T_LIB="/usr/local/lib/libfuse-t.dylib"
if [ -e "$FUSE_T_LIB" ]; then
    export FUSE_LIBRARY_PATH="${FUSE_LIBRARY_PATH:-$FUSE_T_LIB}"
else
    # No fallback on purpose: find_library('fuse') is exactly the macFUSE path that
    # broke, and with macFUSE removed it finds nothing. Fail loudly; KeepAlive retries.
    echo "[mediafs] ERROR: ${FUSE_T_LIB} missing -- fuse-t is not installed? Refusing to mount without it."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Serialized: a bare `git pull` here races any other launcher pulling the same tree
# (KeepAlive respawns this one on every crash), corrupting FETCH_HEAD and leaving the
# daemon on stale code. See scripts/git_pull_locked.sh.
GIT="$(command -v git || true)"
source "$SCRIPT_DIR/scripts/git_pull_locked.sh"
if [ -n "$GIT" ] && "$GIT" -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_pull_locked "$SCRIPT_DIR" || \
        echo "[mediafs] git pull failed; using local files."
fi

CONDA_PY="/opt/homebrew/Caskroom/miniconda/base/envs/media_sync_env/bin/python3"
if [ -x "$CONDA_PY" ]; then PYTHON="$CONDA_PY"; else PYTHON="$(command -v python3)"; fi

MP="$("$PYTHON" -c 'from scripts import config; print(config.MEDIAFS_MOUNT)')"
LOWER="$("$PYTHON" -c 'from scripts import config; print(config.MEDIAFS_LOWER)')"

# Boot ordering: mediafs mounts at MEDIAFS_MOUNT (~/MediaLibrary, a local SSD path)
# over the real media in LOWER (the SSD library root, ~/MediaStore). At boot we must
# wait for LOWER to be populated before mounting -- otherwise we'd mount over a
# missing/empty path. Bail-and-retry (launchd KeepAlive re-runs us) if it
# never shows.
for _ in $(seq 1 60); do
    [ -d "$LOWER/Shows" ] && break
    echo "[mediafs] waiting for real media at ${LOWER}..."
    sleep 5
done
if [ ! -d "$LOWER/Shows" ]; then
    echo "[mediafs] ${LOWER} still absent; exiting (KeepAlive will retry)."
    exit 1
fi

mkdir -p "$MP"

# Tear down any stale mount from a previous crash before remounting. Absolute
# paths: launchd's stripped PATH does not include /sbin, where mount/umount live.
if /sbin/mount | grep -q " ${MP} "; then
    echo "[mediafs] cleaning stale mount at ${MP}"
    /sbin/umount -f "$MP" 2>/dev/null || /usr/sbin/diskutil unmount force "$MP" 2>/dev/null || true
fi

exec "$PYTHON" -m scripts.mediafs "$MP" --foreground
