"""
export_app_data.py
Export batter spray profiles as static JSON for the Experience Builder widget.

App flow:  batter -> pitcher hand (ALL / L / R)
           -> either pitch type (ALL / FB / BRK / OFF)
              or pitch location (ALL / IN / MID / AWAY)

Every batter x side gets 21 precomputed profiles, keyed "hand|sub":
  hand  ALL, L, R
  sub   ALL, FB, BRK, OFF (pitch family)  or  IN, MID, AWAY (pitch location)
The two "All" options are the same profile (ALL), stored once.

Each profile has the batter's raw (unadjusted) shares and blended shares, so the
app can toggle between them. Blending is hierarchical:
  ALL|ALL   -> league ALL|ALL for the same batter side          (K_LEAGUE)
  h|ALL     -> batter's blended ALL|ALL, shifted by the league's
               h|ALL - ALL|ALL difference                       (K_SPLIT)
  ALL|sub   -> batter's blended ALL|ALL, shifted by the league's
               ALL|sub - ALL|ALL difference                     (K_SPLIT)
  h|sub     -> batter's blended h|ALL, shifted by the league's
               h|sub - h|ALL difference                         (K_SPLIT)
So a thin split looks like *this hitter*, adjusted for how hitters in general
change in that split, rather than collapsing to league average.

Pitch location is batter-relative (IN = toward the batter) with cutoffs at the
terciles of league pitch location for each batter side, so IN, MID and AWAY each
hold about a third of balls in play and together add up to ALL.

Output (run from the repo root, writes ./data):
  data/meta.json             settings, location cutoffs, bivariate breaks
  data/grid.json             air hexes and ground-ball wedges (geometry, once)
  data/field.json            foul lines, basepaths, infield arc, fence, bases
  data/league/L.json, R.json league profiles for each batter side
  data/batters/index.json    batter list for the picker
  data/batters/<id>.json     one file per batter

Usage (venv from statcast_bip.py; no arcpy needed):
    cd C:\\path\\to\\baseball_map
    python scripts\\export_app_data.py --data-dir C:\\mlb
"""
import argparse
import json
import math
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SEASONS = [2023, 2024, 2025]
MIN_BIP = 150          # batter side kept if it has at least this many BIP
K_LEAGUE = 150         # league weight (balls) for ALL|ALL
K_SPLIT = 100          # parent weight (balls) for every split
MIN_N_DISPLAY = 40     # app hint: warn below this many GB / air balls
SCALE = 100_000        # share arrays are integers: fraction x SCALE

WEDGE_DEG = 5
WEDGE_LIMIT = 50
INFIELD_ARC_FT = 95
RUBBER_Y_FT = 60.5
BASE_FT = 90
FENCE_LINE_FT = 330
FENCE_CF_FT = 400
HEX_SIDE_FT = 12
GRID_FT = 5
BANDWIDTH_FT = 18
AIR_MAX_FT = 470
PULL_LIMIT_DEG = 15
AIR_TYPES = ("line_drive", "fly_ball")

LOC_CUTS = None        # freeze e.g. {"L": (-0.25, 0.31), "R": (-0.27, 0.29)}
VOL_BREAKS = None      # freeze e.g. (0.00045, 0.0012)  (fractions)
DMG_THRESHOLD = None   # freeze e.g. 0.00006             (fraction)
NEAR_SHARE = 0.5       # share of hexes classed "about league" damage

HANDS = ["ALL", "L", "R"]
SUBS = ["ALL", "FB", "BRK", "OFF", "IN", "MID", "AWAY"]
FAMILY = {"Fastball": "FB", "Breaking": "BRK", "Offspeed": "OFF"}
COMBOS = [f"{h}|{s}" for h in HANDS for s in SUBS]

USECOLS = {
    "game_year", "game_type", "home_team", "away_team", "inning_topbot",
    "batter", "batter_name", "stand", "p_throws", "pitch_family", "plate_x",
    "bb_type", "launch_speed", "estimated_woba_using_speedangle",
    "x_ft", "y_ft",
}
SQRT3 = math.sqrt(3)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def polar_xy(angle_deg, dist):
    a = math.radians(angle_deg)
    return dist * math.sin(a), dist * math.cos(a)


def arc_dist(angle_deg):
    """Home plate to the infield grass arc along a spray angle."""
    c = math.cos(math.radians(angle_deg))
    return RUBBER_Y_FT * c + math.sqrt(INFIELD_ARC_FT ** 2
                                       - RUBBER_Y_FT ** 2 * (1 - c * c))


