# Glossary

Product copy keeps the radio voice; code uses the plain names below.

| Product word | Code | What it is |
|---|---|---|
| artist shelf | `picker/artists.py` (`build`), `Pick.candidate_artists` | The artists a set may draw from when analysis is off. |
| table | `picker/tracks.py` (`build`), `Pick.candidate_ids` | Records grouped by transition, when analysis is on. |
| offer | candidate set (`Pick.candidate_ids`) | The artist or track candidates shown to the picker. |
| brief | `Pick.prompt_context` | The assembled prompt payload the picker reads. |
| reason | `Pick.reason` | The line printed under the track. |
| theme | `Pick.set_rationale` | The one line describing what the whole set is. |
| deep cut / known / mid | `popularity_tier`: `low` / `mid` / `high` | Standings within an artist's catalogue; `unknown` when there is no count. |
| path | `Pick.strategy`: `artists` / `tracks` / `tracks-partial` | Which candidate builder produced the set. |
| standby | fallback rotation | Airings from the pool while the picker was offline. |
| crate / flip | `sample_albums` / `redraw_albums` | A random handful of albums; the tool is `redraw`. |
| roster | `catalog_summary` | Labels and artists a station holds. |
| shelf (genres) | rollup | The operator's grouping of fine genre labels for station selection. |
