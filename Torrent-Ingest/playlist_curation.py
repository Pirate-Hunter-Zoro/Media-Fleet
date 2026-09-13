"""playlist_curation.py -- the shared "watchable" cut philosophy + per-show tuning.

Both curation entry points import from here so the philosophy lives in ONE place:

  * scripts/playlist_autobuild.py -- builds a whole show's manifest from scratch.
  * playlist_watch.py             -- judges each newly-ingested episode inline.

The philosophy the user settled on (see README, "Curated watchable playlists"):
cut DEAD TIME, not un-fun canon. Every episode already carries its real synopsis
in the sidecar `.nfo` `<plot>` on disk -- that description is the ground truth for
whether anything actually HAPPENS in the episode, and it is what both the
whole-show curator and the per-episode judge must read and decide from. The old
rule ("keep only purely-enjoyable episodes; keep only the first+last of every
multi-episode fight") was a structural, content-blind hatchet that gutted good
plot. This replaces it with a description-driven test plus a per-show keep-rate
band as a self-check (NOT a hard quota -- nothing enforces it; it only tells the
run when it has cut far too much or far too little and should reconsider).
"""
from __future__ import annotations

# The cut philosophy every curation run follows. Description-driven, not
# position-driven. Injected verbatim into both prompts.
CUT_PHILOSOPHY = """\
CUT PHILOSOPHY -- cut DEAD TIME, keep everything where something HAPPENS.

Judge every episode from its OWN description -- read the sidecar `.nfo` `<plot>`
next to the video file (same name, `.nfo` extension). That synopsis is the ground
truth for whether the episode actually moves; do not judge by an episode's
position in an arc or by a raw guide number.

KEEP an episode when its plot shows something happen:
  - it advances the story, or delivers a real beat: a fight's actual turn,
    technique reveal, or finish; a reveal, a death, an emotional payoff;
  - it opens or closes an arc;
  - it introduces a character, the crew, or the premise (NEVER cut the cast /
    premise introductions -- establish everyone first);
  - it is a genuinely memorable, funny, or beloved standalone.

CUT an episode when its plot is DEAD TIME:
  - an unresolved power-up / charge-up / "the planet explodes in five minutes"
    countdown that drags across episodes without resolving;
  - a stand-off, staring contest, or stall where nobody makes real progress;
  - crowd / bystander reaction and anticipation padding (everyone watching and
    reacting while nothing changes);
  - recap or flashback rehashing events already shown;
  - a slow-closing hazard milked only for tension (the Dressrosa birdcage crawl);
  - throwaway filler that loses nothing essential.

Cutting CANON is fine when the canon itself is padding -- the viewer fills any
gap from the manga or the re-cut (One Pace / DBZ Kai). Missing a stall costs
nothing; missing a beat or an introduction does.

Multi-episode fights and events: keep the episodes whose descriptions show a turn
or a finish, and cut the ones that are pure stall or reaction. That may be 1 of 5
episodes or 4 of 5 -- decide from what each description says HAPPENS, never a flat
"first and last only" rule.

Film vs equivalent TV arc: include only the better single version, never both.

After curating, sanity-check against this show's KEEP BAND below: if your kept
fraction lands far outside it, you have almost certainly cut too hard (or too
little) -- re-examine before writing.

Emit items as {"path": "<library-relative path>"} tokens in WATCH ORDER. `ls` the
real Season folders and copy exact paths so numbering can't drift; map episodes by
TITLE / the `.nfo`, not a guide's raw number (watch for pilot/recap offsets).
"""


