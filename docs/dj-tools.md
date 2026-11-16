# The DJ's toolbox

What the picker lets the model look at, what it hands back, and why a pick can never
skip a lookup. Describes the code in `spica/picker/`.

## The shape of one set

There are two paths. With audio analysis off — or for a listener request — the set is
the tool loop described below: six function schemas, forced lookups, an artist shelf.
With analysis on and nobody asking for anything, the set is **one call with no tools**:
the brief carries a table of records with their ids, grouped by direction (see
[The table](#the-table)). The set contract, the line and the checks are the same; what
changes is that the offer is made of tracks and no lookup is needed to name one.

In the tool path, one set is one `POST {base}/chat/completions` with six function schemas,
`temperature: 0.7`, `max_tokens: 4096`, `reasoning_effort: "low"`, a 90-second
timeout, and

- `tool_choice: "required"` on round 0,
- `"auto"` on rounds 1–3,
- `"none"` on round 4, preceded by *"No more lookups. Reply now with the JSON object…"*.

`required` is retried as `auto` if the server answers 400/422 — plenty of
"OpenAI-compatible" endpoints don't implement it, and a pick is worth more than the
guarantee.

If every round is exhausted without a usable answer, one last no-tools ask goes out
with the set contract again. If that fails too the pick is abandoned and the station
walks its own pool.

The answer is a set, not a track:

```json
{"theme": "Out of the guitar bands and into the synths: awake and twitchy first, then the floor, then a slow, sun-drunk landing.",
 "ids": ["53dkqGZQDnFJZuPX0HqkXE", "2XU1uFbkWKinwLX7woyDpN", "…"]}
```

One line for the whole set, three to ten ids. Asking for a per-track `why` produced six
separate justifications for one decision and spent the budget on them; the theme is what
listeners read under each of those songs, and it is written once.

## The set, and why it is not a pick

A pick answers *what follows this song*. That question has only one cheap answer — the
nearest thing to what is already playing — which is why the station kept circling the
same corner: fourteen L'Impératrice tracks in an hour, every one of them defensible on
its own.

A set answers *where is this stretch of the night going*, and it is held in
`state/plans/<station>.json` as `{theme, ids, made, shelf}`. `pick_next` hands the queue the
next id and asks the model nothing at all until the set runs out: twelve tracks measured
at six model calls, half a call per track, instead of three or four each.

The set is dropped three ways, all of them something changing the mood it was chosen
for:

- **a listener request**, in `pick_next` and in `queue.promote_requests` — the request
  airs next and the rest of the set was chosen for the mood they asked to leave;
- **ninety minutes on the clock** — a set is for the night it was chosen for, and the
  DJ coming back after an outage should not play out a mood from before it;
- **a track that can no longer play** — aired from the pool in the meantime, or gone
  from the library. That one id is skipped and the set continues without it.

## Why a set cannot skip a lookup

In the tool path, track ids exist only inside tool results. The brief carries titles and
artists, never ids, and `parse_set` then requires each id to exist, to have a playable
`uri`, to be outside the avoid window, to appear once, to be by an artist on the shelf,
and to pass the station's own matcher — the same check the tools apply, so a guessed id
cannot leave the palette either.

The table path is the deliberate exception: there the ids *are* the offer, written into
the brief, and the reply needs no lookup. What replaces the grounding is a stricter
membership check — every id must be one of the table's, so a guessed id still cannot
air; the difference is that a legal id no longer costs a round trip.

Two of those are the only things the code has ever decided about the *shape* of a set
rather than the legality of a track: **every artist has to be on the shelf**, and **no
artist twice in one set**. A listener request passes no shelf, and may put two songs by
one artist in a set.

Everything else is still the DJ's judgement and the operator's prompt: how many tracks,
which of the shelf's artists, in what order, and the line. An installation whose prompt
says something else entirely still works — the prompt steers those choices, the shelf
only bounds where the choice may come from.

So a reply written without calling a tool can only pass by guessing 21-character
Navidrome ids. `tool_choice: "required"` on round 0 makes that explicit instead of
leaving a wasted retry as the failure mode.

## The table

`spica/picker/table.py`. Only with analysis on, and only for a set nobody requested.
The unit of the offer is the **record**, not the artist: analysis is what makes two
tracks by one act distinguishable, and the shelf used artists precisely because nothing
here could do that.

One call returns about thirty-two records in four directions, each row carrying its id,
year, style, tempo, key and energy:

- **Continue the flow** — closest to the on-air track's vector, same temperature.
- **Move one axis** — one of tempo or energy moves, the rest holds.
- **Change it up** — a different Discogs family, or the far end of the station.
- **Land it** — calmer and slower than what is on air; a closer.

Constraints, so the table stays a smaller version of the station rather than a random
draw: one record per artist across the whole table, at most three per label, two decades
when the station can pay for it, and the longest-absent artists get a place. Excluded
before drawing: the last five plays and their artists, what is queued and on air, and
every artist offered on the previous table — rotation memory, as the shelf had.

The ids are in front of each row, so the reply costs one model call and no lookups.
`parse_set` then enforces the table: ids outside it are dropped, no artist appears
twice, and the station rules were already applied when the rows were drawn. A listener
request never takes this path — it still goes through the tools, where a name can be
searched.

A table that cannot be drawn — too few analysed tracks, or analysis off — returns None
and the picker falls back to the artist shelf, unchanged.

## The shelf

A set is drawn from a named list of about a dozen artists, and the reply is checked
against it. `spica/picker/shelf.py`, one call per set.

**What is excluded, before any drawing.** The artists of the last five plays, the five
queued tracks, what is on air, every artist on the previous set's shelf, and the three
closest neighbours of the previous, on-air and next tracks — the neighbours coming from
Last.fm's scored list (`artist_similarity.match >= 0.15`), with edges counted in both
directions because the provider's lists are one-way: L'Impératrice naming Nu Genea has to
count for Nu Genea too. That last rule is what lets Bleu Toucan be kept off a set not
because it played but because Therapie TAXI played and the two are near enough.

**What fills the rest.** Ten slots allocated by label weight (D'Hondt, capped at three
per label, so no single label can own the list), at least two decades, and one artist
from each third of the roster by median play count — a smaller version of the station
rather than a smaller version of the model's taste. Two more slots go to whoever has
been off the air longest among the artists clean of the last forty plays; the threshold
alone would not do, because on a sixty-artist station most artists are clean of forty
plays, so the pair is ranked by how long ago they last aired.

**When it is too small to be a shelf.** Below three names the shelf is dropped and the
set is picked freely: two names is a wall, not a choice, and a station that cannot pick
anything is worse than one that repeats itself.

The list is logged with each pick, which is what lets the next set exclude it. Rotation
memory is the previous shelf, not the previous *set*: an artist offered and not used has
still had its turn.

The shelf replaced a line in the brief that named ten artists which had not played
recently. Measured from the pick log, three to five of every six tracks in a set came off
that line — a list of names is a cast, and the model plays the cast. Keep that in mind
when changing any of this: the mechanism is neutral, and the list is the whole
instrument.

## The brief

Prose, not structured data — the model reads a set, it doesn't parse a table:

```
On air right now: Bloc Party — Halo: that riff has been circling all night, so here it is
at full width, drums first.
Before that, newest first — the reason when you chose it, standby when the station did:
1. Everything Everything — Regret: that clipped guitar wanted some air, so here is a hook
   that opens a window instead.
2. De Lux — Validation (standby — the station played it while you were offline)
3. Pearl Jam — Release (standby — the station played it while you were offline)
Already queued, in order: …
Your set starts right after <track>, so that is the one the listener hears just before it.
On the shelf for this set, and nothing else: Toy Matinee, Soundgarden, Gardenia y el
adversario del tiempo, Odisea, Nil, …
Every track comes from one of those artists, none of them twice, and the set still has to
move — two eras, two genres, one thing changing between each track. Which of them, in
what order, is yours.
```

**The shelf replaces telling the model what played.** The artists that aired are not on
the list, so the brief never has to name them — which is the point: a list of what to
avoid is a list of names to work through, and a list of names is what the model turns
into a cast. Ten names drawn from the station's own shape (label weight, cap three per
label, two decades, one artist from each third of the roster), plus two for whoever has
been off the air longest, are a smaller version of the station. What comes back is
checked against them: an artist off the shelf is dropped like a track off the station,
and no artist appears twice in a set. A listener request is exempt — it is a person
asking, not the station rotating.

The exclusions that build the shelf are: the last five plays, the five queued tracks,
what is on air, and the three closest neighbours (Last.fm match ≥ 0.15, edges counted
both ways) of the records either side of the set. So Bleu Toucan can be excluded not
because it played, but because Therapie TAXI played and the two are near enough.

The crate is no longer handed over in the brief. It was three records with ids in front
of it, which is why it picked from them: the tools cost a round trip and the crate was
free. `flip` still produces that pile on demand.

**One timeline, and the reason only where the reason is the DJ's.** The song on air is
part of that timeline — it carries its reason too when the DJ chose it, and a standby note
when the station did. When it stops, the
A set of six would otherwise print the same theme six times, so the timeline says it once
and marks the rest `(same set)`.

The mixer keeps playing from the standby pool, so a station can be three songs past the last
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

One shape, everywhere — `history`, `search`, `filter` and `similar` all answer with the
same fields, so the DJ never has to wonder whether a tool happened to carry the thing it
needs:

```json
{"id": "53dkqGZQDnFJZuPX0HqkXE", "title": "L’Équilibriste", "artist": "L’Impératrice",
 "album": "Tako Tsubo", "year": 2021, "genre": "Disco / Indie Rock", "duration": "4:05",
 "popularity": 0.48, "played": false, "queued": false}
```

- `genre` is **all** the track's genres, joined with ` / ` — not the primary tag, which is
  what made `filter {genre: "Disco"}` answer with rows reading `Électronique`.
- `duration` is clock time, so a 40-second interlude and a 9-minute epic are distinguishable.
- `popularity` is the track's standing **among its own artist's tracks**, 0 = their deepest
  cut, 1 = the one everybody knows. Absolute play counts would only measure how big the artist
  is; this asks which of their songs people actually know. It is the number the crates
  turn into *a hit / mid / a deep cut*.
- `played` means inside the 40-play avoid window, `queued` means waiting in the mixer.
  `search` and `filter` return these rows flat; `history` adds `songs_ago` and `on_air`.

When the optional audio analysis has run (`SPICA_ANALYSIS=1`), four more fields appear on
the tracks it has heard — and nothing at all changes for a track it has not:

```json
{"id": "…", "tempo": 118, "key": "A min", "sound": "Nu-Disco / Funk", "energy": 0.81}
```

- `tempo` is beats per minute, rounded. A hint, not a rule: the estimator is
  occasionally half or double, so the brief tells the DJ to read it as approximate.
- `key` is the extracted key and mode, for harmonic sequencing.
- `sound` is the two most probable Discogs styles — the vocabulary that survives
  contact with a local collection where genre tags and the model's memory both thin out.
- `energy` is a 0–1 axis derived from the trained mood/danceability heads (not from
  loudness), for building an arc across a set.

**`catalogue` is the exception, and deliberately lean.** It answers with albums, each
carrying `album`, `year` and `genre` once, and inside it only what is needed to choose a
track:

```json
{"id": "…", "title": "The Queen Is Dead", "track_no": 1, "duration": "6:24",
 "popularity": 0.37, "played": false}
```

The same twenty tracks used to appear twice — once nested under the album, once as a flat
`tracks` list — and the trimmer cut the flat list to a single row while the album block
beside it still held everything, so a `catalogue` answer sat at 5,700 characters and said
"showing 1 of 20". One view, lean rows: The Smiths went from 10,681 characters to 3,083,
and Nine Inch Nails from 16,194 (8,235 after trimming, over the cap) to 3,795.

**Every page fits before the model sees it.** `filter` returns `{matching, offset,
returned, results, next_offset}` where `returned` is the number of rows actually there —
a 168-track match ships twenty rows and says so, and `next_offset` is where the next page
really starts. The trimmer downstream is a backstop now, not the paging mechanism.

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

Three records turned over, with their ids: drawn from the tracks this station can play,
never one already dealt in this set, and skipping the artists just played. Answers
`{albums: [...]}`, or a note when the racks are exhausted.

This is the only way a pile of records is put in front of the model now — the brief used
to carry one, which made the tools optional and the same three albums the easiest thing
to pick from. Asking for it costs a call, which is the point: it is a move, not a menu.

### `search` — a name you have in mind

`{query, limit?: 20}`

Every word of the query has to land somewhere — title, artist, album, the record's
artist, or the folded name key — so `Ride Vapour Trail` finds the track and `blink 182`
finds `blink‐182`. It over-fetches `limit×40` rows so the station filter has something
left to keep, and falls back to the whole string as one substring when the words are
scattered rather than all present.

A miss is an answer, not an empty list. `"Parcels"` plays nowhere here, but the model
asked for a sound, not a spelling — so the miss runs the same similar-artist machinery
`similar` uses, keeps only the artists this station plays, and answers with **at most
three artist names and nothing else**:

```json
{"results": [],
 "note": "nothing by 'Parcels' plays on this station. These do, and they are what it sounds like — call catalogue on one of them to see its tracks; no ids here, the record has to be opened.",
 "similar_here": [{"artist": "Everything Everything", "tracks": 28}]}
```

No id, no title, no album. That is deliberate: handing back a ready-to-use id turned
every miss into a pick, and because the acts that sound like the model's favourite
French-touch names on this shelf are the three it owns, a night of asking for that scene
fed the same three acts straight into the sets. A name has to be opened with `catalogue`
to become a track, which puts the pick back through the album order and the
recently-played demotion.

Two more rules on the same path. An act that has aired in the last twenty tracks is not
offered at all; when *everything* similar has just been on air the answer is an empty
list plus "something here does sound like it, but it has been on air recently — take the
set somewhere else and change the temperature". And when nothing here sounds like the
name at all, the answer says so and points at `filter` and `flip` ("find another vibe"),
with `did_you_mean` for a name that is here under a different spelling.

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

It answers with shape, not just rows — `{matching, offset, returned, results, next_offset}`
— so the model can tell a 600-track slice from a 6-track one, and page through it with
`offset`. `returned` counts the rows in the message, not the rows asked for: a page is
trimmed to the 5,200-character row budget before it is sent, and `next_offset` says where
the next one really starts, so paging cannot skip a track that was never shown.
No page carries more than three tracks by one artist, which is what stops a genre with
one big name in it from filling the whole answer.

`genre` is matched against the library's own labels, **without the operator's station
rollup**: asking for `Nu Disco` answers the nu-disco records, not every record the
station files under `Electronic` because an alias says so. Artists that played in the
last twenty tracks sort behind everyone else — demoted, never removed, because a tool
that answers "nothing here" about music the station holds sends the model somewhere
worse than a repeated artist does.

Note the asymmetry: station membership is exact genre-set matching on the collapsed
shelf name, while this tool refines on the fine label. Deliberate — a listener asking
for "punk" shouldn't get nothing because the genre says `Punk Rock`.

### `catalogue` — one artist, or one album

`{artist?, album?, limit?: 25}`

An artist's holdings grouped into albums, or one album's tracklist in running order.
The artist is the record's — `catalogue {'artist': 'L’Impératrice'}` returns all 34
tracks, guests included, and the lookup folds apostrophes and accents.

Each album carries its own name, year and genre once, and each track inside it only what
is needed to choose one: `id`, `title`, `track_no`, `duration`, `popularity`, `played`.
No flat track list behind it, and no album or artist repeated on every row — that
duplication was half the payload. `shown` and `total` say when a big artist was cut short
("showing 25 of 60 tracks"), with the hint to name an album for the rest.

Albums come back in this pick's order, with the tracks still in running order inside
each one — they used to come back alphabetically, so browsing an artist opened on the
same record every time and alphabetical order is a favourite in disguise. When `album`
is given, nothing changes: one album, its own running order.

An album name alone is enough when one artist owns it; when several do, the artist with
the most tracks on the station wins and the reply names them. Artist names are matched
case-insensitively and come back spelled the way the library spells them.

### `similar` — near an artist

`{artist, limit?: 12}`

Audio vectors (when analysis is on) → cached similar artists → ListenBrainz by MBID →
Last.fm by name → shared local genre. Returns `{source, artists, tracks, owned}`;
`owned: false` means the names are the answer, because the station holds none of their
records. The audio level is first and answers `source: "audio"`: it is local, costs
nothing, and can only ever name artists this library owns — which the provider lists
cannot promise.

## Known limits

- **No audio features unless analysis is on, and no per-listener history.** Obscurity is
  inferred from what the model knows about the artists, guided by the station brief. The
  only popularity signal anywhere is public ListenBrainz and Last.fm totals, ranked
  inside each artist's own catalogue; nothing is read from the listener's own plays.
  Audio features are opt-in (`SPICA_ANALYSIS=1`), stored in `state/analysis.db`.
- **Two different windows.** `avoid` and `history` share the forty-play window; the shelf
  looks at the last five plays, the five queued tracks and the three neighbours either
  side, because a set is a decision about the next few minutes, not about the night.
- **Tool descriptions are the whole instruction.** Nothing in the loop decides which
  tool to call and there is no planner; when a pick is bad, the first thing to rewrite is
  a description.
- **Genre matching exists twice** (exact for stations, loose for `filter`). Unifying
  them is still an open question, not an oversight.
- **Schema cost** is roughly 560 tokens per call for the six definitions. Measure before
  adding another.
