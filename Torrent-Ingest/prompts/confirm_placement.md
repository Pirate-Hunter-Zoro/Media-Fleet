# Torrent-Ingest — confirm a computed placement

The hard part of this job is already done. The harness has matched this release's
arcs to the provider's seasons by arithmetic, and that mapping is in the runtime
context below. **Your job is to check it and turn it into a plan, not to re-derive
it.**

This prompt is used ONLY when the harness reached a single unambiguous arc→season
mapping. That is why it is short: everything it left out is a rule about a case
that cannot arise here (comics, novels, absolute-vs-seasonal numbering, matching a
show whose season layout is still unknown). If you find yourself needing one of
those rules, the mapping is wrong — say so and refuse, as described at the end.

## What you must do

1. **Read the computed mapping.** Check it against the actual file listing: the
   file counts must match, and no file may appear twice.
2. **Decide the arcs it left over.** The harness deliberately does not guess at
   these. Each one is exactly one of:
   - a **film** → `Movies/`, with its own TMDB film id;
   - a **special** → `Season 00` of the show;
   - a **separate series** the provider carries under its own name → its own
     `Shows/` folder.
   Use a web lookup if you are unsure which. Do NOT put one in the nearest
   numbered season: that is the specific mistake this whole path exists to stop.
3. **Write the plan JSON** to the path named at the end of the runtime context.
4. **Reply with a short rationale** naming the mapping you confirmed and what you
   did with each left-over arc.

## Naming

- Episode: `Shows/<Show> (<year>)/Season 0N/<Show> (<year>) - S0NE0M.<ext>`
- Special: `Shows/<Show> (<year>)/Season 00/<Show> (<year>) - S00E0M-<Title>.<ext>`
- Movie:   `Movies/<Movie> (<year>).<ext>`
- Zero-pad season and episode to two digits. Subtitles keep their video's base name.
- If the show already exists in the library digest below, **use that folder's exact
  name, year and all** — do not invent a second folder for a show you already hold.

## The plan

```json
{
  "media_type": "show",
  "title": "Monogatari Series",
  "year": 2009,
  "owned": false,
  "anime": true,
  "tmdb_id": 46195,
  "tvdb_id": null,
  "existing_match": true,
  "reasoning": "one paragraph",
  "files": [
    {"src": "/absolute/path/in/download/file.mkv",
     "dst_rel": "Shows/Monogatari Series (2009)/Season 01/Monogatari Series (2009) - S01E01.mkv",
     "season": 1, "episode": 1,
     "episode_title": "...", "plot": "..."}
  ]
}
```

Rules that are enforced by the harness and will get your plan rejected:

- `src` must be an absolute path to a real file inside the download. **Copy each
  filename byte-for-byte from the listing** — never retype or tidy it. One wrong
  character fails the whole torrent.
- `dst_rel` is relative to the media root and starts with `Shows/` or `Movies/`.
- **Every Season-00 special MUST carry a real `episode_title` AND a real `plot`.**
  The provider's Season-00 ordering for a multi-arc show is unreliable, so the
  harness locks these sidecars against it — a blank one would freeze a blank title
  into the library permanently. Look the arc's episodes up if you need to.
- **Every movie MUST pin a TMDB film id** — top-level `tmdb_id` when the plan
  places exactly one movie, a per-file `tmdb_id` on each film otherwise. Two
  movies may not share an id. Three parts of one film trilogy are three films with
  three different ids, not one.
- Set `anime: true` for Japanese animation.
- **Leave junk out entirely**: creditless openings/endings (NCOP, NCED, "Textless",
  "Clean Opening"), previews/PV, samples, screenshots, release `.nfo`/`.txt`.
  Anything you do not list is deleted with the download.
- **One episode, one copy.** Two files mapping to one destination is a hard error.
- **An already-present file is a SUCCESS — list it anyway**, with its correct
  destination. The harness skips it safely. **Never return an empty `files` list**
  because everything looks already-present; empty is correct only for a download
  holding no library media at all.
- Only the files this wave actually has on disk go in the plan. The mapping
  describes the WHOLE release so your numbering is consistent across waves; the
  listing is what you may place.

## If the mapping is wrong

Say so, plainly, in your rationale, and state which arc you believe belongs to
which season instead — then file against your own answer. The harness re-checks
every plan against the release's arcs and will reject a season that draws on two
of them, so a disagreement you act on silently just fails; a disagreement you
state is evidence the next run gets to see.

Do not move, rename or delete any file yourself. Inspect, and write the plan.
