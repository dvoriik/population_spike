"""
Population spike map of Czechia from the CZSO 1 km population grid (Census 2021).

Data:  CZSO / ČSÚ - "Obyvatelstvo za kilometrové gridy - Populační grid (Sčítání 2021)"
       https://geodata.csu.gov.cz/as/data/distribuce/Hosted/Gridy_SLDB2021/FeatureServer/0/gpkg.zip
Border: Eurostat GISCO countries 1:1M (EPSG:3035)
       https://gisco-services.ec.europa.eu/distribution/v2/countries/geojson/CNTR_RG_01M_2020_3035.geojson

Usage: python spike_map_cz.py GRID.gpkg COUNTRIES.geojson OUT.png
Needs: geopandas, pyogrio, matplotlib, numpy, pyproj
"""
import sys
import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from matplotlib.colors import LinearSegmentedColormap, PowerNorm
from pyproj import Transformer
import matplotlib.patheffects as pe

GRID, BORDER, OUT = sys.argv[1:4]

# ---- settings you may want to tweak ---------------------------------------
POP_FIELD = "g131620000"      # usually resident population (obvykle bydlící)
MAX_SPIKE_M = 85_000          # height of the tallest spike, in map metres
HALF_WIDTH_M = 650            # half of the spike base width
BG = "#0b0d14"
CMAP = LinearSegmentedColormap.from_list(
    "spike", ["#3b1f5c", "#a8326e", "#f0703c", "#ffd36e", "#fff6d8"])
# ---------------------------------------------------------------------------

grid = gpd.read_file(GRID, columns=["grd_inspir", POP_FIELD], read_geometry=False)
grid = grid[grid[POP_FIELD] > 0].copy()

# Cell id "1kmN3006E4639" = lower-left corner in km of EPSG:3035 -> cell centre in m
ids = grid["grd_inspir"].str.extract(r"N(\d+)E(\d+)").astype(int)
x = ids[1].to_numpy() * 1000 + 500
y = ids[0].to_numpy() * 1000 + 500
pop = grid[POP_FIELD].to_numpy().astype(float)

# Draw from north to south so spikes in front overlap the ones behind
order = np.lexsort((x, -y))
x, y, pop = x[order], y[order], pop[order]

h = pop / pop.max() * MAX_SPIKE_M
polys = np.stack([
    np.column_stack([x - HALF_WIDTH_M, y]),
    np.column_stack([x, y + h]),
    np.column_stack([x + HALF_WIDTH_M, y]),
], axis=1)

norm = PowerNorm(gamma=0.45, vmin=0, vmax=pop.max())
face = CMAP(norm(pop))
face[:, 3] = 0.25 + 0.6 * norm(pop)          # small spikes fade into the dark
edge = CMAP(np.clip(norm(pop) * 1.15, 0, 1))
edge[:, 3] = 0.35 + 0.65 * norm(pop)

border = gpd.read_file(BORDER)
cz = border[border["CNTR_ID"] == "CZ"].to_crs(3035)

fig, ax = plt.subplots(figsize=(16, 11), dpi=250)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)

cz.plot(ax=ax, facecolor="#141826", edgecolor="#3a4057", linewidth=0.8, zorder=1)
ax.add_collection(PolyCollection(polys, facecolors=face, edgecolors=edge,
                                 linewidths=0.18, zorder=2))

# City labels (placed at the base of the tallest spikes)
cities = {
    "Praha": (14.42, 50.08), "Brno": (16.61, 49.20), "Ostrava": (18.28, 49.83),
    "Plzeň": (13.38, 49.75), "Liberec": (15.06, 50.77), "Olomouc": (17.25, 49.59),
    "České Budějovice": (14.47, 48.97), "Hradec Králové": (15.83, 50.21),
    "Ústí nad Labem": (14.03, 50.66), "Pardubice": (15.78, 50.04),
    "Zlín": (17.67, 49.23), "Jihlava": (15.59, 49.40),
}
tr = Transformer.from_crs(4326, 3035, always_xy=True)
for name, (lon, lat) in cities.items():
    cx, cy = tr.transform(lon, lat)
    ax.text(cx, cy - 4500, name, color="#c9cede", fontsize=8.5, ha="center",
            va="top", fontfamily="DejaVu Sans", zorder=3, alpha=0.95,
            path_effects=[pe.withStroke(linewidth=2.5, foreground=BG)])

xmin, ymin, xmax, ymax = cz.total_bounds
ax.set_xlim(xmin - 15_000, xmax + 15_000)
ax.set_ylim(ymin - 15_000, ymax + MAX_SPIKE_M * 0.55)
ax.set_aspect("equal")
ax.axis("off")

fig.text(0.07, 0.90, "Where Czechs live", color="#f4f1ea", fontsize=30,
         fontweight="bold", fontfamily="DejaVu Sans")
fig.text(0.07, 0.865,
         "Population per 1 km² grid cell · Census 2021 · " + f"{int(pop.sum()):,}".replace(",", " ") + " residents",
         color="#9aa1b5", fontsize=13, fontfamily="DejaVu Sans")
fig.text(0.07, 0.07,
         f"Tallest spike: {int(pop.max()):,} people in one km² (Prague)".replace(",", " "),
         color="#9aa1b5", fontsize=10, fontfamily="DejaVu Sans")
fig.text(0.93, 0.07, "Data: ČSÚ – Populační grid SLDB 2021  ·  Border: Eurostat GISCO",
         color="#6c7389", fontsize=9, ha="right", fontfamily="DejaVu Sans")

fig.savefig(OUT, facecolor=BG, bbox_inches="tight", pad_inches=0.3)
print("saved", OUT, "spikes:", len(pop))
