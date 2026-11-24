"""Optional audio analysis: what a track actually sounds like.

Off unless two things are true: the `analysis` extra is installed (numpy,
onnxruntime, essentia) and `SPICA_ANALYSIS` is on for the worker. With either
missing, nothing here runs, no models are downloaded, no jobs are queued, and the
picker behaves exactly as it did before this module existed.

What it computes, per track, from a 96 kbit/s MP3 streamed out of the Subsonic API:

- a mean-pooled 1280-d Discogs-EffNet embedding — the "sounds like" vector, used
  for track and artist similarity;
- the 400 Discogs style probabilities the same network outputs (the readable
  vocabulary the DJ gets);
- ten small classification heads on that embedding (moods, danceability, timbre,
  voice/instrumental);
- DSP that no model can give: tempo with confidence, key and scale, EBU R128
  loudness and range, dynamic complexity.

The ONNX path is verified equal to essentia's own TensorFlow wrapper: same mel
front-end (TensorflowInputMusiCNN's parameters, reimplemented in numpy), same
patched input, per-patch cosine 1.0000 against the reference. The TensorFlow
wheel is only ever needed to re-run that validation, never in production.

Results live in their own `state/analysis.db`: the index is disposable and gets
rebuilt, the analysis is an hour of CPU and should not be thrown away with it.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import struct
import time
import urllib.parse
from pathlib import Path

from . import config, db, navidrome
from .queries import analysis as analysis_queries

try:  # the extra; a plain install runs everything else without it
    import numpy as np
except ImportError:  # pragma: no cover - depends on how the image was built
    np = None

# Bump when the vector or the stored fields change: rows under an older version are
# re-analyzed rather than silently mixed with the new ones.
MODEL_VERSION = "effnet-bsdynamic-2/heads-1"

BASE_URL = "https://essentia.upf.edu/models"
HEAD_NAMES = (
    "mood_aggressive", "mood_happy", "mood_sad", "mood_relaxed", "mood_party",
    "mood_acoustic", "mood_electronic", "danceability", "timbre", "voice_instrumental",
)
EFFNET = "discogs-effnet-bsdynamic-1"

# The front-end parameters are essentia's, hardcoded there to match training
# (TensorflowInputMusiCNN): 16 kHz mono, 512/256 frames, 96 slaney-mel bands,
# log10(1 + 10000 * power), then patches of 128 frames every 62 with the last
# partial patch dropped.
SAMPLE_RATE = 16000
FRAME = 512
HOP = 256
BANDS = 96
FMAX = SAMPLE_RATE / 2
PATCH = 128
PATCH_HOP = 62


# ------------------------------------------------------------------ availability

def readable() -> bool:
    """Can this process read the store? The picker needs only numpy for vectors."""
    return np is not None


def installed() -> bool:
    """Can this process write the store? The analyser needs the full extra."""
    return np is not None and all(
        importlib.util.find_spec(mod) for mod in ("onnxruntime", "essentia")
    )


def enabled() -> bool:
    """Analysis is on only when the operator asked for it and vectors can be read.

    The picker runs the lean image (numpy); the analyser runs the analysis image.
    Requiring the full extra here made the picker carry onnxruntime and essentia for
    a table it only reads vectors for.
    """
    return bool(config.ANALYSIS) and readable()


def store_path() -> Path:
    return config.STATE_DIR / "analysis.db"


# ----------------------------------------------------------------------- schema




def connect(path: Path | None = None, *, create: bool = False,
            readonly: bool = False) -> sqlite3.Connection | None:
    """Open the analysis store, or None when it does not exist yet.

    The picker only ever reads: a missing file means "analysis never ran", which is
    the normal state of an installation that did not turn it on.
    """
    path = Path(path or store_path())
    if readonly and not path.exists():
        return None
    if not create and not path.exists():
        return None
    conn = db.open_db(path, readonly=readonly, timeout=10 if readonly else 30)
    if create:
        analysis_queries.ensure_schema(conn)
    return conn


# ------------------------------------------------------------------ model files

def model_files() -> dict[str, str]:
    """filename -> URL. Small heads first, the two big graphs last."""
    out = {}
    out[f"{EFFNET}.onnx"] = f"{BASE_URL}/feature-extractors/discogs-effnet/{EFFNET}.onnx"
    out[f"{EFFNET}.json"] = f"{BASE_URL}/feature-extractors/discogs-effnet/{EFFNET}.json"
    for head in HEAD_NAMES:
        name = f"{head}-discogs-effnet-1"
        out[f"{name}.onnx"] = f"{BASE_URL}/classification-heads/{head}/{name}.onnx"
        out[f"{name}.json"] = f"{BASE_URL}/classification-heads/{head}/{name}.json"
    return out


NOTICE = """\
The model weights in this directory were made by the Music Technology Group,
Universitat Pompeu Fabra, and published with Essentia (https://essentia.upf.edu/models).
They are licensed CC BY-NC-SA 4.0 (the LICENSE file shipped with the models carries
the CC BY-NC-ND 4.0 text); commercial use needs a licence from MTG. Spica downloads
them on first use and does not redistribute them.
"""


def ensure_models(models_dir: Path, log=print) -> Path:
    """Download whatever is missing. Atomic per file, so a killed start leaves no
    half-written graph behind."""
    import httpx

    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    notice = models_dir / "NOTICE.txt"
    if not notice.exists():
        notice.write_text(NOTICE, encoding="utf-8")
    missing = [(name, url) for name, url in model_files().items()
               if not (models_dir / name).exists()]
    if not missing:
        return models_dir
    log(f"analysis: downloading {len(missing)} model file(s) to {models_dir}")
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for name, url in missing:
            target = models_dir / name
            tmp = target.with_suffix(target.suffix + ".part")
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in response.iter_bytes(65536):
                        fh.write(chunk)
            tmp.replace(target)
    log("analysis: models ready")
    return models_dir


# ----------------------------------------------------------------- the front-end

def frames(audio):
    """Streaming FrameCutter(512, 256) with startFromZero=False, as the wrapper does:
    the first frame is zero-centred and the last one is zero-padded."""
    n = len(audio)
    n_frames = 1 + int(np.ceil((n - FRAME / 2) / HOP))
    padded = np.concatenate([np.zeros(FRAME // 2, np.float32), audio])
    out = np.zeros((n_frames, FRAME), np.float32)
    for k in range(n_frames):
        chunk = padded[k * HOP : k * HOP + FRAME]
        out[k, : len(chunk)] = chunk
    return out


def _hz2mel(hz):
    hz = np.asarray(hz, dtype=np.float64)
    min_log_hz, lin_slope = 1000.0, 3.0 / 200.0
    min_log_mel = min_log_hz * lin_slope
    log_step = np.log(6.4) / 27.0
    return np.where(
        hz < min_log_hz,
        hz * lin_slope,
        min_log_mel + np.log(np.maximum(hz, 1e-12) / min_log_hz) / log_step,
    )


def _mel2hz(mel):
    mel = np.asarray(mel, dtype=np.float64)
    min_log_hz, lin_slope = 1000.0, 3.0 / 200.0
    min_log_mel = min_log_hz * lin_slope
    log_step = np.log(6.4) / 27.0
    return np.where(
        mel < min_log_mel,
        mel / lin_slope,
        min_log_hz * np.exp((mel - min_log_mel) * log_step),
    )


def _mel_matrix():
    """MelBands + TriangularBands exactly: slaney-spaced edges, triangles in Hz,
    unit_tri area, only bins whose centres fall inside the triangle."""
    n_bins = FRAME // 2 + 1
    scale = (SAMPLE_RATE / 2.0) / (n_bins - 1)
    edges = _mel2hz(np.linspace(_hz2mel(0.0), _hz2mel(FMAX), BANDS + 2))
    mat = np.zeros((n_bins, BANDS), np.float64)
    for i in range(BANDS):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        jbegin = max(0, int(np.ceil(lo / scale)))
        jend = min(n_bins - 1, int(np.floor(hi / scale)))
        for j in range(jbegin, jend + 1):
            f = j * scale
            weight = (f - lo) / (mid - lo) if f < mid else (hi - f) / (hi - mid)
            mat[j, i] = weight / ((hi - lo) / 2.0)
    return mat


_MEL = None
_WINDOW = None


def mel_patches(audio):
    """16 kHz mono -> patches of [1, 128, 96] log-mels (as the ONNX graph wants)."""
    global _MEL, _WINDOW
    if _MEL is None:
        _MEL = _mel_matrix()
        _WINDOW = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(FRAME) / (FRAME - 1.0))).astype(np.float32)
    power = np.abs(np.fft.rfft(frames(audio) * _WINDOW, axis=1)) ** 2  # MelBands type=power
    bands = np.log10(np.maximum(10000.0 * (power @ _MEL) + 1.0, 1e-30)).astype(np.float32)
    count = (len(bands) - PATCH) // PATCH_HOP + 1
    if count < 1:
        return np.zeros((0, PATCH, BANDS), np.float32)
    return np.stack([bands[i * PATCH_HOP : i * PATCH_HOP + PATCH] for i in range(count)])


# ------------------------------------------------------------------ the analyser

class AnalysisUnavailable(RuntimeError):
    """The extra is not installed; the job says so instead of crashing the worker."""


def _positive(classes: list[str], head: str) -> int:
    """The index of the class that means "yes" for this head.

    The model metadata names them (e.g. ["aggressive", "non_aggressive"]); when a
    build omits the metadata, the positive class is assumed first, which is how
    these heads were exported.
    """
    if not classes:
        return 0
    wanted = head.split("_")[-1]
    for i, name in enumerate(classes):
        folded = name.casefold()
        if folded == wanted:
            return i
    for i, name in enumerate(classes):
        folded = name.casefold()
        if not folded.startswith(("non", "not", "no_")):
            return i
    return 0


class Analyzer:
    """One process's sessions. Construct once, analyze many: loading is seconds."""

    def __init__(self, models_dir: Path, threads: int = 1):
        if not installed():
            raise AnalysisUnavailable(
                "audio analysis needs the `analysis` extra (numpy, onnxruntime, essentia)"
            )
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(threads))
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._options = options
        self._providers = ["CPUExecutionProvider"]
        self.models_dir = Path(models_dir)
        self._sessions: dict[str, object] = {}
        self.effnet = self._session(f"{EFFNET}.onnx")
        self.heads = {h: self._session(f"{h}-discogs-effnet-1.onnx") for h in HEAD_NAMES}
        self._classes = {h: self._metadata(f"{h}-discogs-effnet-1.json") for h in HEAD_NAMES}
        meta = self._metadata(f"{EFFNET}.json")
        self.styles = meta.get("classes") or []
        # essentia's DSP is single-threaded C++; imported here, not at module level.
        from essentia.standard import (
            Danceability, DynamicComplexity, KeyExtractor, LoudnessEBUR128,
            MonoLoader, RhythmExtractor2013,
        )
        self._loader, self._rhythm, self._key = MonoLoader, RhythmExtractor2013, KeyExtractor
        self._loudness, self._dance, self._complexity = (
            LoudnessEBUR128, Danceability, DynamicComplexity,
        )

    def _session(self, name: str):
        import onnxruntime as ort

        if name not in self._sessions:
            self._sessions[name] = ort.InferenceSession(
                str(self.models_dir / name), self._options, providers=self._providers
            )
        return self._sessions[name]

    def _metadata(self, name: str) -> dict:
        try:
            return json.loads((self.models_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -------------------------------------------------------------- one track

    def analyze(self, path: Path) -> dict:
        """Everything the store keeps for one file, or an exception the job can log."""
        method = config.ANALYSIS_TEMPO
        mono44 = self._loader(
            filename=str(path), sampleRate=44100, resampleQuality=4
        )()
        audio16 = self._loader(
            filename=str(path), sampleRate=SAMPLE_RATE, resampleQuality=4
        )()
        out: dict = {"duration": round(len(audio16) / SAMPLE_RATE, 2),
                     "model_version": MODEL_VERSION}
        # A cap on what the models hear. The embedding is a mean over 2 s patches,
        # so the tail of a 20-minute track adds cost, not information; tempo and key
        # are settled well inside four minutes too. Duration stays the real one.
        cap = max(0, int(config.ANALYSIS_MAX_SECONDS or 0))
        if cap:
            mono44 = mono44[: cap * 44100]
            audio16 = audio16[: cap * SAMPLE_RATE]
        deep = bool(config.ANALYSIS_DEEP)

        try:
            bpm, _beats, confidence, _estimates, _intervals = self._rhythm(
                method=method)(mono44)
            out["bpm"], out["bpm_confidence"] = float(bpm), float(confidence)
        except Exception as exc:  # a tempo estimate is not worth failing the track
            out["bpm_error"] = str(exc)[:120]

        try:
            key, scale, strength = self._key()(mono44)
            out["key"], out["key_scale"], out["key_strength"] = key, scale, float(strength)
        except Exception as exc:
            out["key_error"] = str(exc)[:120]

        if deep:
            # Nothing in the picker reads these (energy comes from the trained heads),
            # and together they are a quarter of the pass — so they are opt-in.
            try:
                # Loudness on a mono signal pair: within ~1 dB of the stereo figure and
                # it saves a third decode. Loudness is stored as loudness, never as energy.
                _m, _s, integrated, lra = self._loudness(sampleRate=44100)(
                    np.stack([mono44, mono44], axis=1))
                out["lufs"], out["lra"] = float(integrated), float(lra)
            except Exception as exc:
                out["lufs_error"] = str(exc)[:120]

            try:
                complexity, _loudness = self._complexity(sampleRate=44100)(mono44)
                out["dyn_complexity"] = float(complexity)
            except Exception:
                pass

            try:
                dance, _dfa = self._dance()(mono44)
                out["danceability"] = float(dance)
            except Exception:
                pass

        patches = mel_patches(audio16)
        if len(patches):
            # The batch is a memory knob, not a speed one: the activation tensors grow
            # with it and ORT keeps the arena at its peak. 32 keeps a worker near 0.5 GB.
            batch = max(1, int(config.ANALYSIS_BATCH))
            embeddings, activations = [], []
            for start in range(0, len(patches), batch):
                emb, act = self.effnet.run(
                    ["embeddings", "activations"],
                    {"melspectrogram": patches[start : start + batch]},
                )
                embeddings.append(emb)
                activations.append(act)
            emb = np.concatenate(embeddings)
            act = np.concatenate(activations)
            out["vec"] = emb.mean(0).astype(np.float32)
            mean_styles = act.mean(0)
            order = np.argsort(-mean_styles)[:3]
            # The full Discogs label is kept ("Electronic---House"): the parent is the
            # family the table groups by, the leaf is what the DJ reads. Rendering
            # strips it; storing it does not.
            out["styles"] = [
                (self.styles[i], round(float(mean_styles[i]), 3))
                for i in order if i < len(self.styles)
            ]
            heads = {}
            for name, session in self.heads.items():
                probs = session.run(["activations"], {"embeddings": emb})[0]
                heads[name] = float(probs[:, _positive(self._classes[name].get("classes"), name)].mean())
            out["heads"] = heads
            out["vocal"] = 0 if heads.get("voice_instrumental", 0.5) >= 0.5 else 1
            out["energy"] = round(_energy(heads), 4)
        else:
            out["vec"] = np.zeros(0, np.float32)
            out["styles"], out["heads"] = [], {}
        return out


def _energy(heads: dict) -> float:
    """A single 0–1 axis for sequencing, from the trained heads.

    v1 heuristic: arousal is what the aggressive/party/danceable heads capture and
    "calm" is what acoustic/relaxed capture. The raw heads are stored beside it so
    this can be re-derived later without re-analysing anything.
    """
    def h(name):
        return float(heads.get(name, 0.0))
    value = (h("mood_aggressive") + h("mood_party") + h("danceability")
             + (1.0 - h("mood_acoustic")) + (1.0 - h("mood_relaxed"))) / 5.0
    return min(1.0, max(0.0, value))


# ------------------------------------------------------------------- the store

def save(conn: sqlite3.Connection, track_id: str, artist_key: str, data: dict) -> None:
    vec = data.get("vec")
    blob = np.asarray(vec, np.float32).tobytes() if vec is not None and len(vec) else b""
    with conn:
        analysis_queries.insert(conn, (
            track_id, artist_key, data.get("duration"), data.get("bpm"),
            data.get("bpm_confidence"), data.get("key"), data.get("key_scale"),
            data.get("key_strength"), data.get("lufs"), data.get("lra"),
            data.get("dyn_complexity"), data.get("danceability"), data.get("energy"),
            json.dumps(data.get("styles") or []), json.dumps(data.get("heads") or {}),
            data.get("vocal"), blob, data.get("model_version") or MODEL_VERSION,
            int(time.time()),
        ))


def todo(index_conn: sqlite3.Connection, store: sqlite3.Connection,
         limit: int) -> list[sqlite3.Row]:
    """Unanalysed tracks, spread across artists, newest arrivals first within each.

    Walking the album order leaves a half-finished backfill clustered on the first
    letters of the alphabet, and a table drawn from that is a different station. One
    track per artist first means 20% coverage still covers every artist; within an
    artist, a fresh import is measured before the rest of the catalogue.
    """
    done = analysis_queries.track_ids(store, MODEL_VERSION)
    by_artist: dict[str, list] = {}
    for row in analysis_queries.candidate_rows(index_conn):
        if row["id"] in done:
            continue
        key = navidrome.name_key(row["album_artist"] or row["artist"] or "")
        by_artist.setdefault(key, []).append(row)
    for items in by_artist.values():
        items.sort(key=lambda r: (-(r["fetched_at"] or 0), r["id"]))

    ordered: list = []
    depth = 0
    while len(ordered) < limit:
        added = False
        for items in by_artist.values():
            if depth < len(items):
                ordered.append(items[depth])
                added = True
                if len(ordered) >= limit:
                    return ordered
        if not added:
            break
        depth += 1
    return ordered


def backlog(index_conn: sqlite3.Connection, store: sqlite3.Connection | None) -> int:
    """How many tracks still need analysis (0 when the store does not exist yet)."""
    if store is None:
        return analysis_queries.playable_count(index_conn)
    return len(todo(index_conn, store, limit=1_000_000))


def coverage(index_conn: sqlite3.Connection, store: sqlite3.Connection) -> dict:
    """How much of *this* library has been analysed, for the library page.

    The store outlives the index — that is the point of keeping it — so its row count is
    not the library's count: a deleted album leaves its vectors behind, and the page read
    101.3% (5,366 rows for 5,296 tracks). Count the ids that are still in the index, the
    same way `todo()` and `jobs.coverage()` do.
    """
    total = analysis_queries.playable_count(index_conn)
    analysed = analysis_queries.track_ids(store, MODEL_VERSION)
    present = analysis_queries.playable_ids(index_conn)
    known = len(analysed & present)
    return db.coverage(known, total)


def station_coverage(index_conn: sqlite3.Connection, store: sqlite3.Connection) -> dict:
    """Per station, how much of what it can play has been analysed.

    Readiness is decided per station, not per library: a narrow prog station can be
    fully analysed while the rest of the collection is not.
    """
    known = analysis_queries.track_ids(store, MODEL_VERSION)
    out = {}
    for station in config.load_stations():
        rows = navidrome.rows_for_station(index_conn, station)
        total = len(rows)
        got = sum(1 for row in rows if row["id"] in known)
        out[station.id] = {
            "known": got, "total": total,
            "percent": round(got * 100.0 / total, 1) if total else 0.0,
        }
    return out


def coverage_for(index_conn: sqlite3.Connection) -> dict | None:
    """Analysis coverage for the library page, or None when the feature is off."""
    if not enabled():
        return None
    store = connect(readonly=True)
    if store is None:
        return None
    try:
        out = coverage(index_conn, store)
        out["stations"] = station_coverage(index_conn, store)
        return out
    finally:
        store.close()


def refresh_artists(index_conn: sqlite3.Connection, store: sqlite3.Connection) -> int:
    """Recompute one vector per artist: the mean of its tracks' unit vectors.

    Similarity is then artist-to-artist in the same space the tracks live in, which
    is what the shelf's "sounds too like what is playing" exclusion needs — and it
    can only ever name artists this library owns.
    """
    vectors: dict[str, list] = {}
    names: dict[str, str] = {}
    for row in analysis_queries.candidate_rows(index_conn):
        name = row["album_artist"] or row["artist"] or ""
        if not name:
            continue
        key = navidrome.name_key(name)
        names.setdefault(key, name)
        vectors.setdefault(key, [])
    # Joined in Python: the two stores are separate files on purpose.
    by_track = {
        row["track_id"]: row
        for row in analysis_queries.artist_track_rows(store, MODEL_VERSION)
    }
    for row in analysis_queries.candidate_rows(index_conn):
        hit = by_track.get(row["id"])
        if hit is None or not hit["vec"]:
            continue
        name = row["album_artist"] or row["artist"] or ""
        key = navidrome.name_key(name)
        if not key:
            continue
        vectors.setdefault(key, []).append((
            np.frombuffer(hit["vec"], np.float32),
            hit["bpm"], hit["energy"], json.loads(hit["styles"] or "[]"),
        ))

    written = 0
    now = int(time.time())
    with store:
        analysis_queries.clear_artists(store)
        for key, items in vectors.items():
            if not items:
                continue
            matrix = np.stack([unit(v) for v, _b, _e, _s in items])
            vector = unit(matrix.mean(0, keepdims=True))[0]
            bpms = [b for _v, b, _e, _s in items if b]
            energies = [e for _v, _b, e, _s in items if e is not None]
            counts: dict[str, float] = {}
            for _v, _b, _e, styles in items:
                for name, prob in styles:
                    counts[name] = counts.get(name, 0.0) + float(prob)
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
            analysis_queries.insert_artist(
                store,
                (key, names.get(key, ""), vector.astype(np.float32).tobytes(), len(items),
                 float(np.median(bpms)) if bpms else None,
                 float(np.mean(energies)) if energies else None,
                 json.dumps([[n, round(p, 3)] for n, p in top]), now),
            )
            written += 1
    return written


def unit(vector):
    """A unit vector, whichever way numpy handed it over."""
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


# ------------------------------------------------------------------- DJ helpers

def feature_map(store: sqlite3.Connection | None) -> dict[str, dict]:
    """Per-track scalars for tool rows — no embedding blobs, so it stays small."""
    if store is None:
        return {}
    rows = analysis_queries.feature_rows(store, MODEL_VERSION)
    out = {}
    for row in rows:
        styles = json.loads(row["styles"] or "[]")
        out[row["track_id"]] = {
            "bpm": row["bpm"],
            "confidence": row["bpm_confidence"],
            "key": key_label(row["key"], row["key_scale"]),
            "energy": row["energy"],
            "sound": sound_label(styles),
            "family": family_label(styles),
            "vocal": row["vocal"],
            "lufs": row["lufs"],
            "danceability": row["danceability"],
        }
    return out


def vector_map(store: sqlite3.Connection | None) -> dict:
    """track id -> unit embedding, for the table's distance work.

    Read once per picker process and cached there: it is ~5 KB per track, but it is
    also the only way to ask "what sounds like what" without a network call.
    """
    if store is None or np is None:
        return {}
    rows = analysis_queries.vector_rows(store, MODEL_VERSION)
    out = {}
    for row in rows:
        vector = np.frombuffer(row["vec"], np.float32)
        norm = float(np.linalg.norm(vector))
        if norm:
            out[row["track_id"]] = vector / norm
    return out


def tempo_label(bpm, energy) -> str:
    """Tempo as a hint, with the half-time reading when the feel says it is one.

    A calm track at "117 bpm" is usually a ~58-feel ballad the tracker locked onto
    at the subdivision. The energy heads know it is calm; saying both keeps the
    number from arguing with the record.
    """
    if not bpm:
        return ""
    text = f"{round(float(bpm))} bpm"
    if float(bpm) > 100 and energy is not None and float(energy) < 0.35:
        text += f" ({round(float(bpm) / 2)} half-time)"
    return text


def key_label(key: str | None, scale: str | None) -> str:
    """Essentia hands back "A" + "minor"; the DJ reads "A min"."""
    if not key:
        return ""
    scale = (scale or "").casefold()
    short = "min" if scale.startswith("min") else "maj" if scale.startswith("maj") else ""
    return f"{key} {short}".strip()


def _leaf(label: str) -> str:
    """The style under its parent: "Electronic---House" reads as "House"."""
    return str(label).rsplit("---", 1)[-1]


def sound_label(styles: list) -> str:
    """The two most probable Discogs styles, which is plenty for a row."""
    names = []
    for item in styles or []:
        name = _leaf(item[0] if isinstance(item, (list, tuple)) else item)
        if name and name not in names:
            names.append(name)
    return " / ".join(names[:2])


def family_label(styles: list) -> str:
    """The parent of the most probable style, for "same family" comparisons."""
    for item in styles or []:
        label = str(item[0] if isinstance(item, (list, tuple)) else item)
        if "---" in label:
            return label.split("---", 1)[0]
    return ""


def audio_neighbours(index_conn: sqlite3.Connection, store: sqlite3.Connection | None,
                     wanted: set[str], anchors: list[str], k: int = 3) -> set[str]:
    """Artists this station owns that sound most like the tracks either side of a set.

    Rank-based on purpose: absolute cosine is not calibrated across the embedding
    space (everything sits between 0.6 and 0.9), so the cut is relative to the
    anchor's own row — closer than its median plus half a standard deviation. The
    last.fm mechanism next to it can only name artists the library does not own;
    this one only ever returns artists it does.
    """
    if store is None or np is None:
        return set()
    rows = analysis_queries.artist_vector_rows(store)
    vectors = {
        row["artist_key"]: np.frombuffer(row["vec"], np.float32)
        for row in rows if row["vec"] and row["artist_key"] in wanted
    }
    if len(vectors) < 2:
        return set()
    keys = list(vectors)
    matrix = np.stack([unit(vectors[key]) for key in keys])
    blocked: set[str] = set()
    for anchor in anchors:
        key = navidrome.name_key(anchor)
        if key not in vectors:
            continue
        similarity = matrix @ unit(vectors[key])
        options = [(keys[i], float(similarity[i])) for i in range(len(keys)) if keys[i] != key]
        if not options:
            continue
        values = np.array([value for _k, value in options])
        threshold = float(np.median(values) + 0.5 * values.std())
        for other, value in sorted(options, key=lambda kv: -kv[1])[:k]:
            if value >= threshold:
                blocked.add(other)
    return blocked


def shelf_sound(store: sqlite3.Connection | None, names: list[str]) -> str:
    """One compact line: what the shelf sounds like, artist by artist.

    The shelf already tells the model *who* may play; this tells it what they
    sound like, which is the part it cannot infer from a name it does not know.
    """
    if store is None or not names:
        return ""
    rows = analysis_queries.artist_profile_rows(store)
    by_key = {row["artist_key"]: row for row in rows}
    bits = []
    for name in names:
        row = by_key.get(navidrome.name_key(name))
        if row is None:
            continue
        styles = json.loads(row["styles"] or "[]")
        bits.append(_profile_bit(name, styles, row["bpm"], row["energy"]))
    if not bits:
        return ""
    return "How those artists sound, roughly: " + "; ".join(bits) + "."


def _profile_bit(name: str, styles: list, bpm, energy) -> str:
    parts = []
    if styles:
        parts.append(" / ".join(_leaf(item[0]) for item in styles[:2]))
    if bpm:
        parts.append(f"{round(bpm)} bpm")
    if energy is not None:
        parts.append("high energy" if energy >= 0.66 else "low energy" if energy <= 0.33 else "mid energy")
    detail = ", ".join(parts)
    return f"{name} — {detail}" if detail else name


def nearest_artists(store: sqlite3.Connection | None, name: str, k: int = 8) -> list[str]:
    """Artists whose sound sits closest to this one, excluding itself."""
    if store is None or np is None:
        return []
    rows = analysis_queries.artist_pool_rows(store)
    target = navidrome.name_key(name)
    pool = [(row["artist_key"], row["name"], np.frombuffer(row["vec"], np.float32))
            for row in rows if row["vec"]]
    hit = next((item for item in pool if item[0] == target), None)
    if hit is None:
        return []
    vector = unit(hit[2])
    scored = [(float(unit(other) @ vector), other_name) for key, other_name, other in pool
              if key != target]
    scored.sort(reverse=True)
    return [other_name for _value, other_name in scored[:k] if other_name]
