"""Planning rasters on a common 5 m grid.

surface   – max height of the LGL surface model (DOM1, buildings + trees) per cell
terrain   – ground height: DGM1 (.tif or .xyz) where supplied, elsewhere estimated from DOM1
            (morphological opening that removes buildings/trees) – share recorded in the metadata
bheight   – building height (LoD1/LoD2) per cell, 0 = no building
forest    – 1 where ALKIS land use or Basis-DLM is forest/woodland
water     – 1 where Basis-DLM has a water body (river, lake, harbour basin)
tree      – 1 where DOP20 (RGB + infrared) shows vegetation (NDVI) that stands > 2.5 m above the
            ground and is not a building: tree canopy, which DOM1 alone cannot tell from roofs
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import rasterio
from rasterio import features as rfeatures
from rasterio.transform import from_origin
from scipy import ndimage

from .discover import Source


@dataclass
class Grid:
    x0: float  # left
    y0: float  # top
    res: float
    surface: np.ndarray
    terrain: np.ndarray
    bheight: np.ndarray
    forest: np.ndarray
    terrain_source: str
    water: np.ndarray | None = None
    tree: np.ndarray | None = None

    @property
    def shape(self):
        return self.surface.shape

    @property
    def transform(self):
        return from_origin(self.x0, self.y0, self.res, self.res)

    def rc(self, x, y):
        c = np.floor((np.asarray(x) - self.x0) / self.res).astype(np.int64)
        r = np.floor((self.y0 - np.asarray(y)) / self.res).astype(np.int64)
        ok = (r >= 0) & (r < self.shape[0]) & (c >= 0) & (c < self.shape[1])
        return np.clip(r, 0, self.shape[0] - 1), np.clip(c, 0, self.shape[1] - 1), ok

    def sample(self, arr: np.ndarray, x, y, fill=np.nan):
        r, c, ok = self.rc(x, y)
        out = arr[r, c].astype(np.float64)
        out[~ok] = fill
        return out

    def save(self, path):
        extra = {k: v for k, v in (("water", self.water), ("tree", self.tree)) if v is not None}
        np.savez_compressed(path, surface=self.surface, terrain=self.terrain, bheight=self.bheight,
                            forest=self.forest, **extra,
                            meta=json.dumps(dict(x0=self.x0, y0=self.y0, res=self.res,
                                                 terrain_source=self.terrain_source)))

    @classmethod
    def load(cls, path):
        d = np.load(path)
        meta = json.loads(str(d["meta"]))
        return cls(meta["x0"], meta["y0"], meta["res"], d["surface"], d["terrain"], d["bheight"],
                   d["forest"], meta["terrain_source"], d["water"] if "water" in d.files else None,
                   d["tree"] if "tree" in d.files else None)


def _tiles_extent(sources: list[Source]):
    xs = [s.tile()[0] for s in sources]
    ys = [s.tile()[1] for s in sources]
    return min(xs) * 1000, (max(ys) + 1) * 1000, (max(xs) + 1) * 1000, min(ys) * 1000  # l, t, r, b


def _pool_xyz(sources, grid_shape, x0, y0, res):
    """Mean of ASCII 'x y z' point grids (LGL DGM1 .xyz, 1 m) per grid cell; tiles outside are skipped."""
    H, W = grid_shape
    tot, cnt = np.zeros(H * W), np.zeros(H * W)
    for i, s in enumerate(sources):
        t = s.tile()
        if t and not (x0 - 1000 < t[0] * 1000 < x0 + W * res and y0 - H * res - 1000 < t[1] * 1000 < y0):
            continue
        with s.open() as fh:
            a = pd.read_csv(fh, sep=r"\s+", header=None, usecols=[0, 1, 2], dtype=np.float64).to_numpy()
        c = np.floor((a[:, 0] - x0) / res).astype(np.int64)
        r = np.floor((y0 - a[:, 1]) / res).astype(np.int64)
        ok = (r >= 0) & (r < H) & (c >= 0) & (c < W) & np.isfinite(a[:, 2]) & (a[:, 2] > -1000)
        idx = r[ok] * W + c[ok]
        tot += np.bincount(idx, weights=a[ok, 2], minlength=H * W)
        cnt += np.bincount(idx, minlength=H * W)
        if (i + 1) % 25 == 0:
            print(f"   read {i + 1}/{len(sources)} DGM1 tiles")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, tot / cnt, np.nan).reshape(H, W).astype(np.float32)


NDVI_GREEN = 0.2  # NDVI above this = vegetation (uncalibrated 8-bit DOP: conservative)


def _pool_green(sources, grid_shape, x0, y0, res):
    """Share of green (NDVI > NDVI_GREEN) 1 m pixels per grid cell from DOP20 RGBI tiles.
    Tiles are read at 1 m through their internal overviews (~0.5 s per km², no 20 cm decode)."""
    from rasterio.enums import Resampling
    out = np.full(grid_shape, np.nan, np.float32)
    H, W = grid_shape
    for i, s in enumerate(sources):
        with rasterio.open(s.gdal_path) as r:
            if r.count < 4:
                raise ValueError(f"{s.name}: needs 4 bands (RGB + infrared) – download DOP20 'RGBI', not 'RGB'")
            n = int(round((r.bounds.right - r.bounds.left) / 1.0))  # 1 m pixels
            m = int(round((r.bounds.top - r.bounds.bottom) / 1.0))
            a = r.read([1, 4], out_shape=(2, m, n), resampling=Resampling.average).astype(np.float32)
            top, left = r.bounds.top, r.bounds.left
        red, nir = a
        green = ((nir - red) / np.clip(nir + red, 1, None) > NDVI_GREEN).astype(np.float32)
        k = int(round(res))
        hh, ww = (m // k) * k, (n // k) * k
        p = green[:hh, :ww].reshape(hh // k, k, ww // k, k).mean(axis=(1, 3))
        row, col = int(round((y0 - top) / res)), int(round((left - x0) / res))
        r0, c0 = max(row, 0), max(col, 0)
        r1, c1 = min(row + p.shape[0], H), min(col + p.shape[1], W)
        if r1 > r0 and c1 > c0:
            out[r0:r1, c0:c1] = p[r0 - row:r1 - row, c0 - col:c1 - col]
        if (i + 1) % 25 == 0:
            print(f"   read {i + 1}/{len(sources)} DOP20 tiles")
    return out


def _pool_tiles(sources, grid_shape, x0, y0, res, how):
    xyz = [s for s in sources if s.name.lower().endswith(".xyz")]
    sources = [s for s in sources if s not in xyz]
    out = _pool_xyz(xyz, grid_shape, x0, y0, res) if xyz else np.full(grid_shape, np.nan, np.float32)
    for s in sources:
        with rasterio.open(s.gdal_path) as r:
            a = r.read(1).astype(np.float32)
            nod = r.nodata
            if nod is not None:
                a[a == nod] = np.nan
            a[a < -1000] = np.nan
            k = int(round(res / r.res[0]))
            h, w = (a.shape[0] // k) * k, (a.shape[1] // k) * k
            blocks = a[:h, :w].reshape(h // k, k, w // k, k)
            with np.errstate(all="ignore"):
                p = {"max": np.nanmax, "min": np.nanmin, "mean": np.nanmean}[how](blocks, axis=(1, 3))
            row = int(round((y0 - r.bounds.top) / res))
            col = int(round((r.bounds.left - x0) / res))
            out[row:row + p.shape[0], col:col + p.shape[1]] = p
    return out


def _estimate_terrain(dom_min: np.ndarray, forest: np.ndarray, bheight: np.ndarray,
                      ground_from_buildings: np.ndarray) -> np.ndarray:
    nan = np.isnan(dom_min)
    filled = np.where(nan, np.nanmax(dom_min), dom_min)
    small = ndimage.grey_opening(filled, size=(31, 31))     # removes objects < ~150 m
    large = ndimage.grey_opening(filled, size=(81, 81))     # for forest blocks
    terr = np.where(forest > 0, large, small)
    terr = np.minimum(terr, filled)
    has_b = ~np.isnan(ground_from_buildings)
    terr[has_b] = ground_from_buildings[has_b]             # LoD ground heights are measured
    terr = ndimage.gaussian_filter(terr, 1.5)
    terr[nan] = np.nan
    return terr.astype(np.float32)


FOREST_DLM = ("AX_Wald", "AX_Gehoelz")
WATER_DLM = ("AX_Fliessgewaesser", "AX_StehendesGewaesser", "AX_Hafenbecken")


def build_grid(dom: list[Source], dgm: list[Source], buildings, landuse, res: float, dlm=None,
               dop: list[Source] | None = None) -> Grid:
    l, t, r, b = _tiles_extent(dom)
    shape = (int(round((t - b) / res)), int(round((r - l) / res)))
    tr = from_origin(l, t, res, res)
    print(f"   grid {shape[1]}x{shape[0]} cells @ {res} m, extent E{l / 1000:.0f}-{r / 1000:.0f} N{b / 1000:.0f}-{t / 1000:.0f} km")
    surface = _pool_tiles(dom, shape, l, t, res, "max")

    bh = rfeatures.rasterize(((g, h) for g, h in zip(buildings.geometry, buildings.height)),
                             out_shape=shape, transform=tr, fill=0, dtype="float32",
                             merge_alg=rasterio.enums.MergeAlg.replace, all_touched=False)
    bground = rfeatures.rasterize(((g, z) for g, z in zip(buildings.geometry, buildings.ground_z)),
                                  out_shape=shape, transform=tr, fill=np.nan, dtype="float32")
    forest = np.zeros(shape, np.uint8)
    if landuse is not None and len(landuse):
        woods = landuse[landuse["nutzart"].isin(["Wald", "Gehoelz"])]
        if len(woods):
            forest = rfeatures.rasterize(((g, 1) for g in woods.geometry), out_shape=shape,
                                         transform=tr, fill=0, dtype="uint8")
    water = None
    if dlm is not None and len(dlm):
        # Basis-DLM covers the whole grid (ALKIS land use only the Heilbronn districts)
        w = dlm[dlm["OBJART_TXT"].isin(FOREST_DLM)]
        if len(w):
            forest = np.maximum(forest, rfeatures.rasterize(((g, 1) for g in w.geometry), out_shape=shape,
                                                            transform=tr, fill=0, dtype="uint8"))
        wa = dlm[dlm["OBJART_TXT"].isin(WATER_DLM)]
        water = (rfeatures.rasterize(((g, 1) for g in wa.geometry), out_shape=shape, transform=tr, fill=0,
                                     dtype="uint8") if len(wa) else np.zeros(shape, np.uint8))
        print(f"   Basis-DLM: forest/woodland {forest.mean():.1%}, water {water.mean():.1%} of the grid")
    dom_min = _pool_tiles(dom, shape, l, t, res, "min")
    estimated = _estimate_terrain(dom_min, forest, bh, bground)
    if dgm:
        dg = _pool_tiles(dgm, shape, l, t, res, "mean")
        have = np.isfinite(dg)
        terrain = np.where(have, dg, estimated).astype(np.float32)
        share = have[np.isfinite(dom_min)].mean() if np.isfinite(dom_min).any() else have.mean()
        src = ("DGM1" if share > 0.999 else
               f"DGM1 on {share:.0%} of the surface model, elsewhere estimated from DOM1 + LoD ground heights")
    else:
        terrain = estimated
        src = "estimated from DOM1 + LoD ground heights (add DGM1 tiles to replace)"
    # surface must never be below terrain
    surface = np.where(np.isnan(surface), surface, np.fmax(surface, terrain)).astype(np.float32)
    tree = None
    if dop:
        green = _pool_green(dop, shape, l, t, res)
        with np.errstate(invalid="ignore"):
            tree = ((green >= 0.5) & (surface - terrain > 2.5) & (bh <= 0)).astype(np.uint8)
        have = np.isfinite(green) & np.isfinite(surface)
        print(f"   DOP20: {have.sum() / max(np.isfinite(surface).sum(), 1):.0%} of the surface model covered, "
              f"tree canopy {tree[have].mean():.1%} of it")
        src += f"; trees from DOP20 NDVI on {have.sum() / max(np.isfinite(surface).sum(), 1):.0%}"
    return Grid(l, t, res, surface, terrain, bh.astype(np.float32), forest, src, water, tree)
