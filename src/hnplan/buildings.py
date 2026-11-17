"""3D buildings (LGL LoD1/LoD2 CityGML) and ALKIS building outlines with addresses."""
from __future__ import annotations

import numpy as np
import pandas as pd
import geopandas as gpd
import pyogrio
from lxml import etree
from shapely.geometry import Polygon

from .discover import Source

NS = {"bldg": "http://www.opengis.net/citygml/building/1.0",
      "gml": "http://www.opengis.net/gml"}
BTAG = "{%s}Building" % NS["bldg"]


def _parse_gml(src: Source) -> list[dict]:
    rows = []
    with src.open() as fh:
        for _, el in etree.iterparse(fh, tag=BTAG, huge_tree=True):
            parent = el.getparent()
            if parent is not None and parent.tag.endswith("consistsOfBuildingPart"):
                continue
            h = el.findtext("bldg:measuredHeight", namespaces=NS)
            ground, zs = None, []
            for pl in el.iterfind(".//gml:posList", namespaces=NS):
                a = np.asarray(pl.text.split(), float).reshape(-1, 3)
                zs.append(a[:, 2])
                if len(a) >= 4 and np.ptp(a[:, 2]) < 0.01 and (ground is None or a[0, 2] < ground[0, 2]):
                    ground = a
            if ground is not None:
                z = np.concatenate(zs)
                poly = Polygon(ground[:, :2]).buffer(0)
                if not poly.is_empty:
                    rows.append(dict(bid=el.get("{%s}id" % NS["gml"]),
                                     height=float(h) if h else float(z.max() - z.min()),
                                     ground_z=float(z.min()), roof_z=float(z.max()), geometry=poly))
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
    return rows


def build_3d_buildings(sources: list[Source], crs: str) -> gpd.GeoDataFrame:
    rows: list[dict] = []
    for i, s in enumerate(sorted(sources, key=lambda s: s.name)):
        rows += _parse_gml(s)
        if (i + 1) % 20 == 0:
            print(f"   parsed {i + 1}/{len(sources)} CityGML tiles")
    g = gpd.GeoDataFrame(rows, crs=crs).drop_duplicates("bid")
    g["height"] = g["height"].clip(0, 300)
    return g.reset_index(drop=True)


def load_dlm(sources: list[Source], bbox: tuple[float, float, float, float], crs: str) -> gpd.GeoDataFrame | None:
    """Basis-DLM layers (state-wide shapefiles) clipped to the planning area via a bbox filter."""
    parts = [pyogrio.read_dataframe(s.gdal_path, columns=["OBJART", "OBJART_TXT"], bbox=bbox) for s in sources]
    parts = [p for p in parts if len(p)]
    if not parts:
        return None
    g = pd.concat(parts, ignore_index=True)
    return gpd.GeoDataFrame(g, geometry="geometry", crs=parts[0].crs).to_crs(crs).reset_index(drop=True)


def load_alkis(sources: list[Source], columns: list[str], crs: str) -> gpd.GeoDataFrame:
    parts = [pyogrio.read_dataframe(s.gdal_path, columns=columns) for s in sources]
    g = pd.concat(parts, ignore_index=True)
    g = gpd.GeoDataFrame(g, geometry="geometry", crs=parts[0].crs).to_crs(crs)
    if "oid" in g:
        g = g.drop_duplicates("oid")
    return g.reset_index(drop=True)
