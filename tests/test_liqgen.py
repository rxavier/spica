"""What the admin form accepts has to survive the trip into the mixer's script.

A station's name and description are free text, and the generated spica.liq is one
script for every station: a single unescaped quote in one description left the
literal open, Liquidsoap refused the file, and the container crash-looped with the
whole radio off the air. These read the rendered argument back as a literal, so a
value that would break the script fails here instead.
"""

from __future__ import annotations

from spica import config, liqgen


def _argument(line: str, key: str) -> str:
    """The text of one `key=` argument, up to its top-level comma."""
    start = line.index(f"{key}=") + len(key) + 1
    depth = 0
    in_string = False
    i = start
    while i < len(line):
        char = line[i]
        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            return line[start:i]
        i += 1
    raise AssertionError(f"no argument for {key} in {line!r}")


def _read(expr: str) -> str:
    """Read a rendered literal back: `"a"`, or `("a" ^ "#{" ^ "b")`."""
    expr = expr.strip()
    if expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1]
    out: list[str] = []
    for chunk in expr.split(" ^ "):
        assert chunk.startswith('"') and chunk.endswith('"'), chunk
        body = chunk[1:-1]
        i = 0
        while i < len(body):
            if body[i] == "\\":
                out.append({"n": "\n", '"': '"', "\\": "\\"}[body[i + 1]])
                i += 2
            else:
                out.append(body[i])
                i += 1
    return "".join(out)


def _description_line(stations: list[config.Station]) -> str:
    script = liqgen.render(stations, "secret")
    return next(line for line in script.splitlines() if line.startswith("  name="))


def test_a_description_with_quotes_renders_as_one_string(station):
    described = config.Station(
        id="alt",
        name="Alternative",
        description='A station that makes you go "ooohhh what a great song".',
    )
    line = _description_line([described])
    assert '\\"' in line
    assert _read(_argument(line, "description")) == described.description + " · MP3 192k"


def test_a_backslash_and_a_newline_do_not_escape_the_literal(station):
    awkward = "C:\\music\nsecond line"
    described = config.Station(id="alt", name="Alternative", description=awkward)
    line = _description_line([described])
    assert _read(_argument(line, "description")) == awkward + " · MP3 192k"


def test_interpolation_is_broken_out_of_the_literal(station):
    # `#{...}` opens string interpolation and cannot be backslash-escaped in 2.4,
    # so the literal has to be closed and reopened around it.
    described = config.Station(id="alt", name="Alternative", description="costs #{1 + 1}")
    line = _description_line([described])
    assert '^ "#{" ^' in line
    assert _read(_argument(line, "description")) == "costs #{1 + 1} · MP3 192k"


def test_the_station_name_is_escaped_too(station):
    named = config.Station(id="alt", name='The "Good" Station', description="")
    line = _description_line([named])
    assert _read(_argument(line, "name")) == 'The "Good" Station'
    # An empty description falls back to the name, not to an empty literal.
    assert _read(_argument(line, "description")) == 'The "Good" Station · MP3 192k'


def test_main_refuses_to_default_the_source_password(tmp_path, monkeypatch, capsys):
    from spica import liqgen

    monkeypatch.setattr(liqgen.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(liqgen.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("ICECAST_SOURCE_PASSWORD", "")
    monkeypatch.setenv("SPICA_LIQ", str(tmp_path / "spica.liq"))

    code = liqgen.main()

    assert code == 2
    assert not (tmp_path / "spica.liq").exists()
    assert "ICECAST_SOURCE_PASSWORD" in capsys.readouterr().err


def test_write_honours_the_target_env_and_replaces_the_file(tmp_path, monkeypatch):
    from spica import liqgen

    station = config.Station(id="alt", name="Alt")
    target = tmp_path / "spica.liq"
    monkeypatch.setenv("SPICA_LIQ", str(target))

    written = liqgen.write([station], "source-password")

    assert written == target
    assert "source-password" in target.read_text(encoding="utf-8")
    assert not (tmp_path / "spica.liq.tmp").exists()
