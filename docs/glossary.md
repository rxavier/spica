# Glossary

Product copy keeps the radio voice; code uses the plain names below.

| Product word | Code | What it is |
|---|---|---|
| artist shelf | `artist_candidates` (`picker/artists.py`) | The artists a set may draw from when analysis is off. |
| table | `track_candidates` (`picker/tracks.py`) | Records grouped by transition, when analysis is on. |
| offer | candidates | The artist or track candidates shown to the picker. |
| brief | `prompt_context` | The assembled prompt payload the picker reads. |
| reason | `reason` | The line printed under the track. |
| theme | `set_rationale` | The one line describing what the whole set is. |
| deep cut / known / mid | `popularity_tier`: `low` / `high` / `mid` | Standings within an artist's catalogue; `unknown` when there is no count. |
| path | `strategy`: `artists` / `tracks` / `tracks-partial` | Which candidate builder produced the set. |
| standby | fallback rotation | Airings from the pool while the picker was offline. |
| crate / flip | `sample_albums` / `redraw_albums` | A random handful of albums; the tool is `redraw`. |
| roster | `catalog_summary` | Labels and artists a station holds. |
| shelf (genres) | rollup | The operator's grouping of fine genre labels for station selection. |
