"""
Validation of the numbers on the Czechia spike map.

Claims checked
  A. "1.8 % of the land is home to half of all people"
  B. "45 % of all square kilometres have nobody living there"
  C. "23 252 people live in Prague's most crowded km²"

Inputs
  GRID  ČSÚ population grid, Census 2021 (1 km)   grid_obyvatelstvo_sldb2021_20210326.gpkg
  LAU   Eurostat GISCO LAU 2021 (municipalities, POP_2021 = register count 1 Jan 2021)
        https://gisco-services.ec.europa.eu/distribution/v2/lau/geojson/LAU_RG_01M_2021_3035.geojson
  NUTS  Eurostat GISCO NUTS 2021 level 3 (regions)
        https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2021_3035_LEVL_3.geojson

Usage: python validate_facts.py GRID LAU NUTS OUT_DIR
"""
import sys
from pathlib import Path

import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

GRID, LAU, NUTS, OUT = sys.argv[1:5]
out = Path(OUT); out.mkdir(parents=True, exist_ok=True)
POP = "g131620000"
lines = []
say = lambda *a: (print(*a), lines.append(" ".join(str(x) for x in a)))


def half_share(pop, area):
    """Smallest share of area (densest units first) that holds 50 % of people."""
    dens = pop / area
    o = np.argsort(-dens)
    cp = np.cumsum(pop[o])
    k = int(np.searchsorted(cp, cp[-1] / 2) + 1)
    return k, area[o][:k].sum()


g = gpd.read_file(GRID)
lau = gpd.read_file(LAU, where="CNTR_CODE='CZ'").to_crs(g.crs)
nuts = gpd.read_file(NUTS, where="CNTR_CODE='CZ'").to_crs(g.crs)
cz_area = lau.geometry.area.sum() / 1e6
pop = g[POP].to_numpy().astype(float)
n = len(g)

say("=== 0. Inputs ===")
say(f"grid cells: {n:,}  | every cell is a full 1 km² square (min {g.geometry.area.min()/1e6:.4f} km²)")
say(f"grid cells cover {g.geometry.area.sum()/1e6:,.0f} km²; Czechia (LAU polygons) = {cz_area:,.0f} km²"
    "  -> border cells stick out into neighbouring countries")
say(f"grid population total: {pop.sum():,.0f}  (official Census 2021 result: 10 524 167)")
say(f"null values: {g[POP].isna().sum()}, negative: {(pop < 0).sum()}")

say("\n=== A. Half of the people on how much land? ===")
o = np.sort(pop)[::-1]
cum = np.cumsum(o)
k = int(np.searchsorted(cum, cum[-1] / 2) + 1)
say(f"densest {k:,} cells hold {cum[k-1]:,.0f} people = {cum[k-1]/cum[-1]:.2%} of the population")
say(f"  share of grid cells:           {k}/{n} = {k/n:.2%}")
say(f"  share of Czechia's real area:  {k}/{cz_area:,.0f} km² = {k/cz_area:.2%}")
say(f"  the {k}th densest cell still has {o[k-1]:,.0f} people/km²")

# where are those cells?
cells = g.assign(c=g.geometry.centroid).set_geometry("c")
top = cells.loc[np.argsort(-pop)[:k]]
joined = gpd.sjoin(top, lau[["LAU_NAME", "geometry"]], how="left", predicate="within")
by_town = joined.groupby("LAU_NAME")[POP].agg(["count", "sum"]).sort_values("sum", ascending=False)
say("  towns with the most of these cells:")
for name, row in by_town.head(12).iterrows():
    say(f"    {name:<22} {int(row['count']):>4} km²   {int(row['sum']):>10,} people")
say(f"  ... {len(by_town)} municipalities in total")

