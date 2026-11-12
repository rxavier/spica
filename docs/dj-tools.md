# The DJ's toolbox

What the picker lets the model look at, what it hands back, and why a pick can never
skip a lookup. Describes the code in `spica/picker/`.

## The shape of one pick

One pick is one `POST {base}/chat/completions` with six function schemas,
`temperature: 0.7`, `max_tokens: 700`, a 90-second timeout, and

- `tool_choice: "required"` on round 0,
- `"auto"` on rounds 1–3,
- `"none"` on round 4, preceded by *"No more lookups. Reply now with the JSON object…"*.

`required` is retried as `auto` if the server answers 400/422 — plenty of
"OpenAI-compatible" endpoints don't implement it, and a pick is worth more than the
guarantee.

If every round is exhausted without a usable answer, one last no-tools ask goes out
with *"Stop searching…"*. If that fails too the pick is abandoned and the station walks
its own pool.

## Why a pick cannot skip a lookup

Track ids exist only inside tool results. The brief carries titles and artists, never
ids, and `parse_final` then requires the id to exist, to have a playable `uri`, to
be outside the last 40 played ids, and to pass the station's own matcher — the same
check the tools apply, so a guessed id cannot leave the palette either.

So a reply written without calling a tool can only pass by guessing a 21-character
Navidrome id. `tool_choice: "required"` on round 0 makes that explicit instead of
leaving a wasted retry as the failure mode.

## The brief

Prose, not structured data — the model reads a set, it doesn't parse a table:

```
On air right now: Bloc Party — Halo.
Before that, newest first: Bloc Party — Trojan Horse; Everything Everything — Regret; …
Already queued, in order: …
Your pick starts right after <track>, so that is the one the listener hears just before it.
Your last picks, newest first — Regret (Everything Everything): <the reason it gave>; …
```

Recent requests follow, each with its age (`asked 2 songs ago`). They are context, not standing orders: the DJ answered each one when it played, and decides pick by pick whether the room is still in that mood.

The last picks are the only thing in the brief that is the DJ's own: every pick is a new
conversation, so without them the persona's *"vary your phrasing, never reuse the same
construction twice in a night"* would be asking it to avoid words it cannot see. Three is
the cap — enough to continue a thread or to avoid an opening it just used. The
system prompt adds one line taking the model further back than the brief does:

> "The brief tells you what is on air, what came before it and what is already queued.
> For anything further back — the last half hour, what you played before the current
> record — call history."

## The six tools

Every tool result passes the station matcher first, so the toolbox is *what this station
may play*, not *your library*. An empty list is never `[]`; it is

```json
{"results": [], "note": "nothing on station 'Alternative' matched this; the station's palette is applied to every tool result, so try a different angle or pick from results you already have"}
```

Results are JSON, trimmed by whole rows to fit 6,000 characters per call — never cut
mid-object, because the ids the model has to answer with are inside them. A tool
that raises returns `{"error": "…"}` rather than taking the pick down with it.

### `history` — what has been playing

`{limit?: 20}`

The station's recent past, newest first, with `songs_ago` (0 = on air) and `on_air`.
This is the difference between "don't repeat yourself" as an invisible 40-id blocklist
and something the model can reason about — an arc, a run on one artist, a record it
already played three songs ago.

```json
[{"id": "6O0e9…", "title": "Halo", "artist": "Bloc Party", "album": "Intimacy",
  "year": 2008, "songs_ago": 0, "on_air": true}]
```

Deduplicated: a restart re-announcing the current track doesn't create a second entry,
so `songs_ago` counts real advances.

### `search` — a name you have in mind

`{query, limit?: 20}`

`LIKE %query%` across title, artist and album, over-fetching `limit×40` rows so the
station filter has something left to keep.

### `filter` — a kind of thing, not a name

`{genre?, year_from?, year_to?, limit?: 30}`

Year bounds in SQL, `ORDER BY RANDOM()`, genre matched loosely on purpose: `Punk` also
brings `Punk Rock`. Results are a draw, not a ranking — call it twice for a different
sample.

Note the asymmetry: station membership is exact label-set matching, while this tool
refines. Deliberate — a listener asking for "punk" shouldn't get nothing because the
label says `Punk Rock`.

### `catalogue` — one artist, or one album

`{artist?, album?, limit?: 25}`

An artist's holdings grouped into albums, or one album's tracklist in running order.
Each track carries `track_no`, `played` (in the last 40) and `queued`, so the model can
sequence a record instead of picking blind. `played_recently` summarises the lot.

An album name alone is enough when one artist owns it; when several do, the artist with
the most tracks on the station wins and the reply names them. Artist names are matched
case-insensitively and come back spelled the way the library spells them.

### `similar` — near an artist

`{artist, limit?: 12}`

Cached similar artists → ListenBrainz by MBID → Last.fm by name → shared local genre.
Returns `{source, artists, tracks, owned}`; `owned: false` means the names are the
answer, because the station holds none of their records.

### `station_map` — what the station is made of

`{}`

Tracks it can play, the eight dominant genre labels with counts, the year spread as
decades plus `undated` and `ignored_bad_years`, and the ten artists with the most to
choose from. No track ids.

Years below 1900 are counted, not ranked: the library has 34 tracks tagged `1006`,
`1800s` and the like, and letting those into the histogram made the station look like it
spanned a millennium.

```json
{"station": "Alternative", "tracks": 1911,
 "genres": [{"label": "Alternative Rock", "tracks": 1121}, …],
 "years": {"from": 1975, "to": 2026, "decades": {"1970s": 10, …, "2020s": 253},
           "undated": 0, "ignored_bad_years": 34},
 "artists": [{"name": "The Smashing Pumpkins", "tracks": 226}, …]}
```

## Known limits

- **No audio features, no popularity, no per-listener history.** Obscurity is inferred
  from what the model knows about the artists, guided by the station brief; there is no
  play-count signal anywhere by design.
- **History ends at 40 plays.** `avoid` and `history` share that window.
- **Tool descriptions are the whole instruction.** Nothing in the loop decides which
  tool to call and there is no planner; when a pick is bad, the first thing to rewrite is
  a description.
- **Genre matching exists twice** (exact for stations, loose for `filter`). Unifying
  them is still an open question, not an oversight.
- **Schema cost** is roughly 600 tokens per call for the six definitions. Measure before
  adding a seventh.
