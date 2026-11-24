# Slop pass

First pass: 2026-11-23 · `e853854`. Second pass: 2026-11-24 · `2271bb5` + local fixes.
Suite: `uv run pytest -q` → 269 passed.

The first-pass sections below are a record, not the current work list. Checked items
are done, and their paths/line numbers are historical after the rename and SQL
refactor (`picker/shelf.py` → `picker/artists.py`, `picker/table.py` →
`picker/tracks.py`, SQL → `spica/queries/`). **The current work list is
[Second pass](#second-pass-2026-11-24) below.**

## Status

Landed before the second pass:

- **P0 #1–#7** — correctness/state/secrets — PRs #77–#83.
- **SQL/data-access #101–#112** — PRs #84–#94, including the follow-ups that fell out of it:
  test seed factory (#107), lookup batching (#108), column narrowing (#110),
  schema migrated once per process (#111), analysis error policy (#112).
  #109 was verified: every `IN (...)` goes through `db.where_in`.
- **#113 naming/prose** — code renamed (#179, #181), rules written down (#180),
  glossary added. Residual words remain; see #131 below.
- **Docs/config #82–#90, #94, #97, #99** — mostly landed.
- Guard added: no `conn.execute`/`executemany`/`executescript` outside `spica/queries/`.

The first pass found what the refactor fixed; the second pass looks at the refactor
itself, plus the items that were still open. Every item below was checked against the
code at the stated HEAD.

Priority is about risk, not effort:

- **P0** — can corrupt state, leak a secret, or put the wrong thing on air.
- **P1** — high cost paid on every pick/page/job, or two paths that have already
  diverged in a way a user can notice; also broken hermeticity and tests that cannot fail.
- **P2** — real duplication, dead code, indirection, long functions; safe to batch.
- **P3** — docs/comments/stale config, one-line wrappers, polish.

Effort: **S** ≤ half a day, **M** ≈ 1–2 days, **L** > 2 days.

---

## Second pass (2026-11-24)

**Status:** #114–#134 are addressed. #114 split `Library` into a 149-line lookup
facade plus `picker/context.py` (caches), `picker/rows.py` (row shapes) and
`picker/tools.py` (the six tools and dispatch). #127/#132/#133 were completed as far as
their scope allows (the remaining items are deliberate: separate availability
predicates, test-local fakes, and the operator-facing README summary). Suite green.


### Fixed in this revision

- **P0 regression** — `queue.recent` referenced `_when`, which the refactor removed;
  a repeated announce raised `NameError` (`spica/queue.py:281`). Now uses
  `ledger.parse_stamp`. Regression test added.
- **`jobs.coverage` counted unplayable/deleted rows** — `queries/jobs.py:105-125`
  now uses `library_size(playable=True)`, joins labels/popularity/artist counts to
  playable tracks, and uses `db.artist_expr`/`db.PLAYABLE`.
- **Readonly stores ignored `check_same_thread`** — `db.open_db` now passes it on the
  readonly branch, and `db.open_store` opens readonly stores with
  `check_same_thread=False`, so the cached analysis connection is safe from worker
  threads.
- **Missing index** — `tracks(album_artist_key)` added in `queries/index.py`.
- **`run_loudness` leaked its store on error** — body is now `try/finally`; also
  `run_analysis`/`run_loudness` share `_run_tasks` instead of two hand-copied slice
  loops, and the serial analysis path honours `ANALYSIS_THREADS`.
- **Search fallback dropped the playable predicate** — `queries/tracks.py` fallback
  now wraps the OR list and ANDs `db.PLAYABLE`.
- **Migrated indexes reported no artists** — `album_artists`, `album_artist_names`
  and `missing_similarity` now use `db.artist_expr`.
- **Memory N+1** — `picker/memory.recent_artists`/`queued_artists` batch through
  `lib.artists_by_ids`; the dead `artist_of` helper is gone.
- **Per-station coverage rescan** — `analysis.station_coverage` loads playable rows
  and the label map once, then applies each station's matcher.
- **Prompt page blocked the event loop** — the catalog/prompt build is in
  `asyncio.to_thread`; `shelf_text`/`table_text`/`show_*` context keys renamed.
- **Tests** — `conftest` resets the genre cache and isolates `genres.alias_path`;
  pick-log fixtures write `reason`, and `reason_for` is now asserted.

### First-pass items that are still valid

- #23 (two remaining sync blocks: `admin_settings.py` file/load, `admin_stations`
  station read), #26 (play window still in `queue.recent`; `queue.recent_ids`
  duplicates `ledger.PLAY_MEMORY`), #41 (unnecessary inline imports remain at
  `queue.py:115`, `admin_home.py:146`, `bootstrap.py:125`), #52 (coverage shapes),
  #73 (presence checks duplicated), #81 (war-story comments), #84 (deploy env
  omissions), #87 (two local-build sections), #91 (README duplication), #93
  (one-line wrappers), #95 (test fakes), #96 (weak test), #98 (parser tests).

### New and remaining work

**P1 — hot path or already-diverged behaviour**

114. **`Library` is still a 1018-line god object** · `spica/picker/library.py:62-1018` · **L**
     It owns six tool queries, row shaping, five caches, analysis-store access,
     loudness annotation and dispatch. Five caches invalidate by five rules
     (`:76-100,161-187,852-864,970-983`). Fix: extract row rendering/caches to
     `picker/context.py`; keep the query methods thin.

115. **`tracks.build` reads the ledgers four ways** · `spica/picker/tracks.py:124-161,528-539` · **M**
     `recently_out`, `queue.recent(1)` (twice, including `_anchor`), `candidate_ids`
     twice (`:146,161`), and `blocked_artists` re-walks recent plays. Fix: build one
     recent-context object per pick and pass it down.

116. **Recent-artist resolution still has two owners** · `spica/picker/library.py:455-464,569-596`
     vs `spica/picker/memory.py:26-46` · **M**
     `recent_labels` re-implements `memory.recent_artists` and folds the names again.
     Fix: one helper in `memory`, used by both.

117. **Backlogs still materialise full id/row sets** · `spica/analysis.py:482-485`,
     `spica/queries/analysis.py:62-69,142-146`, `spica/loudness.py:205-221` · **M**
     `backlog` loads every analysed id and playable id; loudness loads every playable
     row and the whole store. Fix: `ATTACH` the optional store and `COUNT(*)` with
     `NOT EXISTS`, or one `NOT IN` against a temp table.

118. **Loudness coverage/playable helpers diverged again** · `spica/loudness.py:224-253`,
     `spica/queries/loudness.py:57-88`, `spica/queries/analysis.py:128-146` · **M**
     `loudness.coverage` hand-builds the dict and floor-rounds instead of `db.coverage`;
     `playable_ids` is duplicated verbatim; several queries hardcode `uri != ''` instead
     of `db.PLAYABLE`. Fix: reuse `db.coverage`, `tracks.library_size`, `db.PLAYABLE`.

119. **Label grouping still lives in `navidrome`, not `queries/labels`** · `spica/navidrome.py:472-502`
     (`_collapsed`, `_members_by_shelf`, `_member_labels`); `web/admin_genres.py:9` still
     imports `genre_counts` from `navidrome` · **M**
     SQL moved, but the collapsed/member rule did not. Fix: move `collapsed`, `members`,
     `counts` into `queries/labels.py`; admin and picker call them.

