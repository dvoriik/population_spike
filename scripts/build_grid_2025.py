"""
Estimate a 1 km population grid for 1 January 2025.

There is no official 2025 grid: ČSÚ publishes the 1 km grid only from Census 2021.
ČSÚ does publish population per municipality every year, so this script moves the
2021 grid to 2025 municipality by municipality (dasymetric update):

  1. every 1 km cell is split into the parts that fall into each municipality
     (2021 residents spread evenly across the cell),
  2. each part is multiplied by that municipality's ratio
     population 1 Jan 2025 (ČSÚ register) / population counted in that municipality by the 2021 grid,
  3. parts are summed back into cells.

Municipal totals therefore match ČSÚ 1 Jan 2025 exactly; where people live *inside*
a municipality is still the 2021 pattern.

Inputs
  GRID   ČSÚ population grid, Census 2021 (gpkg)
  LAU    Eurostat GISCO LAU 2021 municipal boundaries (geojson, LAU_ID = ČSÚ municipality code)
  XLSX   ČSÚ "Počet obyvatel v obcích České republiky k 1. 1. 2025" (1300722503.xlsx)
         https://csu.gov.cz/produkty/pocet-obyvatel-v-obcich-rlm0s92pwn
Output
  OUT.gpkg with the same fields the map script reads (grd_inspir, g131620000 = estimate 2025)

Usage: python build_grid_2025.py GRID LAU XLSX OUT.gpkg
"""
import sys

import numpy as np
import pandas as pd
import geopandas as gpd

GRID, LAU, XLSX, OUT = sys.argv[1:5]
POP = "g131620000"

grid = gpd.read_file(GRID, columns=["grd_inspir", POP])
lau = gpd.read_file(LAU, where="CNTR_CODE='CZ'").to_crs(grid.crs)[["LAU_ID", "LAU_NAME", "geometry"]]
lau["LAU_ID"] = lau["LAU_ID"].astype(str)

x = pd.read_excel(XLSX, header=None)
x = x[pd.to_numeric(x[1], errors="coerce").notna() & pd.to_numeric(x[3], errors="coerce").notna()]
p25 = pd.Series(x[3].astype(int).values, index=x[1].astype(int).astype(str), name="pop2025")
print(f"ČSÚ 1 Jan 2025: {len(p25):,} municipalities, {p25.sum():,} residents")

# 1. split cells by municipality (area-weighted)
grid["cell_area"] = grid.geometry.area
parts = gpd.overlay(grid, lau, how="intersection", keep_geom_type=True)
parts["w"] = parts.geometry.area / parts["cell_area"]
parts["p21"] = parts[POP] * parts["w"]
outside = grid[POP].sum() - parts["p21"].sum()
print(f"2021 residents in cell parts outside any municipality polygon: {outside:,.0f}")

# 2. municipal scaling factors
m21 = parts.groupby("LAU_ID")["p21"].sum()
m = pd.concat([m21.rename("grid2021"), p25], axis=1)
missing_in_lau = m["grid2021"].isna() & m["pop2025"].notna()
missing_in_2025 = m["pop2025"].isna() & m["grid2021"].notna()
print(f"municipalities only in 2025 list: {missing_in_lau.sum()}  "
      f"({int(m.loc[missing_in_lau, 'pop2025'].sum()):,} residents)")
print(f"municipalities only in LAU 2021: {missing_in_2025.sum()}  "
      f"({int(m.loc[missing_in_2025, 'grid2021'].sum()):,} residents in 2021 grid)")
m = m.fillna(0.0)
m["factor"] = np.where(m["grid2021"] > 0, m["pop2025"] / m["grid2021"].where(m["grid2021"] > 0, 1), np.nan)

parts = parts.join(m["factor"], on="LAU_ID")
parts["p25"] = parts["p21"] * parts["factor"].fillna(1.0)
# municipalities with residents in 2025 but none in their 2021 cell parts: spread evenly by area
empty = m.index[(m["grid2021"] <= 0) & (m["pop2025"] > 0)]
for code in empty:
    sel = parts["LAU_ID"] == code
    if sel.any():
        a = parts.loc[sel].geometry.area
        parts.loc[sel, "p25"] = m.at[code, "pop2025"] * a / a.sum()
print(f"municipalities with no 2021 grid residents, spread by area: {len(empty)}")
# municipalities in the 2025 list without a polygon cannot be placed; give their people
# to the whole country proportionally so the national total still matches
unplaced = m.loc[missing_in_lau, "pop2025"].sum()

# 3. back to cells
cell25 = parts.groupby("grd_inspir")["p25"].sum()
grid["p2025"] = grid["grd_inspir"].map(cell25).fillna(0.0)
if unplaced:
    grid["p2025"] *= (grid["p2025"].sum() + unplaced) / grid["p2025"].sum()
grid[POP] = np.round(grid["p2025"]).astype(int)

print(f"2021 grid total: {int(gpd.read_file(GRID, columns=[POP])[POP].sum()):,}")
print(f"2025 estimate total: {int(grid[POP].sum()):,}  (ČSÚ register: {p25.sum():,})")
chk = parts.groupby("LAU_ID")["p25"].sum()
both = chk.index.intersection(p25.index)
err = (chk[both] - p25[both]).abs()
print(f"municipal totals reproduced: max abs error {err.max():.2f} people over {len(both):,} municipalities")
grid[["grd_inspir", POP, "geometry"]].to_file(OUT, driver="GPKG")
print("saved", OUT)
