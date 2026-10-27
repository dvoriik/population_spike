"""
Czechia population spike map in 3D -- rendered with forge3d, several colour themes.

Pipeline (same approach as Milos Agathon's population-spike examples in forge3d,
github.com/milos-agathon/forge3d/tree/main/examples/population_spike_worldpop):
  1. ČSÚ 1 km population grid (Census 2021) -> 512 x 512 raster in EPSG:3035
  2. Optional relief: Copernicus DEM resampled onto the same 1 km grid; spikes stand on it
  3. Each spike coloured by population class; empty land tinted by elevation
  4. forge3d TerrainRenderer draws the tilted 3D view offscreen (mesh camera, low sun, shadows)
  5. PIL swaps the render background for the plate and adds title, callout, labels, legend

Data:  https://geodata.csu.gov.cz/as/data/distribuce/Hosted/Gridy_SLDB2021/FeatureServer/0/gpkg.zip
Relief (optional, --dem DIR): Copernicus DEM GLO-90 tiles N48-N51 x E012-E018, e.g.
       https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_N50_00_E014_00_DEM/Copernicus_DSM_COG_30_N50_00_E014_00_DEM.tif
Needs: pip install forge3d geopandas pyogrio pillow numpy pyproj rasterio
       plus a GPU, or software Vulkan on Linux (apt install mesa-vulkan-drivers)

Usage: python czechia_spikes_forge3d.py GRID.gpkg OUT.png --dem DEM_DIR --theme glacier
       themes: glacier, night, terracotta, editorial
"""
from __future__ import annotations

import argparse
import math
import tempfile
from pathlib import Path

import numpy as np
import geopandas as gpd
import forge3d as f3d
from forge3d.terrain_params import (
    DetailSettings, HeightAoSettings, PomSettings, make_terrain_params_config,
)
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer

# ---- shared settings ------------------------------------------------------
BREAKS = [1, 25, 75, 150, 300, 600, 1200, 2500, 5000, 10000]   # people per km²
UNIT_M = 100.0                 # 1 scene unit = 100 m (forge3d keeps coords < 65 536)
TALLEST_SPIKE_KM = 62.0        # Prague's peak height in the scene
RELIEF_EXAGGERATION = 6.0      # terrain height multiplier (Sněžka 1 603 m -> ~10 km)
SPARSE_FADE = 0.55             # how much the 1-24 people/km² class blends into the terrain
SENTINEL = (255, 0, 255)       # outside Czechia; replaced by the plate after render
PERIOD = "Census 2021"                              # shown in the header (--period)
DATA_CREDIT = "Data: ČSÚ – Populační grid SLDB 2021"  # shown in the footer (--data-credit)

THEMES = {
    # light, cool blue-grey relief + hot sunset spikes
    "glacier": dict(
        plate=(243, 245, 248), ink=(22, 26, 42), muted=(102, 110, 130),
        relief=((238, 241, 246), (172, 184, 202)),
        palette=[(255, 222, 128), (255, 192, 84), (255, 158, 62), (248, 120, 56), (234, 82, 64),
                 (208, 46, 78), (166, 24, 88), (120, 14, 86), (78, 10, 74), (42, 8, 56)],
        sun=1.9, ibl=1.2, exposure=1.15, lift=1.05, saturation=1.55),
    # dark navy, glowing spikes
    "night": dict(
        plate=(10, 13, 23), ink=(246, 241, 230), muted=(140, 148, 172),
        relief=((28, 34, 52), (84, 96, 126)),
        palette=[(82, 46, 128), (118, 52, 148), (158, 58, 146), (196, 70, 128), (228, 92, 106),
                 (248, 126, 84), (253, 164, 72), (254, 202, 92), (255, 232, 150), (255, 250, 222)],
        sun=1.7, ibl=0.45, exposure=1.1, lift=1.0, saturation=1.15),
    # warm sand relief + deep teal/blue spikes
    "terracotta": dict(
        plate=(248, 241, 231), ink=(44, 32, 28), muted=(134, 112, 100),
        relief=((241, 228, 208), (194, 156, 122)),
        palette=[(186, 226, 220), (146, 210, 208), (104, 190, 198), (68, 165, 189), (44, 137, 176),
                 (32, 108, 160), (28, 82, 140), (26, 60, 116), (22, 42, 90), (14, 26, 62)],
        sun=1.9, ibl=1.4, exposure=1.15, lift=1.05, saturation=1.25),
    # paper + greys, only the dense places in red
    "editorial": dict(
        plate=(250, 249, 246), ink=(18, 18, 18), muted=(112, 112, 112),
        relief=((241, 241, 239), (178, 178, 176)),
        palette=[(214, 214, 212), (190, 190, 188), (160, 160, 158), (130, 130, 128), (100, 100, 98),
                 (236, 124, 104), (224, 74, 60), (202, 34, 38), (162, 18, 30), (112, 8, 20)],
        sun=1.9, ibl=1.4, exposure=1.15, lift=1.05, saturation=1.25),
}