120. **DDL still runs on every optional-store connect** · `spica/queries/analysis.py:46`,
     `spica/queries/loudness.py:21`, `spica/queries/enrichment.py:13`,
     `spica/queries/jobs.py:12` · **S**
     Only the index has a per-process `_READY` gate. Fix: one guarded `ensure` per store
     per process.

121. **`SELECT *` remains in hot queries** · `spica/queries/tracks.py:216,232,252,258,279`;
     `playable_tracks` defaults to `*` (`:91-96`) · **M**
     Fix: named columns for the hot paths; keep `*` only for the full-row accessors.

122. **The pick→air call sites are still copied** · `spica/app.py:244-298`,
     `spica/web/listen.py:211-233` · **M**
     `air.air_pick` centralises the airing, but `pick_for`, `force_pick` and the listener
     form each call `pick_next` + `air_pick`; `force_pick` has no LLM guard of its own.
     Fix: one `pick_and_air(st, text, *, displace=..., as_request=...)` with one guard.

123. **Admin rebuild/restart/coverage blocks are duplicated** · `spica/web/admin_home.py:143-151`,
     `spica/web/admin_stations.py:262-293`, `spica/app.py:383-396` · **S**
     `admin_reload` re-implements `shared._apply_station_script`; `admin_stations` has a
     local `_rebuild_pools` shadowing the module helper; coverage composition is copied.
     Fix: one helper per operation, both surfaces call it.

124. **Non-atomic writes remain** · `spica/bootstrap.py:120,135`,
     `spica/web/admin_settings.py:253,282`, `spica/mixer.py:156,239` · **S**
     Config/persona/offset writes bypass `atomic.atomic_write`; a crash can truncate
     `stations.json` or `spica.liq`. Fix: route text writes through the helper.

**P2 — structural debt**

125. **Dead code and unused names** · `spica/store.py:170-174` (`station`),
     `spica/config.py:126` (`QUEUE_TARGET`), `spica/app.py:14` (`Path`),
     `spica/web/shared.py` (`Counter`/`Request`), `spica/web/admin_genres.py`
     (`known_labels`), `spica/auth.py` (`base64`), `spica/requests.py` (`time`),
     `spica/bootstrap.py` (`Path`/`atomic_write`); test-only `queries/labels.labels_for_track`,
     `track_rows`, `album_rows`, `loudness.measured`; `tests/test_pick_flow.py:57`
     `FakeLibrary.count` · **S**
     Fix: delete or use; a `ruff --select F401,F811,F841` pass would catch the imports.

126. **`queue` is a facade over `ledger`** · `spica/queue.py:20-33` · **S**
     Callers split between `queue.recent_picks` (`web/admin_queue.py:56`),
     `queue.reason_for` (`plays.py:55`) and `ledger.*`. Fix: import `ledger` directly;
     keep `queue` for the slot/request files only.

127. **Cached-derivation duplication remains** · `spica/picker/prompt.py:105-151`
     (two mtime caches), `spica/picker/llm.py:35-47` (two reply contracts),
     `spica/analysis.py:62-81` + `worker.py:288-292,492` (three availability
     predicates), `spica/analysis.py:537-569` (two full-library scans in
     `refresh_artists`) · **M**
     Fix: one `_read_or_default` for prompt, one parameterised contract, one
     `available(conn)` predicate, one row fetch.

128. **Analysis DSP failures are swallowed unevenly** · `spica/analysis.py:361-368` · **S**
     `dyn_complexity`/`danceability` use bare `except: pass`, while `bpm`/`key`/`lufs`
     store `*_error`. Fix: one error field per failed step.

129. **Loudness `pending` overfetches** · `spica/loudness.py:199-210`,
     `spica/queries/loudness.py:57-66` · **S**
     It requests `limit + len(done)` rows then filters in Python; once the store is
     large this is the whole untagged set. Fix: exclude done ids in SQL (temp table or
     `ATTACH`) and keep the real `LIMIT`.

130. **`genres.merge` re-counts all labels per label** · `spica/genres.py:392-402` · **S**
     Fix: hoist the `label_queries.counts(conn)` map before the loop.

131. **Residual vocabulary from #113** · `spica/picker/llm.py:35-47` and
     `spica/picker/loop.py:477` still use the reply key `"theme"`;
     `picker/__init__.py:10` and `loop.py:175` say "brief"; `memory.py` comments say
     "offer"; `picker/tracks.py:299` talks about the station "orbiting". · **M**
     Fix: rename the reply key to `rationale` (parse old `theme` for one release if
     needed), and finish the prose pass. `admin_queue`/template were renamed in this pass.

132. **Tests still duplicate fakes and encode old words** · `tests/test_artists.py:18-48`,
     `tests/test_tracks.py:362-386`, `tests/test_parse_set.py:17`, `tests/test_pick_flow.py:22`,
     `tests/test_analysis.py:22`, `tests/test_defunnel.py:26`; `tests/test_admin.py:151`
     (weak empty-fragment assert) · **M**
     Fix: one `FakeLibrary`/`FakeTrack`/`track()` in `conftest.py`/`factories.py`; assert
     the empty body; rename `shelf`/`table` in test names.

133. **Docs/config drift still open** · `deploy/.env.example:1-110` omits
     `SPICA_TZ`, `SPICA_LIQ`, `LIQ_TELNET`, `ICECAST_OUTPUT_HOST/PORT`,
     `SPICA_MB_GENRES_DAYS`, `SPICA_PUBLIC_URL`; `deploy/README.md:104-134` still has two
     local-build paths; `README.md:152-213` duplicates `docs/dj-tools.md`;
     `pyproject.toml:4` keeps the "leather-and-brass face" voice in package metadata. · **M**
     Fix: complete the example, keep one build path, link instead of duplicate, use a
     plain package description.

134. **`docs/slop-pass.md` itself is now historical** · this file · **S**
     First-pass line numbers and module names point at files that were renamed; keep it
     as a record, but do not treat unmarked items as current. This section is current.

---

## P0 — correctness, state and secrets (✅ done: #77–#83)

1. ✅ **Slot and request temp files collide** · `spica/queue.py:38`, `spica/queue.py:83` · **S**
   `queue_file` is `<id>.txt` and `due_file` is `<id>.request`; both write
   `path.with_suffix(".tmp")` → the same `<id>.tmp`. A pick and a listener request can
   clobber each other's temp file. Fix: distinct names (`<id>.queue.tmp`,
   `<id>.request.tmp`) or one atomic-write helper with a per-suffix temp name.

2. ✅ **Icecast passwords are written world-readable** · `spica/bootstrap.py:97-106` · **S**
   `icecast.xml` contains source and admin passwords but is written with default umask,
   while every other secret file is `chmod 0600`. Fix: write with `os.open(..., 0o600)`
   or chmod before/after content, same as `config.save_*`.

3. ✅ **`mixer.set_queue` claims atomicity but can leave a partial queue** · `spica/mixer.py:110-127` · **M**
   It removes every pending id, then pushes the new ones; a mid-loop `OSError` returns
   `False` with the old queue already gone, and Liquidsoap `ERROR` replies are not
   treated as failures at all. Fix: treat `ERROR` as failure, restore on failure, or
   document it as best-effort and let `promote_requests` recover.

4. ✅ **Admin secret creation is racy and the signature compare is not constant-time** · `spica/auth.py:28-34`, `spica/auth.py:54` · **S**
   The session secret is created with `exists()` + `write_text` (two processes can write
   different secrets); the cookie signature is compared with `==` while the password uses
   `compare_digest`. Fix: exclusive create (`open("x")`), `hmac.compare_digest`.

