import logging
from pathlib import Path
import math

from . import config
from .utils import stream_binary_command, heal_stale_session, is_stale_session_error
from .vpn import rotate_exit_node

def chunked_download(remote: str, relative_path: Path, local_path: Path, file_size: int) -> bool:
    """Download a file chunk by chunk, rotating the exit node ONLY on a failed chunk.

    **Rotation is reactive, never proactive** -- the same rule the streaming path follows
    (README, *Progressive streaming*). Never rotate after a chunk merely because it
    finished, for two reasons:

    * A rotation is a tunnel drop plus re-auth, 10-40 s of dead air, and paying that once
      per chunk buys nothing when MEGA has not throttled us.
    * It resets every TCP connection on the machine, so a background chunk completing would
      tear down every concurrent upload alongside it.

    IP diversity for throttle-avoidance is preserved: a chunk that genuinely fails rotates
    before retrying, which is the case rotation exists for.

    Args:
        remote (str): Remote to download from
        relative_path (Path): Relative path of file to download on the remote
        local_path (Path): Local path to download file to
        file_size (int): Size of the file to download

    Returns:
        bool: success or failure indication
    """
    num_chunks = math.ceil(file_size / config.DOWNLOAD_CHUNK_SIZE)
    for idx in range(num_chunks):
        offset = idx * config.DOWNLOAD_CHUNK_SIZE
        count = config.DOWNLOAD_CHUNK_SIZE if idx < num_chunks-1 else (file_size - config.DOWNLOAD_CHUNK_SIZE * idx)
        success = False
        for _ in range(config.MAX_DOWNLOAD_TRIES):
            error = stream_binary_command(\
                [config.RCLONE_PATH, "cat", f"--offset={offset}", f"--count={count}", f"{remote}:{relative_path}"],\
                local_path,\
                config.TIMEOUT(config.DOWNLOAD_CHUNK_SIZE),\
                append=idx>0\
            )
            if error != "":
                if is_stale_session_error(error):
                    # Rotation cannot fix a dead session -- the token is bad from every exit
                    # node -- and paying 10-40 s of tunnel re-auth per retry to learn that is
                    # the most expensive way to fail. Heal instead and spend the retry on a
                    # fresh login. Falls through to rotation once the heal budget is spent, on
                    # the chance the signature was a genuine network fault after all.
                    if heal_stale_session(remote):
                        logging.info(f"Chunk {idx} of {relative_path} hit a stale session on "
                                     f"{remote}; retrying on a fresh login...")
                        continue
                # Reactive rotation only: this chunk failed. Never rotate on success.
                logging.info(f"Failed to download chunk {idx} of {relative_path}, switching IPs and retrying...")
                rotate_exit_node()
            else:
                logging.info(f"Downloaded chunk {idx} of {relative_path} successfully.")
                success = True
                break
        if not success:
            logging.info(f"FAILED to download chunk {idx} of {relative_path} {config.MAX_DOWNLOAD_TRIES} times... skipping entire file...")
            # Remove the partial (often zero-byte) file. stream_binary_command opens it
            # for writing before the first chunk, so a failed download leaves a stub that
            # the upload phase would later treat as a real local file and push to the
            # cloud. Delete it so a failed fetch leaves no trace.
            try:
                Path(local_path).unlink(missing_ok=True)
            except OSError as e:
                logging.error(f"Failed to remove partial download {local_path}: {e}")
            return False
    # All chunks successfully downloaded
    return True