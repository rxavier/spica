"""Shared helpers for seeding the index in tests.

Tests still describe their own rows, but the column list and the INSERT live here,
so adding a column to the schema does not break every fixture.
"""

from __future__ import annotations

import json

TRACK_COLUMNS = (
    "id", "uri", "title", "artist", "album", "album_id",
    "track_no", "year", "genre", "duration", "album_artist", "album_artist_key",
)


def insert_tracks(conn, rows, columns=TRACK_COLUMNS, *, replace: bool = True) -> None:
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    holds = ", ".join("?" for _ in columns)
    conn.executemany(
        f"{verb} INTO tracks ({', '.join(columns)}) VALUES ({holds})",
        rows,
    )


def insert_track(conn, **fields) -> None:
    names = ", ".join(fields)
    holds = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT OR REPLACE INTO tracks ({names}) VALUES ({holds})",
        tuple(fields.values()),
    )


def configure_console(monkeypatch, tmp_path, stations, *, pool_counts=None,
                      password: str | None = None) -> tuple:
    """Point the whole console at a temporary config/state and stub the stack.

    Both admin test modules need the same isolation; the only difference is what
    `write_pools` reports, so that is the one knob.
    """
    from spica import config, genres, icecast, mixer, pool, store

    cfg = tmp_path / "config"
    state = tmp_path / "state"
    cfg.mkdir()
    state.mkdir()
    (cfg / "stations.json").write_text(json.dumps(stations), encoding="utf-8")

    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "SESSION_SECRET_FILE", state / ".session-secret")
    monkeypatch.setattr(config, "DJ_PERSONA_FILE", cfg / "dj-persona.md")
    if password is not None:
        monkeypatch.setattr(config, "ADMIN_PASSWORD", password)
    monkeypatch.setattr(genres, "alias_path", lambda: state / "genre-aliases.json")
    genres.reset_cache()

    monkeypatch.setattr(mixer, "close", lambda: None)
    monkeypatch.setattr(mixer, "queued_ids", lambda st: [])
    monkeypatch.setattr(mixer, "pending", lambda st: 0)
    monkeypatch.setattr(mixer, "pending_uris", lambda st: [])
    monkeypatch.setattr(mixer, "set_queue", lambda st, uris: True)
    monkeypatch.setattr(mixer, "now_lines", lambda st: [])
    monkeypatch.setattr(
        pool, "write_pools",
        lambda conn, stations, log=print: dict(pool_counts or {}),
    )
    monkeypatch.setattr(icecast, "listen_states", lambda: {})
    monkeypatch.setattr(icecast, "total_listeners", lambda states, st: 0)
    monkeypatch.setattr(icecast, "live_mount", lambda states, st: None)
    store.reset()
    return cfg, state
