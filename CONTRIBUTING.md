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
git tag -a v0.1.1 -m "0.1.1"
git push origin v0.1.1
```

`.github/workflows/release.yml` builds `linux/amd64` and `linux/arm64` and pushes
`ghcr.io/rxavier/spica` with the version, the minor tag, and `latest`.
