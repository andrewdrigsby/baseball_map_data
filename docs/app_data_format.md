# App data format

Static JSON written by `scripts/export_app_data.py` into `data/`, served from GitHub Pages and read by the Experience Builder widget. All coordinates are feet: home plate at (0, 0), +y toward centre field, +x toward the right-field side.

## Files

| File | Loaded | Contents |
|---|---|---|
| `meta.json` | once | Seasons, settings, labels, location cutoffs, bivariate breaks and palette, `scale` |
| `grid.json` | once | `hexes` (air-ball hexagons) and `wedges` (ground-ball wedges), each with an id and a `ring` of [x, y] points |
| `field.json` | once | `lines` (foul lines, basepaths, infield arc, fence) and `bases` |
| `league/L.json`, `league/R.json` | once per batter side | League profiles for left- and right-handed batters |
| `batters/index.json` | once | Batter picker: id, name, team (most recent season), seasons, sides with BIP counts |
| `batters/<id>.json` | on batter select | All profiles for one batter |

## Profiles

Every batter side has 21 profiles keyed `hand|sub`:

- `hand`: `ALL`, `L` (vs LHP), `R` (vs RHP)
- `sub`: pitch type `ALL`, `FB`, `BRK`, `OFF`, or pitch location `IN`, `MID`, `AWAY`

The UI builds the key from the selectors. "All" in pitch-type mode and "All" in location mode are the same profile (`hand|ALL`).

```json
{
  "id": 665489, "name": "...", "team": "TOR", "seasons": [2023, 2024, 2025],
  "sides": {
    "R": {
      "n_bip": 1702,
      "combos": {
        "L|FB": {
          "n_bip": 210, "n_gb": 95, "n_air": 88,
          "xwoba": 0.402, "avg_ev": 92.4, "gb_pull_pct": 44.1, "air_pull_pct": 30.2,
          "gb":  {"raw": [20 ints], "blend": [20 ints]},
          "air": {"raw": [hex ints], "blend": [hex ints]},
          "xw":  {"raw": [hex ints], "blend": [hex ints]}
        }
      }
    }
  }
}
```

- `gb` arrays line up with `grid.wedges`; `air` and `xw` arrays line up with `grid.hexes`.
- Array values are integers: **share fraction × `meta.scale`** (100,000). Divide by `scale` for a fraction.
- `gb`: share of the batter's ground balls in each 5° wedge (sums to 1).
- `air`: smoothed share of line drives and fly balls in each hex (sums to a little under 1; popups excluded).
- `xw`: same, weighted by expected wOBA (where the damage goes).
- `raw` is the batter's own data and is `null` when the split has no balls of that type. `blend` is always filled.
- Summary stats (`xwoba`, `avg_ev`, pull %) are raw, not blended.
- League files have the same combos with single arrays: `gb`, `air`, `xw`.

## Blending

Hierarchical: each profile is blended toward its parent, shifted by the league's change from parent to split.

| Profile | Shrinks toward | Weight |
|---|---|---|
| `ALL\|ALL` | league `ALL\|ALL` | `k_league` balls |
| `h\|ALL`, `ALL\|sub` | batter `ALL\|ALL` + (league split − league `ALL\|ALL`) | `k_split` balls |
| `h\|sub` | batter `h\|ALL` + (league `h\|sub` − league `h\|ALL`) | `k_split` balls |

`blend = (n × raw + k × prior) / (n + k)`, with `n` = ground balls for `gb` and air balls for `air` / `xw`.

## Computed in the widget

- **Vs league:** `batter array − league array` for the same side and combo (divide by `scale` for a fraction).
- **Bivariate class** for each hex, from `meta.bivariate`:
  - volume: `air < b1` → 1, `< b2` → 2, else 3
  - damage: `xw − league xw < −t` → A, `> t` → C, else B
  - code = letter + digit (e.g. `C3`), colour from `meta.bivariate.palette`
  - Works the same for `raw` and `blend`, so the toggle changes classes too.
- **Low sample:** warn when `n_gb` or `n_air` < `meta.settings.min_n_display`.

## Notes for the UI

- Pitch location is batter-relative (`IN` = toward the batter), with cutoffs at league terciles per batter side (`meta.location_cuts_ft`). `IN` + `MID` + `AWAY` = `ALL`, apart from the ~1% of balls with no pitch location.
- `FB` + `BRK` + `OFF` is slightly less than `ALL`: knuckleballs, eephus and other rare pitches are only in `ALL`.
- Switch hitters have both `L` and `R` sides; a side with fewer than `meta.settings.min_bip` balls in play is left out.