def fence_dist(angle_deg):
    t = max(-1.0, min(1.0, angle_deg / 45.0))
    return FENCE_CF_FT - (FENCE_CF_FT - FENCE_LINE_FT) * t * t


def r1(pts):
    return [[round(x, 1), round(y, 1)] for x, y in pts]


N_WEDGE = int(2 * WEDGE_LIMIT / WEDGE_DEG)


def wedge_ring(i, step=0.5):
    a0 = -WEDGE_LIMIT + i * WEDGE_DEG
    angs = np.arange(a0, a0 + WEDGE_DEG + step / 2, step)
    pts = [(0.0, 0.0)] + [polar_xy(a, arc_dist(a)) for a in angs]
    return pts + [pts[0]]


def hex_centers():
    s = HEX_SIDE_FT
    out = []
    qmax = int(AIR_MAX_FT / (1.5 * s)) + 2
    for q in range(-qmax, qmax + 1):
        for r in range(-2 * qmax, 2 * qmax + 1):
            cx, cy = s * 1.5 * q, s * SQRT3 * (r + q / 2.0)
            if cy <= 0 or math.hypot(cx, cy) > AIR_MAX_FT:
                continue
            if abs(math.degrees(math.atan2(cx, cy))) > WEDGE_LIMIT:
                continue
            out.append((q, r, cx, cy))
    return out


def hex_ring(cx, cy):
    s = HEX_SIDE_FT
    pts = [(cx + s * math.cos(math.radians(a)), cy + s * math.sin(math.radians(a)))
           for a in range(0, 360, 60)]
    return pts + [pts[0]]


def field_geometry():
    b = BASE_FT / math.sqrt(2)
    home, first, second, third = (0, 0), (b, b), (0, 2 * b), (-b, b)
    arc = [polar_xy(a, arc_dist(a)) for a in np.arange(-45, 45.5, 1.0)]
    fence = [polar_xy(a, fence_dist(a)) for a in np.arange(-45, 45.5, 1.0)]
    lines = {"foul_line_3b": [home, fence[0]], "foul_line_1b": [home, fence[-1]],
             "basepaths": [home, first, second, third, home],
             "infield_arc": arc, "fence": fence}
    bases = {"home": home, "first": first, "second": second, "third": third,
             "rubber": (0, RUBBER_Y_FT)}
    return ({k: r1(v) for k, v in lines.items()},
            {k: [round(x, 1), round(y, 1)] for k, (x, y) in bases.items()})


HEXES = hex_centers()
HEX_AREA = 1.5 * SQRT3 * HEX_SIDE_FT ** 2

# ---------------------------------------------------------------------------
# Density (Gaussian-smoothed histogram, sampled at hex centres)
# ---------------------------------------------------------------------------
GX = np.arange(-340, 340 + GRID_FT, GRID_FT)
GY = np.arange(-40, 520 + GRID_FT, GRID_FT)
EDGES_X = np.append(GX, GX[-1] + GRID_FT) - GRID_FT / 2
EDGES_Y = np.append(GY, GY[-1] + GRID_FT) - GRID_FT / 2


def _conv_matrix(n):
    """Matrix form of a 'same'-size Gaussian convolution along one axis."""
    sig = BANDWIDTH_FT / GRID_FT
    rad = int(math.ceil(3 * sig))
    k = np.exp(-0.5 * (np.arange(-rad, rad + 1) / sig) ** 2)
    k /= k.sum()
    m = np.zeros((n, n))
    for i in range(n):
        for j in range(max(0, i - rad), min(n, i + rad + 1)):
            m[i, j] = k[j - i + rad]
    return m


CX, CY = _conv_matrix(len(GX)), _conv_matrix(len(GY))
IX = np.clip(np.round((np.array([h[2] for h in HEXES]) - GX[0]) / GRID_FT)
             .astype(int), 0, len(GX) - 1)
IY = np.clip(np.round((np.array([h[3] for h in HEXES]) - GY[0]) / GRID_FT)
             .astype(int), 0, len(GY) - 1)


def hex_shares(x, y, w=None):
    """Share of all air balls (or of their weight) falling in each hex."""
    h, _, _ = np.histogram2d(x, y, bins=[EDGES_X, EDGES_Y], weights=w)
    h = CX @ h @ CY.T
    tot = h.sum()
    if tot <= 0:
        return None
    return (h / tot)[IX, IY] * HEX_AREA / GRID_FT ** 2


