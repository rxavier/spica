# De-funneling the DJ: fine genres for the model, rollups for stations

**Audience:** whoever implements this. No prior knowledge of the codebase is assumed.
Every change is spelled out with exact files, functions and before/after code.
Follow the sections in order; the migration (section 8) is part of the change,
not an optional extra.

## 1. What is broken, in one paragraph each

The alt station's DJ picks Bloc Party, De Lux, L'Impératrice and Bleu Toucan in
**25% of all picks** (73 of 293 logged picks, `state/history/alt.picks.jsonl`),
although those are 4 of 68 artists on the station and hold 27/43/34/19 of its
2189 tracks. Three mechanical funnels cause it:

1. **Genre rollup at import.** `state/genre-aliases.json` maps `nu disco`,
   `french pop`, `electropop`, `indietronica`, … → `Electronic` (and
   `synth pop`/`indie pop` → `Indie Rock`). The aliases are applied when labels
   are *imported* into `track_genres`, so the fine labels never reach the index.
   The DJ's `filter {genre: "Nu Disco"}` (called 9 times in the log) is silently
   rewritten to a 416-track "Electronic" bucket, and the model picks the acts it
   recognises by name — always the same French nu-disco corner.
2. **The search-miss path hands out fixed track ids.** A search for an absent
   band (Parcels ×3, Hot Chip, Jungle, Polo & Pan, Editors, Foals, …) is answered
   by `Library.similar_here`, which returns the same owned artists every time
   (cached Last.fm similarity), each with the *alphabetically first track of the
   alphabetically first album* as a ready-to-use id, plus a note saying the id
   can be used. Parcels → L'Impératrice "Là-haut", every time. Editors/Foals →
   Bloc Party "Like Eating Glass", every time.
3. **Deterministic ordering and no recency pushback in the tools.**
   `catalogue(artist=…)` returns albums alphabetically, so its first page is
   always the same records; `similar()` loops Interpol ↔ Bloc Party and
   L'Impératrice → Bleu Toucan through the cache; and no tool ever downweights
   an artist that played ten minutes ago — the anti-corner rule exists only as
   prose in the prompt.

**Ruled out** (do not "fix" these): the fresh-artists list in the brief (the four
artists appear in it 3 times in 58 briefs), the standby pool (flat, all 2189
tracks), the model's taste.

## 2. The design rule

> **Operator aliases are a station-selection rollup, applied at match time, and
> nowhere else. The index and the DJ's tools keep the library's fine vocabulary.
> Tools demote recently-played artists; they never exclude them** (an excluded
> artist makes a tool answer "nothing here", which is a lie and a dead end).

## 3. `spica/genres.py` — split canonicalisation into two registers

### 3.1 Add an `operator` flag to `canonical()`

Current (lines ~176–190):

```python
def canonical(label: object) -> str:
    """Operator rule, then builtin, then the label spelled as the library has it."""
    text = str(label or "").strip()
    if not text:
        return ""
    key = fold(text)
    user = operator_aliases()
    if key in user:
        return user[key]
    ...
```

Change the signature and the operator lookup, nothing else:

```python
def canonical(label: object, *, operator: bool = True) -> str:
    """Operator rule, then builtin, then the label spelled as the library has it.

    `operator=False` skips the operator's alias file. The aliases are a rollup
    for station selection (a station that ticks "Electronic" should catch a track
    tagged "Nu Disco"); they are not a rewrite of the library's vocabulary, and
    the DJ's tools match on the fine labels.
    """
    text = str(label or "").strip()
    if not text:
        return ""
    key = fold(text)
    if operator:
        user = operator_aliases()
        if key in user:
            return user[key]
    if key in BUILTIN:
        return BUILTIN[key]
    squashed = key.replace(" ", "")
    if squashed in BUILTIN:
        return BUILTIN[squashed]
    return text
```

### 3.2 Thread the flag through `labels_of()`

Change the signature to `def labels_of(raw: object, *, operator: bool = True) -> list[str]:`
and the inner call to `label = canonical(part, operator=operator)`. Nothing else.

