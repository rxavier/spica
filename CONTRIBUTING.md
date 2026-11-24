# Working on Spica

`main` is the release line and it is protected: nothing lands on it except a squashed
pull request. There is no `dev` branch — every change starts from `main` and goes back
to it.

## The loop

```sh
git switch main && git pull
git switch -c <topic>          # e.g. fix-ledger-times, feat-requests
# work, commit as often as you like
git push -u origin <topic>
gh pr create --fill            # or open it in the browser
gh pr merge --squash --delete-branch
```

The merge is always a squash, titled with the PR title and bodied with the PR
description, and the branch is deleted automatically. So `main` reads as one entry per
change, and the branch is where the messy part lives.

## Commit messages

Write the subject as the sentence the squashed commit should read on `main` — it is
what people see in the log. Imperative, no prefix conventions, no emoji:

```
Fix the ledger's clock across an off-air gap
Add artist shortlists to stations
```

The body says *why*, and quotes numbers when there are numbers to quote ("prog 2,210,
prog + Bloc Party 2,248"). Documentation is part of the change: if behaviour moves,
the README moves in the same PR.

## Tests

```sh
uv sync --group dev
uv run pytest
```

The project targets Python 3.14 (`.python-version`); `uv` picks it up, and the
analysis extra is not needed for the tests.

Hermetic on purpose: no model, no network, no Navidrome and no station. The shared
fixtures point `config.STATE_DIR` at a temporary directory, so no test can touch a real
`state/` — the directory that is on air. A fixture that moves the state must move
`genres.alias_path()` and the other import-time paths with it. What is covered is the part that has to be
right: what the code accepts as an answer and what it refuses, the set's life on disk,
the picker's flow when a set exists or a request arrives, and the arithmetic behind the
shelf and the table — which artists it excludes, how the slots are shared out, the five
directions, and what happens when there are too few left or analysis is partial. The
prompt itself is not tested, because it belongs to whoever runs the station.

A test that cannot fail is not worth its runtime: break the behaviour deliberately and
watch the test go red before trusting it.

## Code, comments and docs

Source reads like ordinary engineering prose; the radio voice belongs in the README,
templates and prompts.

1. The first docstring line says what a function returns or does, in the imperative,
   using the parameter names. No persona, no metaphor.
2. At most one short paragraph follows, and only for a non-obvious constraint:
   ordering, a transaction boundary, a retry policy, why a fallback exists.
3. No changelog. Delete "used to", "the old", "which is why", "for ever" and the rest
   of the bug archaeology; that belongs in the commit or PR.
4. A module docstring is one to three sentences: what the module is, what it talks to,
   and what it deliberately does not do.
5. Comments explain *why*, not *what*, and never narrate a previous implementation. If
   a comment does not help a stranger predict behaviour, delete it.
6. Product words in code have one plain name each. The mapping lives in
   [`docs/glossary.md`](docs/glossary.md); user-facing copy may keep the radio voice.

## Running it locally

```sh
uv sync
SPICA_CONFIG=./config SPICA_STATE=./state \
  ICECAST_SOURCE_PASSWORD=dev NAVIDROME_URL=http://localhost:4533 \
  uv run python -m spica.app
```

`python -m spica.bootstrap` writes the three config files a fresh install needs if
they are missing. The containers are described in `deploy/README.md`; the full
picture of how a pick happens is in `README.md`.

## Releasing

Tagging `main` publishes the image:

```sh
git switch main && git pull
git tag -a v<version> -m "<version>"
git push origin v<version>
```

`.github/workflows/release.yml` builds `linux/amd64` and `linux/arm64` and pushes
`ghcr.io/rxavier/spica` with the version, the minor tag, and `latest`.

The version lives in `pyproject.toml` and `uv.lock`, and it is bumped in the pull request
that changes behaviour — not in a release pull request afterwards. A published tag is
immutable: anything that misses it is a patch release, never a moved tag.