def wedge_shares(spray):
    w = np.clip(np.floor((spray + WEDGE_LIMIT) / WEDGE_DEG), 0, N_WEDGE - 1)
    c = np.bincount(w.astype(int), minlength=N_WEDGE)
    return c / c.sum() if c.sum() else None


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load(data_dir, seasons):
    frames = []
    for s in seasons:
        f = os.path.join(data_dir, f"bip_{s}.csv")
        if os.path.exists(f):
            frames.append(pd.read_csv(f, usecols=lambda c: c in USECOLS,
                                      low_memory=False))
            print(f"  loaded {f}: {len(frames[-1]):,} rows")
        else:
            print(f"  missing {f}, skipped")
    if not frames:
        sys.exit("No season CSVs found.")
    d = pd.concat(frames, ignore_index=True)
    d = d[d["game_type"] == "R"]
    d = d.dropna(subset=["x_ft", "y_ft", "stand", "batter"]).copy()
    d["batter"] = d["batter"].astype(int)
    d["spray"] = np.degrees(np.arctan2(d["x_ft"], d["y_ft"]))
    d["xw"] = d["estimated_woba_using_speedangle"].fillna(0.0)
    d["plate_in_ft"] = np.where(d["stand"] == "R", -d["plate_x"], d["plate_x"])
    d["fam"] = d["pitch_family"].map(FAMILY).fillna("OTHER")
    d["bat_team"] = np.where(d["inning_topbot"] == "Bot",
                             d["home_team"], d["away_team"])
    return d


def location_cuts(d):
    if LOC_CUTS is not None:
        return {k: tuple(v) for k, v in LOC_CUTS.items()}
    cuts = {}
    for side in ("L", "R"):
        v = d.loc[(d["stand"] == side) & d["plate_in_ft"].notna(), "plate_in_ft"]
        lo, hi = np.quantile(v, [1 / 3, 2 / 3])
        cuts[side] = (float(lo), float(hi))
    return cuts


def assign_location(d, cuts):
    lo = d["stand"].map({k: v[0] for k, v in cuts.items()})
    hi = d["stand"].map({k: v[1] for k, v in cuts.items()})
    p = d["plate_in_ft"]
    d["loc"] = np.select([p > hi, p < lo, p.notna()], ["IN", "AWAY", "MID"],
                         default="NA")


def combo_mask(df, key):
    h, s = key.split("|")
    m = np.ones(len(df), bool)
    if h != "ALL":
        m &= (df["p_throws"] == h).to_numpy()
    if s in ("FB", "BRK", "OFF"):
        m &= (df["fam"] == s).to_numpy()
    elif s in ("IN", "MID", "AWAY"):
        m &= (df["loc"] == s).to_numpy()
    return m


def pull_pct(spray, side):
    if len(spray) == 0:
        return None
    s = spray if side == "R" else -spray
    return round(100 * float(np.mean(s < -PULL_LIMIT_DEG)), 1)


def _f(v, nd=3):
    return None if v is None or pd.isna(v) else round(float(v), nd)


def raw_profile(df, side):
    g = df[df["bb_type"] == "ground_ball"]
    a = df[df["bb_type"].isin(AIR_TYPES)]
    ax, ay = a["x_ft"].to_numpy(float), a["y_ft"].to_numpy(float)
    return {
        "n_bip": len(df), "n_gb": len(g), "n_air": len(a),
        "xwoba": _f(df["estimated_woba_using_speedangle"].mean()),
        "avg_ev": _f(df["launch_speed"].mean(), 1),
        "gb_pull_pct": pull_pct(g["spray"].to_numpy(), side),
        "air_pull_pct": pull_pct(a["spray"].to_numpy(), side),
        "gb": wedge_shares(g["spray"].to_numpy()) if len(g) else None,
        "air": hex_shares(ax, ay) if len(a) else None,
        "xw": hex_shares(ax, ay, a["xw"].to_numpy(float)) if len(a) else None,
    }


# ---------------------------------------------------------------------------
# Hierarchical blending
# ---------------------------------------------------------------------------
def parent(key):
    h, s = key.split("|")
    if key == "ALL|ALL":
        return None
    if s == "ALL" or h == "ALL":
        return "ALL|ALL"
    return f"{h}|ALL"


def shift(par, lg_child, lg_par):
    """Parent profile moved by the league's parent -> child difference."""
    p = np.clip(par + lg_child - lg_par, 0, None)
    target = max(par.sum() + lg_child.sum() - lg_par.sum(), 0.0)
    s = p.sum()
    return p * (target / s) if s > 0 else p


def blend(n, raw, prior, k):
    if raw is None or n == 0:
        return prior.copy()
    return (n * raw + k * prior) / (n + k)