5. ✅ **`analysis.coverage` counts unplayable tracks** · `spica/analysis.py:551-558` · **S**
   `total` is `count(*) FROM tracks` while `todo`, `pending`, `station_coverage` and
   loudness all use `uri != ''`; the admin percentage understates real progress. Fix:
   filter the total and `present` set to `uri != ''`.

6. ✅ **`refresh_artists` mixes model versions** · `spica/analysis.py:625-643` · **S**
   `vector_map`/`feature_map` filter `model_version = MODEL_VERSION`; the artist-centroid
   refresh selects every `track_analysis` row. Old vectors leak into artist vectors and
   therefore into shelf exclusion / `nearest_artists`. Fix: add the `model_version`
   predicate to the `by_track` query.

7. ✅ **Request de-duplication compares the wrong key** · `spica/requests.py:112-121` · **S**
   `attach_track` matches on `(t, text)`, which is not stable when the same text is
   submitted twice in one second. Fix: give each request an id on `record` and attach by
   it.

---

## P1 — high-impact duplication, performance, consistency

8. **Two `Library` instances share neither caches nor `draw_seed`** · `spica/app.py:26,176`, `spica/store.py:154-161` · **M**
   The app builds `LIB` in `lifespan` and `store.library()` builds a second; `listen.py`
   and `admin_settings.py` use the latter. Analysis caches, vector caches and the per-pick
   seed are split, and `app.lib()` asserts if lifespan did not run. Fix: one
   `store.library()` and delete the `LIB` global.

9. **The pick→air flow is copied four times, already divergent** · `spica/app.py:96-123`, `spica/app.py:277-298`, `spica/app.py:335-351`, `spica/web/listen.py:214-242` · **M**
   `queue.replace`/`queue.push`, `pool.rewind_to`/`skip_past`, `record_pick`, presence and
   `LAST` are assembled by hand in each path; `listen.py` also reconciles requests
   differently. Fix: one `air_pick(st, pick, *, displace=…)` used by the loop, REST,
   forced pick and the request form.

10. **`by_id` opens `loudness.db` once per row** · `spica/picker/library.py:576-578,951-967`, `spica/loudness.py:176-189` · **M**
    `_full` calls `loudness.measured(row["id"])`, which opens and closes SQLite for every
    row; `shelf._blocked`, `table._offered_artists`, `_absence` and `_longest_absent` call
    `by_id` in loops. Fix: preload `loudness.all_measured()` once per `Library`/`ctx`, or
    add a lightweight id→(artist,title,album) lookup that does not annotate gains.

11. **The station matcher and label map are rebuilt per tool call** · `spica/navidrome.py:529-605`, `spica/picker/library.py:130-140` · **M**
    `rows_on_station` builds a fresh `station_matcher`, which scans `track_genres` and
    canonicalises every label (each `canonical` stats the alias file). `search`, `filter`,
    `catalogue`, `nearest`, `similar` all pay it. Fix: cache matcher + `labels_by_track`
    on `Library` keyed by index mtime (same lifetime as `_rows_cache`).

12. **`table._groups.turns` is O(n² log n)** · `spica/picker/table.py:347-350` · **S**
    `turns(c)` calls `_percentile([x["sim"] for x in candidates], 25)` for each candidate,
    rebuilding and sorting the full list every time. Fix: compute the 25th percentile once
    before the comprehension.

13. **`analysis.backlog` materialises up to a million rows just to count them** · `spica/analysis.py:535-540`, `spica/worker.py:509-510` · **M**
    `_analysis_due` runs every idle tick, opens a read-only connection it never closes,
    then `backlog` builds and sorts the entire todo list. Fix: `SELECT count(*)` with an
    `IN`/join against the store, and close the connection.

14. **Loudness job ignores the `LOUDNESS_MEASURE` opt-out** · `spica/worker.py:398-414` · **S**
    `_loudness_due` honours `LOUDNESS_MEASURE`, but a manually queued `loudness` job only
    checks `config.LOUDNESS`, so an operator who asked for tag-only still gets ffmpeg work.
    Fix: guard `run_loudness` with `LOUDNESS_MEASURE`.

15. **Analysis store is never closed in `run_analysis`** · `spica/worker.py:327,394` · **S**
    `run_loudness` closes its store; `run_analysis` returns on three paths with it open.
    Fix: `try/finally: store.close()`.

16. **`filters`/`roster`/`crate`/`recent_labels` re-scan `track_genres` repeatedly** · `spica/picker/library.py:354,406,509,555` · **M**
    A single pick can load the label map four or five times, and `_context` loads it again.
    Fix: one memoised `Library._labels()` (mtime-keyed) passed through `_context`.

17. **`_member_labels` scans/canonicalises all `track_genres` per call and twice per query** · `spica/navidrome.py:643-650`, `:733,746,778` · **M**
    `label_albums`/`label_tracks` call it once for the `IN` list and again for the args;
    `label_explorer` calls it per selected label. Fix: build the collapsed→members map once
    per request and pass it in.

18. **`apply_genre_precedence` does N+1 DELETE/INSERT/COUNT per track** · `spica/navidrome.py:481-502` · **M**
    The rebuild loop issues a `DELETE`, an `executemany` and a `COUNT(*)` for every artist
    that has MusicBrainz genres. Fix: batch deletes/inserts and compute `tagged` with one
    grouped query.

19. **Test suite is not fully hermetic** · `tests/test_admin.py:34-66` · **S**
    The fixture patches `config.STATE_DIR` but not `genres._FILE`, which is bound at import
    (`spica/genres.py:150`); admin/genre rendering can read or write the live station's
    `state/genre-aliases.json`. Fix: patch `genres._FILE` (and reset `genres._cache`) in the
    shared console fixture.

20. **One request test can never fail** · `tests/test_admin.py:224-232` · **S**
    It checks `state/history/alt.requests.jsonl`; real path is
    `state/history/requests/alt.jsonl` (`spica/requests.py:30`). It also asserts inside
    `if requests_file.exists()`. Fix: assert the file exists and contains the text.

21. **Job kinds live in five places** · `spica/jobs.py:19-31`, `spica/worker.py:568-577`, templates · **M**
    `KINDS`, `KIND_LABELS`, `worker.HANDLERS`, `worker.FOLLOWUPS` and the admin buttons can
    drift; a missing handler is a runtime `KeyError` on a queued job. Fix: derive
    `HANDLERS`/labels/UI from one registry validated at import.

22. **The three "job is due" predicates are copies** · `spica/worker.py:490-537,580-615` · **M**
    `_analysis_due`, `_loudness_due` and `_retry_due` repeat the pending-job query, the
    one-hour failure window and the backlog try/except. Fix: one `_due(conn, kind, enabled,
    backlog)` helper; keep only the kind-specific enable/backlog lambdas.

23. **Blocking DB/mixer work runs on the event loop** · `spica/web/admin_home.py:63-138`, `spica/web/admin_stations.py:69-212`, `spica/web/admin_genres.py:28-52,124-204`, `spica/web/listen.py:197-211` · **M**
    Several `async def` handlers call `rebuild_index`, `write_pools`, `now_playing`,
    `history_of`, `request_rows`, `label_explorer` and `subsonic.scan_status` directly.
    Fix: `await asyncio.to_thread(...)` consistently, as `station_page` already does.

