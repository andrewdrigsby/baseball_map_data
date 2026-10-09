# baseball_map_data

Data pipeline and static JSON for an MLB Statcast batter spray-chart app built with ArcGIS Experience Builder (widget code: [baseball_map](https://github.com/andrewdrigsby/baseball_map)).

Users pick a batter, a pitcher hand (All / L / R), and then either a pitch type (All / Fastball / Breaking / Offspeed) or a pitch location (All / Inside / Center / Away). Every combination is precomputed here and served as static files from GitHub Pages.

## Contents

| Path | What it is |
|---|---|
| `scripts/export_app_data.py` | Builds `data/` from the season ball-in-play CSVs made by `statcast_bip.py` |
| `docs/app_data_format.md` | File formats, blending method and what the widget computes |
| `data/` | The exported JSON the app reads (written by the script) |

## Rebuilding the data

From the repo root, in a Python environment with pandas and numpy:

```
python scripts\export_app_data.py --data-dir C:\mlb
```

`--data-dir` is the folder holding `bip_2023.csv`, `bip_2024.csv` and `bip_2025.csv`. Check the `=== CHECKS ===` block it prints, then commit `data/`.

## Method in brief

- Ground balls are profiled by spray direction (5° wedges); line drives and fly balls by smoothed location on 12 ft hexagons.
- Each split is blended hierarchically toward the batter's broader profile, shifted by the league's change for that split, so thin samples stay batter-specific.
- Pitch location is batter-relative, with cutoffs at league terciles so each band holds about a third of balls in play.

## Data source

Statcast via Baseball Savant, MLB Advanced Media, retrieved with [pybaseball](https://github.com/jldbc/pybaseball). Statcast data © MLB Advanced Media, L.P., subject to MLB's terms of use. This project is for personal, non-commercial analysis.
