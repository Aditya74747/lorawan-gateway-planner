"""Where is the water meter inside a building?  (rules from TAB Wasser + Ergänzende Bedingungen)

* The house connection runs straight from the street main to the building by the shortest
  route, and the meter is mounted right behind the house entry  ->  the meter sits on the inside
  of the building wall that faces the nearest street.
* If the pipe on the property is longer than `shaft_pipe_length_m` (40 m), the utility can
  require a meter shaft at the property line  ->  buildings set back > 40 m from the street are
  flagged `shaft_likely` (meter underground at the street, under a lid).

For every point (meter GPS / address point) we return the moved meter position, the outward
direction of that wall (to tell whether the wall faces a gateway), the setback from the street and
the shaft flag.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely import STRtree


class MeterPlacer:
    def __init__(self, buildings: gpd.GeoDataFrame, landuse: gpd.GeoDataFrame | None, pcfg: dict, facts: dict):
        self.b = buildings.geometry.to_numpy()
        self.btree = STRtree(self.b)
        roads = landuse[landuse["nutzart"].isin(pcfg["road_classes"])] if landuse is not None else landuse
        self.r = roads.geometry.to_numpy() if roads is not None and len(roads) else None
        self.rtree = STRtree(self.r) if self.r is not None else None
        self.snap = float(pcfg["snap_radius_m"])
        self.inset = float(pcfg["inset_m"])
        self.shaft_len = float(facts.get("shaft_pipe_length_m") or 40)

    def place(self, x: np.ndarray, y: np.ndarray) -> pd.DataFrame:
        n = len(x)
        out = pd.DataFrame(dict(px=np.asarray(x, float), py=np.asarray(y, float), fdx=np.nan, fdy=np.nan,
                                setback_m=np.nan, shaft_likely=0.0, placed=False))
        ok = np.isfinite(out.px.to_numpy()) & np.isfinite(out.py.to_numpy())
        if self.rtree is None or not ok.any():
            return out
        pts = shapely.points(out.px.to_numpy()[ok], out.py.to_numpy()[ok])
        pi, bi = self.btree.query_nearest(pts, max_distance=self.snap, all_matches=False)
        rows = np.flatnonzero(ok)[pi]
        bgeom = self.b[bi]
        # nearest road for each matched building
        ridx = self.rtree.query_nearest(bgeom, all_matches=False)[1]
        rgeom = self.r[ridx]
        setback = shapely.distance(bgeom, rgeom)
        line = shapely.shortest_line(bgeom, rgeom)
        c = shapely.get_coordinates(line).reshape(-1, 2, 2)
        p0, p1 = c[:, 0, :], c[:, 1, :]
        d = p1 - p0
        L = np.hypot(d[:, 0], d[:, 1])
        # building touches the road: use centroid -> wall point as outward direction
        cen = shapely.get_coordinates(shapely.centroid(bgeom))
        d2 = p0 - cen
        L2 = np.hypot(d2[:, 0], d2[:, 1]).clip(1e-6)
        use2 = L < 0.5
        ux = np.where(use2, d2[:, 0] / L2, d[:, 0] / L.clip(1e-6))
        uy = np.where(use2, d2[:, 1] / L2, d[:, 1] / L.clip(1e-6))
        shaft = setback > self.shaft_len
        # normal case: inside the street-facing wall; shaft case: at the property line by the street
        out.loc[rows, "px"] = np.where(shaft, p1[:, 0] - 1.0 * ux, p0[:, 0] - self.inset * ux)
        out.loc[rows, "py"] = np.where(shaft, p1[:, 1] - 1.0 * uy, p0[:, 1] - self.inset * uy)
        out.loc[rows, "fdx"], out.loc[rows, "fdy"] = ux, uy
        out.loc[rows, "setback_m"] = setback
        out.loc[rows, "shaft_likely"] = shaft.astype(float)
        out.loc[rows, "placed"] = True
        return out