### 3.3 Add `targets_fine()` next to `targets()`

```python
def targets_fine(genres) -> list[list[str]]:
    """DJ-tool genre words -> canonical word lists, WITHOUT the operator rollup.

    `filter` must answer what the model actually asked: "Nu Disco" matches the
    nu-disco labels, not the "Electronic" shelf the operator rolls them up into
    for stations. Builtin spelling fixes ("synthpop" -> "Synth Pop") still apply.
    """
    return [_words(canonical(g, operator=False)) for g in (genres or ()) if fold(g)]
```

Keep `targets()` exactly as it is (it becomes unused by the picker after section
5; leave it — it is the station-side helper and costs nothing).

### 3.4 Update the module docstring

The "three layers" docstring at the top of the file gains one paragraph:

```
Two registers use these layers. The INDEX and the DJ's tools use layers 1–2 only
(fine vocabulary). STATION MATCHING uses all three: an operator alias like
"nu disco -> Electronic" is how the admin ticks one shelf instead of twelve, and
it is applied when a station is matched, not when a label is stored.
```

## 4. Stop flattening at import — `spica/navidrome.py`, `spica/worker.py`

Three call sites start passing `operator=False`. No other change at these sites.

1. `navidrome.py`, inside the album loop of `rebuild_index()` (~line 315):
   `for label in genre_lib.labels_of(raw):` → `for label in genre_lib.labels_of(raw, operator=False):`
2. `navidrome.py`, `_canonical_entries()` inside `apply_genre_precedence()` (~line 448):
   `name = genre_lib.canonical(entry["name"])` → `name = genre_lib.canonical(entry["name"], operator=False)`
   (The `mb_artist_genres` cache stores raw MusicBrainz names, so this re-canonicalises
   from cache on the next rebuild — no network needed.)
3. `worker.py`, `run_genre_tags()` (~line 79):
   `label = genres.canonical(tag)` → `label = genres.canonical(tag, operator=False)`
   (The `artist_tags` cache stores raw Last.fm tags, so re-running the job rebuilds
   the external labels from cache — no network for already-cached artists.)

After the next rebuild, `track_genres` holds fine labels: "Nu Disco",
"Post-Disco", "Indie Pop", "Synth Pop", "Chillwave", … instead of everything
being "Electronic"/"Indie Rock".

## 5. Apply the rollup at match time — `station_matcher()` in `spica/navidrome.py`

Current (~lines 505–541) folds both sides only:

```python
    by_track = labels_by_track(conn)
    # A station picks labels from the vocabulary, so this is set membership: no
    # substring guessing about what a genre name might have meant.
    include = {genre_lib.fold(g) for g in st.genres if genre_lib.fold(g)}
    exclude = {genre_lib.fold(g) for g in st.exclude_genres if genre_lib.fold(g)}
    ...
        if include or wanted:
            labels = {genre_lib.fold(label) for label in by_track.get(row["id"], ())}
            ...
        if exclude:
            labels = {genre_lib.fold(label) for label in by_track.get(row["id"], ())}
            ...
```

Replace with canonicalisation (operator aliases **on**) of both sides:

```python
    by_track = labels_by_track(conn)
    # A station picks labels from the vocabulary, so this is set membership: no
    # substring guessing about what a genre name might have meant. The operator's
    # aliases are a station-selection rollup and they act HERE, at match time:
    # both the station's ticked genres and each track's labels go through
    # canonical(), so ticking "Electronic" still catches a track stored under its
    # fine label "Nu Disco" — while the index and the DJ keep the fine label.
    canon: dict[str, str] = {}
    for track_labels in by_track.values():
        for label in track_labels:
            if label not in canon:
                canon[label] = genre_lib.fold(genre_lib.canonical(label))
    include = {genre_lib.fold(genre_lib.canonical(g)) for g in st.genres if genre_lib.fold(g)}
    exclude = {genre_lib.fold(genre_lib.canonical(g)) for g in st.exclude_genres if genre_lib.fold(g)}
    wanted = {a.strip().casefold() for a in st.artists if a.strip()}
    unwanted = {a.strip().casefold() for a in st.exclude_artists if a.strip()}
```

