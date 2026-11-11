<p align="center"><img src="spica/static/logo.png" width="120" alt="Spica"></p>

# Spica

Internet radio for a local music library: several stations on air at once, an LLM DJ
that picks, and a page you can leave open.

## Where it comes from

Spica is inspired by [SUB/WAVE](https://github.com/perminder-klair/subwave), the project it
grew out of, and is built from the ground up around two things SUB/WAVE does not do.

The first is **several stations at once**. SUB/WAVE puts one station on air: switching
means restarting the broadcaster. Here every station has its own Icecast mount and its
own Liquidsoap output, all live simultaneously, so switching is a player-side action and
a station never goes silent because somebody chose another one.

The second is a **deliberately trimmed feature set**, to spend almost nothing on
resources. SUB/WAVE is a complete station product — DJ voice, shows, jingles, ducking,
audio analysis, moods, embeddings — and pays for it in a broadcast container, an
analyser, a TTS service and about a gigabyte of RAM. Spica drops all of that and keeps
the picking: rules, pools, an LLM that reads a brief, and a ledger. What remains runs
continuously in roughly 190 MB and about 4% of one core with two stations on air,
because a station nobody is listening to costs a decode, not an encode.

It is the same idea, at a different point on the cost curve.

## What it does

Every station is a rule set plus an optional brief. The rules decide which tracks may
play; the brief tells the DJ what to reach for inside that set.

```
rules     genres picked from the vocabulary, artists to include or exclude,
          year range, how the rotation leans (deep / balanced / hits)
brief     free text the DJ reads on every pick
pool      rules applied to the library, ordered deterministically, written as an m3u
```

**A station is always on air.** Each station's Liquidsoap clock runs whether or not
anyone is connected: it decodes the next track, crossfades, and throws the audio away.
Tuning in joins a track already in progress, the way radio works. Connecting also
attaches the Icecast output to that same source, which is where the encoder starts —
one MP3 encode per station, fanned out by Icecast to every listener.

**The DJ only speaks to somebody.** Every few seconds the picker checks who is really
listening and, for a station with an audience, asks the LLM to choose the next track;
the choice goes to `state/queue/<station>.txt`, Liquidsoap polls that file, pushes it
into the station's request queue and deletes it. With nobody there, the station keeps
walking its pool and the DJ is not called at all.

**The ledger records what aired.** The mixer writes each track it starts to
`state/now/<station>.txt`; the picker tails that file and appends to the play log. It
is a record of the station, not of your listening — it fills whether or not anyone is
tuned in, and each entry carries the time that track started.

"Listening" is not the same as an open socket. The browser says so — the player posts a
heartbeat every 20 s while it plays. A player on the relay counts while it is taking
bytes. A connection straight to a mount counts if it did not come from the relay. A tab
left in the background, or a stream that died holding its socket open, does not.

## Quick start

```sh
mkdir spica && cd spica
curl -O https://raw.githubusercontent.com/rxavier/spica/main/deploy/compose.yaml
curl -o .env https://raw.githubusercontent.com/rxavier/spica/main/deploy/.env.example
chmod 600 .env
$EDITOR .env        # the three passwords, your Navidrome address, your timezone
docker compose up -d
```

Then open `http://localhost:8030`; the console is at `/admin`, with the password you put
in `SPICA_ADMIN_PASSWORD`. The first run writes `config/icecast.xml`,
`config/stations.json` and `config/spica.liq` and starts one station called
**Everything** that plays the whole library, so there is nothing else to create.

**Where the credentials go**, all of them in that one `.env` file next to
`compose.yaml`, mode 600:

- `ICECAST_SOURCE_PASSWORD` — what the mixer publishes with. Written into `icecast.xml`
  on first run, so set it before the first start.
- `ICECAST_ADMIN_PASSWORD` — Icecast's own `/admin` pages.
- `SPICA_ADMIN_PASSWORD` — the console. Empty disables admin writes.
- `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASS` — a Navidrome account for the
  radio. Nothing is ever written to it, and no scrobbles are sent.
- `LLM_BASE`, `LLM_KEY`, `LLM_MODEL`, `LLM_HEADERS` — defaults for the endpoint. The
  admin's *settings* page can override them, and what it saves to
  `config/llm.json` (mode 600) wins.
- `LASTFM_KEY` — optional: name-based similarity and tag backfill.
- `MB_USER_AGENT` — optional contact string for MusicBrainz.

Running behind a reverse proxy, building the image yourself, and the full file-by-file
deployment story: [`deploy/README.md`](deploy/README.md).

## Features

- **Several stations at once.** Every station has its own mounts and its own outputs. No
  fixed number of stations; `icecast.xml` ships with a limit of forty sources, which is
  stations multiplied by streams.
- **Several streams per station.** MP3, Opus and FLAC are independent switches per
  station, each its own encoder and mount, all fed by the same source — so a phone can
  take Opus, a Sonos MP3 and a laptop FLAC, from the same station at the same moment.
  See [Streams](#streams).
- **Always on air, cheap when unheard.** The clock keeps decoding; the encoder only runs
  while a mount is open. See [Resources](#resources).
- **Deterministic pools.** Sequencing is `blake2b(station, day, track_id)`, blended with
  the station's popularity lean, then two constraints: no two tracks from one album
  adjacent, no artist repeat within 12 placed tracks. A station has the same shape all
  day and reshuffles tomorrow.
- **LLM picks with real tools.** Six of them: `history` (what the station just played,
  with how many songs ago), `search`, `filter` (genre and years), `catalogue` (an
  artist's holdings or one album in running order), `similar`, and `station_map` (the
  genres, decades and artists the station is actually made of). Every result is filtered
  through the station's rules, so a narrow station cannot return something outside its
  palette. Track ids exist only inside tool results, so a pick is always grounded in a
  lookup rather than a remembered name.
- **Similarity without a hard dependency.** Cached, then artist MBID from Navidrome,
  then MusicBrainz, then ListenBrainz, then Last.fm by name, then local genre. No
  provider is required; a library with no ids and no API key still gets an answer.
- **Listener requests that last more than one song.** Free text, kept for N tracks, and
  the DJ is told both how many songs have passed and how many remain. The system prompt
  tells it to honour requests, including mood changes, without ever moving the station
  away from its own identity.
- **Genres from the best source available, not from guesses.** Your file tags, then
  MusicBrainz genres with their vote counts, and Last.fm only for artists MusicBrainz
  does not know. Every label records where it came from — `file`, `musicbrainz`,
  `last.fm`, or `by hand`.
- **One label unless the artist has range.** MusicBrainz votes decide: 23 for progressive
  rock against 7 for art rock is a progressive rock band, while a tie at 2 and 2 is
  genuinely both. Stations pick labels from that vocabulary, so matching is set
  membership and not string guessing.
- **Popularity from real play counts.** ListenBrainz listen counts per track (batched),
  ranked inside the artist's own catalogue, so "hits" and "deep cuts" mean which of their
  songs people know rather than how big the artist is. Nothing is read from your own
  listening.
- **Coverage reporting and background jobs.** `GET /coverage` reports how much of the
  library the DJ actually knows. `POST /jobs` queues work for the worker.
- **No storage coupling.** The library is read through the Subsonic API and played back
  as stream URLs, so no container mounts the music tree or reads another service's
  database.

## Non-features

Deliberate omissions, not backlog:

- **No listener-facing skip.** The answer to "I don't like this" is another station.
  There is a DJ pick endpoint, but it is for the operator.
- **No accounts.** One admin password, no listener accounts, no per-user history.
- **No DJ voice.** No TTS, no spoken links, no jingles, no ducking.
- **No shows or scheduling.** A station is one continuous stream.
- **No scrobbling anywhere.** Plays are written to a local JSONL history and nowhere
  else. Nothing is sent to Last.fm or to Navidrome.
- **No transcoding ladder.** Every stream is one encoder at a fixed bitrate; there is no
  adaptive switching inside a stream, no per-listener negotiation, and no bitrate
  dial — the mount name is the extension.
- **No tempo.** A BPM axis was prototyped and dropped: on synthetic click tracks with
  exact ground truth the best estimator got 20 of 24, failing precisely on the
  slow-tempo-versus-half-time case that "play something slower" depends on, and 70% on a
  real library. No BPM field, tool parameter or station rule exists.
- **No mood or energy metric.** Loudness was measured and rejected as a proxy: in
  measurement it separated albums by mastering level, not tracks by intensity.
- **No play-counts of yours.** The popularity lean reads public ListenBrainz and Last.fm
  totals — never your Navidrome history, which is not consulted at all.
- **No multi-user auth, no HA, no failover.** One host, one library.
- **No tag writing.** Genre fixes in the library are a separate job done with beets and a
  rescan; this project only reads the result.

## Resources

Measured on one host (8 cores), a 4,800-track library, two stations running:

- **A station on air and unheard costs about 1.5–2% of one core.** That is decoding the
  next track from the library (mostly FLAC over the Navidrome API, resampled to 48 kHz),
  crossfading it and discarding the audio.
- **A stream being listened to costs about 12% of one core**, independent of how many
  people are listening — one encoder per stream, and Icecast copies those bytes to every
  client. Three formats listened to at once is three encoders: FLAC is the expensive
  one, because lossless at 48 kHz is roughly 700 kbit/s against MP3's 192.
- **A listener costs about 0.07% on Icecast, 10 KB of memory and 192 kbit/s of
  bandwidth.** One encode per station serves every listener on it, so ten listeners cost
the same as one.
- **The whole stack idle** — picker, worker, Icecast and both station clocks — is about
  4% of one core and 190 MB of RAM. The DJ itself is invisible: the picker idles at
  0.2%.

For scale: `icecast.xml` allows 25 concurrent clients and 8 sources by default. Both are
in the generated file, not compiled in.

## Layout

```
this repo                code, Dockerfile, deploy/
deployment dir           compose.yaml, .env, optional compose.override.yaml
config/                  stations.json, spica.liq (generated), icecast.xml,
                         dj-persona.md, llm.json, artist-mbids.json
state/                   index.db, pools/, queue/, history/, now/, requests/,
                         art/, genre-aliases.json, logs/
```

`config/` is the part worth backing up: it is your stations and your DJ. Everything in
`state/` can be rebuilt from Navidrome except the history and the request log. Both
directories are bind mounts; the paths inside the containers are `/config` and `/state`
and nothing about them is compiled into the code.

## Stations

`stations.json` in `config/`. It is deployment data, not part of this repo; the admin
writes it, or copy the shape below. Every list is optional and an empty one means no
constraint.

```json
{
  "id": "example",
  "name": "Example",
  "description": "Free text the DJ reads on every pick.",
  "include_genres": ["Progressive Rock"],
  "include_artists": ["King Crimson"],
  "exclude_genres": [],
  "exclude_artists": [],
  "year_from": 1969,
  "year_to": null,
  "popularity": "deep",
  "streams": ["mp3", "opus", "flac"]
}
```

- `include_genres` / `exclude_genres` are **labels from the vocabulary**, compared
  exactly (case and punctuation fold). There is no substring matching: a station asking
  for `Alternative` does not sweep up `Alternative Metal` because the word appears;
  `Alternative` is not a label, and the picker will not store it.
- `include_artists` is a second way *in*, not a filter on the genres: a track plays if it
  carries an included label **or** is by an included artist. That is how a station
  reaches outside its genres — tick `Progressive Rock`, add `Bloc Party`, and those Bloc
  Party tracks play beside the prog. Artists alone make a shortlist station; genres alone
  make a genre station.
- `exclude_genres` / `exclude_artists` are absolute: they win over either list, and names
  are compared case-insensitively.
- `popularity` is `deep`, `balanced` (the default) or `hits`. It weights the order of the
  pool — it never filters. `deep` opens on the tracks that fewest people know, `hits` on
  the ones they do.
- `streams` is what the station publishes: any of `mp3`, `opus` and `flac`, as toggles
  in the console. **MP3 is the default** whenever it is on — that is what
  `/listen/<station>` and the play button use. Leave the key out and you get MP3 alone.
  Details in [Streams](#streams).
- No rules at all means the whole library.

A station whose rules match nothing logs a warning and plays the whole library rather
than going silent.

Editing in the admin saves and rebuilds the pools immediately. Adding, renaming or
deleting a station regenerates `spica.liq` and restarts the mixer for you; renaming the
id moves the station's pool, history and queue along with it, so the ledger survives the
rename. Deleting keeps the history on disk — re-creating the same id picks it back up.

## Streams

A station publishes a set of streams: MP3 at 192 kbit/s, Opus at 128, and FLAC
lossless. They are switches, not settings — on or off — because the mount name is the
extension, and a bitrate you could change would make `/alt.mp3` a lie for whoever is
holding the URL.

| codec | mount | encoder | cost |
|---|---|---|---|
| MP3 | `/<station>.mp3` | `%mp3(192, 48 kHz, stereo)` | ~12% of a core while listened to |
| Opus | `/<station>.opus` | `%opus(128, 48 kHz, stereo)` | ~12% of a core, half the bits of MP3 |
| FLAC | `/<station>.flac` | `%ogg(%flac(44.1 kHz, stereo))` | ~12% of a core, ~700 kbit/s per listener |

FLAC goes out as Ogg FLAC — bare FLAC over Icecast gives players no framing to resync
on — on a `.flac` mount, because the extension is what a player sniffs. It runs at
44.1 kHz rather than 48 so a lossless stream is not resampled for nothing; MP3 and Opus
run at 48.

ICY titles are sent only on the MP3 mount: Icecast does not apply out-of-band titles to
Ogg streams, which carry their own.

**URLs to hand out.** Two shapes, one for players and one for people:

```
https://host/alt.mp3                 straight to Icecast, and only alive while the mount is open
https://host/alt.opus
https://host/alt.flac

https://host/listen/alt              the default stream, opened on demand
https://host/listen/alt/opus         a specific one, by codec
https://host/listen/alt/flac
```

The `/listen/` URLs are the ones to give VLC, Sonos, a car head unit or a phone: they
open the mount if it is closed and relay the bytes, so the client sees an always-on
stream instead of a 404. The bare `/<station>.<ext>` URLs only work while somebody
already has that mount open.

**MP3 is the default.** `/listen/<station>` and the page's play button use the first
codec in MP3 → Opus → FLAC order that the station publishes, which means MP3 whenever
it is on. The station page shows a format selector when there is more than one, and
switching mid-song reconnects to the other mount while the station keeps playing.

**Cost.** A stream costs nothing until it is opened, and then the encoder's share of a
core for as long as anybody listens, whether that is one listener or twenty. Idle is the
shared decode, so three streams cost the same as one until a second is actually in use.
FLAC also costs the listener four times the bandwidth.

**Changing streams** regenerates `spica.liq` and restarts the mixer, which is a few
seconds of silence on every station, not just the one being edited.

## Genres

A genre is a label, and every label carries where it came from. Two sources speak, and
one fills the gaps:

1. **file** — the genre in the files, as Navidrome reports it. Split on commas,
   semicolons, pipes and spaced slashes, so `Disco | Indie Pop` is two labels.
2. **musicbrainz** — artist genres with their vote counts, cached per artist
   (`mb_artist_genres`). Names are ambiguous — `Solaris` is a Hungarian prog band and
   also an ambient producer, `Frost*` loses the search to Celtic Frost — so when several
   artists share a name the candidates are checked against **the album titles in the
   library** and the one that owns those records wins. If none does, the artist gets no
   genre from MusicBrainz rather than a confident wrong one, and
   `config/artist-mbids.json` (`{"frost*": "<mbid>"}`) settles a case by hand if the
   database itself is wrong. Consulted for **every artist it knows**, tag or no tag,
   because the tags themselves are the least reliable layer: Banco's *Darwin!* says
   `Rock` on disk and MusicBrainz answers `progressive rock`, so the track ends up
   carrying both. Of those genres it keeps **the top one**, and every name tied with it —
   a tie is the data saying it cannot choose. Everything below is a single editor's
   opinion that happens to be written down.
3. **last.fm** — only for artists MusicBrainz has no genres for, as imported tags.

Provider tags are checked against **MusicBrainz's own genre list** — 2,222 names it
considers genres, cached in `mb-genres.json` for a month. `psychedelic rock`, `nu disco`
and `stoner rock` are on it; `France`, `Genius`, `Cuatro Puertorriqueno` and
`Www.mp3 Ogg.ru` are not, and never become labels. A short blocklist covers what the
genre list itself still lets through — nationalities, decades, `instrumental`,
`atmospheric`, `live`, `cover`, `karaoke` — because the most-voted tag for an
instrumental band is `instrumental` and the genre would never surface.

Before any of that, labels are normalised:

- **fold**: case, padding, punctuation and hyphens fold, so `Post-Rock`, `post rock` and
  `Post  Rock` are one key, and one spelling survives — the one the library uses most, in
  a consistent house style (`Post Rock`, not `Post-Rock`).
- **builtin merges**: a short list of spelling and translation noise
  (`rock progresivo` → `Progressive Rock`, `alt rock` → `Alternative Rock`).
- **operator rules**: `genre-aliases.json` in `state/`, which win over the builtin list.
  Merging in the admin writes one of these, so a rebuild lands in the same place.

Two things outrank all of it, set in the admin's genres pane:

- **by hand** (`source = 'manual'`) — a label assigned to selected tracks. It survives
  every rebuild.
- **a veto** — removing a label from one or more tracks records the refusal, so a rebuild
  cannot quietly hand it back.

Labels live in `track_genres` (canonical, with source) and `track_raw_genres` (as the
library spells it). `GET /genres` reports both.

## Where the library comes from

Navidrome's Subsonic API. The index pages `getAlbumList2` (albums) and `getAlbum`
(songs), which for a 4,500-track library is 448 calls and about five seconds.

This is deliberate, and the alternative was tried: reading Navidrome's SQLite database
directly works, gives exact file paths and needs no credentials, but it couples this
project to a schema that changes between Navidrome versions, requires the database file
to be reachable, and hands one service a view into another's storage. The API route has
none of those properties; the price is an HTTP hop for every track, which on a local
Docker network is nothing.

Playback follows the same rule. Pools and queue files carry
`/rest/stream?id=<songId>` URLs with Subsonic token auth (salted md5), so Liquidsoap
plays them over HTTP and no container needs the filesystem layout. A library mount would
let Liquidsoap read files instead, but only with path resolution heuristics and existence
proofs, because the API's `path` is built from tags rather than read off disk.

The files themselves are served as they are — mostly FLAC in this library, resampled to
48 kHz inside Liquidsoap and encoded to 192 kbit/s MP3 for the stream. Asking Navidrome
to transcode would move that work from one container to another on the same host for no
gain.

## Web UI

One process serves both sides. The listener side is public; everything that writes is
under `/admin` and needs `SPICA_ADMIN_PASSWORD` from the environment.

```
/                    lands on the first station
/s/<station>         now playing, the ledger, and the request box
/listen/<station>    the same stream as a plain URL, for players
/admin               coverage, stations, recent jobs, rebuild and reload
/admin/stations      the rack; add, rename, delete, and edit each station's rules
/admin/genres        the vocabulary, merging, and a table to explore tracks by genre
/admin/prompt        the global prompt: who the DJ is and how it talks
/admin/settings      the model endpoint: base URL, model, key, extra headers, and a test
/admin/library       coverage panel and the background jobs with their results
```

Templates and `htmx.min.js` live in `spica/templates` and `spica/static`, so the
container serves them with no build step. Now playing polls every five seconds; the
ledger every ten; the job table every three.

**The station page is a sleeve, not a dashboard.** The cover is the hero — large and
centred — with the artist, title, listener count and the DJ's line under it, then the
play button on the centre line. There is no tuning dial: with a handful of stations it
was decoration.

**Nothing plays until you press play.** Landing on a station opens its mount a second
later so the first press is near-instant, and because the station is already on air the
audio starts mid-track. No source is attached to the audio element until you press, so no
sound is made. A mount nobody listens to closes after `IDLE_STOP_S` (90 s), which stops
the encoder and not the station — a page left open therefore costs a minute and a half of
encoding, not a stream. Switching stations while listening follows you across; switching
on a silent page stays silent.

**The ledger shows what aired, in your clock.** Every row carries an ISO timestamp that
the browser rewrites in its own timezone; the server fallback is `SPICA_TZ` (default
`America/Montevideo`). Entries come from the mixer, so they appear whether or not anyone
was listening, and a track that airs twice keeps both times rather than being mistaken
for a duplicate.

**The admin can add, rename and delete stations.** Renaming the id moves the pool,
history, queue and request files with it; deleting keeps the history on disk so
re-creating the id resumes the ledger. Both regenerate `spica.liq` and restart the mixer,
which is a few seconds of silence on every station.

Cover art is proxied through `/art/<albumId>` and cached in `state/art`, so the browser
never sees Subsonic credentials. A track the index cannot identify — the mixer sometimes
reports a title Icecast has mangled — still gets artwork, because the lookup falls back
to the same artist and a title that matches once the holes stand for anything.

**The model endpoint lives in the admin**, under *settings*: base URL, model name, key
and any extra headers, saved to `config/llm.json` (mode 600). It is read on every pick,
so a save applies immediately with no restart, and the environment
(`LLM_BASE`/`LLM_KEY`/`LLM_MODEL`/`LLM_HEADERS`) is only the default. *Test this
endpoint* sends one small request with a tool attached and reports what came back —
latency, the model that answered, and whether it actually called the tool, which is the
one thing picking needs and which not every "OpenAI-compatible" server does.

The global prompt is the DJ's character — the voice it speaks in, which is also the line
listeners read under each song. It is prepended to every pick on every station; each
station's brief is added on top, and the mechanics of picking (one track at a time, tools
only, the JSON reply) stay in the code so an edit cannot break them. It lives in
`config/dj-persona.md`; with no file the built-in voice is used, and *restore the built-in
voice* deletes it.

The genres pane is the operator's whole tool for taxonomy: tick any number of labels and
merge them into one name (rows move, a rule is recorded, pools follow); or open
*explore*, pick genres, and read the tracks under them, each with every genre it really
carries and where each came from. Ticked tracks can be given a label by hand or have one
taken away, and both decisions outlive a rebuild.

**Jobs say what they did.** The library pane lists each run with its name, its state, the
result as a sentence (*4,832 tracks · todo 4,832, prog 2,210, alt 1,545 in pools*), the
local finish time and how long it took.

**The look** is a radio in a lit room: warm paper as the ground, and the leather kept for
objects — a band across the top with the badge, the play button, the level bars. Cream is
the paper the tracks are printed on. Values were chosen by contrast, not by taste:
`#bf5c35` cannot carry small text in either polarity (cream 3.5:1, ink 4.1:1), so it
holds only the large wordmark and fills; surfaces with small text step down to `#8a4a2c`
(cream 5.4:1); leather used as text on paper is `#6f3a22` (8.0:1); red survives as a
signal because on paper it reads 5.7:1.

## Metadata and artwork

Each stream announces itself over ICY, which is what a player in a car, on a Sonos or
in Music Assistant actually reads:

```
icy-name: Alternative
icy-description: Alternative and indie rock, …
icy-br: 192
icy-metaint: 16000        # only sent to clients that ask with Icy-MetaData: 1
StreamTitle='Future Islands - Peach';
```

The title block is inserted every 16 KB by Icecast itself, from the metadata the mixer
pushes as each track starts. Ogg streams (Opus, FLAC) have no ICY; they carry
`artist=`/`title=` as Vorbis comments inside the stream, which VLC reads.

**Artwork cannot travel in ICY.** Icecast emits exactly one field, `StreamTitle`, and no
image or image-URL field exists in the protocol. `StreamUrl` is a de-facto extra that
some broadcasters stuff with an image link — Music Assistant honours it when it ends in
`.png`/`.jpg` — but Icecast does not emit it and VLC ignores it, so it is not a channel
worth building on.

What open-source players actually do with that:

- **Music Assistant** parses ICY, HLS, Ogg comments and SHOUTcast v1. Given
  `"Artist - Title"` it looks the release up on MusicBrainz by itself and shows single,
  then album, then artist artwork — the station logo only when there is no metadata or
  no match. No cooperation needed beyond announcing the track, which we do.
- **VLC, Strawberry, Cantata, mpv** show `StreamTitle` text and no per-track art; art
  only appears if the stream carries ID3 `APIC`, which Liquidsoap's Icecast output
  cannot inject per track.
- **Kodi** add-ons combine `StreamTitle` with a per-station logo configured in the
  add-on or directory.
- **radio-browser.info, TuneIn and Sonos** serve a station `favicon`/logo from their own
  directories — configured once per station, never per track.

So: per-track art is ours to serve (`/art/<albumId>`, and the `art` field in
`/status`), and free for anything that speaks Music Assistant's metadata path. Anything
that wants a station logo wants one image, per station, not per song.

`/logo/<station>.png` is that URL. Every station answers with the Spica mark for now —
a 300-pixel PNG behind a day of cache — so a directory, a client or a Sonos favourite
can be pointed at it today, and swap to a station's own artwork later without the URL
changing.

## Listening elsewhere

The browser player streams through `https://<host>/listen/<station>`, which is also the
URL to hand to VLC, Music Assistant, a Sonos or a car head unit. It opens the mount if it
is closed, waits for Icecast, then copies the stream, so the client sees an ordinary
always-on radio URL instead of a 404.

A station's mount closes after `IDLE_STOP_S` (90 s) with nobody reading it — the station
itself keeps playing — which is why the relay exists. The rules for "nobody":

| client | counts as listening | stops counting |
|---|---|---|
| this web player | while it heartbeats (every 20 s) and the audio is advancing | 60 s after the last beat, or ~40 s after the audio stops moving |
| VLC / Sonos / Music Assistant via `/listen` | while it reads bytes | when it disconnects, or once its buffers fill after a pause |
| anything straight on `/<station>.mp3` | while connected | when it disconnects |
| a backgrounded browser tab | never | — |

The pause case is bounded by TCP, not by our timer: a paused player stops reading, but
its socket buffers absorb a few minutes of audio before the connection stops accepting
bytes. `GET /status` shows the presence picture per station, plus the track split into
`artist`/`title`/`album` and an absolute `art` URL — which 404s for an album with no
cover, so treat it as optional.

## API

Bound to `0.0.0.0:8030` inside the network and published on `SPICA_PORT`; put it behind
your proxy or firewall rather than the open internet, except for the paths the listener
page uses.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | LLM configured, which stations |
| GET | `/status` | per station: listeners, presence, on-air title, **artist / title / album**, an absolute `art` URL, queued track, pool size |
| GET | `/logo/<station>.png` | the station's picture: the Spica mark, for every station |
| GET | `/coverage` | how much of the library has genres, and what is missing |
| GET | `/genres` | labels with counts and sources, plus the raw tags behind them |
| GET | `/aliases` | operator rules in force and the builtin table |
| GET | `/similar?artist=X` | similarity chain result, says which level answered |
| GET | `/requests/<station>` | requests still in force, with age and remaining songs |
| POST | `/pick/<station>` | force a DJ pick now |
| POST | `/request` | `{"text", "station", "songs"}`: record a request and pick |
| POST | `/rebuild` | re-index Navidrome and rewrite every pool |
| POST | `/liq` | render `spica.liq` from `stations.json` |
| POST | `/enrich?limit=N` | fill artist MBIDs, 0 for all |
| POST | `/s/<station>/start?stream=<codec>` | open a stream's mount (what the play button calls) |
| GET | `/s/<station>/state?stream=<codec>` | live/presence json, per stream and in total |
| GET | `/listen/<station>` | the default stream, relayed and opened on demand |
| GET | `/listen/<station>/<codec>` | one stream: `mp3`, `opus` or `flac` |
| POST | `/s/<station>/hear` | the web player saying it is still playing (204) |
| POST | `/jobs` | `{"kind": "genre_tags" \| "popularity" \| "musicbrainz" \| "refresh"}` |
| GET | `/jobs` | recent jobs with progress |
| DELETE | `/requests/<station>` | clear the station's live requests |

## Configuration

Everything deployment-specific comes from the environment; `deploy/.env.example` lists
the keys, and that is the file to start from.

| Variable | Meaning |
|---|---|
| `SPICA_TAG` | the image tag to run, e.g. `0.2.0` |
| `PUID`, `PGID` | the user that owns `config/` and `state/` (default `1000`) |
| `TZ` | the container timezone, for logs and the ledger's server fallback |
| `ICECAST_SOURCE_PASSWORD` | what the mixer publishes with; written into `icecast.xml` on first run |
| `ICECAST_ADMIN_PASSWORD` | Icecast's own `/admin` pages |
| `SPICA_ADMIN_PASSWORD` | admin login for the web UI; empty disables admin writes |
| `SPICA_HOSTNAME` | the host Icecast puts in its stream URLs |
| `ICECAST_PORT`, `SPICA_PORT` | the two published ports (default `8000` audio, `8030` web) |
| `NAVIDROME_URL` | Navidrome base URL as the containers reach it |
| `NAVIDROME_USER`, `NAVIDROME_PASS` | a Navidrome account for the radio |
| `LLM_BASE`, `LLM_KEY`, `LLM_MODEL` | defaults for the endpoint; the admin's settings page wins |
| `LLM_HEADERS` | optional extra headers, `k=v` comma separated |
| `LASTFM_KEY` | optional: name-based similarity and tag backfill |
| `MB_USER_AGENT` | optional: contact string for MusicBrainz |
| `SPICA_STATE`, `SPICA_CONFIG` | where state and config live inside the container (`/state`, `/config`) |
| `ICECAST_URL` | Icecast base URL as the picker sees it (`http://icecast:8000`) |
| `ICECAST_OUTPUT_HOST`, `ICECAST_OUTPUT_PORT` | where the mixer publishes — separate from `ICECAST_URL` because the two containers see Icecast at different addresses |
| `LIQ_TELNET` | Liquidsoap control server (default `mixer:1234`) |
| `SPICA_TZ` | the timezone the server falls back to when the browser cannot say (default `America/Montevideo`) |

On first start the `init` service writes `icecast.xml`, `stations.json` and `spica.liq` if
they are missing — one starter station, called *Everything*, with no rules — so a fresh
install has something on the air without any file editing. `python -m spica.bootstrap`
does the same thing by hand and never overwrites a file that exists.

## Operations

```bash
cd /path/to/deployment          # wherever compose.yaml lives
docker compose up -d            # start; pull the tag in SPICA_TAG
docker compose logs -f picker
docker compose down             # stop everything, state untouched

A=http://127.0.0.1:8030
curl -X POST $A/rebuild             # after new music lands in Navidrome
curl $A/coverage                    # what the DJ knows
curl -X POST $A/jobs -H 'content-type: application/json' -d '{"kind":"genre_tags"}'
```

Upgrading is a tag change: edit `SPICA_TAG`, then `docker compose pull` and
`docker compose up -d`. If a release changes the generated Liquidsoap script, regenerate
it and restart the mixer:

```bash
docker compose run --rm init            # writes nothing that already exists
docker compose restart mixer
```

The worker watches the library for you: it compares Navidrome's track count every 15
minutes, rebuilds the index and pools when it changes, and queues the cheap follow-ups —
popularity for tracks that have none, MusicBrainz for artists without genres. The jobs
are also on the admin's library page. Operator merges and the alias maps are applied at
rebuild time, so a rebuild also re-canonicalises every label.

## State

`state/` holds everything that survives a restart:

- `index.db`: SQLite projection of the library. Disposable, rebuilt from Navidrome.
- `pools/<station>.m3u`: what the station plays when nobody is steering; one stream URL
  per line.
- `queue/<station>.txt`: the single-file hand-off between picker and mixer.
- `now/<station>.txt` and `.offset`: the mixer's own record of each track it starts, and
  how far the picker has read. This is what keeps the ledger honest when no mount is open.
- `history/<station>.jsonl`: append-only play log, written by this project only.
- `history/<station>.picks.jsonl`: the DJ's choices with their reasons and the presence
  that justified them.
- `requests/<station>.jsonl`: live listener requests.
- `art/<albumId>`: cached cover art, one file per album.
- `genre-aliases.json`: operator merge rules, empty by default.
- `logs/`: worker and job output.

Rebuilding the index wipes `index.db` content, not the other files, so history and
requests survive it.

## Contributing

`main` is release-only: changes arrive as squashed pull requests. The loop, the commit
style and how to cut a release are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