LABEL_SHIFT = {"Ústí nad Labem": (-70, 0)}   # px at 3000 px plate width, to dodge Prague's spike
CITIES = {
    "Praha": (14.42, 50.08), "Brno": (16.61, 49.20), "Ostrava": (18.28, 49.83),
    "Plzeň": (13.38, 49.75), "Liberec": (15.06, 50.77), "Olomouc": (17.25, 49.59),
    "České Budějovice": (14.47, 48.97), "Hradec Králové": (15.83, 50.21),
    "Ústí nad Labem": (14.03, 50.66), "Zlín": (17.67, 49.23),
}
FONTS = Path("/usr/share/fonts/opentype/inter")   # Inter; falls back to DejaVu


def font(name: str, px: float):
    p = FONTS / f"{name}.otf"
    if not p.exists():
        p = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if "Bold" in name or "Black" in name
                 else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    return ImageFont.truetype(str(p), max(8, int(px)))


# ---- data -------------------------------------------------------------------
def load_grid(path: str, size: int = 512):
    """ČSÚ grid -> square population raster (row 0 = north), NaN outside Czechia.

    forge3d's mesh camera draws a fixed 512 x 512 vertex grid over the raster,
    so a 512 x 512 raster at 1 km gives exactly one vertex (one spike) per km².
    """
    g = gpd.read_file(path, columns=["grd_inspir", "g131620000"], read_geometry=False)
    ids = g["grd_inspir"].str.extract(r"N(\d+)E(\d+)").astype(int)
    n, e = ids[0].to_numpy(), ids[1].to_numpy()
    pop = g["g131620000"].to_numpy().astype(np.float32)
    rows_used, cols_used = n.max() - n.min() + 1, e.max() - e.min() + 1
    assert rows_used <= size and cols_used <= size, "country larger than the mesh grid"
    n1 = n.max() + (size - rows_used) // 2          # northern edge (km)
    e0 = e.min() - (size - cols_used) // 2          # western edge (km)
    grid = np.full((size, size), np.nan, np.float32)
    grid[n1 - n, e - e0] = pop
    extent = dict(e0=e0 * 1000.0, n1=(n1 + 1) * 1000.0, cols=size, rows=size)
    return grid, extent