# Per-show tuning, keyed by the library-relative show folder. For a combined
# franchise task the FLAGSHIP entry (the show whose examples best teach the cut)
# carries the band + few-shot pair for the whole task. Bands are keep-rate targets
# (fraction of episodes that survive), set by how filler-/padding-heavy the show
# is. `keep` and `remove` are REAL on-disk episodes with their real descriptions --
# concrete examples steer a judge far harder than abstract rules.
SHOW_CURATION: dict[str, dict[str, str]] = {
    "Shows/One Piece (1999)": {
        "band": "35-45%",
        "keep": ('Shows/One Piece (1999)/Season 01/One Piece (1999) - S01E0110.mkv '
                 '("Merciless Mortal Combat! Luffy vs. Crocodile!" -- Luffy\'s first '
                 'duel with Crocodile; his rubber attacks are useless against the '
                 'Sand-Sand logia). KEEP: a real fight with a genuine turn -- the plot '
                 'moves.'),
        "remove": ('Shows/One Piece (1999)/Season 01/One Piece (1999) - S01E0054.mkv '
                   '("Precursor to a New Adventure! Apis, a Mysterious Girl!" -- begins '
                   'the Warship Island FILLER arc). REMOVE: non-canon filler island; '
                   'nothing essential is lost. Cut padded CANON the same way -- the '
                   'Dressrosa birdcage crawl, Whole Cake stalls, Wano raid padding.'),
    },
    "Shows/Dragon Ball Z (1989)": {
        "band": "40-50%",
        "keep": ('Shows/Dragon Ball Z (1989)/Season 01/Dragon Ball Z (1989) - S01E105.mkv '
                 '("Mighty Blast of Rage" -- an enraged Goku obliterates Frieza with a '
                 'Kamehameha as Namek\'s destruction looms). KEEP: the fight\'s actual '
                 'payoff -- the beat everything was building to.'),
        "remove": ('Shows/Dragon Ball Z (1989)/Season 01/Dragon Ball Z (1989) - S01E098.mkv '
                   '("A Final Attack" -- "Even as the planet crumbles beneath them, Frieza '
                   'keeps fighting the vastly superior Super Saiyan Goku. His attacks prove '
                   'useless, and his desperation mounts."). REMOVE: the classic Namek '
                   '"five minutes" stall -- the countdown is running but nothing resolves '
                   'for several episodes.'),
    },
    "Shows/Naruto (2002)": {
        "band": "45-55%",
        "keep": ('Shows/Naruto (2002)/Season 01/Naruto (2002) - S01E16.mkv '
                 '("The Broken Seal" -- an enraged Naruto taps the Nine-Tailed Fox\'s '
                 'chakra, shatters Haku\'s ice mirrors, and corners him). KEEP: a major '
                 'beat and a power awakening that closes the fight.'),
        "remove": ('Shows/Naruto (2002)/Season 05/Naruto (2002) - S05E04.mkv '
                   '("Open for Business! The Leaf Moving Service" -- Naruto, Choji and '
                   'Hinata escort a group of peddlers relocating). REMOVE: a self-contained '
                   'filler escort with no bearing on the story (~41% of Naruto is filler).'),
    },
    "Shows/Bleach (2004)": {
        "band": "45-55%",
        "keep": ('Shows/Bleach (2004)/Season 01/Bleach (2004) - S01E118.mkv '
                 '("Ikkaku\'s Bankai! The Power That Breaks Everything" -- Ikkaku reveals '
                 'his bankai against the Arrancar Edrad). KEEP: a fight with a real reveal.'),
        "remove": ('Shows/Bleach (2004)/Season 01/Bleach (2004) - S01E228.mkv '
                   '("Summer! Sea! Swimsuit Festival!!" -- "The characters take a break on '
                   'the beach."). REMOVE: pure filler downtime -- zero plot. Cut the whole '
                   'Bount filler arc and the Karakuraizer episodes the same way.'),
    },
    "Shows/Sailor Moon (1992)": {
        "band": "45-55%",
        "keep": ('Shows/Sailor Moon (1992)/Season 01/Sailor Moon (1992) - S01E25.mkv '
                 '("Jupiter Comes Thundering In" -- the tomboyish Makoto Kino transfers in '
                 'and awakens as Sailor Jupiter). KEEP: a Guardian introduction.'),
        "remove": ('Shows/Sailor Moon (1992)/Season 01/Sailor Moon (1992) - S01E17.mkv '
                   '("Shutter Bugged" -- Nephrite targets a gifted young photographer while '
                   'Usagi dreams of modelling). REMOVE: a monster-of-the-week filler with '
                   'no arc progress.'),
    },
    "Shows/Fairy Tail (2009)": {
        "band": "45-55%",
        "keep": ('Shows/Fairy Tail (2009)/Season 01/Fairy Tail (2009) - S01E01.mkv '
                 '("The Fairy Tail" -- Natsu and Happy rescue Lucy and expose the fake '
                 'Salamander). KEEP: the premise and cast introduction.'),
        "remove": ('Shows/Fairy Tail (2009)/Season 00/Fairy Tail (2009) - S00E07.mkv '
                   '("The Exciting Ryuzetsu Land" -- the mages spend a Grand Magic Games '
                   'break at an aquatic resort amid swimsuit antics). REMOVE: filler '
                   'downtime; nothing happens.'),
    },
    "Shows/Steven Universe (2013)": {
        # Fast-paced / lore-forward: keep the serialized Gem lore + the strong
        # standalones + intros; drop the early-season Beach-City slice-of-life.
        "band": "55-65%",
        "keep": ('Shows/Steven Universe (2013)/Season 01/Steven Universe (2013) - S01E02.mkv '
                 '("Laser Light Cannon" -- Steven recovers his late mother Rose Quartz\'s '
                 'cannon to stop a giant red eye threatening Beach City). KEEP: serialized '
                 'lore and a mother-Rose reveal.'),
        "remove": ('Shows/Steven Universe (2013)/Season 01/Steven Universe (2013) - S01E21.mkv '
                   '("Joking Victim" -- Steven works at the Big Donut when Lars leaves Sadie '
                   'with all the work). REMOVE: low-stakes slice-of-life with no lore.'),
    },
    "Shows/Adventure Time (2010)": {
        # Fast-paced / spine-forward: keep the Lich / Simon-Marceline / Finn-origin
        # / GOLB spine + the beloved standalones + intros; drop throwaway one-offs.
        "band": "55-65%",
        "keep": ('Shows/Adventure Time (2010)/Season 02/Adventure Time (2010) - S02E01.mkv '
                 '("It Came from the Nightosphere" -- Marceline\'s evil father goes on a '
                 'soul-sucking rampage after being released). KEEP: core Marceline lore on '
                 'the show\'s spine.'),
        "remove": ('Shows/Adventure Time (2010)/Season 01/Adventure Time (2010) - S01E17.mkv '
                   '("When Wedding Bells Thaw" -- the Ice King gets Finn and Jake to throw '
                   'him a manlorette party). REMOVE: a throwaway one-off off the spine.'),
    },
    "Shows/Gintama (2006)": {
        # Fast-paced ARC-FORWARD cut (user prefers fast pacing). Gintama's serious
        # arcs are the tight, plot-heavy payoff -- keep ALL of them plus the cast
        # intros. Gags are bimodal: keep only the genuinely ELITE/legendary
        # standalones and cut the many slow, leisurely one-offs. Keeping 70-80%
        # would drag; this lands leaner.
        "band": "45-55%",
        "keep": ('Shows/Gintama (2006)/Season 01/Gintama (2006) - S01E215.mkv '
                 '("Odds or Evens" -- Takasugi overthrows the Harusame leadership and Kamui '
                 'rises to Admiral). KEEP: a serious-arc plot advance -- keep EVERY serious '
                 'arc (Benizakura, Yoshiwara in Flames, Mitsuba, Shinsengumi Crisis, Shogun '
                 'Assassination, Farewell Shinsengumi, Rakuyo, Silver Soul) plus cast intros, '
                 'and only the ELITE gag episodes.'),
        "remove": ('Shows/Gintama (2006)/Season 01/Gintama (2006) - S01E113.mkv '
                   '("Cleaning The Toilet Cleanses The Soul" -- a Shinsengumi washroom-'
                   'cleaning initiative escalates into a battle). REMOVE: a slow throwaway '
                   'gag with no character or arc weight -- cut the leisurely one-offs, not '
                   'just the flat ones.'),
    },
    "Shows/Regular Show (2010)": {
        # Self-contained gag comedy with little plot spine. Fast-paced cut: keep the
        # inventive/memorable episodes + the recurring-cast/arc threads; drop the
        # forgettable, repetitive, low-stakes one-offs (there are more of these than
        # the show's reputation suggests).
        "band": "60-70%",
        "keep": ('Shows/Regular Show (2010)/Season 01/Regular Show (2010) - S01E12.mkv '
                 '("Mordecai and the Rigbys" -- they get help from their future selves to '
                 'win the battle of the bands). KEEP: an inventive, memorable fan favorite.'),
        "remove": ('Shows/Regular Show (2010)/Season 02/Regular Show (2010) - S02E07.mkv '
                   '("High Score" -- Mordecai and Rigby grind an arcade game for a high '
                   'score). REMOVE: a forgettable, low-stakes, repetitive one-off.'),
    },
}


def curation_for(show_rels: list[str]) -> dict[str, str] | None:
    """The tuning entry (band + few-shot pair) for a task, chosen from its shows.
    Returns the first listed show that has an entry (the flagship), or None."""
    for rel in show_rels:
        entry = SHOW_CURATION.get(rel.rstrip("/"))
        if entry:
            return entry
    return None


def examples_block(show_rels: list[str]) -> str:
    """A formatted KEEP/REMOVE few-shot block for a task's flagship show, or ''."""
    entry = curation_for(show_rels)
    if not entry:
        return ""
    return (
        "WORKED EXAMPLES for this show (real episodes with their real descriptions):\n"
        f"  KEEP  -> {entry['keep']}\n"
        f"  REMOVE-> {entry['remove']}\n"
    )


def band_for(show_rels: list[str], default: str = "50-60%") -> str:
    """This task's keep-rate band, from its flagship show (or a default)."""
    entry = curation_for(show_rels)
    return entry["band"] if entry else default
