"""
Build a current 1 km population grid from ČSÚ building points (RSO, "Budovy s číslem domovním").

Each building point carries BUDOBYEV = number of residents registered in the building
(population register, including foreigners). The points are summed into the same
INSPIRE 1 km cells as the Census 2021 grid, so the map script can use the output as is.

Office addresses: people with no real address are registered at the municipal office
(radnice, úřad městské části), which would turn town halls into the tallest spikes.
Buildings that look like that -- non-residential building type (TVYBU != 03), at least
MIN_OFFICE residents and at least RATIO residents per flat -- have their residents spread
over the other inhabited buildings of the same municipality (in Prague: of the same
administrative district), proportionally to those buildings' residents.

Data: https://geodata.csu.gov.cz/as/data/distribuce/Hosted/Open_data_RSO/FeatureServer/2/gpkg.zip
      (the zip holds Budovy_s_cislem_domovnim_YYYYMMDD.shp)

Usage: python build_grid_rso.py BUILDINGS.shp CENSUS_GRID_2021.gpkg OUT.gpkg [--keep-office]
"""
import argparse

import numpy as np
import pandas as pd
import geopandas as gpd
import pyogrio
from pyproj import Transformer

MIN_OFFICE = 50     # residents
RATIO = 10          # residents per flat
POP = "g131620000"  # field name the map script reads

ap = argparse.ArgumentParser()
ap.add_argument("buildings")
ap.add_argument("census_grid")
ap.add_argument("out")
ap.add_argument("--keep-office", action="store_true", help="do not redistribute office registrations")
a = ap.parse_args()

cols = ["budobyev", "sum_byt", "tvybu", "kod_obec", "naz_obec", "kod_sop", "naz_cast", "cis_d"]
b = pyogrio.read_dataframe(a.buildings, columns=cols)
b["budobyev"] = b["budobyev"].fillna(0).astype(float)
print(f"buildings: {len(b):,}, inhabited: {(b.budobyev > 0).sum():,}, registered residents: {b.budobyev.sum():,.0f}")

# --- office registrations -------------------------------------------------------
office = ((b.budobyev >= MIN_OFFICE) & (b.tvybu != "03")
          & (b.budobyev / np.maximum(b.sum_byt.fillna(0), 1) >= RATIO))
print(f"office-address buildings: {office.sum():,} with {b.loc[office, 'budobyev'].sum():,.0f} residents")
top = b.loc[office].nlargest(8, "budobyev")
for _, r in top.iterrows():
    print(f"   {int(r.budobyev):>5}  {r.naz_obec} – {r.naz_cast} {r.cis_d}")

b["pop"] = b["budobyev"]
if not a.keep_office:
    prague = b.kod_obec == "554782"
    b["area_key"] = np.where(prague & b.kod_sop.notna(), "P" + b.kod_sop.astype(str), b.kod_obec.astype(str))
    moved = b.loc[office].groupby("area_key")["budobyev"].sum()
    b.loc[office, "pop"] = 0.0
    base = b.loc[~office].groupby("area_key")["budobyev"].sum()
    share = moved.reindex(base.index).fillna(0) / base.where(base > 0)
    b["pop"] += np.where(~office, b["budobyev"] * b["area_key"].map(share).fillna(0), 0)
    lost = moved.sum() - (b["pop"].sum() - b.loc[~office, "budobyev"].sum())
    print(f"redistributed within municipality/district; not placeable (no other residents): {lost:,.0f}")

# --- to 1 km INSPIRE cells ------------------------------------------------------------
tr = Transformer.from_crs(b.crs, 3035, always_xy=True)
x, y = tr.transform(b.geometry.x.to_numpy(), b.geometry.y.to_numpy())
cell = ("1kmN" + (np.floor(y / 1000).astype(int)).astype(str)
        + "E" + (np.floor(x / 1000).astype(int)).astype(str))
per_cell = pd.Series(b["pop"].to_numpy(), index=cell).groupby(level=0).sum()

grid = gpd.read_file(a.census_grid, columns=["grd_inspir"])
extra = per_cell.index.difference(grid["grd_inspir"])
print(f"cells: {grid.shape[0]:,} census cells; {len(extra)} cells with buildings outside them "
      f"({per_cell[extra].sum():,.0f} residents) -- added")
if len(extra):
    n = extra.str.extract(r"N(\d+)E(\d+)").astype(int)
    from shapely.geometry import box
    geoms = [box(e * 1000, nn * 1000, e * 1000 + 1000, nn * 1000 + 1000) for nn, e in zip(n[0], n[1])]
    add = gpd.GeoDataFrame({"grd_inspir": extra}, geometry=geoms, crs=3035).to_crs(grid.crs)
    grid = pd.concat([grid, add], ignore_index=True)
grid[POP] = np.round(grid["grd_inspir"].map(per_cell).fillna(0)).astype(int)
print(f"grid total: {grid[POP].sum():,}  | densest cell {grid.loc[grid[POP].idxmax(), 'grd_inspir']}: {grid[POP].max():,}")
grid.to_file(a.out, driver="GPKG")
print("saved", a.out)
