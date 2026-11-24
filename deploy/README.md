# Running Spica

Four containers and one first-run step. You need Docker with the Compose plugin,
a music library served by Navidrome (or anything that speaks Subsonic), and about
200 MB of RAM.

## 1. Get the files

```sh
mkdir spica && cd spica
curl -O https://raw.githubusercontent.com/rxavier/spica/main/deploy/compose.yaml
curl -o .env https://raw.githubusercontent.com/rxavier/spica/main/deploy/.env.example
```

## 2. Fill in `.env`

This file is where your credentials live, and it sits next to `compose.yaml`, so
`chmod 600 .env`. Three things must change, and everything else has a working default:

```sh
# generate the three passwords
openssl rand -hex 16   # ICECAST_SOURCE_PASSWORD
openssl rand -hex 16   # ICECAST_ADMIN_PASSWORD
openssl rand -hex 16   # SPICA_ADMIN_PASSWORD
```

Then set `NAVIDROME_URL`, `NAVIDROME_USER` and `NAVIDROME_PASS`, and `TZ`.

`.env` is not quite the only place a credential lives: the radio's Subsonic token is
derived from a salt kept in `state/` and never expires, so `state/` is worth as much as
`.env` — see the credentials list in the main README.

`SPICA_HOSTNAME` matters if you will listen from another machine: it is the host
Icecast puts in its stream URLs.

## 3. Start it

```sh
docker compose up -d
docker compose logs -f picker
```

The first run writes `config/icecast.xml`, `config/stations.json` and
`config/spica.liq`, and starts one station called **Everything** (id `main`) that plays
the whole library. Nothing else is required.

Open `http://localhost:8030` for the radio, `http://localhost:8030/admin` for the
console (password: `SPICA_ADMIN_PASSWORD`), and give `http://localhost:8030/listen/main`
to VLC, Sonos or Music Assistant — that URL opens the stream on demand and survives a
bitrate change.

A station can publish MP3 (192), Opus (128) and FLAC, as switches under **Stations**
in the console. Each gets its own URL:

```
http://localhost:8030/listen/main          the default stream
http://localhost:8030/listen/main/opus     one by codec
http://localhost:8030/listen/main/flac
```

## 4. Teach it your library

In the admin:

1. **Library** → *Recheck the library now*. It pages your Navidrome library into a
   local index; a few seconds per thousand tracks.
2. **Genres** → wait for a *Genres from Last.fm* or *MusicBrainz genres* job, then
   merge and rename labels until the list makes sense to you.
3. **Stations** → edit **Everything** or add another, tick the genres it should
   play, and add artists that walk outside them.
4. **Library** → a *Similar artists (scored)* job fills the neighbour graph the DJ's
   shelf uses to keep an artist off the next set when something close to it just played.
   It needs `LASTFM_KEY`; without one the shelf simply does not exclude neighbours.
5. **Settings** → an OpenAI-compatible endpoint, if you want the DJ. Without one
   the stations still play, in order, with no picks and no reasons.

## Audio analysis and loudness

The picker image is the picker's half: numpy, to read vectors, and nothing else. It is
built on Alpine (~200 MB) for amd64 and arm64. The worker runs a second image, the same
Alpine base plus ffmpeg (~330 MB), because measuring ReplayGain-less tracks is what
needs a decoder; it is tagged `-worker`. The analyser image must be Debian (glibc),
because onnxruntime and essentia publish manylinux wheels only, and it is amd64-only
because essentia has no Linux arm64 wheel. It also carries ffmpeg, for the loudness job.
On an arm64 host, add `platform: linux/amd64` under the worker in
`compose.analysis.yaml` and enable amd64 emulation; analysis will be slower. With
analysis off, the worker and picker run natively. Analysis runs from the `-analysis`
image:

```sh
# .env
SPICA_TAG=0.15.3
SPICA_ANALYSIS=1

docker compose -f compose.yaml -f compose.analysis.yaml up -d
```

`compose.analysis.yaml` only swaps the worker's image to
`ghcr.io/rxavier/spica:<tag>-analysis`. The picker, init, Icecast and Liquidsoap keep
the lean image. On first use the worker downloads the model weights into
`state/models/` (~40 MB, CC BY-NC, not redistributable in an image) and starts the
backfill; `/coverage` shows progress per station.

If you build locally, build three:

```sh
docker build -t ghcr.io/rxavier/spica:dev .
docker build --target worker -t ghcr.io/rxavier/spica:dev-worker .
docker build --platform linux/amd64 --build-arg BASE=python:3.14-slim \
  --build-arg SPICA_EXTRAS=analysis --build-arg SPICA_RUNTIME_DEPS="libatomic1 ffmpeg" \
  -t ghcr.io/rxavier/spica:dev-analysis .
```

With a local clone, set `SPICA_TAG=dev` so compose picks `dev-worker` (and
`dev-analysis` under the overlay).

## Building the image yourself

`compose.yaml` runs `ghcr.io/rxavier/spica:0.15.3` for the picker and init, and
`ghcr.io/rxavier/spica:0.15.3-worker` for the worker. To build from source instead,
replace the `image:` lines in `init`, `picker` and `worker` with:

```yaml
    build: https://github.com/rxavier/spica.git#v0.15.3
```

and give the worker a `build.target: worker` (or use the local build commands in
[Audio analysis and loudness](#audio-analysis-and-loudness) above).

With a local clone, use the three `dev` builds from
[Audio analysis and loudness](#audio-analysis-and-loudness) above, then set
`SPICA_TAG=dev` so compose picks `dev`, `dev-worker` and (under the overlay)
`dev-analysis`. Leave the worker on the overlay's image rather than adding a `build:`
stanza. The other services keep the lean image.

## Behind a reverse proxy

Spica speaks plain HTTP: `:8030` for the app, `:8000` for audio. Caddy, nginx and
Traefik all work. Two things to know:

- The stream needs buffering off (`flush_interval -1` in Caddy, `proxy_buffering off`
  in nginx), or players stall after half a minute.
- If the proxy is on the same Docker network, give the picker
  `ICECAST_URL=http://icecast:8000` and let the proxy reach `picker:8030` — the
  published ports are only a convenience.

Audio at `:8000` is what players use; the web UI only needs `:8030`.

## Ports and data

| path | what it holds |
|---|---|
| `config/` | stations, Icecast, the generated Liquidsoap script, the DJ's persona |
| `state/` | the library index, pools, play history, queue, cached cover art |

Both are bind mounts, so a backup is `tar` of those two directories. `state/` also
holds `index.db`, which is rebuilt from Navidrome at any time with *Recheck the
library now*; `config/` is the part worth keeping.

## Stopping

`docker compose down` stops everything and keeps your data. Stations go off air
when the containers stop; nothing is written to your music library, ever.
