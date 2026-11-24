<p align="center"><img src="spica/static/logo.png" width="120" alt="Spica"></p>

# Spica

Internet radio for a local music library: several stations on air at once, an LLM DJ
that picks, and a page you can leave open.

> **Alpha software.** Spica runs and is in daily use, but it is not a product: expect
> rough edges, thin documentation in places, and breaking changes between minor versions
> — in configuration, in `state/`, and in the API. Pin `SPICA_TAG` to a version rather
> than riding `latest`, keep a copy of `config/`, and read what changed before pulling a
> new one. Nothing here is written with multi-user installs in mind.

## Where it comes from

Spica is inspired by [SUB/WAVE](https://github.com/perminder-klair/subwave), the project it
grew out of, and is built from the ground up around two things SUB/WAVE does not do.

The first is **several stations at once**. SUB/WAVE puts one station on air: switching
means restarting the broadcaster. Here every station has its own Icecast mount and its
own Liquidsoap output, all live simultaneously, so switching is a player-side action and
a station never goes silent because somebody chose another one.

The second is a **deliberately trimmed feature set**, to spend almost nothing on
resources. SUB/WAVE is a complete station product — DJ voice, shows, jingles, ducking,
an always-on analyser, CLAP embeddings — and pays for it in a broadcast container, an
analyser, a TTS service and about a gigabyte of RAM. Spica drops the always-on stack
and keeps the picking: rules, pools, an LLM that reads a brief, and a ledger. Audio
analysis is here too, but as one optional worker job on the leanest stack that still
helps — numpy, onnxruntime and essentia, no torch, off by default. With analysis off,
what runs continuously is roughly 190 MB and about 4% of one core with two stations on
air, because a station nobody is listening to costs a decode, not an encode.

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

**The DJ only speaks to somebody.** Every twenty seconds the picker looks at who is
really listening and, for a station with an audience, asks the LLM for the next set of
tracks; the choice goes to `state/queue/<station>.txt`, Liquidsoap polls that file,
pushes it into the station's request queue and deletes it. With nobody there, the station
keeps walking its pool and the DJ is not called at all.

**The ledger records what aired.** The mixer writes each track it starts to
`state/now/<station>.txt`; the picker tails that file and appends to the play log. It
is a record of the station, not of your listening — it fills whether or not anyone is
tuned in, and each entry carries the time that track started.

"Listening" is not the same as an open socket. The browser says so — the player posts a
heartbeat every 20 s while it plays. A player on the relay counts while it is taking
bytes. A connection straight to a mount counts if it did not come from the relay. A tab
left in the background, or a stream that died holding its socket open, does not.

## DJ modes

The DJ is one global mode, set in the admin under *Settings*, and it is independent of
whether a model is configured:

- **Off** — no model is called. Every station walks its own rotation, the ledger fills,
  and there are no reasons under the songs. The rotation is rebuilt with the last 40
  plays left out (when enough remain) and its first track avoids the artist and album
  that just finished. This is a real choice, not what happens when a key is missing.
- **Artist-led** — the DJ is given a short list of artists (the *artist shelf*) and
  searches their catalogues for tracks. No audio analysis is needed. The set is the
  DJ's own, with little guidance beyond the list.
- **Track-led** — the DJ is given a table of tracks grouped by how they relate to the
  current song (continue the flow, move one axis, change it up, land, the one everyone
  knows). This needs the optional audio analysis and generally makes for more fluid
  sets. A station the analyser has not measured enough falls back to the artist shelf.

With Artist-led or Track-led selected but no model endpoint saved, the stations play
their own rotation and the admin says so. The two path instructions are editable on the
*DJ Persona* page; the JSON reply contract and the legality checks stay in the code, so
an edit cannot let an illegal track air.

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
**Everything** (id `main`, so its stream is `/listen/main`) that plays the whole library,
so there is nothing else to create.

**Where the credentials go**, all of them in that one `.env` file next to
`compose.yaml`, mode 600:

- `ICECAST_SOURCE_PASSWORD` — what the mixer publishes with. Written into `icecast.xml`
  on first run, so set it before the first start.
- `ICECAST_ADMIN_PASSWORD` — Icecast's own `/admin` pages.
- `SPICA_ADMIN_PASSWORD` — the console. Empty disables admin writes.
- `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASS` — a Navidrome account for the
  radio. Nothing is ever written to it, and no scrobbles are sent. The Subsonic
  token is minted from a salt kept in `state/` and it never expires, so any stream
  URL that leaves the machine — a playlist file, a log line, a screenshot of the
  network tab — is a working credential for as long as the salt exists. Give the
  radio its own Navidrome user rather than an admin one, and treat a leaked URL the
  way you would treat a leaked password.
- `LLM_BASE`, `LLM_KEY`, `LLM_MODEL`, `LLM_HEADERS` — defaults for the endpoint. The
  admin's *settings* page can override them, and what it saves to
  `config/llm.json` (mode 600) wins.
- `LASTFM_KEY` — optional: name-based similarity and tag backfill. The admin's
  *settings* page can override it.
- `MB_USER_AGENT` — optional contact string for MusicBrainz. The admin can override it.

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
- **Pools reshuffle on every rebuild.** Sequencing is a weighted random draw over the
  station's rules — the popularity lean decides which tracks come sooner — then two
  constraints: no two tracks from one album adjacent, no artist repeat within 12 placed
  tracks. The tracks from the last 40 plays are left out of the new list when the
  station has enough left, and the first placement avoids the artist and album that just
  finished, so a rebuild does not open on the last record. Two rebuilds of the same
  library do not produce the same order, so the playlist on disk is the record of what a
  station was playing.
- **Sets, not picks.** The DJ is asked for three to ten tracks at once and one line
  that says what the set is, and the set is then played out one track at a time from
  `state/plans/<station>.json`. Asking "what follows this song" once per track has only
  one cheap answer — the nearest thing to what is already playing — which is how the
  station ended up circling the same corner; asking for a set costs one decision per
  stretch of night instead of one per record. A listener request throws the rest of the
  set away, because it was chosen for the mood they asked to leave.
- **LLM picks with real tools, when a set needs them.** Six of them: `history` (what
  the station just played, with how many songs ago), `flip` (three more records from the
  racks, never the ones already dealt), `search` (word-wise, and a name that plays
  nowhere here answers with the artists that sound like it), `filter` (a draw from the
  station, narrowed by genre and years, with totals and paging), `catalogue` (an
  artist's holdings or one album in running order), and `similar`. The station's genre
  weights and its size are in the system prompt; the artists are not — in the tool path
  the shelf names the only ones that can play, and a second list of names beside it is
  one the model can name from. Every result is filtered through the station's rules, so
  a narrow station cannot return something outside its palette. In the tool path track
  ids exist only inside tool results, so a pick is always grounded in a lookup rather
  than a remembered name; a table set carries its ids in the brief instead, already
  checked against the table. The toolbox and its contract are in
  [`docs/dj-tools.md`](docs/dj-tools.md). With analysis on, a normal set is one call and
  no tools; the tools are for listener requests and the analysis-off path.
- **The shelf, and the table.** With analysis off, each set is drawn from a named shelf of
  about twelve artists, and the reply is checked against it — an artist that is not on the
  shelf is dropped like a track that is not on the station, and no artist appears twice
  in a set. The shelf leaves out what played in the last five tracks, the artists the DJ
  picked in its last eight, what is queued,
  and the three closest neighbours of the records either side of the set (Last.fm match
  ≥ 0.15, edges taken both ways), then fills ten slots by label weight with a cap of
  three per label, at least two decades and one artist from each third of the roster.
  Two more slots go to whoever has been off the air longest, so a record that has not
  had its turn in hours is not excluded by a stratification that never thinks about
  time. The point is that a smaller version of the station is a better brief than a
  larger version of the model's taste: given a list, the DJ plays from the list. With
  analysis on, a normal set is drawn from the record table instead (see below); the
  shelf remains the analysis-off path and the fallback for a station with no usable
  anchor.
- **Audio analysis, optional and off by default.** With `SPICA_ANALYSIS=1` the worker
  reads each track once through Navidrome — the original file, not a transcode — and stores what it sounds like:
  a Discogs-EffNet embedding, the 400 Discogs style probabilities, ten mood/danceability/
  timbre/voice heads, tempo with confidence and key (plus EBU R128 loudness and dynamic
  range, opt-in). The offer to the DJ becomes **records, not artists**: about thirty-two
  tracks in five directions — continue the flow, move one axis, change it up, land it,
  and the one everyone knows — each with style, tempo, key, energy and how well known it
  is, and the ids written in front, so a set is one model call and no lookups. A record
  stays out of the table for sixty DJ picks or eight hours of air, whichever is longer,
  and for four sets after it was merely *offered*: being offered is having had a turn,
  and a record that never played used to come back on table after table. Absence also
  lifts a candidate inside its direction, so near neighbours that have not had a turn come
  first instead of the same few orbiting what is on air. The familiar option is the top
  third of the artist's catalogue, ordered by whoever has been off the air longest rather
  than by how famous the single is; the station's own popularity lean decides how much of
  the table it gets. Listener
  requests still go through the tools, where a name can be searched. `similar` answers
  from the vectors first, and the fallback shelf's "sounds too like what is playing"
  rule stops depending on whether Last.fm happens to name an artist you own. Readiness
  is per station: while a backfill is partial the table is drawn from what has been
  measured and gains a *not measured yet* group (recent arrivals, no sonic numbers), and
  with no usable anchor it falls back to the artist shelf. ONNX on the CPU, about 4 s of
  CPU per track
  (four-minute cap, extra DSP off) — roughly 2–3 h for a 5,000-track library on four
  cores — models downloaded once. Off, nothing below changes.
- **Similarity without a hard dependency.** Cached, then artist MBID from Navidrome,
  then MusicBrainz, then ListenBrainz, then Last.fm by name, then local genre. No
  provider is required; a library with no ids and no API key still gets an answer. The
  shelf uses the scored `artist_similarity` table when Last.fm has filled it (the
  `similarity` job); with no key it simply does not exclude neighbours, rather than
  guessing them from genres.
- **Listener requests that last more than one song.** Free text, kept for N tracks, and
  the DJ is told both how many songs have passed and how many remain. A request
  supersedes what is waiting rather than queueing behind it: "play something heavier" is
  about the mood, and both the waiting track and the rest of the set were chosen for the
  old one, so they are dropped and a new set is picked with the request in the brief. The
  system prompt tells it to honour requests, including mood changes, without ever moving
  the station away from its own identity.
- **Genres from the best source available, not from guesses.** Your file tags, then
  MusicBrainz genres with their vote counts, and Last.fm only for artists MusicBrainz
  does not know. Every genre records where it came from — `file`, `musicbrainz`,
  `last.fm`, or `by hand`.
- **One genre unless the artist has range.** MusicBrainz votes decide: 23 for progressive
  rock against 7 for art rock is a progressive rock band, while a tie at 2 and 2 is
  genuinely both. Stations pick genres from that vocabulary, so matching is set
  membership and not string guessing.
- **Popularity from real play counts.** ListenBrainz listen counts per track (batched),
  Last.fm for whatever has no MusicBrainz id, ranked inside the artist's own catalogue, so
  "hits" and "deep cuts" mean which of their songs people know rather than how big the
  artist is. Nothing is read from your own listening.
- **One level across the library.** Each track's ReplayGain tag becomes one static gain
  toward −14 LUFS, applied before the crossfade, so a record mastered hot does not jump
  out of the stream. It is normalisation, not a loudness-as-intensity measure, and a
  track with no tag plays untouched. See [Loudness](#loudness).
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
- **No tempo unless analysis is on.** A BPM axis alone was prototyped and dropped: on
  synthetic click tracks with exact ground truth the best estimator got 20 of 24,
  failing precisely on the slow-tempo-versus-half-time case that "play something
  slower" depends on, and 70% on a real library. The optional analysis stores tempo
  *with its confidence* and the brief treats it as a hint, never a rule — tempo is one
  signal among several, not the axis.
- **No mood or energy metric in the base install.** Loudness was measured and rejected
  as a proxy: in measurement it separated albums by mastering level, not tracks by
  intensity. With analysis on, energy comes from trained heads rather than from
  loudness, and loudness is stored as loudness — for context and normalisation, not as
  intensity.
- **No play-counts of yours.** The popularity lean reads public ListenBrainz and Last.fm
  totals — never your Navidrome history, which is not consulted at all.
- **No multi-user auth, no HA, no failover.** One host, one library.
- **No tag writing.** Genre fixes in the library are a separate job done with beets and a
  rescan; this project only reads the result.

## Resources

Measured on one host (8 cores), a 4,832-track library, two stations running:

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

**Audio analysis is off by default and lives in its own image.** The picker runs the
lean Alpine image (~200 MB; numpy, to read vectors). The worker runs the `-worker`
image (~330 MB, the same base plus ffmpeg, which is only used by the one-time loudness
measurement). The optional `-analysis` image (~480 MB, Debian for glibc and amd64 only
— essentia has no Linux arm64 wheel) also carries onnxruntime, essentia and ffmpeg.
Turned on, each analysis
process uses about 0.5–0.7 GB and a track costs roughly 4 s of CPU (four-minute cap,
`SPICA_ANALYSIS_DEEP` off), so a 5,000-track backfill is a couple of hours once — after
that only new tracks are analysed. Decoding scales with track length even under the cap,
so a 20-minute track costs more than five four-minute ones. Idle, the analyser process
is a few tens of MB; the DJ itself stays invisible.

For scale: the generated `icecast.xml` allows 25 concurrent clients and 40 sources. Both
are in the file, not compiled in.

## Layout

```
this repo                code, Dockerfile, deploy/
deployment dir           compose.yaml, .env, optional compose.override.yaml
config/                  stations.json, spica.liq (generated), icecast.xml,
                         dj-persona.md, dj.json, dj-shelf.md, dj-table.md,
                         llm.json, providers.json, artist-mbids.json
state/                   index.db, analysis.db, loudness.db, models/, pools/, plans/,
                         queue/, history/, now/, art/, genre-aliases.json, logs/
```

`config/` is the part worth backing up: it is your stations and your DJ. Everything in
`state/` can be rebuilt from Navidrome except the history and the request log — and the
analysis and loudness stores, which cost CPU to measure again. Both
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

- `include_genres` / `exclude_genres` are **genres from the vocabulary**, compared
  exactly (case and punctuation fold). There is no substring matching: a station asking
  for `Alternative` does not sweep up `Alternative Metal` because the word appears;
  `Alternative` is not a genre, and the picker will not store it.
- `include_artists` is a second way *in*, not a filter on the genres: a track plays if it
  carries an included genre **or** is by an included artist — the record's artist, so
  naming an artist also catches its tracks with guests. That is how a station
  reaches outside its genres — tick `Progressive Rock`, add `Bloc Party`, and those Bloc
  Party tracks play beside the prog. Naming an artist is the deliberate act, so it also
  beats a genre *veto*: a station that excludes Progressive Rock and lists Giraffe gets
  Giraffe, which is the reading that makes the list worth having. Artists alone make a
  shortlist station; genres alone make a genre station. The station editor keeps the
  include/exclude artist dropdowns;
  below them, *What this station plays now* lists every artist the saved rules can play,
  grouped by the record's artist, with search and paging. Each artist has an *exclude*
  button that ticks the same never-play list in the form.
- `exclude_genres` / `exclude_artists` win over their include counterparts, names are
  compared case-insensitively, and `exclude_artists` is the strongest rule there is: an
  artist in both lists is out. A genre veto does not reach a named artist (see above).
- Nothing plays shorter than `SPICA_MIN_TRACK` seconds (default `30`). Album gaps, a
  four-second interlude and a hidden `[silence]` file are tracks to the index; they are
  dead air on a station, and the standby rotation aired one fifty-two times. Set it to
  `0` to allow them.
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
| Opus | `/<station>.opus` | `%opus(128, 48 kHz, stereo)` | ~12% of a core, two thirds of MP3's bits |
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

## Loudness

A library is a pile of masters cut at different levels, and a station plays them back
to back. Each track's ReplayGain tag — written by your tagger, read through Navidrome —
becomes one static gain, put on the request as Liquidsoap's `liq_amplify` annotation so
the mixer applies it before the crossfade. Nothing is compressed and the programme bus
is untouched: the record's own dynamics are preserved.

- **Target** −14 LUFS. ReplayGain's own reference is −18, so a tagged track is lifted
  4 dB from its tag.
- **A boost** is capped at +6 dB and needs a known peak. A tag with a gain but no peak
  is left at 0 dB rather than pushed into the encoder's ceiling; a peak too close to
  full scale leaves no room to lift either.
- **A cut** stops at −12 dB, and no gain may leave a track above −1 dBFS sample peak.
- **No tag, no measurement, no gain.** A track with neither plays exactly as it did.

**When the file has no tag, the worker measures it once.** The `loudness` job reads
the track through Navidrome and runs ffmpeg's EBU R128 filter, storing integrated
loudness and the sample peak in `state/loudness.db`. That store is separate from the
index on purpose: the index is disposable, and a measurement is CPU. The job is
incremental and resumable, measures only tracks with no tag and no attempt, and does
not retry a file that failed to decode. It runs from the `-worker` image, which is the
lean image plus ffmpeg; the picker and init stay lean.

On this project's library a full backfill of roughly 4,700 untagged tracks cost about
**1.8 CPU-hours** (about 30 minutes on four concurrent workers, ~52 MB each). It is a
one-time cost, not an always-on one. `SPICA_LOUDNESS_MEASURE=0` keeps it tag-only; the
admin's *Library* page has a **Measure loudness for untagged tracks** button, and
`POST /jobs {"kind": "loudness"}` does the same.

The gain is computed when a pool or a pick writes its request, from the tags stored in
the index and the measurements in `state/loudness.db`. A target change needs no
re-analysis; new tags reach Spica after Navidrome next scans the files and the index is
rebuilt, which the worker does when the library changes. `SPICA_LOUDNESS=0` turns the
gain off, and the knobs are `SPICA_LOUDNESS_TARGET`, `SPICA_LOUDNESS_MAX_BOOST`,
`SPICA_LOUDNESS_MAX_CUT` and `SPICA_LOUDNESS_PEAK_CEILING`. This is the normalisation
use of loudness the [Non-features](#non-features) note leaves open — not the
loudness-as-intensity signal the DJ rejected.

## Genres

Every genre carries where it came from. Two sources speak, and
one fills the gaps:

1. **file** — the genre in the files, as Navidrome reports it. Split on commas,
   semicolons, pipes and spaced slashes, so `Disco | Indie Pop` is two genres.
2. **musicbrainz** — artist genres with their vote counts, cached per artist
   (`mb_artist_genres`). The lookup uses **the id Navidrome already carries** for the
   artist when there is one, and only falls back to a name search: the search is where
   the failures came from, since `blink‐182` is stored with a typographic hyphen and can
   never match MusicBrainz's spelling. Either way, names are ambiguous — `Solaris` is a
   Hungarian prog band and
   also an ambient producer, `Frost*` loses the search to Celtic Frost — so when several
   artists share a name the candidates are checked against **the album titles in the
   library** and the one that owns those records wins. If none does, the artist gets no
   genre from MusicBrainz rather than a confident wrong one — and that *is* cached, as an
   empty answer, for `SPICA_MB_GENRES_DAYS` (default 30). Without that, a name the
   database cannot answer for was re-asked on every refresh: forty artists, eight minutes
   of throttled requests per import, one or two of them finding anything. A request that
   fails is not cached, so an outage just tries again next time. And
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
`Www.mp3 Ogg.ru` are not, and never become genres. A short blocklist covers what the
genre list itself still lets through — nationalities, decades, `instrumental`,
`atmospheric`, `live`, `cover`, `karaoke` — because the most-voted tag for an
instrumental band is `instrumental` and the genre would never surface.

Before any of that, genres are normalised:

- **fold**: case, padding, punctuation and hyphens fold, so `Post-Rock`, `post rock` and
  `Post  Rock` are one key, and one spelling survives — the one the library uses most, in
  a consistent house style (`Post Rock`, not `Post-Rock`).
- **builtin merges**: a short list of spelling and translation noise
  (`rock progresivo` → `Progressive Rock`, `alt rock` → `Alternative Rock`).
- **operator rules**: `genre-aliases.json` in `state/`, which win over the builtin list.
  These are a **station-selection rollup, applied when a station is matched** — ticking
  `Electronic` catches a record the library files under `Nu Disco`. They are not a rewrite
  of the index: the tools and the DJ keep the library's own vocabulary, which is what makes
  `filter {'genre': 'Nu Disco'}` answer with nu-disco records instead of the whole shelf.
  Merging in the admin writes one of these rules; relabelling the stored tracks is a
  separate, explicit action, because it throws the fine label away.

The admin's genres pane shows one row per collapsed name with the fine labels nested
underneath: the console exists to build stations, so it keeps the short list, while the
index keeps the long one.

Two things outrank all of it, set in the admin's genres pane:

- **by hand** (`source = 'manual'`) — a genre assigned to selected tracks. It survives
  every rebuild.
- **a veto** — removing a genre from one or more tracks records the refusal, so a rebuild
  cannot quietly hand it back.

Genres live in `track_genres` (canonical, with source) and `track_raw_genres` (as the
library spells it). `GET /genres` reports both.

## Where the library comes from

Navidrome's Subsonic API. The index pages `getAlbumList2` (albums) and `getAlbum`
(songs): for a 4,832-track library that is one page of the album list plus one call per
album — 486 calls, about eight seconds.

Every track keeps two names: `artist`, the credit on the recording, and
`album_artist`, the artist of the record it sits on. The second is the identity
everything groups by — counts, `catalogue`, station artist rules, the pool's
"don't repeat an artist for twelve tracks", genre and MusicBrainz lookups, similarity,
and the admin's artist lists — because a guest turns one artist into two
("L’Impératrice & Fabiana Martone" is L’Impératrice), and because names are typed,
not copied: lookups compare a folded key, so `L'Impératrice` with an ASCII apostrophe
finds the typographic one.

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
/admin               redirects to the first tab, stations
/admin/stations      the rack; add, rename, delete, and edit each station's rules
/admin/queue         per-station tabs: on air, in hand, the set in play, and the pick log
/admin/library       how the DJ is picking, what each job fills, and the job history
/admin/genres        the vocabulary, merging, and a table to explore tracks by genre
/admin/prompt        DJ Persona: the editable voice and the full assembled system prompt
/admin/settings      the model endpoint, and the Last.fm / MusicBrainz providers
```

Templates and `htmx.min.js` live in `spica/templates` and `spica/static`, so the
container serves them with no build step. Now playing polls every five seconds and the
ledger every ten — both only while the tab is visible — and the job table every three.

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

**Providers live beside it**, under the same *settings* page, and are saved to
`config/providers.json` (mode 600): the Last.fm key used for similar artists and the tag
backfill, and the MusicBrainz contact. The environment is only the default; the saved
values win, and the worker reads them on its next job.

**The library page answers "what kind of DJ is this?"** It shows which offer each station
is using — the record table or the artist shelf — the last pick's path and measured
coverage, and one row per input (genres, popularity, similarity, analysis, loudness) with
the job that fills it. Rebuilds and mixer regeneration live there too.

The global prompt is the DJ's character — the voice it speaks in, which is also the line
listeners read under each song. It is prepended to every pick on every station; each
station's brief is added on top, and the mechanics of picking (sets rather than single
tracks, tools only, the JSON reply, and the shelf or table the answer has to come from)
the JSON contract and the legality checks stay in the code so an edit cannot let an
illegal track air. The voice lives in `config/dj-persona.md`, and the two path
instructions in `config/dj-shelf.md` and `config/dj-table.md`; with no file the built-in
text is used, and each *restore* button deletes the override. The *DJ Persona* page
shows all three editable blocks beside the complete system prompt for a chosen station,
so what the model is actually told is visible.

The genres pane is the operator's whole tool for taxonomy: tick any number of genres and
merge them into one name (rows move, a rule is recorded, pools follow); or open
*explore*, pick genres, and read the tracks under them, each with every genre it really
carries and where each came from. Ticked tracks can be given a genre by hand or have one
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

### Getting the logo into a player

Nothing discovers a logo by itself, so it has to be handed over. Two ways are built in:

- **`/stations.m3u`** (every station) and **`/<station>.m3u`** (one) — playlists carrying
  `tvg-logo` and the `/listen/<station>` URL. Kodi's IPTV Simple Client, TiviMate and the
  rest read the name, picture and stream straight out of the file.
- **`icy-url`** — with `SPICA_PUBLIC_URL` set, each stream advertises its own page
  (`icy-url: https://…/s/<station>`). It is a page, not an image, but clients that fetch
  a stream's homepage for its favicon have something to find.

Directories are the third way and the only one that reaches clients you never touch:
radio-browser.info takes a station record with a `homepage` and a `favicon`, and that
favicon field is where `/logo/<station>.png` goes. Submit the `/listen/<station>` URL
rather than `/<station>.mp3` — a mount answers 404 while nobody is listening, because the
encoder starts on demand.

## Listening elsewhere

The browser player streams through `https://<host>/listen/<station>`, which is also the
URL to hand to VLC, Music Assistant, a Sonos or a car head unit. It opens the mount if it
is closed, waits for Icecast, then copies the stream, so the client sees an ordinary
always-on radio URL instead of a 404.

A station's mount closes after `IDLE_STOP_S` (90 s) with nobody reading it — the station
itself keeps playing — which is why the relay exists. The rules for "nobody":

| client | counts as listening | stops counting |
|---|---|---|
| this web player | while it heartbeats (every 20 s) and the audio is advancing | 60 s after the last beat, or 45 s after the audio stops moving |
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
| GET | `/stations.m3u`, `/<station>.m3u` | M3U playlists with `tvg-logo`, for Kodi, TiviMate and friends |
| GET | `/coverage` | how much the DJ knows: genres, popularity and similarity, plus analysis and loudness when on |
| GET | `/genres` | genres with counts and sources, plus the raw tags behind them |
| GET | `/aliases` | operator rules in force and the builtin table |
| GET | `/similar?artist=X` | similarity chain result, says which level answered |
| GET | `/requests/<station>` | requests the station still remembers, what each produced, and how long ago |
| POST | `/pick/<station>` | force a DJ pick now; it takes the waiting slot and the pick it displaces goes to the top of the pool |
| POST | `/request` | `{"text", "station", "songs"}`: record a request and air it next; the pick already waiting is dropped and chosen again in the new mood |
| POST | `/rebuild` | re-index Navidrome and rewrite every pool |
| POST | `/liq` | render `spica.liq` from `stations.json` |
| POST | `/enrich?limit=N` | fill artist MBIDs, 0 for all |
| POST | `/s/<station>/start?stream=<codec>` | open a stream's mount (what the play button calls) |
| GET | `/s/<station>/state?stream=<codec>` | live/presence json, per stream and in total |
| GET | `/listen/<station>` | the default stream, relayed and opened on demand |
| GET | `/listen/<station>/<codec>` | one stream: `mp3`, `opus` or `flac` |
| POST | `/s/<station>/hear` | the web player saying it is still playing (204) |
| POST | `/jobs` | `{"kind": "genre_tags" \| "popularity" \| "similarity" \| "musicbrainz" \| "refresh" \| "analysis" \| "loudness"}` |
| GET | `/jobs` | recent jobs with progress |
| DELETE | `/requests/<station>` | clear the station's live requests |

## Configuration

Everything deployment-specific comes from the environment; `deploy/.env.example` lists
the keys, and that is the file to start from.

| Variable | Meaning |
|---|---|
| `SPICA_TAG` | the image tag to run, e.g. `0.15.2` |
| `PUID`, `PGID` | the user that owns `config/` and `state/` (default `1000`) |
| `TZ` | the container timezone, for logs and the ledger's server fallback |
| `ICECAST_SOURCE_PASSWORD` | what the mixer publishes with; written into `icecast.xml` on first run |
| `ICECAST_ADMIN_PASSWORD` | Icecast's own `/admin` pages |
| `SPICA_ADMIN_PASSWORD` | admin login for the web UI; empty disables admin writes |
| `SPICA_DJ_MODE` | default DJ mode when `config/dj.json` has not chosen one: `off`, `artist` or `track` (default `track`) |
| `SPICA_HOSTNAME` | the host Icecast puts in its stream URLs |
| `SPICA_LOCATION`, `SPICA_ADMIN_EMAIL` | the `<location>` and `<admin>` Icecast advertises |
| `ICECAST_PORT`, `SPICA_PORT` | the two published ports (default `8000` audio, `8030` web) |
| `HOST`, `PORT` | where the app binds inside the container (default `0.0.0.0:8030`) |
| `NAVIDROME_URL` | Navidrome base URL as the containers reach it |
| `NAVIDROME_USER`, `NAVIDROME_PASS` | a Navidrome account for the radio |
| `LLM_BASE`, `LLM_KEY`, `LLM_MODEL` | defaults for the endpoint; the admin's settings page wins |
| `LLM_HEADERS` | optional extra headers, `k=v` comma separated |
| `LASTFM_KEY` | optional: name-based similarity and tag backfill; the admin's settings page wins |
| `MB_USER_AGENT` | optional: contact string for MusicBrainz; the admin's settings page wins |
| `SPICA_ANALYSIS` | optional: `1` turns on audio analysis in the worker; the worker must run the `-analysis` image (see [deploy/README.md](deploy/README.md#audio-analysis-and-loudness)) |
| `SPICA_ANALYSIS_TEMPO` | `degara` (fast, default) or `multifeature` (more accurate, ~3× CPU) |
| `SPICA_ANALYSIS_SLICE` | tracks analysed per job (default `200`); other jobs interleave |
| `SPICA_ANALYSIS_WORKERS` | processes for one analysis job (default `2`); each needs ~0.5 GB |
| `SPICA_ANALYSIS_BATCH` | mel patches per inference (default `32`); raise it only with RAM to spare |
| `SPICA_ANALYSIS_THREADS` | ONNX threads per analysis process (default `2`) |
| `SPICA_ANALYSIS_MAX_SECONDS` | how much of each track to analyse (default `240`; `0` = all) |
| `SPICA_ANALYSIS_DEEP` | also compute loudness, classic danceability, dynamic complexity (off by default; nothing reads them yet) |
| `SPICA_ANALYSIS_TRANSCODE` | `1` streams a transcode instead of the original (default off: CPU beats bandwidth when Navidrome is local) |
| `SPICA_ANALYSIS_BITRATE` | bitrate of that transcode (default `96`) |
| `SPICA_LOUDNESS` | per-track ReplayGain normalisation; `0` turns it off (on by default) |
| `SPICA_LOUDNESS_TARGET` | target integrated loudness for the gain (default `-14` LUFS) |
| `SPICA_LOUDNESS_MAX_BOOST` | largest lift, and only with a known peak (default `6` dB) |
| `SPICA_LOUDNESS_MAX_CUT` | largest cut (default `12` dB) |
| `SPICA_LOUDNESS_PEAK_CEILING` | a lift may not push the sample peak past this (default `-1` dBFS) |
| `SPICA_LOUDNESS_MEASURE` | measure tracks with no ReplayGain tag (default on; `0` = tag-only) |
| `SPICA_LOUDNESS_SLICE` | tracks measured per job slice (default `200`) |
| `SPICA_LOUDNESS_WORKERS` | concurrent ffmpeg workers (default `2`; `0` = min(2, cores)) |
| `SPICA_LOUDNESS_TIMEOUT` | per-track ffmpeg timeout in seconds (default `300`) |
| `SPICA_STATE`, `SPICA_CONFIG` | where state and config live inside the container (`/state`, `/config`) |
| `ICECAST_URL` | Icecast base URL as the picker sees it (`http://icecast:8000`) |
| `SPICA_PUBLIC_URL` | optional: the address listeners use, for the stream's `icy-url` |
| `ICECAST_OUTPUT_HOST`, `ICECAST_OUTPUT_PORT` | where the mixer publishes — separate from `ICECAST_URL` because the two containers see Icecast at different addresses |
| `ICECAST_POLL_S` | how often the status page is read into the snapshot the page, `/status` and the DJ share (default `2`) |
| `QUEUE_TARGET` | how far ahead of the mixer the picker keeps a listened-to station (default `1`) |
| `POLL_S` | how often the picker looks at the mixer and the ledger (default `20`) |
| `PICK_COOLDOWN` | seconds between DJ picks when Liquidsoap's control server cannot answer (default `240`) |
| `KEEP_AHEAD` | requests left waiting while one plays (default `1`) |
| `IDLE_STOP_S` | close a mount nobody is reading after this long; the station itself keeps playing (default `90`) |
| `SPICA_MIN_TRACK` | shortest track a station will play, in seconds (default `30`; `0` allows gaps and interludes) |
| `SPICA_MB_GENRES_DAYS` | how long a MusicBrainz genre miss stands before the artist is asked again (default `30`) |
| `REFRESH_CHECK_S` | how often the worker compares the index with Navidrome's own count (default `900`) |
| `SPICA_LIQ` | where the generated Liquidsoap script is written (default `config/spica.liq`) |
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
popularity for tracks that have none, scored similar artists for artists with no
neighbours cached, and MusicBrainz for artists without genres. The jobs are also on the
admin's library page. Operator merges and the alias maps are applied at rebuild time, so
a rebuild also re-canonicalises every genre.

## State

`state/` holds everything that survives a restart:

- `index.db`: SQLite projection of the library. Disposable, rebuilt from Navidrome.
- `analysis.db` and `models/`: the optional audio analysis — per-track vectors, styles,
  tempo, key and loudness, one vector per artist, and the ONNX weights that produced
  them. Its own file because the index is disposable and this is hours of CPU:
  deleting it only costs a re-analysis.
- `loudness.db`: measured integrated loudness and sample peak for tracks whose files
  carry no ReplayGain tag. Also separate from the index for the same reason; deleting
  it only costs the measurement job again.
- `pools/<station>.m3u`: what the station plays when nobody is steering; one stream URL
  per line.
- `plans/<station>.json`: the set being played out — its line, its ids, and the shelf or
  table artists it was chosen from.
- `queue/<station>.txt`: the single-file hand-off between picker and mixer.
- `now/<station>.txt` and `.offset`: the mixer's own record of each track it starts, and
  how far the picker has read. This is what keeps the ledger honest when no mount is open.
- `history/<station>.jsonl`: append-only play log, written by this project only.
- `history/<station>.picks.jsonl`: the DJ's choices with their reasons, the presence
  that justified them, the shelf or table artists it was offered, the path it took
  (`table`, `table-partial` or `shelf`) with the analysis coverage it saw, and — for
  picks recorded from here on — the brief it read, the tools it called and the reply it
  gave. The last 500 are kept.
- `history/requests/<station>.jsonl`: live listener requests.
- `art/<albumId>`: cached cover art, one file per album.
- `mb-genres.json`: MusicBrainz's own genre vocabulary — the list a provider tag has to
  be on to become a genre. Cached for a month; deleting it costs twenty-three throttled
  calls to rebuild.
- `genre-aliases.json`: operator merge rules, empty by default.
- `logs/`: worker and job output.

Rebuilding the index wipes `index.db` content, not the other files, so history and
requests and the analysis survive it.

## Contributing

`main` is release-only: changes arrive as squashed pull requests. The loop, the commit
style and how to cut a release are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