say("\n  sensitivity to cell size (aggregate the 1 km grid):")
ids = g["grd_inspir"].str.extract(r"N(\d+)E(\d+)").astype(int)
for size in (1, 2, 5, 10):
    key = (ids[0] // size).astype(str) + "_" + (ids[1] // size).astype(str)
    agg = g.groupby(key)[POP].sum().to_numpy().astype(float)
    a = np.full(agg.size, size * size, float)
    kk, ar = half_share(agg, a)
    say(f"    {size:>2} km cells: half the people on {ar:,.0f} km² = {ar/cz_area:.1%} of Czechia")
kk, ar = half_share(lau["POP_2021"].to_numpy().astype(float), lau.geometry.area.to_numpy() / 1e6)
say(f"    municipalities (LAU, register 1 Jan 2021): half the people on {ar:,.0f} km² = {ar/cz_area:.1%}"
    f" ({kk} of {len(lau)} municipalities)")

say("\n=== B. Empty square kilometres ===")
zero = int((pop == 0).sum())
say(f"cells with 0 residents: {zero:,} of {n:,} = {zero/n:.1%}")
inside = gpd.sjoin(cells[["c", POP]], lau[["geometry"]], how="inner", predicate="within")
inside = inside[~inside.index.duplicated()]
z_in = int((inside[POP] == 0).sum())
say(f"only cells whose centre lies inside Czechia: {z_in:,} of {len(inside):,} = {z_in/len(inside):.1%}")

say("\n=== C. Prague ===")
peak_i = int(np.argmax(pop))
peak = g.iloc[peak_i]
lonlat = gpd.GeoSeries([peak.geometry.centroid], crs=g.crs).to_crs(4326).iloc[0]
muni = gpd.sjoin(gpd.GeoDataFrame(geometry=[peak.geometry.centroid], crs=g.crs), lau, predicate="within")
say(f"densest cell {peak['grd_inspir']}: {int(pop[peak_i]):,} people, centre {lonlat.y:.4f} N, {lonlat.x:.4f} E,"
    f" municipality: {muni['LAU_NAME'].iloc[0] if len(muni) else '?'}")
say(f"next densest: {', '.join(f'{int(v):,}' for v in np.sort(pop)[::-1][1:6])}")
praha = nuts[nuts.NUTS_ID == "CZ010"]
in_p = gpd.sjoin(cells[["c", POP]], praha[["geometry"]], predicate="within")
say(f"grid cells with centre inside Prague: {len(in_p)} km², {int(in_p[POP].sum()):,} people"
    "  (Census 2021 Prague: 1 301 432; register 1 Jan 2021: "
    f"{int(lau.loc[lau.LAU_NAME.str.contains('Praha'), 'POP_2021'].max()):,})")

say("\n=== D. Grid vs municipal register, per municipality ===")
per_muni = inside.join(gpd.sjoin(cells[["c"]], lau[["LAU_ID", "geometry"]], predicate="within")["LAU_ID"]) \
    .groupby("LAU_ID")[POP].sum()
cmp = lau.set_index("LAU_ID")[["LAU_NAME", "POP_2021"]].join(per_muni.rename("grid"), how="left").fillna(0)
r = np.corrcoef(cmp["POP_2021"], cmp["grid"])[0, 1]
say(f"correlation grid vs register over {len(cmp)} municipalities: r = {r:.4f}")
big = cmp.sort_values("POP_2021", ascending=False).head(6)
for _, row in big.iterrows():
    say(f"    {row['LAU_NAME']:<14} register {int(row['POP_2021']):>9,}   grid {int(row['grid']):>9,}")

# Lorenz-style curve
fig, ax = plt.subplots(figsize=(8, 5.4), dpi=160)
x = np.arange(1, n + 1) / cz_area * 100
y = cum / cum[-1] * 100
ax.plot(x, y, color="#c2185b", lw=2.2)
ax.axhline(50, color="#999", lw=0.8, ls="--"); ax.axvline(k / cz_area * 100, color="#999", lw=0.8, ls="--")
ax.annotate(f"half of all people\non {k/cz_area:.1%} of the land", (k / cz_area * 100, 50),
            xytext=(12, 30), textcoords="axes fraction" if False else "offset points", fontsize=10)
ax.set_xlim(0, 60); ax.set_ylim(0, 101)
ax.set_xlabel("% of Czechia's area (densest km² first)"); ax.set_ylabel("% of population")
ax.set_title("How concentrated is Czechia's population? (Census 2021, 1 km grid)", fontsize=11)
ax.grid(alpha=0.25)
fig.tight_layout(); fig.savefig(out / "concentration_curve.png")
(out / "validation_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