def load_dem(dem_dir: str, extent) -> np.ndarray:
    """Mosaic DEM GeoTIFFs and average them onto the 1 km EPSG:3035 grid (metres)."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, Resampling

    tiles = sorted(Path(dem_dir).glob("*.tif"))
    if not tiles:
        raise SystemExit(f"no .tif files in {dem_dir}")
    srcs = [rasterio.open(t) for t in tiles]
    mosaic, transform = merge(srcs)
    crs = srcs[0].crs
    for s in srcs:
        s.close()
    dem = np.zeros((extent["rows"], extent["cols"]), np.float32)
    reproject(mosaic[0].astype(np.float32), dem, src_transform=transform, src_crs=crs,
              dst_transform=from_origin(extent["e0"], extent["n1"], 1000.0, 1000.0),
              dst_crs="EPSG:3035", resampling=Resampling.average, src_nodata=-32767, dst_nodata=0)
    return np.clip(dem, 0, None)


def build_layers(grid, dem, theme):
    """Height raster (terrain + one spike per km²) and per-cell colour raster."""
    inside = np.isfinite(grid)
    pop = np.where(inside, grid, 0.0)
    scale = TALLEST_SPIKE_KM * 1000.0 / UNIT_M / pop.max()
    base = np.zeros_like(pop) if dem is None else dem / UNIT_M * RELIEF_EXAGGERATION
    h = (pop * scale + base).astype(np.float32)

    cls = np.zeros(grid.shape, np.int32)
    for i, b in enumerate(BREAKS):
        cls[pop >= b] = i + 1
    lo, hi = (np.array(c, np.float32) for c in theme["relief"])
    elev = dem if dem is not None else np.full(pop.shape, 300.0, np.float32)
    t = (np.clip((elev - 150.0) / 1250.0, 0, 1) ** 0.8)[..., None]
    land = (1 - t) * lo + t * hi
    lut = np.array([(0, 0, 0)] + theme["palette"], np.float32)
    rgb = lut[cls]
    rgb = np.where((cls == 0)[..., None], land, rgb)
    rgb = np.where((cls == 1)[..., None], SPARSE_FADE * land + (1 - SPARSE_FADE) * rgb, rgb)
    rgb = rgb.astype(np.uint8)
    rgb[~inside] = SENTINEL
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    return np.ascontiguousarray(h), np.ascontiguousarray(rgba), base


# ---- render -------------------------------------------------------------------
def neutral_hdr(path: Path, rgb=(1.0, 1.0, 1.0)):
    """Tiny uniform-sky Radiance HDR so ambient light stays neutral."""
    w, hgt = 8, 4
    with open(path, "wb") as f:
        f.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n")
        f.write(f"-Y {hgt} +X {w}\n".encode())
        e = math.frexp(max(rgb))[1]
        mant = [int(c / (2.0 ** e) * 256) for c in rgb]
        for _ in range(w * hgt):
            f.write(bytes(mant + [e + 128]))


def render(heights, rgba, theme, *, size, cam, sun_compass, sun_el):
    span = float(heights.shape[1]) * (1000.0 / UNIT_M)        # 1 vertex per km
    settings = make_terrain_params_config(
        size_px=tuple(size), render_scale=1.0, msaa_samples=1, terrain_span=span, z_scale=1.0,
        camera_mode="mesh:zup:north:dem", cam_radius=cam["radius"], cam_phi_deg=cam["phi"],
        cam_theta_deg=cam["theta"], cam_target=cam["target"], fov_y_deg=cam["fov"],
        clip=(10.0, 60000.0),
        domain=(0.0, float(heights.max())), overlays=[f3d.OverlayLayer.from_raster_rgba(rgba)],
        albedo_mode="colormap", hue_variation_strength=0.0, colormap_strength=1.0,
        # push forge3d's height-banded material layers (grass/rock/snow) to the very top so
        # tall spikes keep the matte base material instead of glossy "snow"
        material_layer_centers=(0.997, 0.998, 0.999, 1.0), material_slope_bias=0.0,
        # renderer azimuth = compass azimuth - 90 (same conversion forge3d's MapScene uses)
        light_azimuth_deg=sun_compass - 90.0, light_elevation_deg=sun_el,
        sun_intensity=theme["sun"], ibl_enabled=True, exposure=theme["exposure"], ao_weight=0.0,
        pom=PomSettings(False, "Occlusion", 0.0, 1, 1, 0, False, False),
        detail=DetailSettings(enabled=False), height_ao=HeightAoSettings(enabled=False),
    )
    settings.shadows.enabled = True
    settings.shadows.technique = "PCF"
    session = f3d.Session(window=False, backend="vulkan")
    renderer = f3d.TerrainRenderer(session)
    hdr = Path(tempfile.gettempdir()) / "forge3d_neutral_white.hdr"
    neutral_hdr(hdr)
    ibl = f3d.IBL.from_hdr(str(hdr), intensity=theme["ibl"])
    params = f3d.TerrainRenderParams(settings)
    frame = renderer.render_terrain_pbr_pom(f3d.MaterialSet.terrain_default(), ibl, params, heights)
    return frame.to_numpy()


def to_plate_colours(img: np.ndarray, theme) -> Image.Image:
    """Replace render background + outside-Czechia floor with the plate colour."""
    rgb = img[:, :, :3].astype(np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    magenta = np.clip(np.minimum(r - g, b - g) / 70.0, 0, 1)
    clear = np.all(np.abs(rgb - rgb[0, 0]) <= 3, axis=2)
    mix = np.maximum(magenta, clear.astype(float))[..., None]
    lifted = np.clip(rgb.astype(np.float32) * theme["lift"], 0, 255)
    # restore saturation lost to sky reflections on the steep spike walls
    grey = lifted.mean(axis=2, keepdims=True)
    lifted = np.clip(grey + (lifted - grey) * theme.get("saturation", 1.0), 0, 255)
    out = lifted * (1 - mix) + np.array(theme["plate"], np.float32) * mix
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


# ---- annotation ---------------------------------------------------------------
def make_projector(extent, img_shape, cam):
    """EPSG:3035 x/y (m) + scene height -> pixel, using the same Z-up orbit camera."""
    hgt_px, wid_px = img_shape[:2]
    th, ph = math.radians(cam["theta"]), math.radians(cam["phi"])
    tx, ty, tz = cam["target"]
    # forge3d zup:north orbit -- phi counter-clockwise from +X (east), +Y north
    eye = np.array([tx + cam["radius"] * math.sin(th) * math.cos(ph),
                    ty + cam["radius"] * math.sin(th) * math.sin(ph),
                    tz + cam["radius"] * math.cos(th)])
    fwd = np.array([tx, ty, tz]) - eye
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 0, 1]); right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    f = 1 / math.tan(math.radians(cam["fov"]) / 2)
    aspect = wid_px / hgt_px
    k = 1000.0 / UNIT_M

    def project(x, y, z=0.0):
        """Works on scalars or numpy arrays."""
        x, y, z = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float), np.asarray(z, float))
        sx = ((x - extent["e0"]) / 1000.0 - extent["cols"] / 2) * k
        sy = (extent["rows"] / 2 - (extent["n1"] - y) / 1000.0) * k
        p = np.stack([sx, sy, z], axis=-1) - eye
        cx, cy, cz = p @ right, p @ up, p @ fwd
        u = (cx / cz * f / aspect + 1) / 2 * wid_px
        v = (1 - (cy / cz * f + 1) / 2) * hgt_px
        return (float(u), float(v)) if u.ndim == 0 else (u, v)

    return project


def fit_camera(grid, heights, base, extent, cam, size, margin=0.03, max_steps=40):
    """Back the camera off until every Czech cell (floor and spike tip) is in frame.

    Without this, a low, close camera clips the southern border (Šumava, Jižní Morava).
    """
    rows, cols = np.where(np.isfinite(grid))
    x = extent["e0"] + (cols + 0.5) * 1000.0
    y = extent["n1"] - (rows + 0.5) * 1000.0
    cam = dict(cam)
    w, h = size

    def v_centre(c):
        p = make_projector(extent, (h, w), c)
        vb = p(x, y, base[rows, cols])[1]; vt = p(x, y, heights[rows, cols])[1]
        return (min(vb.min(), vt.min()) + max(vb.max(), vt.max())) / 2 - h / 2

    for _ in range(6):                       # centre vertically by sliding the target north/south
        off = v_centre(cam)
        if abs(off) < 0.005 * h:
            break
        probe = dict(cam, target=(cam["target"][0], cam["target"][1] + 50.0, cam["target"][2]))
        slope = (v_centre(probe) - off) / 50.0
        if abs(slope) < 1e-6:
            break
        t = cam["target"]
        cam["target"] = (t[0], t[1] - off / slope, t[2])
    for _ in range(max_steps):
        project = make_projector(extent, (h, w), cam)
        ub, vb = project(x, y, base[rows, cols])
        ut, vt = project(x, y, heights[rows, cols])
        u = np.concatenate([ub, ut]); v = np.concatenate([vb, vt])
        inside = (u.min() >= margin * w and u.max() <= (1 - margin) * w
                  and v.min() >= margin * h and v.max() <= (1 - margin) * h)
        if inside:
            break
        cam["radius"] *= 1.04
    print(f"camera radius {cam['radius']:.0f} (frame u {u.min():.0f}-{u.max():.0f} of {w}, "
          f"v {v.min():.0f}-{v.max():.0f} of {h})")
    return cam


def compose(render_img, out_png, theme, *, grid, heights, base, extent, cam, relief, labels=True):
    plate_rgb, ink, muted = theme["plate"], theme["ink"], theme["muted"]
    arr = np.array(render_img).astype(np.int16)
    ys, xs = np.where(np.abs(arr - np.array(plate_rgb)).sum(axis=2) > 10)
    pad = 40
    box = (max(xs.min() - pad, 0), max(ys.min() - pad, 0),
           min(xs.max() + pad, render_img.width), min(ys.max() + pad, render_img.height))
    r = render_img.crop(box)
    top, bottom, side = int(r.width * 0.16), int(r.width * 0.085), int(r.width * 0.045)
    W, H = r.width + 2 * side, r.height + top + bottom
    plate = Image.new("RGB", (W, H), plate_rgb)
    plate.paste(r, (side, top))
    d = ImageDraw.Draw(plate)
    s = W / 3000
    project = make_projector(extent, np.array(render_img).shape, cam)
    to_plate = lambda u, v: (side + u - box[0], top + v - box[1])

    # title block
    d.text((side, int(92 * s)), "WHERE CZECHS LIVE", font=font("InterDisplay-Black", 118 * s), fill=ink)
    d.text((side + 4, int(238 * s)),
           "Every spike is one square kilometre. Its height and colour show how many people live there.",
           font=font("Inter-Regular", 40 * s), fill=muted)
    total = f"{int(np.nansum(grid)):,}".replace(",", " ")
    d.text((side + 4, int(296 * s)), f"{PERIOD}  ·  {total} residents",
           font=font("Inter-Medium", 40 * s), fill=muted)

    # city labels at the base of each city
    tr = Transformer.from_crs(4326, 3035, always_xy=True)
    rows, cols = grid.shape
    if labels:
        for name, (lon, lat) in CITIES.items():
            if name == "Praha":
                continue
            x, y = tr.transform(lon, lat)
            ri = int((extent["n1"] - y) // 1000); ci = int((x - extent["e0"]) // 1000)
            u, v = to_plate(*project(x, y, float(base[ri, ci])))
            f_city = font("Inter-SemiBold", 34 * s)
            tw = d.textlength(name, font=f_city)
            dx, dy = LABEL_SHIFT.get(name, (0, 0))
            d.text((u - tw / 2 + dx * s, v + (14 + dy) * s), name, font=f_city, fill=ink,
                   stroke_width=max(2, int(6 * s)), stroke_fill=plate_rgb)

    # Prague callout: leader line from the tallest spike
    ri, ci = np.unravel_index(np.nanargmax(grid), grid.shape)
    px = extent["e0"] + (ci + 0.5) * 1000.0
    py = extent["n1"] - (ri + 0.5) * 1000.0
    bu, bv = to_plate(*project(px, py, float(base[ri, ci])))
    tu, tv = to_plate(*project(px, py, float(heights[ri, ci])))
    # the mesh samples the raster between texels, so peaks render a bit lower than
    # their nominal height: find the actual topmost spike pixel above Prague's base
    pa = np.array(plate).astype(np.int16)
    x0, x1 = int(bu - 30 * s), int(bu + 30 * s)
    col = np.abs(pa[int(tv):int(bv), x0:x1] - np.array(plate_rgb)).sum(axis=2) > 30
    rows_hit = np.where(col.any(axis=1))[0]
    if rows_hit.size:
        tv = int(tv) + rows_hit[0]
        tu = x0 + float(np.mean(np.where(col[rows_hit[0]])[0]))
    ly = max(int(tv - 150 * s), int(360 * s))
    lx = tu + 260 * s
    d.line([(tu, tv - 6 * s), (tu, ly), (lx, ly)], fill=ink, width=max(2, int(3 * s)))
    d.ellipse([tu - 7 * s, tv - 13 * s, tu + 7 * s, tv + 1 * s], outline=ink, width=max(2, int(3 * s)))
    peak = f"{int(np.nanmax(grid)):,}".replace(",", " ")
    d.text((lx + 14 * s, ly - 46 * s), peak, font=font("InterDisplay-Black", 64 * s), fill=ink,
           stroke_width=max(2, int(6 * s)), stroke_fill=plate_rgb)
    d.text((lx + 16 * s, ly + 26 * s), "people in a single km² — Praha",
           font=font("Inter-Medium", 30 * s), fill=ink,
           stroke_width=max(2, int(5 * s)), stroke_fill=plate_rgb)

    # legend
    lx0, sw, sh = side, int(92 * s), int(22 * s)
    ly0 = H - int(122 * s)
    d.text((lx0, ly0 - int(48 * s)), "PEOPLE PER KM²", font=font("Inter-SemiBold", 26 * s), fill=muted)
    for i, rgb in enumerate(theme["palette"]):
        d.rectangle([lx0 + i * sw, ly0, lx0 + (i + 1) * sw - 3, ly0 + sh], fill=tuple(rgb))
        d.text((lx0 + i * sw, ly0 + sh + int(8 * s)), f"{BREAKS[i]:,}".replace(",", " "),
               font=font("Inter-Regular", 25 * s), fill=muted)
    credit = (DATA_CREDIT
              + ("  ·  Relief: Copernicus DEM GLO-90" if relief else "")
              + "  ·  Rendered with forge3d")
    f_cr = font("Inter-Regular", 25 * s)
    d.text((W - side - d.textlength(credit, font=f_cr), H - int(84 * s)), credit, font=f_cr, fill=muted)
    plate.save(out_png)
    return plate.size


def concentration_facts(grid):
    """Numbers for the stat tiles: how unevenly people are spread."""
    pop = np.sort(grid[np.isfinite(grid)])[::-1]
    cum = np.cumsum(pop)
    half_cells = int(np.searchsorted(cum, cum[-1] / 2) + 1)
    return dict(half_cells=half_cells, half_share=half_cells / pop.size,
                empty_share=float((pop == 0).mean()), peak=int(pop[0]), total=int(cum[-1]))


def compose_linkedin(render_img, out_png, theme, *, grid, heights, base, extent, cam, relief,
                     labels=True, W=2160, H=2700):
    """4:5 portrait plate for a LinkedIn feed post: title, map, three stat tiles, legend."""
    plate_rgb, ink, muted = theme["plate"], theme["ink"], theme["muted"]
    accent = tuple(theme.get("accent", theme["palette"][-3]))
    arr = np.array(render_img).astype(np.int16)
    ys, xs = np.where(np.abs(arr - np.array(plate_rgb)).sum(axis=2) > 10)
    pad = 20
    box = (max(xs.min() - pad, 0), max(ys.min() - pad, 0),
           min(xs.max() + pad, render_img.width), min(ys.max() + pad, render_img.height))
    side = 100
    map_w = W - 2 * side + 40
    k = map_w / (box[2] - box[0])
    map_h = int((box[3] - box[1]) * k)
    head_end, foot_start, tiles_h = 495, H - 330, 270
    gap = max(30, (foot_start - head_end - map_h - tiles_h) / 3)
    mx, my = side - 20, int(head_end + gap)
    plate = Image.new("RGB", (W, H), plate_rgb)
    plate.paste(render_img.crop(box).resize((map_w, map_h), Image.LANCZOS), (mx, my))
    d = ImageDraw.Draw(plate)
    project = make_projector(extent, np.array(render_img).shape, cam)
    to_plate = lambda u, v: (mx + (u - box[0]) * k, my + (v - box[1]) * k)
    facts = concentration_facts(grid)
    nbsp = lambda n: f"{n:,}".replace(",", " ")

    # header
    title, size = "WHERE CZECHS LIVE", 190
    while d.textlength(title, font=font("InterDisplay-Black", size)) > W - 2 * side:
        size -= 4
    d.text((side - 6, 105), title, font=font("InterDisplay-Black", size), fill=ink)
    d.text((side, 335), "Every spike is one square kilometre.", font=font("Inter-Medium", 50), fill=ink)
    d.text((side, 401), "The taller it gets, the more people live there.",
           font=font("Inter-Regular", 50), fill=muted)

    # city labels
    tr = Transformer.from_crs(4326, 3035, always_xy=True)
    if labels:
        for name, (lon, lat) in CITIES.items():
            x, y = tr.transform(lon, lat)
            ri = int((extent["n1"] - y) // 1000); ci = int((x - extent["e0"]) // 1000)
            u, v = to_plate(*project(x, y, float(base[ri, ci])))
            if name == "Praha":
                # put Prague's name above its peak, which towers over the base
                pr, pc = np.unravel_index(np.nanargmax(grid), grid.shape)
                px = extent["e0"] + (pc + 0.5) * 1000.0
                py = extent["n1"] - (pr + 0.5) * 1000.0
                tu, tv = to_plate(*project(px, py, float(heights[pr, pc])))
                bu, bv = to_plate(*project(px, py, float(base[pr, pc])))
                pa = np.array(plate).astype(np.int16)
                x0, x1 = int(bu - 20), int(bu + 20)
                y0 = max(int(tv) - 40, my)
                col = np.abs(pa[y0:int(bv), x0:x1] - np.array(plate_rgb)).sum(axis=2) > 30
                hit = np.where(col.any(axis=1))[0]
                top_v = y0 + (hit[0] if hit.size else 0)
                f_p = font("Inter-Bold", 40)
                tw = d.textlength("Praha", font=f_p)
                d.text((bu - tw / 2, top_v - 64), "Praha", font=f_p, fill=ink,
                       stroke_width=6, stroke_fill=plate_rgb)
                continue
            f_city = font("Inter-SemiBold", 30)
            tw = d.textlength(name, font=f_city)
            dx, dy = LABEL_SHIFT.get(name, (0, 0))
            d.text((u - tw / 2 + dx * 0.7, v + 10 + dy), name, font=f_city, fill=ink,
                   stroke_width=5, stroke_fill=plate_rgb)

    # three stat tiles
    ty = int(my + map_h + gap)
    tiles = [
        (f"{facts['half_share'] * 100:.1f} %".replace(".", ","), "of the land is home to",
         "half of all people"),
        (f"{round(facts['empty_share'] * 100)} %", "of all square kilometres", "have nobody living there"),
        (nbsp(facts["peak"]), "people live in Prague's", "most crowded km²"),
    ]
    tw_ = (W - 2 * side) / 3
    for i, (big, l1, l2) in enumerate(tiles):
        x = side + i * tw_
        if i:
            d.line([(x - 30, ty + 10), (x - 30, ty + 250)], fill=muted, width=2)
        d.text((x, ty), big, font=font("InterDisplay-Black", 112), fill=accent)
        d.text((x + 4, ty + 150), l1, font=font("Inter-Regular", 34), fill=muted)
        d.text((x + 4, ty + 196), l2, font=font("Inter-SemiBold", 34), fill=ink)

    # legend + credits
    ly0 = H - 250
    sw, sh = 104, 22
    d.text((side, ly0 - 50), "PEOPLE PER KM²", font=font("Inter-SemiBold", 28), fill=muted)
    for i, rgb in enumerate(theme["palette"]):
        d.rectangle([side + i * sw, ly0, side + (i + 1) * sw - 4, ly0 + sh], fill=tuple(rgb))
        d.text((side + i * sw, ly0 + sh + 10), nbsp(BREAKS[i]), font=font("Inter-Regular", 26), fill=muted)
    credit = (DATA_CREDIT
              + ("  ·  Relief: Copernicus DEM" if relief else "")
              + "  ·  Made with Python & forge3d")
    d.text((side, H - 100), credit, font=font("Inter-Regular", 28), fill=muted)
    plate.save(out_png)
    return plate.size


def main():
    global PERIOD, DATA_CREDIT, TALLEST_SPIKE_KM
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", choices=["landscape", "linkedin"], default="landscape",
                    help="linkedin = 4:5 portrait 2160 x 2700 for a feed post")
    ap.add_argument("grid")
    ap.add_argument("out")
    ap.add_argument("--theme", choices=sorted(THEMES), default="glacier")
    ap.add_argument("--dem", help="folder with DEM GeoTIFF tiles (adds terrain relief)")
    ap.add_argument("--size", type=int, nargs=2, default=(3600, 2250))
    ap.add_argument("--theta", type=float, default=60.0, help="tilt: 0 = top-down, 90 = horizon")
    ap.add_argument("--phi", type=float, default=270.0, help="camera azimuth: 270 = looking north from the south")
    ap.add_argument("--radius", type=float, default=5700.0)
    ap.add_argument("--fov", type=float, default=30.0)
    ap.add_argument("--target", type=float, nargs=3, default=(0.0, 250.0, 0.0))
    ap.add_argument("--sun-compass", type=float, default=245.0, help="sun direction, compass degrees")
    ap.add_argument("--sun-el", type=float, default=28.0, help="sun elevation, degrees (low = long shadows)")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--period", default=PERIOD, help='header label, e.g. "1 Jan 2025 (estimate)"')
    ap.add_argument("--data-credit", default=DATA_CREDIT, help="footer data credit")
    ap.add_argument("--no-autofit", action="store_true", help="keep the camera exactly as given")
    ap.add_argument("--spike-km", type=float, default=None, help="height of the tallest spike (default 62)")
    a = ap.parse_args()

    PERIOD, DATA_CREDIT = a.period, a.data_credit
    theme = THEMES[a.theme]
    if a.spike_km:
        TALLEST_SPIKE_KM = a.spike_km
    grid, extent = load_grid(a.grid)
    dem = load_dem(a.dem, extent) if a.dem else None
    heights, rgba, base = build_layers(grid, dem, theme)
    cam = dict(theta=a.theta, phi=a.phi, radius=a.radius, fov=a.fov, target=tuple(a.target))
    if not a.no_autofit:
        cam = fit_camera(grid, heights, base, extent, cam, a.size)
    img = render(heights, rgba, theme, size=a.size, cam=cam, sun_compass=a.sun_compass, sun_el=a.sun_el)
    out = Path(a.out)
    composer = compose_linkedin if a.layout == "linkedin" else compose
    size = composer(to_plate_colours(img, theme), out, theme, grid=grid, heights=heights, base=base,
                    extent=extent, cam=cam, relief=dem is not None, labels=not a.no_labels)
    print("saved", out, size)


if __name__ == "__main__":
    main()
