"""First run: write the config a fresh install needs, and nothing else.

A new install has three files that must exist before anything can play —
`icecast.xml`, `stations.json` and `spica.liq`. Making the operator write them by
hand is the difference between "docker compose up" and an afternoon. So on start
the picker (and the worker, in case it comes first) calls `ensure()`, which creates
whatever is missing and never touches a file that already exists.

Passwords come from the environment when given, and are generated when not — the
generated ones are printed once, because the Icecast source password has to match
what the mixer uses, and both read it from `ICECAST_SOURCE_PASSWORD`.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from . import config

ICECAST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<icecast>
    <location>{location}</location>
    <admin>{admin}</admin>
    <hostname>{hostname}</hostname>

    <limits>
        <clients>25</clients>
        <sources>40</sources>
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-size>65536</burst-size>
    </limits>

    <authentication>
        <source-password>{source_password}</source-password>
        <relay-password>{source_password}</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>{admin_password}</admin-password>
    </authentication>

    <listen-socket>
        <port>8000</port>
    </listen-socket>

    <paths>
        <logdir>/tmp</logdir>
        <webroot>/usr/share/icecast/web</webroot>
        <adminroot>/usr/share/icecast/admin</adminroot>
    </paths>

    <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>3</loglevel>
    </logging>
</icecast>
"""

STARTER_STATIONS = {
    "stations": [
        {
            "id": "main",
            "name": "Everything",
            "description": "The whole library, no rules. Edit this station, or add another.",
            "include_genres": [],
            "exclude_genres": [],
            "include_artists": [],
            "exclude_artists": [],
            "popularity": "balanced",
            "year_from": None,
            "year_to": None,
        }
    ]
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def ensure(verbose: bool = True) -> list[str]:
    """Create the missing pieces of a first install. Returns what it wrote."""
    config.ensure_dirs()
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    made: list[str] = []

    icecast = config.CONFIG_DIR / "icecast.xml"
    if not icecast.exists():
        source_password = _env("ICECAST_SOURCE_PASSWORD") or secrets.token_hex(16)
        admin_password = _env("ICECAST_ADMIN_PASSWORD") or secrets.token_hex(16)
        icecast.write_text(
            ICECAST_XML.format(
                location=_env("SPICA_LOCATION", "Somewhere"),
                admin=_env("SPICA_ADMIN_EMAIL", "admin@example.com"),
                hostname=_env("SPICA_HOSTNAME", "localhost"),
                source_password=source_password,
                admin_password=admin_password,
            ),
            encoding="utf-8",
        )
        made.append("icecast.xml")
        if verbose and not _env("ICECAST_SOURCE_PASSWORD"):
            print(
                "spica: generated an Icecast source password — set "
                f"ICECAST_SOURCE_PASSWORD={source_password} in your .env and restart,\n"
                "       or keep it here and give the mixer the same value.",
                flush=True,
            )

    stations = config.CONFIG_DIR / "stations.json"
    if not stations.exists():
        stations.write_text(json.dumps(STARTER_STATIONS, indent=2) + "\n", encoding="utf-8")
        made.append("stations.json")

    script = config.CONFIG_DIR / "spica.liq"
    if not script.exists():
        from . import liqgen

        password = _env("ICECAST_SOURCE_PASSWORD")
        if not password:
            # read what the generated icecast.xml carries, so the two agree
            try:
                text = icecast.read_text(encoding="utf-8")
                password = text.split("<source-password>")[1].split("</source-password>")[0]
            except (OSError, IndexError):
                password = ""
        script.write_text(liqgen.render(config.load_stations(stations), password), encoding="utf-8")
        made.append("spica.liq")

    if made and verbose:
        print(f"spica: first-run setup wrote {', '.join(made)} in {config.CONFIG_DIR}", flush=True)
    return made


if __name__ == "__main__":
    ensure(verbose=True)