24. **Admin authentication is repeated per handler** · `spica/web/__init__.py:29-40` · **M**
    ~17 GET handlers hand-roll the redirect check; `admin_queue.py:100` documents that one
    shipped without it. Fix: attach `Depends(auth.require_admin)` (or a redirecting
    variant) to the admin routers instead of each route.

25. **`.env.example` points at the wrong names** · `.env.example:1-8` · **S**
    It says copy to `radio.env` and sets `RADIO_STATE`/`RADIO_CONFIG`/`radio.liq`; the code
    reads `SPICA_STATE`/`SPICA_CONFIG` and writes `spica.liq`. Following the file changes
    nothing. Fix: rewrite to `spica.env`/`SPICA_*`/`spica.liq`, or delete and point at
    `deploy/.env.example`.

26. **Duplicate/near-duplicate request, play and pick memory** · `spica/queue.py:315-414`, `spica/requests.py:39-109`, `spica/plays.py:24-62` · **M**
    Three windows (40 plays / 60 picks / 8 h / 15 min / 120 s) and three JSONL readers;
    `recently_out` hardcodes `recent_ids(40)` while exposing a different `picks` window.
    Fix: one ledger module with `read_tail`, `recent_plays`, `recent_picks`, `offered`,
    and one documented "recent memory" policy.

27. **`provider_settings()` re-reads and parses JSON on every enrichment request** · `spica/config.py:333-346` · **M**
    Called at least once per Last.fm/MusicBrainz lookup. Fix: cache with invalidation from
    `save_provider_settings`.

28. **Enrichment HTTP is four hand-rolled clients** · `spica/enrich.py:46-49,93-145,304-379` · **M**
    `_get_json`, `lfm_track`, `lb_popularity` and `_mb_get` each build requests, retry and
    swallow exceptions differently; `lfm_track` even skips `_throttle`. Fix: one small
    provider client (`get_json`, throttle, retries, error policy) and provider methods on
    top.

29. **Operator MusicBrainz pins never match most names** · `spica/enrich.py:385-398,442` · **S**
    Overrides are keyed with `.strip().lower()` but looked up with `genres.fold()`, so
    `Frost*`, `AC/DC`, `blink-182` cannot match. Fix: key overrides with `genres.fold`.

---

## P2 — structural debt: duplication, dead code, indirection

30. **Three seed-hash helpers** · `spica/picker/library.py:425-427`, `shelf.py:71-72`, `table.py:645-646` · **S**
    All are `blake2b(f"{seed}|{key}", digest_size=8)`. Fix: one shared `seed_key`.

31. **Shelf and table duplicate blocked-artist logic, already diverged** · `spica/picker/shelf.py:146-212`, `table.py:526-552` · **M**
    Same `artist_of`/`name_of` + `queue.recent` + `last_shelf` + `queued_ids`; shelf also
    blocks `recent_pick_artists`, table does not. Fix: one `blocked_artists(lib, station,
    include_picks=…)`, with the difference explicit.

32. **Absence is computed three ways** · `spica/picker/table.py:555-564`, `shelf.py:333-358`, `library.py:555-574` · **M**
    `_absence`, `_longest_absent` and `recent_labels` each walk `queue.recent` and resolve
    artists through `by_id`. `ABSENCE = 40` is defined twice. Fix: one
    `queue.artist_absence(station)` map plus `queue.recent_artists(station)`.

33. **Two different `_spread_decades` implementations** · `spica/picker/shelf.py:315-330`, `table.py:430-453` · **M**
    Same "two eras" rule, different replacement policies. Table's version also mutates
    `used_ids`/`used_artists` without updating `label_counts`. Fix: share one helper; fix or
    drop the label-cap bookkeeping before the swap.

34. **Table rebuilds candidates twice** · `spica/picker/table.py:234-271,472-503` · **M**
    `_candidates` and `_unmeasured` duplicate played/queued/on-air/blocked/artist-key
    filtering and row shaping. Fix: one filter returning `measured` as a flag.

35. **Artist vector/feature queries duplicate** · `spica/picker/table.py:591-618` · **S**
    `_artist_vector` and `_artist_feature` run the same `SELECT id … album_artist_key = ?`
    separately, and `_median`/`_percentile` reimplement `statistics`. Fix: one query and
    `statistics.median` where a true median is wanted.

36. **`plan.clear(reason)` is a lie, `remaining` is test-only** · `spica/plan.py:51-57,89-91` · **S**
    `clear` does `del reason`; callers pass `"listener request"` expecting a log line.
    `remaining` has no production caller. Fix: remove the parameter (or log it) and remove
    `remaining` plus its tests, or use it in the admin.

37. **`recently_out` parameters are dead** · `spica/queue.py:409-414` · **S**
    Every caller uses defaults; `picks`/`hours` are never overridden, and the play window
    is the hardcoded `40`. Fix: drop the parameters and name the ledger window constant.

38. **Dead/test-only functions** · various · **S**
    `Library.count`, `icecast.listeners`, `queue.waiting_requests`, `subsonic.ping` have no
    callers; `navidrome.genres` and `label_albums` are test-only. Fix: delete (and update
    tests), or put them to use.

39. **`crate` duplicates the `_dealt_seed` reset** · `spica/picker/library.py:454-459,479-482` · **S**
    `_remember_dealt` already resets by seed; `crate` repeats it. Also assigns
    `self._crate_skip` which is never read. Fix: call `_remember_dealt(())` and delete
    `_crate_skip`.

40. **Manual alias delete bypasses `genres` and folds wrongly** · `spica/web/admin_genres.py:102-114`, `spica/genres.py:327-339` · **S**
    The handler reads/writes the private `_FILE` non-atomically and compares
    `a.get("from") != source` even though keys are stored folded. Fix: add
    `genres.remove_alias(source)` (folded, atomic) and use it; rebuild pools like merge.

41. **Six inline `from .. import genres` in one module** · `spica/web/admin_genres.py:32,73,95,105,134,185` · **S**
    Also `admin_settings.py:222-223` and `shared.py:171,304,341`; `app.py` imports five
    modules inside handlers. Fix: import once at module level.

42. **`live` is rebound to mean two different things** · `spica/picker/loop.py:177,301` · **S**
    First the Icecast mount, then the listener-request list. Fix: rename the second.

43. **`plan_pick` is ~285 lines with four closures** · `spica/picker/loop.py:159-443` · **L**
    Brief construction, tool loop, retry and `finish` are nested inside one function. Fix:
    extract `_brief(...)`, `_tool_rounds(...)`, `_finish(...)`; keep the flow readable.

44. **The trailing `ANSWER_NOW` at the end of the tool loop is unreachable** · `spica/picker/loop.py:368-443` · **S**
    The last round is already `no_tools=True`, so the no-calls branch always returns. Fix:
    delete the trailing block or route the last round through it.

45. **`RECENT_PLAYS` is unused and inaccurate** · `spica/picker/loop.py:36` · **S**
    Value is 5, comment says three, nothing references it. Fix: delete or use it for the
    hardcoded windows (`queue.recent(station, 7)` etc.).

46. **Three ledger timestamp parsers** · `spica/queue.py:366,439`, `spica/picker/loop.py:75`, `spica/plays.py:28`, `spica/web/shared.py:192`, `spica/web/admin_queue.py:41` · **S**
    `calendar.timegm`, `strptime`, `fromisoformat` variants for the same stamp. Fix: one
    `parse_stamp`/`utc_stamp` pair in `queue`.

