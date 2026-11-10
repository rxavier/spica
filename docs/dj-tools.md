# The DJ's toolbox

A draft, for review before any code. What the picker gives the model today, what it
cannot do, and the smallest set of additions that fixes it.

## 1. What happens now

One pick is one `POST {base}/chat/completions` with `tool_choice: auto`,
`temperature: 0.7`, `max_tokens: 700`, a 90 s timeout, and three function schemas.

| tool | parameters | runs |
|---|---|---|
| `search` | `query`, `limit` (20) | `LIKE %query%` over title, artist, album; over-fetches `limit×40` so the station filter has something to keep |
| `filter` | `genre`, `year_from`, `year_to`, `limit` (30) | year bounds in SQL, `ORDER BY RANDOM()`, genre matched by the loose fold-and-refinement (`Punk` finds `Punk Rock`) |
| `similar` | `artist`, `limit` (12) | cached similar artists → ListenBrainz by MBID → Last.fm by name → shared local genre |

Every result passes `station_matcher` first, so the toolbox is "what this station may
play", not "your library". An empty result is not `[]` but
`{"results": [], "note": "nothing on station '<name>' matched this; the station's
palette is applied to every tool result…"}` — the note exists because a bare empty
array made the model retry the same dead end for three rounds.

Results are five fields per track — `id, title, artist, album, year, genre` — with no
URI and no duration.

**What the model is told.** A system message: the persona, the station brief, its
genres and year range, the pool size, "pick exactly one track using the tools, never
invent a track id", the requests rule, and the JSON contract. Then a user message that
is prose, not structured data:

```
On air right now: Aphex Twin — Xtal.
Before that, newest first: SBB — Fikołek; Nils Frahm — Says; …
Already queued, in order: Spirit of the Beehive — Tape Hiss.
Your pick starts right after Spirit of the Beehive — Tape Hiss, so that is the one
the listener hears just before it.
```

Plus any live requests with their age (`asked 2 songs ago, about 1 still to go`).

**The loop.** Up to three tool rounds, then a mandatory turn with `tool_choice: none`
("No more lookups. Reply now…"). Each tool result is appended as `role: tool` with its
`tool_call_id`, JSON-encoded, truncated at 6,000 characters. The final answer must be
`{"track_id": "…", "why": "…"}`; `_parse_final` strips code fences, slices between the
first `{` and last `}`, and then requires that the id exists, has a playable `uri`, and
is not among the last 40 played ids. `why` is clipped to 200 characters. An unusable
reply gets exactly one retry, then the pick is abandoned and the station walks its pool.

## 2. How can the model pick without ids?

It cannot, and that is worth stating plainly because it decides the `tool_choice`
question.

Track ids enter the conversation **only** through tool results. The brief carries
titles and artists, never ids. `_parse_final` then rejects any id that is not a real,
playable track on the station. So a reply produced without a tool call can only be
valid by guessing an id from the library — practically impossible at 21-character
Navidrome ids, and when it does happen the reply is thrown away and re-asked, which
costs a round trip for nothing.

So `tool_choice: auto` is already, in effect, "call a tool first". Making it explicit
on round 0 (`tool_choice: required`) would turn a silent waste into a guarantee, at the
price of one lookup per pick even when the model already knows what it wants — which it
cannot, since it has no ids. The honest options are in §5.

## 3. Keep the brief, add one sentence

The prose timeline stays exactly as it is: what is on air, what came before it, what is
already queued, and where the pick lands. It is the part that makes the set cohere, and
it is cheaper to read than a table.

The addition is one line in the system prompt:

> "The brief tells you what is on air and what is queued. For anything further back —
> the last half hour, what you played before the current record — call `history`."

Then the tools below do the rest.

## 4. Proposed tools

Six in total: the three that exist, three new.

### `history` — what has been playing

```json
{"limit": {"type": "integer", "default": 20}}
```

> *"The station's own recent past, newest first: what played, how many songs ago, and
> whether it is on air or already queued. Call it before picking when the current track
> suggests an artist you may have just played, to read the arc of the last half hour, or
> when a request says 'like what you just put on'. Do not use it to find a track — every
> id here has already played."*

Returns `[{id, title, artist, album, year, songs_ago, on_air}]`. This is what turns the
40-id blocklist the model cannot see into something it can reason about.

### `catalogue` — one artist, or one album

```json
{"artist": {"type": "string"}, "album": {"type": "string"}, "limit": {"type": "integer", "default": 25}}
```

> *"What the station owns by one artist, or one album's tracklist in running order
> (`artist` + `album`, or `album` alone when the name is unique). Call it to build a set
> from a single record, to see how much of a band there is to work with, or when a
> request names a band rather than a song. Tracks the station cannot play are omitted, so
> an empty list means it holds none of it."*

Returns `{artist, albums: [{album, year, tracks}], tracks: […], played_recently: bool}`,
or for an album `{album, year, tracks: [{…, track_no, played, queued}]}`.

### `station_map` — what the station is made of

```json
{}
```

> *"The station's palette as numbers: how many tracks it can play, which genres dominate
> and how much of each, the year spread, and the artists with the most to choose from.
> Call it when a request may not be possible here, or to aim a set — 'mostly the
> seventies' is only true if the years say so. It returns no track ids."*

Returns `{tracks, genres: [{label, tracks}], years: {from, to, histogram}, artists: [{name, tracks}]}`.

### The existing three, with descriptions that say when

**`search`** — *"Free-text lookup across titles, artists and albums. Best when you have a
name in mind: a song, a band, half an album title. Everything it returns is playable
here. Use `catalogue` when you want order or completeness, `filter` when you want a kind
of thing rather than a name."*

**`filter`** — *"A random sample of what this station can play, narrowed by genre label
and year bounds. Genre matching is deliberately loose: `Punk` also brings `Punk Rock`.
Results are not ranked — call it twice for a different draw. Use it to explore, not to
find one specific track."*

**`similar`** — *"Artists and tracks that sit near a given artist: cached similarity,
then ListenBrainz, then Last.fm, then a shared local genre. Use it when someone asks for
'something in the vein of X', or to step outside the station's genres. It can return
artists you own nothing by — `owned: false` means the answer is names, not playable
tracks."*

## 5. Open decisions

- **Genre semantics.** `filter` refines (`Punk` → `Punk Rock`) while station membership
  is exact label matching. Unify onto exact (predictable, but "punk" then finds nothing
  when the label is `Punk Rock`), or keep loose and say so in the description? The draft
  keeps it loose and states it.
- **Force one lookup?** `tool_choice: required` on round 0 guarantees the pick is
  grounded, and since ids only come from tool results it costs nothing in correctness —
  only a round trip that `auto` was already taking. Recommendation: yes, required on
  round 0, `auto` afterwards, `none` on the last round.
- **Rounds.** Three becomes four with six tools, since `history` + `catalogue` + `filter`
  is a plausible sequence. Truncation stays at 6,000 characters per result.
- **Schema cost.** Roughly 600 extra tokens of tool definitions per pick. Worth
  measuring on one station before and after rather than estimating.