and in `ok()`, both places:

```python
            labels = {canon.get(label, genre_lib.fold(label)) for label in by_track.get(row["id"], ())}
```

Why this preserves today's behaviour: until now the rollup happened at import
(stored label = canonical), and the matcher folded. Folding `canonical(label)`
on both sides at match time is the same comparison, one step later — so stations,
pools (`pool.write_pools` uses `rows_for_station`), and `Library.on_station` all
keep working, only now the stored labels are fine.

## 6. Recency demotion + fine genres — `spica/picker/library.py`

### 6.1 Two small helpers on `Library` (put them near `_shuffle_key`)

```python
    def _act_key(self, row) -> str:
        """The folded identity of the record's artist, for recency comparisons."""
        return navidrome.name_key(row["album_artist"] or row["artist"])

    def _recent_act_keys(self, station: config.Station | None, window: int = 20) -> set[str]:
        """Artists aired in the last `window` plays, folded.

        Tools demote these; they never exclude them. Excluding would let a tool
        answer "nothing here" about music the station holds, and an empty page
        sends the model somewhere worse than a repeated artist does.
        """
        if station is None:
            return set()
        return {navidrome.name_key(a) for a in self.recent_labels(station, window)[1] if a}
```

### 6.2 `filter()`: fine targets, recent artists to the back of the page

Replace the genre block and the shuffle sort (currently ~lines 239–249):

```python
        want = kw.get("genre")
        if want:
            # Fine labels, not the operator's station rollup: "Nu Disco" answers
            # the nu-disco shelf, not everything the station files under Electronic.
            target = genres_module.targets_fine([want])
            by_track = navidrome.labels_by_track(self.conn)
            rows = [r for r in rows if genres_module.matches(by_track.get(r["id"], ()), target)]

        # A shuffle keyed to this pick, not to this call. `offset` can only mean
        # "the next page" if the order holds still between the two calls, so the
        # seed is drawn once per pick — every call within one decision pages the
        # same list, and the next pick gets a fresh order. Artists that aired
        # recently sort behind everyone else, deterministically, so paging stays
        # stable within the pick.
        recent = self._recent_act_keys(station)
        rows = sorted(rows, key=lambda r: (self._act_key(r) in recent, self._shuffle_key(r)))
        total = len(rows)
```

`matching = total` is unchanged (demotion reorders, it does not remove), the
`PER_ARTIST = 3` spread is unchanged, and `offset` paging stays stable within a
pick because the whole sort key is seeded.

### 6.3 `similar()`: demote before slicing, in BOTH branches

At the top of `similar()`, after `ctx = self._context(station)`, add:

```python
        recent = self._recent_act_keys(station)
```

In the cached-names branch, change:

```python
            rows = self.rows_on_station(rows, station)
            rows = rows[: int(limit)]
```

to:

```python
            rows = self.rows_on_station(rows, station)
            # Stable sort: the artist that played ten minutes ago goes to the back,
            # the random order survives within each group.
            rows.sort(key=lambda r: self._act_key(r) in recent)
            rows = rows[: int(limit)]
```

Do the same in the genre-fallback branch (it has the identical
`rows_on_station` → `rows[: int(limit)]` pair).

### 6.4 `similar_here()`: seeded lead track, recent artists last

At the top, after `rows = self._station_rows(station)`, add:

```python
        recent = self._recent_act_keys(station)
```

In `gather()`, replace `lead = hits[0]` with a seeded draw (the same track was
handed out on every miss — "Là-haut" for L'Impératrice, "Like Eating Glass" for
Bloc Party):

```python
                lead = sorted(hits, key=self._shuffle_key)[0]
```

Replace both `found.sort(...)` / `out.sort(...)` calls (the one in `gather` and
the one after the `lfm_similar` widening) with the recency-aware key:

```python
            found.sort(key=lambda d: (navidrome.name_key(str(d["artist"])) in recent,
                                      -d["tracks"], str(d["artist"]).casefold()))
```

The offer list itself is unchanged: same artists, same fields, same limit — only
the order and the lead id vary.

### 6.5 `catalogue()`: seeded album order when browsing an artist

Currently the SQL is `ORDER BY artist, album, track_no`, so the first page is
always the alphabetically-first albums (Bloc Party → "A Weekend in the City",
every time). After `rows = self.rows_on_station(rows, station)`, the empty
check, and the shared-album-name resolution, and **only when `album` was not
given**, insert:

```python
        if not album:
            # Albums in this pick's order, tracks still in running order within
            # each album: browsing an artist used to open on the same records
            # every time — alphabetical order is a favorite in disguise.
            grouped: dict[str, list] = {}
            for row in rows:
                grouped.setdefault(row["album_id"] or row["album"], []).append(row)
            ordered = sorted(grouped.values(), key=lambda rs: self._shuffle_key(rs[0]))
            rows = [r for rs in ordered for r in rs]
```

The `for row in rows[:limit]` loop and the response shape are unchanged. When
`album` IS given, nothing changes: one album, running order.

## 7. Tests — new file `tests/test_defunnel.py`

Conventions to follow (look at these first): `tests/conftest.py` provides the
`station` fixture (monkeypatches `config.STATE_DIR` to `tmp_path`);
`tests/test_admin_genres.py` shows how to seed a real database and how the alias
file is moved (`monkeypatch.setattr(genres, "_FILE", ...)` — the path is bound
at import time, and also reset the memo with `monkeypatch.setattr(genres, "_cache", (-1.0, {}))`);
`tests/test_fresh_names.py` shows stubbing `Library` lookups. No test may touch
the network or the real `state/`.

Build a real in-memory-ish database with `navidrome.connect(tmp_path / "index.db")`
and seed it with `INSERT INTO tracks (id, uri, title, artist, album, album_id, track_no, year, genre, duration, album_artist, album_artist_key) VALUES (...)`
(`album_artist_key = navidrome.name_key(album_artist)`) plus
`INSERT INTO track_genres (track_id, label, source) VALUES (?,?, 'tag')`.
Seed plays with `queue.record(station, track_id, title, artist, "dj")` —
`Library.recent_labels` reads them back through the `station` fixture's
`STATE_DIR`.

Cases to write:

1. **Rollup moved to match time.** Alias file contains `nu disco -> Electronic`;
   track T1 labelled "Nu Disco", track T2 labelled "Electronic".
   `rows_for_station` with station genres `("Electronic",)` contains both;
   with `("Nu Disco",)` contains T1 only; with `exclude_genres=("Electronic",)`
   and `genres=()` contains neither.
2. **`canonical` honours the flag.** With the same alias file:
   `genres.canonical("Nu Disco") == "Electronic"`;
   `genres.canonical("Nu Disco", operator=False) == "Nu Disco"`;
   `genres.canonical("synthpop", operator=False) == "Synth Pop"` (builtin still on).
3. **`filter` uses fine labels.** Same seed as case 1: `filter(genre="Nu Disco")`
   returns T1 and not T2; `filter(genre="Electronic")` returns T2 and not T1.
4. **`filter` demotes recent artists, `matching` unchanged.** Artists A and B,
   two tracks each, all labelled "Rock"; record two recent plays of A.
   `filter()` returns all 4 (`matching == 4`) with B's rows ahead of A's.
5. **`similar` demotes recent artists.** Monkeypatch
   `enrich.similar_artists` to `(["B", "A"], "cache")`; A has a recent play;
   B's tracks come first in the result. Also monkeypatch `enrich.lfm_similar`
   to `lambda name, limit: []` so nothing reaches the network.