47. **Three JSONL tail readers** · `spica/queue.py:285-312`, `spica/requests.py:39-63`, `spica/mixer.py:187-242` · **M**
    Different incremental strategies for the same append-only files. Fix: one small
    `ledger.py` used by queue/requests/mixer.

48. **Atomic write is reimplemented six times** · `queue.py:38/83`, `plan.py:38`, `requests.py:83`, `web/shared.py:262`, `pool.py:122/252`, `analysis.py:202` · **S**
    Fix: one `atomic_write(path, text)`; all callers get distinct temp names and 0600 where
    appropriate.

49. **`_tail_lines` bounds are fine but the record writers are not** · `spica/queue.py:274-282` · **S**
    `record_pick` trims a >8 MB log with a non-atomic `write_text` while other writers may
    append. Fix: trim via the atomic helper.

50. **`store` has four module globals and manual invalidation** · `spica/store.py:18-102,151-161` · **M**
    `_conn`, `_stations`, `_tracks`, `_lib` are mutated from app/worker/web with no lock or
    staleness contract; `app.py:40` patches index staleness by hand. Fix: one small state
    object with `reload()`, or inject the handles.

51. **`db_path`/`_db`/`_conn`/`stations` wrappers** · `spica/store.py:14`, `spica/web/shared.py:51,329`, `spica/worker.py:36` · **S**
    One-line forwarders with a single real caller each. Fix: inline most; keep `store.conn`.

52. **`loudness` and `analysis` duplicate store plumbing** · `spica/loudness.py:150-282`, `spica/analysis.py:134-154,494-604` · **M**
    Two `connect`, two `save`, two `pending/backlog/coverage` shapes, different availability
    checks and rounding. Fix: a shared `sqlite_store` helper and one `coverage(known, total)`
    formatter.

53. **`loudness.pending` ignores its `limit` parameter in SQL** · `spica/loudness.py:220-233` · **S**
    It loads every playable row and breaks at `limit` in Python; `_done_ids` loads the whole
    store. Fix: one query with `LIMIT` and a single done-set.

54. **`_analysis_task` and `fetch_and_analyze` are the same helper** · `spica/worker.py:304-309,348-352` · **S**
    Fix: one module-level `fetch_and_analyze(analyzer, track_id)`.

55. **`run_popularity` sleeps 0.35 s per track instead of using the shared throttle** · `spica/worker.py:173` · **S**
    Fix: let `enrich` throttle Last.fm as the other paths do.

