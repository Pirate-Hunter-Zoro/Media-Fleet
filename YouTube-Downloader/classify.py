"""Route a playlist's new items to either a music folder or the library.

The rule, by design, is a name, not a judgment: a playlist listed in
`ytconfig.AUDIO_PLAYLIST_DIRS` (matched by ID) files everything in it as audio, into the
folder that map names -- iCloud `Soundtracks/` for the OST playlist, Google Drive `Music/`
for Tally's "Download" one. Every other playlist goes through the library pipeline, no matter how
short or music-like an item looks. There is deliberately no length gate and no title/AI
classifier anymore -- a short OST rip sitting in a show playlist is an episode/short to
place, not a track, and letting a classifier second-guess that was filing
soundtrack-looking shorts from every playlist into Soundtracks/.

The clean track title still matters for the music playlists: both destinations are folders
of human-named files (`Katakuri Theme.mp3`, `The 3 Towers.mp3`) -- not YouTube titles full
of channel names, "(Official)", "【HQ】", view-bait and emoji.
"""
from __future__ import annotations

import re

import ytconfig

# Filename-hostile characters and the noise that YouTube music uploads are full of.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_BRACKET_NOISE = re.compile(
    r"[\(\[\{【][^\)\]\}】]*"
    r"(official|hq|hd|4k|lyrics?|audio|video|full|extended|1 ?hour|loop|remaster\w*)"
    r"[^\)\]\}】]*[\)\]\}】]",
    re.IGNORECASE,
)


def sanitize(name: str) -> str:
    """Make a string safe as a macOS filename component, following the library's own
    convention rather than a blind character substitution.

    A colon cannot go in a filename, and the library already answers that consistently --
    and it draws a distinction worth copying, because the two cases read very differently:

        `Avatar: The Last Airbender`  ->  `Avatar - The Last Airbender`   (title: subtitle)
        `Re:ZERO`, `Fate/Zero`       ->  `Re-ZERO`, `Fate-Zero`          (part of the word)

    So a colon that separates a subtitle (it has whitespace after it) becomes ` - `, and a
    colon inside a word becomes a bare `-`. Both match what is on the shelf today; a
    single blanket rule would produce `Re - ZERO` or `Avatar-The Last Airbender`."""
    name = re.sub(r"\s*:\s+", " - ", name)      # "Title: Subtitle" -> "Title - Subtitle"
    name = name.replace(":", "-")               # "Re:ZERO"        -> "Re-ZERO"
    name = _ILLEGAL.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or "Untitled"


def clean_track_title(raw: str) -> str:
    """Best-effort tidy of a YouTube music title into a track name."""
    t = _BRACKET_NOISE.sub("", raw)
    t = re.sub(r"\s*[|·]\s*.*$", "", t)          # trailing " | Channel Name"
    t = re.sub(r"\s*-\s*(topic|official.*)$", "", t, flags=re.IGNORECASE)
    return sanitize(t)


def audio_dir_for(playlist_id: str):
    """The folder this playlist's items file into as audio, or None for library media.

    The single routing decision in this repo, and it is a dict lookup on purpose: which
    folder a track lands in is a fact about which playlist it came from, never something
    inferred from the item itself.
    """
    return ytconfig.AUDIO_PLAYLIST_DIRS.get(playlist_id or "")


def split(entries: list, log_fn=print) -> tuple[list, list, list]:
    """Partition `entries` into (videos, tracks, duplicates).

    Only an audio playlist yields tracks: everything in it is a track (a track entry gains
    `track_title` and `audio_dir`, and one already present in that same folder gains
    `duplicate_of` and is never downloaded). Every other playlist yields pure videos,
    untouched.

    Dedupe is against the destination folder alone, so the two music folders never suppress
    each other: an OST already in `Soundtracks/` does not stop the same piece being filed
    into Tally's `Music/`, which is a different folder for a different person.
    """
    if not entries:
        return [], [], []

    playlist_id = str(entries[0].get("playlist_id") or "")
    audio_dir = audio_dir_for(playlist_id)
    if audio_dir is not None:
        pl_title = str(entries[0].get("playlist_title") or "")
        log_fn(f"  {pl_title!r} is an audio playlist; routing all "
               f"{len(entries)} item(s) to {audio_dir.name}/")
        import soundtracks
        existing = {n.lower() for n in soundtracks.existing_titles(audio_dir)}
        tracks, dupes = [], []
        for e in entries:
            e = dict(e)
            e["track_title"] = clean_track_title(e["title"])
            e["audio_dir"] = str(audio_dir)
            if e["track_title"].lower() in existing:
                e["duplicate_of"] = e["track_title"]
                dupes.append(e)
            else:
                tracks.append(e)
        return [], tracks, dupes

    # Every other playlist is library media, whatever it looks like.
    return list(entries), [], []
