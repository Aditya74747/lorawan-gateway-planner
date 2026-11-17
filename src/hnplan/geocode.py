"""Address -> coordinates.

Sources, in order of preference: LGL Hauskoordinaten (official address points, if supplied),
then ALKIS building outlines (each carries its street address). Exact match on normalised
"street housenumber"; fallback = centroid of all addresses on the same street (flagged)."""
from __future__ import annotations

import re

import geopandas as gpd
import numpy as np
import pandas as pd


def norm_addr(s: str) -> str:
    s = str(s).lower().replace("ß", "ss")
    s = re.sub(r"stra(ss|ß)e\b", "str", s)
    s = s.replace("str.", "str")
    return re.sub(r"[^a-z0-9äöü/]", "", s)


def norm_street(s: str) -> str:
    s = re.sub(r"\s*\d.*$", "", str(s))  # drop house number part
    return norm_addr(s)


class AddressIndex:
    def __init__(self, alkis_buildings: gpd.GeoDataFrame, house_coords: pd.DataFrame | None = None):
        rows = []
        if house_coords is not None:  # first = preferred by drop_duplicates below
            for a, x, y in zip(house_coords["address"], house_coords["x"], house_coords["y"]):
                rows.append((norm_addr(a), norm_street(a), x, y))
        b = alkis_buildings[alkis_buildings["lagebeztxt"].notna()]
        pts = b.geometry.representative_point()
        for txt, p in zip(b["lagebeztxt"], pts):
            for part in str(txt).split(","):
                part = part.strip()
                if part:
                    rows.append((norm_addr(part), norm_street(part), p.x, p.y))
        df = pd.DataFrame(rows, columns=["key", "street", "x", "y"])
        self.exact = df.drop_duplicates("key").set_index("key")[["x", "y"]]
        self.street = df.groupby("street")[["x", "y"]].mean()

    def lookup(self, addresses: pd.Series) -> pd.DataFrame:
        keys = addresses.map(norm_addr)
        out = self.exact.reindex(keys).reset_index(drop=True)
        out["quality"] = np.where(out.x.notna(), "exact", None)
        miss = out.x.isna().to_numpy()
        if miss.any():
            st = self.street.reindex(addresses[miss].map(norm_street)).reset_index(drop=True)
            out.loc[miss, ["x", "y"]] = st[["x", "y"]].to_numpy()
            out.loc[miss & out.x.notna().to_numpy(), "quality"] = "street"
        out.index = addresses.index
        return out
