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
Before that, newest first — the reason when you chose it, standby when the station did:
1. Everything Everything — Regret: that clipped guitar wanted some air, so here is a hook
   that opens a window instead.
2. De Lux — Validation (standby — the station played it while you were offline)
3. Pearl Jam — Release (standby — the station played it while you were offline)
Already queued, in order: …
Your pick starts right after <track>, so that is the one the listener hears just before it.
The last 5 songs went out as Alternative Rock and Indie Rock — Bloc Party, Everything Everything, De Lux, Pearl Jam.
Three albums picked uniformly at random from the tracks this station can play. Each comes
from a different act, and acts in the recent history above are excluded. Every track line
is: track id, length, popularity (0 = that act's least played track, 1 = its most played),
title — five tracks per album, with a count of the rest. Pick any id listed here, call flip
for another three albums, or use search, filter or similar if these three do not cover what
you want.
1. Doolittle — Pixies (1989) · Alternative Rock / Indie Rock
   2FNFckudFqIFAYQnksJBbU   2:53  pop 0.95  Debaser
   (+13 more)
```

**One timeline, and the reason only where the reason is the DJ's.** When it stops, the
mixer keeps playing from the standby pool, so a station can be three songs past the last
thing the DJ chose. Listing those under "your last picks" let it build a set on a choice it
never made, and the brief then disagreed with what was actually playing. Each entry now
carries its own provenance: the DJ's reason where it chose the track, and a standby note
where the station did.The timeline is the only thing in the brief that is the DJ's own: every pick is a new
conversation, so without it the persona's *"vary your phrasing, never reuse the same
construction twice in a night"* would be asking it to avoid words it cannot see. Four
previous songs is the cap — enough to continue a thread or to avoid an opening it just used. The
system prompt adds one line taking the model further back than the brief does:

> "The brief tells you what is on air, what came before it and what is already queued.
> For anything further back — the last half hour, what you played before the current
> record — call history."

## The row every tool returns

One shape, everywhere — `history`, `search`, `filter`, `catalogue` and `similar` all
answer with the same fields, so the DJ never has to wonder whether a tool happened to
carry the thing it needs:

```json
{"id": "53dkqGZQDnFJZuPX0HqkXE", "title": "L’Équilibriste", "artist": "L’Impératrice",
 "album": "Tako Tsubo", "year": 2021, "genre": "Disco / Indie Rock", "duration": "4:05",
 "popularity": 0.48, "played": false, "queued": false}
```

- `genre` is **all** the track's genres, joined with ` / ` — not the primary tag, which is
  what made `filter {genre: "Disco"}` answer with rows reading `Électronique`.
- `duration` is clock time, so a 40-second interlude and a 9-minute epic are distinguishable.
- `popularity` is the track's standing **among its own act's tracks**, 0 = their deepest
  cut, 1 = the one everybody knows. Absolute play counts would only measure how big the act
  is; this asks which of their songs people actually know. It is the number the crates
  turn into *a hit / mid / a deep cut*.
- `played` means inside the 40-play avoid window, `queued` means waiting in the mixer.
  `catalogue` adds `track_no`; `history` adds `songs_ago` and `on_air`.

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

### `flip` — another three records

`{}`

Same pile, turned over: three albums drawn the same way as the brief's crate, never one
already dealt in this pick, still skipping the acts just played. Answers `{albums: [...]}`
in the crate's shape, or a note when the racks are exhausted.

It exists because the DJ reliably picks from ids it has already been handed rather than
going looking, so "nothing here fits" needed an answer that produces ids. One pick can
hold the crate plus three flips, twelve different records.

### `search` — a name you have in mind

`{query, limit?: 20}`

Every word of the query has to land somewhere — title, artist, album, the record's
artist, or the folded name key — so `Ride Vapour Trail` finds the track and `blink 182`
finds `blink‐182`. It over-fetches `limit×40` rows so the station filter has something
left to keep, and falls back to the whole string as one substring when the words are
scattered rather than all present.

A miss is an answer, not an empty list. `"Parcels"` plays nowhere here, but the model
asked for a sound, not a spelling — so the miss runs the same similar-artist machinery
`similar` uses (cache, ListenBrainz, Last.fm), keeps only the acts this station plays,
and hands back a track id for each:

```json
{"results": [],
 "note": "nothing by 'Parcels' plays on this station. These do, and they are what it sounds like …",
 "similar_here": [{"artist": "L’Impératrice", "tracks": 34, "id": "6tphReoW…", "title": "Là-haut", "album": "…", "year": 2024}]}
```

On this library that turns the ledger's worst misses into routes: *Metronomy* → Future
Islands, Everything Everything, Ratatat; *Hot Chip* → Everything Everything, Bloc Party,
Ratatat; *Polo & Pan* → L’Impératrice, Therapie TAXI, Bleu Toucan. A cached list is only
twenty names and the act that plays here is often the twenty-first, so a thin result
re-asks Last.fm for a hundred. When nothing here sounds like it, the note says so and
points at `filter` — no invented matches.

Whether the name exists on disk but not on the station, or nowhere at all, is not a
distinction the caller can use: either way it cannot play, and saying which invites it
to rephrase and try again. The one case where it matters is the DJ's own line — "not in
the house tonight" — and that lives in the persona, not in a tool result.

### `filter` — a kind of thing, not a name

`{genre?, year_from?, year_to?, limit?: 30}`

**No arguments is the widest view of the station**: it samples everything the station
can play, which is the right first move when nothing specific is in mind. Year bounds in
SQL, `ORDER BY RANDOM()`, genre matched loosely on purpose: `Punk` also brings
`Punk Rock`.

It answers with shape, not just rows — `{matching, offset, returned, results}` — so the
model can tell a 600-track slice from a 6-track one, and page through it with `offset`.
No page carries more than three tracks by one artist, which is what stops a genre with
one big name in it from filling the whole answer.

Note the asymmetry: station membership is exact genre-set matching, while this tool
refines. Deliberate — a listener asking for "punk" shouldn't get nothing because the
genre says `Punk Rock`.

### `catalogue` — one artist, or one album

`{artist?, album?, limit?: 25}`

An artist's holdings grouped into albums, or one album's tracklist in running order.
The artist is the record's — `catalogue {'artist': 'L’Impératrice'}` returns all 34
tracks, guests included, and the lookup folds apostrophes and accents.
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
- **Schema cost** is roughly 560 tokens per call for the six definitions. Measure before
  adding another.