6. **`similar_here` rotates the lead id.** One artist with two albums; monkeypatch
   `enrich.similar_artists` to return it. Collect the offered `id` over
   `lib.draw_seed = 1 … 20`: more than one distinct id must appear.
7. **`similar_here` puts recent artists last.** Two offered artists, one with a
   recent play: the fresh one is first in `similar_here`'s list.
8. **`catalogue` album order follows the seed.** One artist, three albums of
   three tracks. Artist-only `catalogue` over `draw_seed = 1 … 20` shows more
   than one album ordering; within every album the `track_no`s are ascending;
   `catalogue(album=…)` still returns running order.

Run: `uv run pytest` (or `.venv/bin/pytest`). All existing tests must keep
passing untouched — if one fails because it asserted the old flatten-at-import
behaviour, that test encodes the bug; adapt the assertion, not the new code, and
say so in the commit message.

## 8. Migration on the live station (`/srv/spica`) — do this with the deploy

The fine labels only appear after the index is rebuilt; until then the new
`filter` matches nothing for rolled-up genres. Order matters.

1. **Deploy the code.** Build the image from the repo and restart picker + worker:

   ```sh
   cd /home/rafael/code/spica
   docker build -t ghcr.io/rxavier/spica:0.11.1 .
   cd /srv/spica
   # set SPICA_TAG=0.11.1 in .env (compose.yaml reads ${SPICA_TAG})
   docker compose up -d picker worker
   ```

2. **Drop the derived labels, keep hand-set ones.** `refresh` only deletes
   non-`external` rows and `genre_tags` skips tracks that already have an
   `external` row, so the flattened labels must be cleared by hand once:

   ```sh
   sqlite3 /srv/spica/state/index.db \
     "DELETE FROM track_genres WHERE source != 'manual';"
   ```

   (If the host has no `sqlite3`: `docker compose exec worker python -c
   "import sqlite3; c=sqlite3.connect('/state/index.db'); c.execute(\"DELETE FROM track_genres WHERE source != 'manual'\"); c.commit()"`.
   If you get `database is locked`, wait a few seconds and retry.)

3. **Rebuild, then re-tag.** Log in to the admin API and queue two jobs, in
   order, waiting for the first to finish:

   ```sh
   PW=$(grep '^SPICA_ADMIN_PASSWORD=' .env | cut -d= -f2-)
   curl -c /tmp/spica.jar -X POST --data-urlencode "password=$PW" \
     http://localhost:8030/admin/login
   curl -b /tmp/spica.jar -X POST -H 'content-type: application/json' \
     -d '{"kind":"refresh"}' http://localhost:8030/jobs
   curl -b /tmp/spica.jar http://localhost:8030/jobs   # poll until refresh is done
   curl -b /tmp/spica.jar -X POST -H 'content-type: application/json' \
     -d '{"kind":"genre_tags"}' http://localhost:8030/jobs
   ```

   `refresh` re-indexes file tags, inherited labels and MusicBrainz labels with
   the new code (MusicBrainz comes from the `mb_artist_genres` cache — no
   network), and rewrites the pools with the new matcher. `genre_tags` rebuilds
   the Last.fm (`external`) labels from the `artist_tags` cache.

4. **Do NOT touch** `config/stations.json` or `state/genre-aliases.json`. The
   aliases are what keep the alt station catching nu-disco tracks now that they
   are stored under fine labels.

## 9. Verification

Run these against the live state after the migration
(`SPICA_STATE=/srv/spica/state SPICA_CONFIG=/srv/spica/config`, repo venv):

1. `SELECT label, COUNT(*) c FROM track_genres GROUP BY label ORDER BY c DESC LIMIT 30;`
   — expect fine labels back ("Nu Disco", "Post-Disco", "Indie Pop", "Synth
   Pop", "Chillwave", …) and "Electronic" down from 416 to a few dozen.
2. `Library.count(alt)` — expect ~2189, i.e. the station did not shrink (the
   rollup at match time still catches the fine labels). A small change is fine
   (labels genuinely added/removed); a collapse is not.
3. `lib.filter(station=alt, genre="Nu Disco")` — expect `matching` in the
   dozens, all of it actual nu-disco acts (De Lux, Bleu Toucan, Nu Genea, …),
   not 416.
4. `lib.similar_here("Parcels", alt)` — still L'Impératrice (that answer is
   *correct*), but the offered `id` must vary with `lib.draw_seed`, and an
   artist in the last 20 plays must sort after fresh ones.
