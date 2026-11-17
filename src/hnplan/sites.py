"""Gateway sites (existing + candidates): ground height, mounting point and antenna height.

The exact mounting points are not known (HNVG). Rule: the gateway (Kerlink Wirnet iStation, wall/pole bracket,
external antenna) sits on a top-level surface – preferably a building roof:

1. roof      – a building (LoD1/LoD2) within `roof_search_radius_m`: top of the tallest building
               (terrain + LoD building height, so chimneys / roof antennas in the surface model don't count)
2. structure – no building nearby, but a non-tree structure (tower, mast, silo …) taller than
               `structure_min_m` within `site_search_radius_m`: its top from the surface model
3. free mast – nothing to mount on: a free-standing pole of `free_mast_m`
Trees (DOP20 canopy) are never used as a mounting point. The antenna sits `mast_m` above the roof/structure.
Sites outside the 3D data get the median height of the roof sites."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .rasters import Grid


def _disk(grid: Grid, radius: float) -> tuple[np.ndarray, np.ndarray]:
    k = int(np.ceil(radius / grid.res))
    offs = np.arange(-k, k + 1) * grid.res
    ox, oy = np.meshgrid(offs, offs)
    keep = (ox ** 2 + oy ** 2) <= radius ** 2
    return ox[keep], oy[keep]


def site_heights(sites: pd.DataFrame, grid: Grid, radio: dict) -> pd.DataFrame:
    s = sites.copy()
    mast = float(radio.get("mast_m", 2.0))
    r_roof = float(radio.get("roof_search_radius_m", 30))
    r_struct = float(radio.get("site_search_radius_m", 15))
    min_struct = float(radio.get("structure_min_m", 8))
    free_mast = float(radio.get("free_mast_m", 10))
    roof_d, struct_d = _disk(grid, r_roof), _disk(grid, r_struct)
    tree = grid.tree if grid.tree is not None else np.zeros_like(grid.bheight, dtype=np.uint8)

    grounds, tops, mounts, cov = [], [], [], []
    for x, y in zip(s.x, s.y):
        sx, sy = x + struct_d[0], y + struct_d[1]
        surf = grid.sample(grid.surface, sx, sy)
        terr = grid.sample(grid.terrain, sx, sy)
        cov.append(np.isfinite(surf).mean())
        ground = np.nanmin(terr) if np.isfinite(terr).any() else np.nan
        grounds.append(ground)
        # 1) building roof
        bx, by = x + roof_d[0], y + roof_d[1]
        bh = grid.sample(grid.bheight, bx, by, fill=0)
        if (bh > 0).any():
            roof = grid.sample(grid.terrain, bx, by) + bh
            roof = np.where(bh > 0, roof, np.nan)
            if np.isfinite(roof).any():
                tops.append(float(np.nanmax(roof)))
                mounts.append("roof")
                continue
        # 2) non-tree structure (tower, mast, silo)
        is_tree = grid.sample(tree, sx, sy, fill=0) > 0
        st = np.where(is_tree, np.nan, surf)
        if np.isfinite(st).any() and np.isfinite(ground) and np.nanmax(st) - ground > min_struct:
            tops.append(float(np.nanmax(st)))
            mounts.append("structure")
            continue
        # 3) free-standing mast
        tops.append(ground + free_mast - mast if np.isfinite(ground) else np.nan)
        mounts.append("free mast")

    s["ground_z"] = grounds
    s["struct_top_z"] = tops
    s["mount"] = mounts
    s["antenna_z"] = s["struct_top_z"] + mast
    s["h_agl"] = s["antenna_z"] - s["ground_z"]
    s["in_surface_model"] = np.array(cov) > 0.5
    # outside the surface model: assume a typical rooftop site (median of the roof sites)
    known = s.in_surface_model & (s["mount"] == "roof")
    med_h = float(np.nanmedian(s.loc[known, "h_agl"])) if known.any() else 20.0
    s.loc[~s.in_surface_model, "h_agl"] = med_h
    s.loc[~s.in_surface_model, "mount"] = "assumed roof (outside 3D data)"
    return s