56. **Job-kind requeue is a magic tuple** · `spica/worker.py:680` · **S**
    `("analysis", "loudness")` duplicated from `FOLLOWUPS`/`KINDS`. Fix: derive from the job
    registry (see #21).

57. **`pool` reloads global tables per station** · `spica/pool.py:24-50,242` · **M**
    `track_popularity` and `loudness.all_measured()` are loaded once per station inside
    `write_pools`; `_recent_context` does one `SELECT` per recent play. Fix: hoist loads
    and batch the id lookup.

58. **`pool.order` is O(n²)** · `spica/pool.py:84-102` · **M**
    `pool.pop(i)` inside the scan copies the remaining list every time. Fix: free-index set
    or deque, or build from a heap.

59. **`genres.canonical` stats the alias file on every call** · `spica/genres.py:183-204,254-269` · **M**
    `operator_aliases()` is cached by mtime but `canonical` calls it constantly, and
    `explain` repeats the whole branch ladder. Fix: a cached `shelf_key(label)` and have
    `canonical`/`explain` share one resolver.

60. **`rows_for_station` and `Library.on_station` duplicate the empty-match fallback** · `spica/navidrome.py:608-623`, `spica/picker/library.py:583-596` · **M**
    `on_station` re-scans every track to reproduce "rules matched nothing → whole library".
    Fix: expose the fallback state from `rows_for_station` (or a cached `matcher` + match
    count) and call it.

61. **`admin_settings_test` is a 75-line handler** · `spica/web/admin_settings.py:127-202` · **M**
    Payload building, HTTP call and result formatting in one place. Fix: `_llm_probe(...)`.

62. **`admin_station_save` is 74 lines mixing rename, validation, pools and restart** · `spica/web/admin_stations.py:138-212` · **M**
    Fix: extract `_rename_station`, `_apply_form`, `_rebuild_and_reload`.

63. **`now_playing` is an N+1 resolver** · `spica/web/shared.py:84-158` · **M**
    Up to two `find_track` calls, an album query and `next_up`'s query per page poll. Fix:
    one id/name lookup path and accept the Icecast snapshot.

64. **`history_of` does three DB lookups per row** · `spica/web/shared.py:181-208` · **M**
    `album_for`, `find_track` and `cover_tag` per entry. Fix: batch album/cover lookup by id.

65. **`admin_library` recomputes five full-table counts on every load** · `spica/jobs.py:184-219`, `spica/web/admin_home.py:75-81` · **M**
    Fix: cache the coverage payload for a few seconds, or compute it in one query.

66. **Station lookup / pool size / stream payload are duplicated** · `spica/app.py:158-169`, `spica/web/shared.py:55-59,253-257`, `spica/web/listen.py:166-194`, `spica/web/media.py:68` · **S**
    Three `_station` implementations with different failure modes, two identical
    `_pool_size`/`pool_size`, two stream-payload builders. Fix: one helper each.

67. **`admin_genres` assembles the same template context twice** · `spica/web/admin_genres.py:149-157,196-204` · **S**
    Fix: `_genre_tracks_context(conn, labels)`.

68. **`_genre_tracks` and `admin_genres` compile private `_FILE`/`_cache` state across modules** · `spica/web/admin_genres.py:107`, `tests/` · **S**
    Fix: keep alias mutation inside `genres` (see #40).

69. **`icecast.refresh` can crash on malformed status JSON** · `spica/icecast.py:50-67` · **S**
    `int(src.get("listeners"))` is outside the try; one bad mount kills the refresher thread's
    iteration. Fix: guard the int and narrow the except.

70. **`start_refresher` is not actually idempotent under concurrency** · `spica/icecast.py:79-91` · **S**
    Two callers can both see `_thread is None`. Fix: a lock.

71. **`mixer.pending` and `_pending_rids` duplicate the telnet query** · `spica/mixer.py:30-44,73-81` · **S**
    Fix: `pending = len(_pending_rids(...) or [])`.

72. **`mixer.now_tail` can return half a line** · `spica/mixer.py:162-184` · **S**
    Fixed 4096-byte window; a long line is cut. Fix: back up to the previous newline.

73. **`presence.listening` and `presence.reason` duplicate the same three checks** · `spica/presence.py:73-95` · **S**
    Fix: compute one summary and derive both.

74. **`plays` and `queue` disagree on the duplicate-announce window** · `spica/plays.py:22` vs `spica/queue.py:431` · **S**
    15 min vs 120 s for the same "same announce twice" event. Fix: one constant and one
    rationale.

75. **`subsonic` and `enrich` use different HTTP clients and retry policies** · `spica/subsonic.py:90-103`, `spica/enrich.py:46-49,361-379` · **M**
    urllib here, httpx in icecast/llm/web; `call` has no retry while `_mb_get` retries three
    times. Fix: one HTTP policy; streaming downloads via `copyfileobj` rather than
    `read()`-into-memory.

76. **`subsonic._salt` has three creation branches** · `spica/subsonic.py:39-70` · **S**
    A 16-hex token does not need `_new_salt` plus an exclusive-open fallback. Fix: one
    `O_CREAT|O_EXCL` path.

77. **`liqgen.main` silently defaults the source password to `hackme`** · `spica/liqgen.py:138` · **S**
    Fix: fail loudly when unset, as the API path does.

78. **`spica.liq` is written in four places, non-atomically** · `spica/app.py:382`, `spica/liqgen.py:140`, `spica/web/admin_home.py:149`, `spica/web/shared.py:309` · **S**
    Fix: one `liqgen.write(stations, password)` with an atomic replace.

79. **`picker/__init__.py` re-exports a surface nothing uses** · `spica/picker/__init__.py` · **S**
    Only `Library`, `Pick`, `pick_next`, `DEFAULT_PERSONA` are consumed; trim `__all__`.

80. **Module docstrings are design diaries** · `spica/analysis.py:1-26`, `spica/queue.py:1-17`, `spica/worker.py:1-16`, `spica/picker/loop.py:1-6`, `spica/loudness.py:1-17`, `spica/pool.py:1-10` · **M**
    They narrate past bugs and fixes ("this used to…", "the old …") instead of stating the
    current invariant. Fix: keep the contract and the one non-obvious fact; move history to
    the PR/commit. This is the largest single source of noise in the codebase.

81. **Historical war-story comments sit next to live logic** · e.g. `spica/picker/library.py:146-151,190-204,330-339`, `spica/navidrome.py:169-214`, `spica/web/shared.py:41-45`, `spica/web/admin_queue.py:100-101`, `spica/analysis.py:543-550` · **M**
    Fix: rewrite as present-tense rules. Comments that explain *why a constraint exists*
    are valuable; comments that explain what the previous implementation did are not.

---

## P3 — documentation, config and polish

82. **README request-log path is wrong** · `README.md:318-322,918` · **S**
    It documents `state/requests/<station>.jsonl`; real path is
    `state/history/requests/<station>.jsonl`. Also the layout omits `providers.json`,
    `dj.json`, `dj-shelf.md`, `dj-table.md`.

83. **README has stale version and a broken anchor** · `README.md:805,822` · **S**
    `SPICA_TAG=0.14.0` (repo is 0.15.2); `deploy/README.md#audio-analysis` should be
    `#audio-analysis-and-loudness`.

84. **`.env.example` omits required vars and contradicts defaults** · `.env.example:2-38` · **S**
    Missing `ICECAST_SOURCE_PASSWORD`, `ICECAST_ADMIN_PASSWORD`, `SPICA_ADMIN_PASSWORD`,
    `ICECAST_OUTPUT_HOST/PORT`, `SPICA_MB_GENRES_DAYS`, `SPICA_TZ`, `LIQ_TELNET`; the
    `MB_USER_AGENT=radio/0.1` example contradicts the code default `Spica/1.0`.

85. **`deploy/.env.example`/compose do not pass `SPICA_TZ`** · `deploy/.env.example`, `deploy/compose*.yaml` · **S**
    Compose passes `TZ`, but the ledger fallback reads `SPICA_TZ`
    (`spica/web/shared.py:175`), so the container's TZ is ignored. Fix: add
    `SPICA_TZ=${TZ}` to the env files.

86. **`compose.analysis.yaml` is documented as amd64-only but not pinned** · `deploy/compose.analysis.yaml:8-11` · **S**
    Fix: `platform: linux/amd64`, or document the arm64 limitation next to the overlay.

87. **Two conflicting local-build sections in the deploy guide** · `deploy/README.md:104-142` · **M**
    One builds `:0.15.2` tags, one builds `:dev` tags and then says to use `SPICA_TAG=dev`.
    Fix: keep one path and make tags consistent.

88. **CONTRIBUTING describes a test mechanism that does not exist** · `CONTRIBUTING.md:46-48` · **S**
    It says every test points `SPICA_STATE` at a temp dir; `conftest.py` monkeypatches
    `config.STATE_DIR`. Fix: reword.

89. **`docs/dj-tools.md` has stale character count and changelog prose** · `docs/dj-tools.md:92,302-306,440` · **S**
    "21-character" ids are 22; "went from 10,681 to 3,083 characters" is history. Fix:
    correct the fact, move history out.

90. **`docs/handoff-drop-lastfm.md` is a stale, untracked planning artefact** · whole file · **S**
    Status says "planned, not started; wait for feat-loudness", but loudness is on main; the
    baseline and every line reference are stale. Fix: delete, or refresh against HEAD and
    keep it as an issue, not a doc.

91. **README carries design-diary prose** · `README.md:152-213,673-679` · **M**
    Narrative about how the station "ended up circling", hex contrast ratios, and feature
    bullets that duplicate `docs/dj-tools.md`. Fix: keep the operator contract; move
    rationale to `docs/`.

92. **`_sheet`/`_conn`/`stations()`/`lib()` wrappers** · `spica/web/shared.py:51,329`, `spica/app.py:32`, `spica/store.py:14` · **S**
    Fix: inline the trivial ones; keep the ones that carry a contract.

93. **One-line `for_tag`, `lfm_similar`, `missing_musicbrainz` wrappers** · `spica/loudness.py:107-112`, `spica/enrich.py:125-127`, `spica/worker.py:201-203` · **S**
    Fix: inline at the single call site, or make the wrapper the only API and delete the
    duplicate.

94. **`_count`, `_count`-style helpers and unreachable `len(messages) > 1`** · `spica/picker/loop.py:82-97,319` · **S**
    Fix: keep `_count` (it has a real contract) but remove the always-true condition.

95. **`tests/` duplicates fixtures and fakes** · `tests/test_admin.py:34-66`, `tests/test_admin_genres.py:26-60`, `tests/test_analysis.py:19-36`, `test_defunnel.py`, `test_mb_genres.py`, `test_station_rules.py`, `test_tool_payloads.py`, `test_parse_set.py:17-36` · **L**
    The console fixture, DB seeding, `track()` tuple and five `FakeLibrary`/`FakeTrack`
    classes are repeated. Fix: one `conftest.py` with `console`, `seed_tracks`, `FakeLibrary`.

96. **Weak tests that can pass without the feature** · `tests/test_admin.py:143-148,224-232`, `tests/test_admin_genres.py:198-202,221-226`, `tests/test_shelf.py:239-241`, `tests/test_analysis.py:62-68` · **S**
    Fix: assert the observable result (the row, the file, the empty body), not status 200 or
    a dataclass default.

97. **Tests reimplement production folding** · `tests/test_admin_genres.py:99-104` · **S**
    `has_alias` uses `.lower()` instead of `genres.fold`, hiding punctuation regressions.
    Fix: import the real helper.

98. **`test_parse_set` and `test_llm` both cover `json_object`** · `tests/test_parse_set.py:54-72`, `tests/test_llm.py:10-24` · **S**
    Fix: keep parsing in `test_llm`; assert only `parse_set` legality here.

99. **`store.norm`/`norm_loose` are duplicated in spirit by `navidrome.name_key`** · `spica/store.py:46-62`, `spica/navidrome.py:46-60` · **M**
    Two normalisation philosophies (Icecast mangling vs artist identity) with overlapping
    responsibilities. Fix: document which is which and keep them in one module (`names.py`)
    so callers cannot pick the wrong one by accident.

100. **`SPICA_LIQ` is read but undocumented; `MB_USER_AGENT` is read outside `provider_settings`** · `spica/liqgen.py:139`, `spica/config.py:34` · **S**
     Fix: add to the env docs; mention the env default next to the admin override.

---

## SQL and data access (added after the first pass) (✅ done: #84–#94)

There are 294 SQL/execute sites across 17 modules and three SQLite stores
(`index.db`, `analysis.db`, `loudness.db`), with no data-access layer. SQL appears in
HTTP routes, the REST API, CLI-ish helpers and tests, so the schema is understood in
many places at once. This is not a reason to add an ORM; it is a reason to put SQL in a
few named query functions and stop every module composing its own statements.

101. ✅ **SQL is spread across the web/API layer** · `spica/web/shared.py:76,140,346`,
     `spica/web/admin_genres.py:36,50,120`, `spica/web/admin_stations.py:102,111`,
     `spica/web/admin_home.py:114`, `spica/app.py:396,417` · **M**
     Routes and the API build queries over `tracks`, `track_genres` and `artist_mbids`
     directly. Fix: move those into query functions (`navidrome`, `jobs`, `enrich`) and
     let the web layer read return values.

102. ✅ **The artist expression is repeated eight times** · `spica/worker.py:62,64,194,195,220`,
     `spica/picker/library.py:762`, `spica/navidrome.py:475`, `spica/web/admin_stations.py:112` · **S**
     `COALESCE(NULLIF(album_artist, ''), artist)` is the record-artist rule and is copied
     into every module that groups tracks. Fix: one `ARTIST_EXPR` constant (or a `tracks`
     view exposing `record_artist`) and use it everywhere.

103. ✅ **`SELECT * FROM tracks WHERE id = ?` is reinvented six times** · `spica/picker/library.py:567,577,590,712`,
     `spica/pool.py:214`, `spica/web/shared.py:77` · **S**
     Some need the whole row, some need three columns, some silently return `None` for an
     empty id. Fix: `track_by_id(conn, id)` and `tracks_by_ids(conn, ids)` in one module;
     callers pass the columns they want.

104. ✅ **The same counts are written as ad-hoc SQL** · `count(*) FROM tracks` in
     `spica/jobs.py:186`, `spica/analysis.py:551`, `spica/web/admin_genres.py:50`,
     `spica/web/admin_home.py:114`; `uri != ''` appears 13 times · **S**
     This is where the playable/unplayable mistakes come from (#5). Fix: `library_size(conn,
     playable=True)` and a single `PLAYABLE` fragment.

105. ✅ **Genre grouping has four owners** · `spica/navidrome.py:402,626,643` (`labels_by_track`,
     `_collapsed`, `_member_labels`), `spica/picker/library.py:555`,
     `spica/web/admin_genres.py:117` (`_all_labels`), `spica/web/admin_stations.py:100-105` · **M**
     Each re-implements "stored label → collapsed shelf → members". Fix: one `labels.py`
     with `by_track`, `collapsed`, `members`, `counts`, `for_tracks`; admin and picker call
     those functions.

106. ✅ **Three SQLite stores each own their connect/schema/save/coverage code** ·
     `spica/navidrome.py:63-200`, `spica/analysis.py:99-154,472-604`, `spica/loudness.py:38-282`,
     plus `enrich.ensure_schema`/`jobs.ensure_schema` · **M**
     Different read-only handling, timeout, rounding and availability checks; DDL runs on
     connect. Fix: a small `db.py` (`open_db(path, schema, readonly=…)`, `coverage(known,
     total)`) and one place per store for schema + migrations.

107. ✅ **Schema knowledge leaks into tests** · `tests/test_admin.py:353-363`,
     `tests/test_analysis.py:19-36`, `tests/test_defunnel.py`, `tests/test_mb_genres.py`,
     `tests/test_station_rules.py`, `tests/test_tool_payloads.py` · **L**
     Tests insert raw `tracks` rows with a hand-maintained column list, so a schema change
     breaks fixtures instead of the code under test. Fix: once #103/#106 exist, seed through
     `tracks.insert_many(...)` / a `tests/factories.py` helper.

108. ✅ **Rows are fetched one at a time inside loops** · `spica/picker/library.py:567,712` and
     `:318-321`, `spica/picker/shelf.py:151-155,344`, `spica/picker/table.py:282-286`,
     `spica/pool.py:211-215`, `spica/web/shared.py:76,140,196-206`,
     `spica/analysis.py:615-643`, `spica/loudness.py:176-189` · **M**
     The classic N+1: resolve a play's artist, a table candidate's vectors, a history row's
     album, a loudness measurement. Fix: batch by id (`WHERE id IN (…)`) in the query
     functions; callers pass the ids they already have.

109. ✅ **`IN (...)` placeholders are hand-built in six places** · `spica/navidrome.py:733,746,783,791,806`,
     `spica/picker/library.py:619-621,635-641` · **S**
     Each repeats `", ".join("?" * len(items))` and has its own empty-list handling.
     Fix: `where_in(column, values) -> (sql, args)` and use it.

110. ✅ **`SELECT *` in hot paths** · `spica/picker/library.py:577,590,712`,
     `spica/navidrome.py:609`, `spica/navidrome.py:474-476` · **M**
     Every call pulls `replaygain_*`, `mbz_*`, `fetched_at`, `artist_id`, etc., even when
     the caller needs artist/title/album. `rows_for_station` also scans the whole table and
     filters in Python (#60). Fix: named-column queries for the hot paths and a station
     query that filters on the indexed columns where possible.

111. ✅ **DDL and `PRAGMA table_info` run on every `connect`** · `spica/navidrome.py:63-200`
     (three `PRAGMA` scans, `CREATE TABLE`/`CREATE INDEX` per connection) · **S**
     Fix: read the columns once, keep one migration function, and let `connect` only open.

112. ✅ **`except sqlite3.Error: return {}` hides schema drift** · `spica/analysis.py:693-695,725-727,798-800,838-840`,
     `spica/loudness.py` callers · **S**
     A missing column becomes "no analysis" rather than an error. Fix: let the query
     functions raise; the picker can catch at the `Library` boundary and log once.

Recommended shape: not an ORM and not a repository-per-table hierarchy. A `spica/db.py`
(open/migrate/coverage/atomic helpers), a `spica/queries/` package with plain functions
returning `sqlite3.Row`/dicts (`tracks.py`, `labels.py`, `jobs.py`, `analysis.py`,
`loudness.py`), and the rule that no `conn.execute` appears outside those modules. That
also makes #11/#16/#17/#18/#60/#65/#107 fall out rather than needing separate fixes.

---

## Do these first (they delete clusters)

These are the high-leverage refactors; each removes several items above and prevents
further drift:

1. **`spica/ledger.py`** — one JSONL tail reader, one timestamp parser, one atomic append,
   one "recent memory" policy. Absorbs #26, #37, #46, #47, #48, #49, parts of #74.
2. **`spica/names.py`** — `name_key` (identity), `store.norm`/`norm_loose` (Icecast title
   matching), one `seed_key`. Absorbs #30, #99 and removes the import-cycle workarounds.
3. **One `Library` + memoised `labels_by_track`/matcher** — absorbs #8, #10, #11, #16,
   #17, #60 and most of the per-call scans.
4. **One `blocked_artists`/`absence` in `queue`** — absorbs #31, #32, #33 and makes the
   shelf/table behave identically by construction.
5. **One `air_pick` service function** — absorbs #9, #23, #66 and makes REST and HTML
   share one path.
6. **One job registry** — absorbs #21, #22, #56 and the admin buttons.
7. **One `atomic_write`** — absorbs #1, #2, #4 (secret creation), #48, #78.
8. **Router-level admin auth** — removes ~17 repeated checks (#24).
9. ✅ **`spica/db.py` + `spica/queries/`** — one place to open/migrate a store and one named
   function per question; no `conn.execute` outside it. Absorbs #101–#112 and makes
   #5/#11/#16/#17/#18/#60/#65/#103/#107 fall out.

## Suggested execution order

- **Batch 1 (P0, safe, S/M):** #1, #2, #3, #4, #5, #6, #7, #19, #20.
- **Batch 2 (P1 hot paths):** #8, #10, #11, #12, #13, #14, #15, #18, #27, #28, #29.
- **Batch 3 (P1 structure):** #9, #16, #17, #21, #22, #23, #24, #25, #26.
- **Batch 4 (P2 consolidation):** `db.py`/`queries/`, ledger/names/library/blocked/
  job-registry refactors, then the individual cleanups that fall out.
- **Batch 4b (SQL migration):** #102–#112 as the query modules land; move web/API SQL
  (#101) last, once the functions exist.
- **Batch 5 (P3):** docs/config drift, then test fixtures and weak tests.
- **Batch 6 (naming/prose, before public launch):** #113a renames (code first, stored-log
  read fallback), then #113b docstring/comment policy in `CONTRIBUTING.md`. This is the
  batch that decides whether an outside reader can work in this repo.

## What is *not* slop (keep)

The domain comments that explain a non-obvious constraint are the best part of this
codebase: why a set is stored before it plays, why the shelf exists, why analysis lives in
its own SQLite file, why the station matcher applies operator aliases at match time and not
at import time, why the pool is rotated rather than shuffled. When trimming #80/#81, keep
those reasons and delete only the "this used to be wrong" narration.

---

## 113. ✅ Naming and prose: write for a stranger, not for the DJ

Landed in #179/#181 (code renames) and #180 (the rules, `CONTRIBUTING.md`). Residual
words and the reply key `"theme"` are tracked as #131 in the second pass; the table
below is kept as the mapping that was applied.

**Priority: P2 (no runtime risk) · Effort: L · Do before the repo is advertised as public.**

This codebase invents its own vocabulary — *shelf, table, crate, flip, offer, brief,
trail, deep cut, standby, the DJ* — and then uses it as if every reader already knows it.
A newcomer cannot tell whether "the shelf" is a data structure, a UI panel, or a metaphor,
and nothing where it is used says. The prose has the same problem: docstrings are written
like liner notes or a postmortem, not like an API contract. The line that finally makes
this obvious is `spica/worker.py:465`:

```python
"""A backfill has work, the feature is on, and a recent failure is not blocking.
```

That is a sentence about three conditions, written as a riddle. It should be:

```python
"""Return True when the job is enabled, has pending work, and did not fail in the last hour."""
```

The fix is not to delete the personality from the README. It is to keep the product voice
in user-facing docs and make the code read like ordinary, boring software engineering.

### 113a. Replace the private vocabulary

Rename the code; keep the product words in the UI only, with a glossary if they are
genuinely part of the product.

| Current | Problem | Rename to |
|---|---|---|
| `shelf` (`picker/shelf.py`, `shelf_sound`, `Pick.shelf`) | sounds like furniture; actually "artists allowed for one pick" | `artist_candidates`, `candidate_artists`, `style_summary` |
| `table` (`picker/table.py`, `Pick.path="table"`) | collides with a SQL table; actually "tracks grouped by transition" | `track_candidates`, `candidate_tracks` |
| `crate` / `flip` (`Library.crate`, `Library.flip`) | crate-digging metaphor; actually "sample albums" / "redraw sample" | `sample_albums`, `redraw_albums` |
| `the offer` | not a thing; it is the candidate set | `candidates` |
| `the brief` | not a thing; it is the prompt payload | `prompt_context` (or just `prompt`) |
| `trail` | not a standard term; it is a timeline | `timeline` |
| `deep cut` / `known` / `mid` (`_prominence`) | genre-critic words for a percentile | `popularity_tier: low / mid / high` |
| `standby` | ambiguous; it is the fallback rotation | `fallback_rotation` |
| `the DJ` in code comments | product persona in implementation prose | `the picker` / `the model` / `the selector` |
| `roster` (`Library.roster`) | sports metaphor; it is a catalog summary | `catalog_summary`, `station_summary` |
| `why` / `theme` on `Pick` | vague; they are a reason and a set rationale | `reason`, `set_rationale` |
| `offered` on `Pick` | passive and unclear | `candidate_ids` |
| `path="table-partial"` | leaks the metaphor into stored data | `strategy="tracks-partial"` |
| group names `continue / shift / contrast / land / known` | mixed verbs and adjectives | enum: `continue / single_axis / contrast / closing / familiar` |

Also rename the modules to match (`picker/shelf.py` → `picker/artists.py`,
`picker/table.py` → `picker/tracks.py`), and the tests that import them.

**Compatibility:** `shelf`, `offered`, `path` and `why` are written into
`history/<station>.picks.jsonl` and read back by the admin and the candidate logic. This
deployment has a single operator, so no read fallback is needed: rename the writer and
rewrite the existing log in place (or delete it — it is a diagnostic log, not state the
station needs). A one-off `jq`/`sed` pass that maps the old keys to the new ones is enough.
If this ever ships to other operators, add a read fallback for the old keys first.

If the full rename is too large for one change, add `docs/glossary.md` first (one paragraph
per term, with the data structure it means) and link it from `CONTRIBUTING.md`. A glossary
is a stopgap, not the destination: a reader should not need a glossary to read a function.

### 113b. Docstring and comment rules

Add these to `CONTRIBUTING.md` and apply them in the same pass:

1. The first line says what the function returns or does, in the imperative, using the
   names of the parameters. No metaphor, no persona, no "the DJ".
2. At most one short paragraph after that, and only for a non-obvious constraint
   (ordering, transaction boundary, retry policy, why a fallback exists).
3. No changelog. Delete "used to", "the old", "this is what made X fail", "for ever",
   "which is why", and the rest of the bug archaeology. That belongs in the commit/PR.
4. Module docstring: one to three sentences — what this module is, what it talks to, what
   it deliberately does not do. Not a feature list, not a design essay.
5. Comments explain *why*, not *what*, and never narrate a past implementation. If a
   comment does not help a stranger predict behaviour, delete it.
6. User-facing copy (README, templates, prompts) may keep the radio/DJ voice. Source,
   docstrings and commit messages use plain engineering English.

### 113c. Acceptance criteria

- `rg -niE "\bshelf\b|\btable\b|\bcrate\b|\bflip\b|deep cut|the offer|the brief|\btrail\b|standby" spica`
  returns only UI strings or a documented product term, not identifiers or comments.
- Every remaining product metaphor is defined once in `docs/glossary.md` or the README.
- No docstring or comment in `spica/` contains "used to", "the old", "for ever", "which is
  why", or a first-person narrator.
- A new contributor can explain what `artist_candidates` and `track_candidates` mean from
  the function signatures and their first docstring line, without asking.
- `uv run pytest` passes after the renames. The existing `history/<station>.picks.jsonl`
  is rewritten or deleted in the same change; it is a diagnostic log, not state the
  station needs.