5. Watch `state/history/alt.picks.jsonl` over the next days: the four-artist
   share should fall from ~25% toward single digits, and no artist should hold
   more than ~10% of picks.

## 10. Rollback

`git revert` the change, rebuild the image, redeploy, and re-run section 8 —
with the old code the aliases act at import again and the flattened vocabulary
is rebuilt from the same raw tags and provider caches. No operator data is ever
destroyed by this plan: the alias file, `stations.json`, `track_raw_genres`,
`track_genre_veto`, `artist_tags`, `mb_artist_genres` and `manual` labels all
survive.

## 11. Documentation updates (same commit)

- `docs/dj-tools.md`: the `filter` section gains "fine labels, recent artists
  demoted to the back of the page"; the `catalogue` section gains "albums in the
  pick's seeded order"; the search-miss/`similar_here` section loses any claim
  of a fixed lead track; the closing note about genre matching existing twice
  (exact for stations, loose for `filter`) is updated — the difference is now
  *rollups for stations, fine labels for the DJ*.
- `README.md`: the sections describing `genre-aliases.json` ("operator rules …
  win over the builtin list", and the credentials/state file list around them)
  must say the aliases are a station-matching rollup applied at match time, not
  an import-time rewrite.
- `spica/picker/library.py`: the `filter`/`catalogue`/`similar` docstrings get
  one line each, in the house style (say what used to go wrong, not just what
  the code does).

## 12. The admin keeps the collapse — separate register for building stations

The rollup is an operator tool for *building* stations: ticking one shelf instead of
twelve. That purpose does not survive storing fine labels unless the admin keeps
presenting the collapsed vocabulary, so the console works on `canonical(label)` and the
index keeps the fine ones.

- **`navidrome.genre_report()` / `genres()`**: group in Python by
  `genre_lib.canonical(label)` and sum the counts, instead of trusting the stored label:
  one entry per collapsed name, each carrying its member fine labels and their counts.
  `genre_counts()`, `label_albums()` and `label_tracks()` take a collapsed name and
  expand it to every stored label that canonicalises to it (`WHERE label IN (…)`), so
  "everything with…" still works from the admin.
- **`web/shared.known_labels()`**: accept a submitted name when *any* stored label
  canonicalises to it. Without this a station saved after the migration silently loses
  a genre whose only rows are now fine labels.
- **`genre_lib.merge()`**: writes the alias rule and returns how many stored labels it
  now covers — it must not rewrite `track_genres`, which would undo the fine vocabulary.
  Keep the row rewrite as an explicit second action ("also relabel the stored tracks")
  for the case where the operator really does want one label instead of two.
- **Veto**: `apply_genre_precedence()` compares `lower(v.label) = lower(track_genres.label)`,
  a literal match. Canonicalise both sides, or every veto placed from the console
  (which offers collapsed names) stops biting as soon as the row is stored fine.
- **The console screen**: one row per collapsed name with its summed track count, and
  the fine labels nested under it with their own counts — which is more informative than
  today's flat list, not less, and keeps the picker short (63 entries, not ~200).

Three registers, stated once: index = fine, admin and station matching = collapsed,
DJ tools = fine.

## 13. Explicit non-goals


- Do not edit `state/genre-aliases.json` or `config/stations.json` in the repo —
  they are operator data, and the rollup is intentional.
- Do not exclude recently-played artists from any tool result. Demote only.
- Do not change `PER_ARTIST`, the fresh-artists mechanism, the prompt, or the
  pool builder. They are not the bug.
- Do not add dependencies, and do not let any test reach the network, a model,
  or the live `state/`.