def blended_profile(raw, lg, key, done):
    par = parent(key)
    out = {}
    for part, n in (("gb", raw["n_gb"]), ("air", raw["n_air"]),
                    ("xw", raw["n_air"])):
        if par is None:
            prior, k = lg[key][part], K_LEAGUE
        else:
            prior = shift(done[par][part], lg[key][part], lg[par][part])
            k = K_SPLIT
        out[part] = blend(n, raw[part], prior, k)
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def q(arr):
    return None if arr is None else np.round(arr * SCALE).astype(int).tolist()


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), allow_nan=False)


def stats_only(p):
    return {k: p[k] for k in ("n_bip", "n_gb", "n_air", "xwoba", "avg_ev",
                              "gb_pull_pct", "air_pull_pct")}


def dir_size(path):
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _, fs in os.walk(path) for f in fs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=r"C:\mlb", help="folder with bip_<season>.csv")
    ap.add_argument("--out", default="data", help="output folder (default ./data)")
    ap.add_argument("--seasons", type=int, nargs="+", default=SEASONS)
    ap.add_argument("--min-bip", type=int, default=MIN_BIP)
    a = ap.parse_args()

    print("Loading...")
    d = load(a.data_dir, a.seasons)
    cuts = location_cuts(d)
    assign_location(d, cuts)
    seasons_found = sorted(int(s) for s in d["game_year"].unique())

    # League profiles per batter side
    print("League profiles...")
    league = {}
    for side in ("L", "R"):
        ds = d[d["stand"] == side]
        lg = {}
        for key in COMBOS:
            p = raw_profile(ds[combo_mask(ds, key)], side)
            for part, n in (("gb", N_WEDGE), ("air", len(HEXES)), ("xw", len(HEXES))):
                if p[part] is None:
                    p[part] = np.zeros(n)
            lg[key] = p
        league[side] = lg
        dump(os.path.join(a.out, "league", f"{side}.json"), {
            "side": side,
            "combos": {k: dict(stats_only(p), gb=q(p["gb"]), air=q(p["air"]),
                               xw=q(p["xw"])) for k, p in lg.items()}})

    # Batters
    print("Batter profiles...")
    sizes = d.groupby(["batter", "stand"]).size()
    keep = sizes[sizes >= a.min_bip]
    batter_ids = sorted(keep.index.get_level_values(0).unique())
    index, dmg_diffs = [], []
    for i, bid in enumerate(batter_ids, 1):
        bd = d[d["batter"] == bid]
        names = bd["batter_name"].dropna()
        name = names.mode().iloc[0] if len(names) else str(bid)
        last = bd[bd["game_year"] == bd["game_year"].max()]
        team = last["bat_team"].mode().iloc[0] if len(last) else None
        out = {"id": int(bid), "name": name, "team": team,
               "seasons": sorted(int(s) for s in bd["game_year"].unique()),
               "sides": {}}
        entry = {"id": int(bid), "name": name, "team": team,
                 "seasons": out["seasons"], "sides": {}}
        for side in sorted(bd["stand"].dropna().unique()):
            if (bid, side) not in keep.index:
                continue
            bs = bd[bd["stand"] == side]
            lg = league[side]
            done, combos = {}, {}
            for key in COMBOS:
                raw = raw_profile(bs[combo_mask(bs, key)], side)
                bl = blended_profile(raw, lg, key, done)
                done[key] = bl
                dmg_diffs.append(np.abs(bl["xw"] - lg[key]["xw"]).astype(np.float32))
                combos[key] = dict(
                    stats_only(raw),
                    gb={"raw": q(raw["gb"]), "blend": q(bl["gb"])},
                    air={"raw": q(raw["air"]), "blend": q(bl["air"])},
                    xw={"raw": q(raw["xw"]), "blend": q(bl["xw"])})
            out["sides"][side] = {"n_bip": len(bs), "combos": combos}
            entry["sides"][side] = {"n_bip": len(bs),
                                    "n_gb": combos["ALL|ALL"]["n_gb"],
                                    "n_air": combos["ALL|ALL"]["n_air"]}
        dump(os.path.join(a.out, "batters", f"{bid}.json"), out)
        index.append(entry)
        if i % 50 == 0 or i == len(batter_ids):
            print(f"  {i}/{len(batter_ids)}")
    index.sort(key=lambda e: e["name"].split(" ")[-1] + " " + e["name"])
    dump(os.path.join(a.out, "batters", "index.json"), index)

    # Bivariate breaks (fixed for every batter, combo and raw/blend mode)
    lg_all = np.concatenate([league[s]["ALL|ALL"]["air"] for s in ("L", "R")])
    vb = (tuple(VOL_BREAKS) if VOL_BREAKS is not None
          else tuple(float(x) for x in np.quantile(lg_all, [1 / 3, 2 / 3])))
    dt = (float(DMG_THRESHOLD) if DMG_THRESHOLD is not None
          else float(np.quantile(np.concatenate(dmg_diffs), NEAR_SHARE)))

    # Geometry
    dump(os.path.join(a.out, "grid.json"), {
        "units": "feet; home plate (0,0), +y to centre field, +x to right field",
        "hex_side_ft": HEX_SIDE_FT,
        "hexes": [{"id": f"H{q_}_{r_}", "q": q_, "r": r_,
                   "x": round(cx, 1), "y": round(cy, 1),
                   "ring": r1(hex_ring(cx, cy))} for q_, r_, cx, cy in HEXES],
        "wedges": [{"i": i, "from": -WEDGE_LIMIT + i * WEDGE_DEG,
                    "to": -WEDGE_LIMIT + (i + 1) * WEDGE_DEG,
                    "ring": r1(wedge_ring(i))} for i in range(N_WEDGE)]})
    lines, bases = field_geometry()
    dump(os.path.join(a.out, "field.json"), {"lines": lines, "bases": bases})

    dump(os.path.join(a.out, "meta.json"), {
        "built": date.today().isoformat(),
        "seasons": seasons_found, "game_type": "R",
        "n_batters": len(index),
        "scale": SCALE,
        "scale_note": "gb, air and xw arrays are integers: share fraction x scale",
        "hands": HANDS,
        "subs": {"type": ["ALL", "FB", "BRK", "OFF"],
                 "location": ["ALL", "IN", "MID", "AWAY"]},
        "labels": {"ALL": "All", "L": "vs LHP", "R": "vs RHP", "FB": "Fastball",
                   "BRK": "Breaking", "OFF": "Offspeed", "IN": "Inside",
                   "MID": "Center", "AWAY": "Away"},
        "settings": {"min_bip": a.min_bip, "k_league": K_LEAGUE,
                     "k_split": K_SPLIT, "min_n_display": MIN_N_DISPLAY,
                     "hex_side_ft": HEX_SIDE_FT, "bandwidth_ft": BANDWIDTH_FT,
                     "wedge_deg": WEDGE_DEG, "wedge_limit_deg": WEDGE_LIMIT,
                     "air_max_ft": AIR_MAX_FT, "pull_limit_deg": PULL_LIMIT_DEG,
                     "air_types": list(AIR_TYPES)},
        "location_cuts_ft": {k: {"away_below": round(v[0], 4),
                                 "in_above": round(v[1], 4)}
                             for k, v in cuts.items()},
        "bivariate": {
            "vol_breaks": [round(vb[0] * SCALE, 2), round(vb[1] * SCALE, 2)],
            "dmg_threshold": round(dt * SCALE, 3),
            "units": "same integer scale as the share arrays",
            "rule": ("vol 1/2/3: air < b1 / < b2 / >= b2. "
                     "dmg 1/2/3: xw - league xw < -t / within t / > t. "
                     "code = 'ABC'[dmg-1] + vol"),
            "palette": {
                "A1": "#e8e8e8", "A2": "#ace4e4", "A3": "#5ac8c8",
                "B1": "#dfb0d6", "B2": "#a5add3", "B3": "#5698b9",
                "C1": "#be64ac", "C2": "#8c62aa", "C3": "#3b4994"}},
    })

    # Report
    print("\n=== CHECKS ===")
    for side in ("L", "R"):
        ds = d[d["stand"] == side]
        sh = ds["loc"].value_counts(normalize=True).reindex(
            ["IN", "MID", "AWAY", "NA"], fill_value=0)
        print(f"{side}HB location bands: " + ", ".join(
            f"{k} {v:.1%}" for k, v in sh.items())
            + f"   cuts {cuts[side][0]:+.3f} / {cuts[side][1]:+.3f} ft")
    print(f"Pitch family OTHER (in ALL only): {(d['fam'] == 'OTHER').mean():.2%}")
    print(f"Volume breaks (x{SCALE}): {vb[0] * SCALE:.2f}, {vb[1] * SCALE:.2f}")
    print(f"Damage threshold (x{SCALE}): {dt * SCALE:.3f}")
    n_sides = sum(len(e["sides"]) for e in index)
    print(f"Batters: {len(index)} ({n_sides} batter-sides with >= {a.min_bip} BIP)")
    print(f"Output: {os.path.abspath(a.out)}  ({dir_size(a.out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
