"""Future city scenario from official spatial plans (XPlanung, Geoportal Raumordnung BW).

Inputs (any XPlanGML 5/6 WFS download dropped into data/raw, matched by `datasets.xplan`):
  FP  Flächennutzungsplan  – FP_BebauungsFlaeche = land the municipality designates for building
                             (residential / mixed / commercial / special), FP_Gemeinbedarf = public buildings
  RP  Regionalplan         – RP_WohnenSiedlung / RP_IndustrieGewerbe = regional growth areas (polygons only)
  BP  Bebauungspläne       – BP_BaugebietsTeilFlaeche (with max. storeys Z / height hMax) when a city publishes
                             full plans; the BW portal currently only carries plan outlines for most towns

What we derive
  1. Plan zones inside the planning grid (cached as data/processed/plan_zones.parquet).
  2. "Open building land": parts of a building zone that are still empty today
       – ALKIS actual use is farmland or waste ground (`greenfield_classes`); yards and car parks of existing
         sites are excluded this way (without it 2/3 of the "open" land were commercial yards)
       – no LoD building within `gap_m` metres, no forest or water
       – connected pieces of at least `min_area_m2`
  3. Future buildings on that land, as a regular block pattern with the zone's typical site coverage
     (GRZ) and a WORST-CASE height = 90th percentile of today's LoD buildings of the same use within
     1.5 km (plans set maximums; the FNP gives no heights, so we take what the neighbourhood already has).
  4. Future demand: every future building = one water connection (one meter), grouped into the same
     50 m planning cells as today's addresses.

The future grid (surface + building height) feeds the same feature builder and reception model as today,
so "today" and "future city" are directly comparable.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio import features as rfeatures
from scipy import ndimage
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union

from .rasters import Grid

# use classes -> label, typical site coverage (GRZ, BauNVO §17 upper values are 0.4 / 0.6 / 0.8),
# block pitch in metres for the synthetic footprints
USE = {
    "residential": dict(label="Residential (W)", grz=0.4, pitch=25.0),
    "mixed": dict(label="Mixed use (M)", grz=0.6, pitch=25.0),
    "commercial": dict(label="Commercial / industrial (G)", grz=0.8, pitch=40.0),
    "special": dict(label="Special / public (S)", grz=0.6, pitch=35.0),
}
# XPlanung code lists
FP_ALLG = {"1000": "residential", "2000": "mixed", "3000": "commercial", "4000": "special"}
KEEP = {
    "FP_BebauungsFlaeche": "fp", "FP_Gemeinbedarf": "fp",
    "RP_WohnenSiedlung": "rp", "RP_IndustrieGewerbe": "rp",
    "BP_BaugebietsTeilFlaeche": "bp",
}
# ALKIS current use (tatsächliche Nutzung) that counts as still-unbuilt land
GREENFIELD = ("Landwirtschaft", "Unland Vegetationslose Flaeche")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rings(el):
    """Polygons (lon, lat) from any gml:Polygon below `el` (posList is lat lon in EPSG:4258)."""
    polys = []
    for p in el.iter():
        if _local(p.tag) != "Polygon":
            continue
        ext, holes = None, []
        for part in p:
            kind = _local(part.tag)
            pl = next((q for q in part.iter() if _local(q.tag) == "posList"), None)
            if pl is None or not pl.text:
                continue
            v = np.asarray(pl.text.split(), float).reshape(-1, 2)[:, ::-1]
            if len(v) < 4:
                continue
            if kind == "exterior":
                ext = v
            elif kind == "interior":
                holes.append(v)
        if ext is not None:
            try:
                g = Polygon(ext, holes).buffer(0)
                if not g.is_empty:
                    polys.append(g)
            except Exception:  # noqa: BLE001 – broken ring in a source plan: skip it
                pass
    return polys


def parse_xplan(path_or_file, bbox_lonlat) -> pd.DataFrame:
    """Stream-parse one XPlanGML file; keep KEEP features whose polygon intersects bbox_lonlat."""
    bb = box(*bbox_lonlat)
    rows, plans, bereich_plan = [], {}, {}
    stack = []
    for ev, el in ET.iterparse(path_or_file, events=("start", "end")):
        if ev == "start":
            stack.append(_local(el.tag))
            continue
        stack.pop()
        # features are children of wfs:member / gml:featureMember (possibly inside a nested collection)
        if not stack or stack[-1] not in ("member", "featureMember"):
            continue
        name = _local(el.tag)
        if name in ("SimpleFeatureCollection", "FeatureCollection"):
            continue
        if name.endswith("_Plan"):
            gid = el.get("{http://www.opengis.net/gml/3.2}id") or ""
            nm = next((c.text for c in el if _local(c.tag) == "name"), None)
            gem = next((q.text for q in el.iter() if _local(q.tag) == "gemeindeName"), None)
            plans[gid] = dict(plan=nm, gemeinde=gem, date=next((c.text for c in el if _local(c.tag) in
                                                               ("wirksamkeitsDatum", "datumDesInkrafttretens")), None))
            for c in el:
                if _local(c.tag) == "bereich":
                    bereich_plan[(c.get("{http://www.w3.org/1999/xlink}href") or "").lstrip("#")] = gid
        elif name.endswith("_Bereich"):
            gid = el.get("{http://www.opengis.net/gml/3.2}id") or ""
            for c in el:
                if _local(c.tag) == "gehoertZuPlan":
                    bereich_plan.setdefault(gid, (c.get("{http://www.w3.org/1999/xlink}href") or "").lstrip("#"))
        elif name in KEEP:
            polys = _rings(el)
            polys = [p for p in polys if p.intersects(bb)]
            if polys:
                attrs = {}
                for q in el.iter():
                    t = (q.text or "").strip()
                    if t and len(q) == 0 and _local(q.tag) not in ("posList", "pos"):
                        attrs.setdefault(_local(q.tag), t)
                ber = next((c.get("{http://www.w3.org/1999/xlink}href") for c in el
                            if _local(c.tag) == "gehoertZuBereich"), "") or ""
                rows.append(dict(ftype=name, family=KEEP[name], bereich=ber.lstrip("#"),
                                 geometry=unary_union(polys), **{k: attrs.get(k) for k in (
                                     "allgArtDerBaulNutzung", "besondereArtDerBaulNutzung", "typ", "gebietsTyp",
                                     "rechtscharakter", "rechtsstand", "Z", "ZMax", "hMax", "GRZ", "GFZ", "text")}))
        el.clear()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["plan_id"] = df.bereich.map(bereich_plan)
    meta = pd.DataFrame.from_dict(plans, orient="index")
    if len(meta):
        df = df.join(meta, on="plan_id")
    return df


def _use_class(r) -> str | None:
    if r.ftype == "FP_BebauungsFlaeche" or r.ftype == "BP_BaugebietsTeilFlaeche":
        a = str(r.allgArtDerBaulNutzung or "")
        b = str(r.besondereArtDerBaulNutzung or "")
        if a in FP_ALLG:
            return FP_ALLG[a]
        if b[:2] in ("10", "11", "12", "13"):   # 1000–1300 Kleinsiedlung / reines / allg. / bes. Wohngebiet
            return "residential"
        if b[:2] in ("14", "15", "16"):         # Dorf-, Misch-, urbanes Gebiet
            return "mixed"
        if b[:2] in ("17", "18"):               # Gewerbe-, Industriegebiet
            return "commercial"
        return "special" if b else None
    if r.ftype == "FP_Gemeinbedarf":
        return "special"
    if r.ftype == "RP_WohnenSiedlung":
        return "residential"
    if r.ftype == "RP_IndustrieGewerbe":
        return "commercial"
    return None


def load_plan_zones(sources, grid_extent_utm, crs) -> gpd.GeoDataFrame | None:
    l, b, r, t = grid_extent_utm
    bb = gpd.GeoSeries([box(l, b, r, t)], crs=crs).to_crs("EPSG:4258").total_bounds
    frames = []
    seen = set()
    for s in sources:
        # browser re-downloads ("xplan_RP (1).gml", "xplan_RP (2).gml") of the same service: parse once
        key = (re.sub(r"\s*\(\d+\)", "", s.name), s.size)
        if key in seen:
            print(f"   {s.name}: duplicate download, skipped")
            continue
        seen.add(key)
        t0 = time.time()
        with s.open() as fh:
            df = parse_xplan(fh, bb)
        print(f"   {s.name}: {len(df)} plan zones in the planning area  [{time.time() - t0:.0f}s]")
        if len(df):
            df["source"] = s.name
            frames.append(df)
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    g = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4258").to_crs(crs)
    # the same plan downloaded twice ("xplan_RP (1).gml") -> drop exact duplicates
    g["wkb"] = g.geometry.apply(lambda x: x.wkb)
    g = g.drop_duplicates(["ftype", "wkb"]).drop(columns="wkb")
    g["use"] = [_use_class(r) for r in g.itertuples()]
    g = g[g["use"].notna()].reset_index(drop=True)
    g["area_m2"] = g.geometry.area
    return g


def build_future(grid: Grid, zones: gpd.GeoDataFrame, buildings: gpd.GeoDataFrame, landuse, dlm,
                 sites: pd.DataFrame, fcfg: dict):
    """Returns (future Grid, future buildings GeoDataFrame, open-land GeoDataFrame, summary dict)."""
    shape, tr, res = grid.shape, grid.transform, grid.res
    gap = float(fcfg.get("gap_m", 25))
    min_area = float(fcfg.get("min_area_m2", 3000))
    q = float(fcfg.get("height_quantile", 0.9))
    keep_out = float(fcfg.get("site_keep_out_m", 20))

    zone_id = rfeatures.rasterize(((g, i + 1) for i, g in enumerate(zones.geometry)), out_shape=shape,
                                  transform=tr, fill=0, dtype="int32")
    built = grid.bheight > 0
    near_bldg = ndimage.distance_transform_edt(~built) * res < gap
    blocked = near_bldg | (grid.forest > 0)
    if grid.water is not None:
        blocked |= grid.water > 0
    # Only land that is still unbuilt TODAY according to ALKIS (fields, waste ground). Without this check
    # 2/3 of the "open" land were yards and car parks of existing commercial sites (checked 2026-11-14).
    green_cls = set(fcfg.get("greenfield_classes", GREENFIELD))
    if landuse is not None and len(landuse):
        gf = landuse[landuse["nutzart"].isin(green_cls)]
        allowed = (rfeatures.rasterize(((g, 1) for g in gf.geometry), out_shape=shape, transform=tr,
                                       fill=0, dtype="uint8") > 0) if len(gf) else np.zeros(shape, bool)
        blocked |= ~allowed
    elif not fcfg.get("allow_without_landuse", False):
        blocked |= True
    # not around candidate/existing sites: a mast keeps its clearance
    sr, sc, ok = grid.rc(sites.x.to_numpy(), sites.y.to_numpy())
    near_site = np.zeros(shape, bool)
    near_site[sr[ok], sc[ok]] = True
    near_site = ndimage.distance_transform_edt(~near_site) * res < keep_out
    open_land = (zone_id > 0) & ~blocked & ~near_site & np.isfinite(grid.surface)
    lab, n = ndimage.label(open_land)
    sizes = ndimage.sum(open_land, lab, index=np.arange(1, n + 1)) * res * res
    big = np.zeros(n + 1, bool)
    big[1:] = sizes >= min_area
    open_land = big[lab]
    lab, n = ndimage.label(open_land)
    print(f"   open building land: {n} areas, {open_land.sum() * res * res / 1e4:.1f} ha "
          f"(of {(zone_id > 0).sum() * res * res / 1e4:.0f} ha designated building zones in the grid)")

    # ---- worst-case height per use class: P90 of today's buildings of that use within 1.5 km
    bc = buildings.geometry.centroid
    bz = np.asarray(zone_id[grid.rc(bc.x.to_numpy(), bc.y.to_numpy())[:2]])
    buse = np.where(bz > 0, zones["use"].to_numpy()[np.clip(bz - 1, 0, None)], None)
    bh = buildings.height.to_numpy(float)
    bx, by = bc.x.to_numpy(), bc.y.to_numpy()

    # ---- synthetic blocks
    rr, cc = np.indices(shape)
    xs = grid.x0 + (cc + 0.5) * res
    ys = grid.y0 - (rr + 0.5) * res
    surface, bheight = grid.surface.copy(), grid.bheight.copy()
    fb_rows, area_rows = [], []
    objs = ndimage.find_objects(lab)
    for k, sl in enumerate(objs, start=1):
        m = lab[sl] == k
        zi = np.bincount(zone_id[sl][m]).argmax() - 1
        z = zones.iloc[zi]
        u = USE[z["use"]]
        p = u["pitch"]
        a = p * np.sqrt(u["grz"])
        cx, cy = xs[sl][m].mean(), ys[sl][m].mean()
        same = (buse == z["use"]) & (np.hypot(bx - cx, by - cy) < 1500) & (bh > 2.5)
        pool = bh[same] if same.sum() >= 20 else bh[(buse == z["use"]) & (bh > 2.5)]
        if len(pool) < 20:
            pool = bh[bh > 2.5]
        H = float(np.quantile(pool, q))
        try:
            zmax = float(z.get("hMax")) if z.get("hMax") else None
        except (TypeError, ValueError):
            zmax = None
        if zmax:
            H = zmax
        # block pattern: footprint where (x mod p) < a and (y mod p) < a, keeping whole blocks only
        lx, ly = xs[sl], ys[sl]
        inb = m & ((lx % p) < a) & ((ly % p) < a)
        bid = (np.floor(lx / p).astype(np.int64) * 100003 + np.floor(ly / p).astype(np.int64))
        ids = np.unique(bid[inb])
        full = (a / res) ** 2 * 0.9
        cnt = pd.Series(bid[inb]).value_counts()
        ok_ids = set(cnt[cnt >= full].index)
        put = inb & np.isin(bid, list(ok_ids))
        terr = grid.terrain[sl]
        s_old = surface[sl]
        surface[sl] = np.where(put, np.fmax(np.nan_to_num(s_old, nan=-1e9), terr + H), s_old)
        bheight[sl] = np.where(put, np.fmax(bheight[sl], H), bheight[sl])
        nb_ = 0
        for b_ in ids:
            if b_ not in ok_ids:
                continue
            sel = put & (bid == b_)
            if not sel.any():
                continue
            x_, y_ = lx[sel], ly[sel]
            fb_rows.append(dict(area=k, use=z["use"], height=H, x=float(x_.mean()), y=float(y_.mean()),
                                geometry=box(x_.min() - res / 2, y_.min() - res / 2, x_.max() + res / 2, y_.max() + res / 2)))
            nb_ += 1
        area_rows.append(dict(area=k, use=z["use"], height=H, n_buildings=nb_, area_m2=float(m.sum() * res * res),
                              plan=z.get("plan"), gemeinde=z.get("gemeinde"), ftype=z["ftype"],
                              cx=float(cx), cy=float(cy)))
    fut_b = gpd.GeoDataFrame(fb_rows, geometry="geometry", crs=zones.crs) if fb_rows else None
    # open-land outlines for the map
    shapes = [(sh, int(v)) for sh, v in rfeatures.shapes(lab.astype(np.int32), mask=lab > 0, transform=tr)]
    polys = {}
    from shapely.geometry import shape as _shape
    for sh, v in shapes:
        polys.setdefault(v, []).append(_shape(sh))
    areas = pd.DataFrame(area_rows)
    if len(areas):
        areas = areas[areas.n_buildings > 0].reset_index(drop=True)  # tiny/irregular pieces fit no full block
        areas["geometry"] = [unary_union(polys.get(a, [])).simplify(res / 2) for a in areas.area]
        areas = gpd.GeoDataFrame(areas, geometry="geometry", crs=zones.crs)
        areas["geometry"] = [g if isinstance(g, (Polygon, MultiPolygon)) else g.buffer(0) for g in areas.geometry]
    fgrid = Grid(grid.x0, grid.y0, res, surface.astype(np.float32), grid.terrain, bheight.astype(np.float32),
                 grid.forest, grid.terrain_source + "; future city: plan zones (XPlanung)", grid.water, grid.tree)
    summary = dict(
        zones=int(len(zones)), zone_ha=float(zones.area_m2.sum() / 1e4),
        zones_by_use={k: int(v) for k, v in zones["use"].value_counts().items()},
        plans=sorted({f"{r.plan} ({r.gemeinde})" if r.gemeinde else str(r.plan)
                      for r in zones.drop_duplicates("plan_id").itertuples() if r.plan}),
        open_areas=int(len(areas)), open_ha=float(areas.area_m2.sum() / 1e4) if len(areas) else 0.0,
        future_buildings=int(len(fut_b)) if fut_b is not None else 0,
        heights={u: float(areas[areas["use"] == u].height.median()) for u in areas["use"].unique()} if len(areas) else {},
        rules=dict(gap_m=gap, min_area_m2=min_area, height_quantile=q, site_keep_out_m=keep_out,
                   grz={k: v["grz"] for k, v in USE.items()}),
    )
    return fgrid, fut_b, areas, summary


def future_cells(fut_b: gpd.GeoDataFrame, cell_size: float, start_id: int) -> pd.DataFrame:
    """One meter per future building, grouped into the planning cells."""
    f = pd.DataFrame(dict(x=fut_b.x, y=fut_b.y, area=fut_b.area, use=fut_b.use))
    f["cx"], f["cy"] = (f.x // cell_size).astype(int), (f.y // cell_size).astype(int)
    c = f.groupby(["cx", "cy"]).agg(n_addr=("x", "size"), mx=("x", "mean"), my=("y", "mean"),
                                    area=("area", "first"), use=("use", "first")).reset_index()
    f = f.merge(c[["cx", "cy", "mx", "my"]], on=["cx", "cy"])
    f["dd"] = np.hypot(f.x - f.mx, f.y - f.my)
    rep = f.sort_values("dd").drop_duplicates(["cx", "cy"])[["cx", "cy", "x", "y"]]
    c = c.merge(rep, on=["cx", "cy"]).reset_index(drop=True)
    c["example"] = [f"Planned {u} area #{a}" for u, a in zip(c.use, c.area)]
    c["shaft_share"] = 0.0
    c["setback_med"] = np.nan
    c["px"], c["py"] = c.x, c.y
    c["future"] = True
    c["cell_id"] = start_id + np.arange(len(c))
    return c


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s)
