"""Configuration: env for services, stations.json for the station rack."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

STATE_DIR = Path(os.environ.get("SPICA_STATE", "./state"))
CONFIG_DIR = Path(os.environ.get("SPICA_CONFIG", "./config"))

# Navidrome is a service, not a filesystem dependency: the index is paged out of
# its Subsonic API and playback hands Liquidsoap a stream URL by id.
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")

# Where the picker reads listener counts from.
ICECAST_URL = os.environ.get("ICECAST_URL", "http://127.0.0.1:8020").rstrip("/")
# Where LIQUIDSOAP publishes to. Separate from ICECAST_URL because the mixer and
# the picker may see Icecast at different addresses (same container vs another).
ICECAST_OUTPUT_HOST = os.environ.get("ICECAST_OUTPUT_HOST", "icecast")
ICECAST_OUTPUT_PORT = int(os.environ.get("ICECAST_OUTPUT_PORT", "8000"))

# Optional enrichment providers (see radio/enrich.py). Both are optional: with
# neither, the DJ works from names, genre and year alone.
# MusicBrainz asks callers to identify themselves; the project's own name and URL are
# an honest default, and an operator can point it at their address instead.
MB_USER_AGENT = os.environ.get(
    "MB_USER_AGENT", "Spica/1.0 ( https://github.com/rxavier/spica )"
)
LASTFM_KEY = os.environ.get("LASTFM_KEY", "")

# --- web UI ---------------------------------------------------------------
# Admin side is one password; listener side is public by design.
ADMIN_PASSWORD = os.environ.get("SPICA_ADMIN_PASSWORD", "")
SESSION_SECRET_FILE = STATE_DIR / ".session-secret"
# Global DJ house rules, editable from the admin UI, injected into every pick.
DJ_PERSONA_FILE = CONFIG_DIR / "dj-persona.md"
# Liquidsoap's control server, used to restart the mixer after a station change.
LIQ_TELNET = os.environ.get("LIQ_TELNET", "mixer:1234")
SESSION_HOURS = 12

LLM_BASE = os.environ.get("LLM_BASE", "").rstrip("/")
LLM_KEY = os.environ.get("LLM_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_HEADERS = {}
for _pair in filter(None, (p.strip() for p in os.environ.get("LLM_HEADERS", "").split(","))):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        LLM_HEADERS[_k.strip()] = _v.strip()

# How far ahead the picker keeps each listened-to station.
QUEUE_TARGET = int(os.environ.get("QUEUE_TARGET", "1"))
# Poll interval for the loop, seconds.
POLL_S = int(os.environ.get("POLL_S", "20"))
# Only used when Liquidsoap's control server is unreachable: without it the loop
# would pick a track every POLL_S and the DJ would run hours ahead.
PICK_COOLDOWN = int(os.environ.get("PICK_COOLDOWN", "240"))
# Requests left waiting while one plays. Liquidsoap lists only what is waiting, so
# 1 means "always have the next track ready": the request then has a whole track to
# be fetched and decoded before it is needed, instead of arriving at the last
# second and losing the boundary to the pool.
KEEP_AHEAD = int(os.environ.get("KEEP_AHEAD", "1"))
# Nothing encodes until someone tunes in; a station that goes quiet is closed
# after this many seconds, so tuning back in within the window costs no wait.
IDLE_STOP_S = int(os.environ.get("IDLE_STOP_S", "90"))
# How often the worker compares the index with Navidrome's own count.
REFRESH_CHECK_S = int(os.environ.get("REFRESH_CHECK_S", "900"))


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    # What may play. An empty genre list means "the whole library"; an empty
    # artist list means "anyone". When both are given a track has to clear both,
    # and the exclusions always win over everything.
    genres: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    exclude_genres: tuple[str, ...] = ()
    exclude_artists: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    # How the rotation leans: "deep" for album tracks, "hits" for what people know,
    # "balanced" for neither. It only weights the seeded order.
    popularity: str = "balanced"
    description: str = ""

    @property
    def deep_bias(self) -> float:
        return {"deep": 1.0, "balanced": 0.0, "hits": -1.0}.get(self.popularity, 0.0)

    @property
    def mount(self) -> str:
        return f"/{self.id}.mp3"

    @property
    def pool_file(self) -> Path:
        return STATE_DIR / "pools" / f"{self.id}.m3u"

    @property
    def queue_file(self) -> Path:
        return STATE_DIR / "queue" / f"{self.id}.txt"

    @property
    def history_file(self) -> Path:
        return STATE_DIR / "history" / f"{self.id}.jsonl"

    @property
    def now_file(self) -> Path:
        """Where the mixer writes the track it has just started playing."""
        return STATE_DIR / "now" / f"{self.id}.txt"


def load_stations(path: Path | None = None) -> list[Station]:
    path = path or (CONFIG_DIR / "stations.json")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[Station] = []
    for item in raw["stations"]:
        out.append(
            Station(
                id=item["id"],
                name=item["name"],
                genres=tuple(item.get("include_genres", ()) or ()),
                artists=tuple(item.get("include_artists", ()) or ()),
                exclude_genres=tuple(item.get("exclude_genres", ()) or ()),
                exclude_artists=tuple(item.get("exclude_artists", ()) or ()),
                year_from=item.get("year_from"),
                year_to=item.get("year_to"),
                popularity=str(item.get("popularity") or "balanced").lower(),
                description=item.get("description", ""),
            )
        )
    return out


def ensure_dirs() -> None:
    for sub in ("pools", "queue", "history", "logs", "now"):
        (STATE_DIR / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- llm settings
#
# The environment is the default; the admin writes llm.json and that wins, so an
# operator can point the station at any OpenAI-compatible endpoint without editing a
# container's environment and restarting it.
LLM_FILE = CONFIG_DIR / "llm.json"


def llm_settings() -> dict:
    out = {"base": LLM_BASE, "key": LLM_KEY, "model": LLM_MODEL, "headers": dict(LLM_HEADERS)}
    try:
        raw = json.loads(Path(LLM_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for key in ("base", "key", "model"):
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value.rstrip("/") if key == "base" else value
    headers = raw.get("headers")
    if isinstance(headers, dict):
        out["headers"] = {**out["headers"], **{str(k): str(v) for k, v in headers.items() if k}}
    return out


def save_llm_settings(base: str, key: str, model: str, headers: dict | None = None) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "base": str(base or "").strip().rstrip("/"),
        "key": str(key or "").strip(),
        "model": str(model or "").strip(),
    }
    if headers:
        payload["headers"] = {str(k): str(v) for k, v in headers.items() if str(k).strip()}
    Path(LLM_FILE).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        Path(LLM_FILE).chmod(0o600)
    except OSError:
        pass
