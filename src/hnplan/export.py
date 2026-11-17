"""Export compact data files for the browser app (web/data/*.json)."""
from __future__ import annotations

import base64
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from .analysis import insights
from .config import Config
from .discover import discover
from .optimize import Planner
from .pipeline import device_stats
from .rasters import Grid


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=np.uint8).tobytes()).decode()


def _dump(path, obj):
    path.write_text(json.dumps(obj, separators=(",", ":"), default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def _export_aerial(cfg: Config, grid: Grid, out, res: float = 2.5, chunk: int = 2000) -> list[dict]:
    """DOP20 true-colour mosaic at `res` m as JPEG tiles (each ≤ chunk px) with exact lon/lat corners."""
    from PIL import Image
    from pyproj import Transformer
    from rasterio.enums import Resampling

    dop = discover(cfg, verbose=False).get("dop20") or []
    if not dop:
        return []
    H, W = grid.shape
    tx = [s.tile() for s in dop]
    l = max(grid.x0, min(t[0] for t in tx) * 1000)
    r = min(grid.x0 + W * grid.res, (max(t[0] for t in tx) + 1) * 1000)
    b = max(grid.y0 - H * grid.res, min(t[1] for t in tx) * 1000)
    t = min(grid.y0, (max(t[1] for t in tx) + 1) * 1000)
    if r <= l or t <= b:
        return []
    n, m = int(round((r - l) / res)), int(round((t - b) / res))
    img = np.zeros((m, n, 3), np.uint8)
    for s in dop:
        with rasterio.open(s.gdal_path) as src:
            bx = src.bounds
            if bx.right <= l or bx.left >= r or bx.top <= b or bx.bottom >= t:
                continue
            pw, ph = int(round((bx.right - bx.left) / res)), int(round((bx.top - bx.bottom) / res))
            a = src.read([1, 2, 3], out_shape=(3, ph, pw), resampling=Resampling.average)
        row, col = int(round((t - bx.top) / res)), int(round((bx.left - l) / res))
        r0, c0, r1, c1 = max(row, 0), max(col, 0), min(row + ph, m), min(col + pw, n)
        img[r0:r1, c0:c1] = np.moveaxis(a, 0, -1)[r0 - row:r1 - row, c0 - col:c1 - col]
    tr = Transformer.from_crs(cfg.raw["crs"], "EPSG:4326", always_xy=True)
    tiles = []
    for i0 in range(0, m, chunk):
        for j0 in range(0, n, chunk):
            sub = img[i0:i0 + chunk, j0:j0 + chunk]
            if not sub.any():
                continue
            name = f"aerial_{i0 // chunk}_{j0 // chunk}.jpg"
            Image.fromarray(sub).save(out / name, quality=80, optimize=True)
            x_l, x_r = l + j0 * res, l + (j0 + sub.shape[1]) * res
            y_t, y_b = t - i0 * res, t - (i0 + sub.shape[0]) * res
            lon, lat = tr.transform([x_l, x_l, x_r, x_r], [y_b, y_t, y_t, y_b])
            tiles.append(dict(url=f"data/{name}", bounds=[[round(x, 7), round(y, 7)] for x, y in zip(lon, lat)]))
    print(f"[export] aerial photo: {len(tiles)} tile(s), {n}x{m} px @ {res} m")
    return tiles


def _evidence(cfg: Config, pl: Planner, sites: pd.DataFrame, cells: pd.DataFrame, tx: pd.DataFrame,
              link: pd.DataFrame, metrics: dict, ins: dict, n_buildings: int, n_pred_links: int) -> dict:
    """All numbers behind the app as plain tables (web/data/evidence.json -> 'Data & evidence' page)."""
    P, w = pl.P, pl.w
    base_miss = np.prod(1 - P[:, pl.existing], axis=1)
    alone = (w[:, None] * P).sum(0)                     # expected addresses heard by this site alone
    reach50 = (w[:, None] * (P >= 0.5)).sum(0)          # addresses in cells with P >= 50 %
    within = (w[:, None] * pl.NEAR).sum(0)              # addresses within the 300 m rule
    gain = (w * base_miss) @ P                          # expected NEW addresses on top of today's gateways
    targets = (0.9, 0.95, 0.98)
    plans = {t: pl.min_sites(t) for t in targets}
    rank = {t: {sid: i + 1 for i, sid in enumerate(r["sites"])} for t, r in plans.items()}
    fail = {f["site_id"]: f["coverage_loss"] for f in pl.failure_analysis()}
    rec = tx.groupby("gateway_id").agg(receptions=("id", "size"), meters=("device_id", "nunique"),
                                       rssi_med=("rssi", "median"), snr_med=("snr", "median"))
    nf = lambda v, d=1: None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), d)  # noqa: E731
    site_rows = []
    for i, s in sites.iterrows():
        r = rec.loc[s.site_id] if s.site_id in rec.index else None
        site_rows.append(dict(
            id=s.site_id, name=s["name"], kind=s.kind, mount=s.get("mount"), ground_m=nf(s.ground_z),
            antenna_m=nf(s.h_agl), confidence=s.confidence, in_3d=bool(s.in_surface_model),
            addr_alone=nf(alone[i], 0), addr_reach50=nf(reach50[i], 0), addr_300m=nf(within[i], 0),
            addr_new=None if s.kind == "existing" else nf(gain[i], 0),
            **{f"rank_{int(t * 100)}": rank[t].get(s.site_id) for t in targets},
            fail_loss_pt=nf(100 * fail[s.site_id], 2) if s.site_id in fail else None,
            receptions=None if r is None else int(r.receptions), meters_heard=None if r is None else int(r.meters),
            rssi_med=None if r is None else nf(r.rssi_med), snr_med=None if r is None else nf(r.snr_med)))

    plan_rows = []
    weak = Planner(cfg, device="weak") if "p_weak" in pd.read_parquet(cfg.processed("predictions.parquet")).columns else None
    for t in (0.8, 0.9, 0.95, 0.97, 0.98, 0.99):
        a = pl.min_sites(t)
        b = weak.min_sites(t) if weak is not None else None
        plan_rows.append(dict(target=t, k_mix=a["k"] if a["reached"] else None, coverage_mix=a["coverage"],
                              greedy_k=a["greedy_k"], swaps=a["swaps"],
                              k_weak=(b["k"] if b and b["reached"] else None), coverage_weak=b["coverage"] if b else None,
                              sites=a["sites"] if a["reached"] else [],
                              # coverage after each site of the build order, starting from today -> gain per site
                              steps=([a["baseline"]] + list(a["steps"])) if a["reached"] else []))
    curve = pl.min_sites(1.0)

    # measured reception by distance (training links = every eligible meter x every existing gateway)
    L = pd.read_parquet(cfg.processed("train_links.parquet"))
    bands = [0, 250, 500, 1000, 2000, 3000, 5000, 8000]
    L["band"] = pd.cut(L.d_m, bands)
    dist_rows = [dict(band=f"{int(k.left)}–{int(k.right)} m", links=int(len(g)), heard=float(g.heard.mean()),
                      rssi_med=nf(g.loc[g.heard == 1, "rssi_med"].median()), snr_med=nf(g.loc[g.heard == 1, "snr_med"].median()))
                 for k, g in L.groupby("band", observed=True)]
    cv = metrics.get("cv", {})
    model_rows = [dict(model=k.split("/")[1], scheme=k.split("/")[0], auc=v.get("auc"), logloss=v.get("logloss"),
                       brier=v.get("brier"), mean_pred=v.get("mean_pred"), mean_true=v.get("mean_true"))
                  for k, v in cv.items()]
    bench = json.loads(cfg.processed("benchmark.json").read_text()).get("results", []) if cfg.processed("benchmark.json").exists() else []
    msgs = tx.drop_duplicates("id")
    return dict(
        generated=pd.Timestamp.now().isoformat(timespec="minutes"),
        overview=dict(
            addresses=int(cells.n_addr.sum()), cells=int(len(cells)), buildings=n_buildings,
            meters=int(ins["meters_total"]), meters_heard=int(ins["meters_heard"]),
            receptions=int(len(tx)), messages=int(len(msgs)), week=ins.get("week"),
            existing=len(pl.existing), candidates=len(pl.candidates),
            train_links=int(len(L)), pred_links=int(n_pred_links),
            coverage_today=float(pl.summary(pl.existing)["coverage"]),
            within300_today=float(pl.within_radius(pl.existing)),
            coverage_all=float(curve["maximum"]), model=metrics.get("selected"),
            logo_auc=cv.get(f"logo/{metrics.get('selected')}", {}).get("auc"),
            logo_logloss=cv.get(f"logo/{metrics.get('selected')}", {}).get("logloss"),
            rssi_mae=metrics.get("rssi", {}).get("mae_model"), rssi_mae_textbook=metrics.get("rssi", {}).get("mae_logdistance")),
        sites=site_rows, plans=plan_rows, curve=[curve["baseline"]] + curve["curve"],
        reception_by_distance=dist_rows, models=model_rows, benchmark=bench,
        importance=metrics.get("importance", []), by_maker=metrics.get("by_maker", {}), device_mix=metrics.get("device_mix", {}),
        devices=ins.get("devices"), weather=ins.get("weather"),
        meter_status=ins.get("meter_status"), sf_mix=ins.get("sf_mix"), gateway_load=ins.get("gateway_load"),
        delivery_ratio=ins.get("delivery_ratio"), radio_observed=ins.get("radio_observed"), hardware=ins.get("hardware"),
        link_budget=ins.get("link_budget"), excess_loss_db=ins.get("excess_loss_db_under_500m"),
        hnvg_docs=ins.get("hnvg_docs"), business_case=ins.get("business_case"),
        sources=dict(manifest_counts=ins.get("data", {}).get("manifest_counts"), terrain=ins.get("data", {}).get("terrain_source")),
    )


def _future_plans(pl: Planner, pf: Planner, targets=(0.9, 0.95, 0.98)) -> list[dict]:
    """Minimum sites for today vs the future city, and how today's plan holds up in the future city."""
    cov = lambda p, act: float((p.w * (1 - p.miss(act))).sum() / p.W)  # noqa: E731
    rows = []
    for t in targets:
        a, b = pl.min_sites(t), pf.min_sites(t)
        ia = pl.existing + pl.site_index(a["sites"])
        ib = pf.existing + pf.site_index(b["sites"])
        rows.append(dict(target=t, k_today=a["k"], k_future=b["k"], reached_today=a["reached"], reached_future=b["reached"],
                         today_plan_today=cov(pl, ia), today_plan_future=cov(pf, ia),
                         future_plan_today=cov(pl, ib), future_plan_future=cov(pf, ib),
                         extra_sites=sorted(set(b["sites"]) - set(a["sites"])),
                         dropped_sites=sorted(set(a["sites"]) - set(b["sites"]))))
    return rows


def _export_future(cfg: Config, out, pl: Planner, nS: int, u16) -> None:
    """future.json: planned buildings + open building land + reception matrices of the future city."""
    P = cfg.processed
    if not (P("predictions_future.parquet").exists() and P("future_cells.parquet").exists()):
        (out / "future.json").unlink(missing_ok=True)
        return
    from .pipeline import future_cell_table
    cells = future_cell_table(cfg)
    nC = len(cells)
    fr = pd.read_parquet(P("predictions_future.parquet"))
    ci, si = fr.cell_idx.to_numpy(), fr.site_idx.to_numpy()
    mats = {}
    for col, std in (("p", "p_std"), ("p_weak", "p_weak_std")):
        pm, sm = np.zeros((nC, nS), np.uint16), np.zeros((nC, nS), np.uint8)
        pm[ci, si] = np.round(fr[col].to_numpy() * 65535)
        sm[ci, si] = np.round(np.clip(fr[std].to_numpy() * 510, 0, 255))
        mats[col] = (u16(pm), _b64(sm))
    rm = np.zeros((nC, nS), np.uint8)
    rm[ci, si] = np.clip(np.round(fr.rssi.to_numpy() + 160), 1, 255)

    fb = gpd.read_parquet(P("future_buildings.parquet"))
    areas = gpd.read_parquet(P("future_areas.parquet"))
    fc = pd.read_parquet(P("future_cells.parquet"))
    cs = cfg.raw["grid"]["cell_size_m"]
    key = {(int(a), int(c)): int(i) for a, c, i in zip(fc.cx, fc.cy, fc.cell_id)}
    fb["cell"] = [key.get((int(x // cs), int(y // cs)), -1) for x, y in zip(fb.x, fb.y)]
    fb = fb.to_crs("EPSG:4326")
    lon0, lat0 = 9.0, 49.0
    off, xy = [0], []
    for g in fb.geometry:
        c = np.asarray(g.exterior.coords)[:-1]
        xy.extend(np.round((c - [lon0, lat0]) * 1e6).astype(int).ravel().tolist())
        off.append(len(xy) // 2)
    areas = areas[areas.n_buildings > 0].to_crs("EPSG:4326")
    rings = []
    for g in areas.geometry:
        polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        rings.append([np.round(np.asarray(p.exterior.coords), 6).tolist() for p in polys])
    summary = json.loads(P("future_summary.json").read_text())
    pf = Planner(cfg, scenario="future")
    plans = _future_plans(pl, pf)
    summary["plans_compare"] = plans
    for t in plans:
        print(f"[export] future city, target {t['target']:.0%}: today {t['k_today']} sites, future {t['k_future']} "
              f"(today's plan in the future city: {t['today_plan_future']:.1%})")
    _dump(out / "future.json", dict(
        summary=summary, n_cells=nC, n_today=int((~cells.future).sum()),
        lon=fc.lon.round(6).tolist(), lat=fc.lat.round(6).tolist(), n=fc.n_addr.tolist(), ex=fc.example.tolist(),
        p16=mats["p"][0], std=mats["p"][1], p_weak16=mats["p_weak"][0], std_weak=mats["p_weak"][1], rssi=_b64(rm),
        buildings=dict(lon0=lon0, lat0=lat0, scale=1e-6, h=fb.height.round(1).tolist(), cell=fb.cell.tolist(),
                       area=fb["area"].tolist(), use=fb["use"].tolist(), off=off, xy=xy),
        areas=[dict(id=int(r.area), use=r.use, h=round(float(r.height), 1), n=int(r.n_buildings),
                    ha=round(r.area_m2 / 1e4, 1), plan=r.plan if isinstance(r.plan, str) else None,
                    kind=("Regional plan growth area" if str(r.ftype).startswith("RP_") else "Land-use plan building area"),
                    rings=rg) for r, rg in zip(areas.itertuples(), rings)]))
    # compact copy for the "Data & evidence" page
    ev_path = out / "evidence.json"
    if ev_path.exists():
        ev = json.loads(ev_path.read_text())
        ev["future"] = dict(summary={k: v for k, v in summary.items() if k != "plans_compare"}, plans=plans,
                            areas=[dict(id=int(r.area), kind=("Regional plan" if str(r.ftype).startswith("RP_") else "Land-use plan"),
                                        plan=r.plan if isinstance(r.plan, str) else None, use=r.use, ha=round(r.area_m2 / 1e4, 2),
                                        height_m=round(float(r.height), 1), buildings=int(r.n_buildings),
                                        lon=round(float(c.x), 5), lat=round(float(c.y), 5))
                                   for r, c in zip(areas.itertuples(), areas.geometry.representative_point())])
        _dump(ev_path, ev)


def _landmarks(cfg: Config, b: gpd.GeoDataFrame) -> list[dict]:
    """config.yaml -> landmarks: buildings highlighted briefly when the app opens (e.g. Hochschule Heilbronn).
    The site = the ALKIS land-use parcel that contains `point` (all buildings on it); without land use a radius."""
    out = []
    lms = cfg.raw.get("landmarks") or []
    if not lms:
        return out
    from shapely.geometry import Point
    P = cfg.processed
    lu = gpd.read_parquet(P("landuse.parquet")) if P("landuse.parquet").exists() else None
    rp = b.geometry.representative_point()
    for lm in lms:
        lon, lat = lm["point"]
        pt = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(b.crs).iloc[0]
        area = None
        if lu is not None and len(lu):
            hit = lu[lu.contains(pt)]
            if len(hit) and hit.geometry.area.min() < float(lm.get("max_parcel_m2", 300_000)):
                area = hit.loc[hit.geometry.area.idxmin()].geometry
        if area is None:
            area = pt.buffer(float(lm.get("radius_m", 120)))
        idx = np.flatnonzero(rp.within(area).to_numpy()).tolist()
        if not idx:
            continue
        c = gpd.GeoSeries([area.centroid], crs=b.crs).to_crs("EPSG:4326").iloc[0]
        out.append(dict(name=lm["name"], sub=lm.get("sub", ""), color=lm.get("color", "#4c8dff"), seconds=float(lm.get("seconds", 5)),
                        lon=round(c.x, 6), lat=round(c.y, 6), idx=idx))
        print(f"[export] landmark {lm['name']}: {len(idx)} buildings")
    return out


def _write_core(cfg: Config, out, cells: pd.DataFrame, sites: pd.DataFrame, pr: pd.DataFrame, meas: dict | None = None):
    """cells.json (reception matrices), sites.json, buildings.json – shared by Heilbronn and other cities.
    Returns (u16 encoder, devices, filtered buildings)."""
    P = cfg.processed
    nC, nS = len(cells), len(sites)
    # ---- reception matrices (cells x sites). P is stored with 16 bits (1/65535 steps): with 8 bits the browser's
    # minimum-site search differed from Python's by one site exactly at a target boundary.
    u16 = lambda a: base64.b64encode(np.ascontiguousarray(a, dtype="<u2").tobytes()).decode()  # noqa: E731
    pm = np.zeros((nC, nS), np.uint16)
    sm = np.zeros((nC, nS), np.uint8)
    rm = np.zeros((nC, nS), np.uint8)
    ci, si = pr.cell_idx.to_numpy(), pr.site_idx.to_numpy()
    pm[ci, si] = np.round(pr.p.to_numpy() * 65535)
    sm[ci, si] = np.round(np.clip(pr.p_std.to_numpy() * 510, 0, 255))
    rm[ci, si] = np.clip(np.round(pr.rssi.to_numpy() + 160), 1, 255)
    # conservative scenario: every meter behaves like the weakest manufacturer type
    weak = {}
    if "p_weak" in pr:
        pw, sw = np.zeros((nC, nS), np.uint16), np.zeros((nC, nS), np.uint8)
        pw[ci, si] = np.round(pr.p_weak.to_numpy() * 65535)
        sw[ci, si] = np.round(np.clip(pr.p_weak_std.to_numpy() * 510, 0, 255))
        weak = dict(p_weak16=u16(pw), std_weak=_b64(sw))
    devices = json.loads(P("devices.json").read_text()) if P("devices.json").exists() else {}

    # ---- per-site confidence (geometry coverage + extrapolation)
    sc = pr.groupby("site_idx").agg(valid=("valid_frac", "mean"), extrap=("extrap", "median"))
    sites["valid"] = sc.valid.reindex(range(nS)).fillna(0).to_numpy()
    sites["extrap"] = sc.extrap.reindex(range(nS)).fillna(9).to_numpy()
    sites["confidence"] = np.select([(~sites.in_surface_model) | (sites.valid < 0.6),
                                     (sites.valid < 0.9) | (sites.extrap > 0.25)], ["low", "medium"], "high")

    sites["meas_devices"] = sites.site_id.map(meas or {}).fillna(0).astype(int)
    _dump(out / "sites.json", [dict(i=i, id=r.site_id, name=r["name"], kind=r.kind, lon=round(r.lon, 6), lat=round(r.lat, 6),
                                    ground=round(float(r.ground_z), 1) if np.isfinite(r.ground_z) else None,
                                    h=round(float(r.h_agl), 1), conf=r.confidence, devices=int(r.meas_devices),
                                    mount=r.get("mount", None))
                               for i, r in sites.iterrows()])

    _dump(out / "cells.json", dict(
        n_cells=nC, n_sites=nS, cell_size=cfg.raw["grid"]["cell_size_m"],
        device_radius_m=cfg.raw["radio"].get("device_radius_m", 300), devices=devices, **weak,
        lon=cells.lon.round(6).tolist(), lat=cells.lat.round(6).tolist(),
        n=cells.n_addr.tolist(), ex=cells.example.tolist(),
        shaft=cells.get("shaft_share", pd.Series(0.0, index=cells.index)).fillna(0).round(2).tolist(),
        setb=cells.get("setback_med", pd.Series(np.nan, index=cells.index)).round(0).fillna(-1).astype(int).tolist(),
        p16=u16(pm), std=_b64(sm), rssi=_b64(rm)))

    # ---- 3D buildings (compact): heights, ground, owning cell, footprint rings
    b = gpd.read_parquet(P("buildings.parquet"))
    b = b[(b.geometry.area > 12) | (b.height > 3)].copy()
    cs = cfg.raw["grid"]["cell_size_m"]
    key = {(int(a), int(c)): i for i, (a, c) in enumerate(zip(cells.cx, cells.cy))}
    cen = b.geometry.centroid
    b["cell"] = [key.get((int(x // cs), int(y // cs)), -1) for x, y in zip(cen.x, cen.y)]
    marks = _landmarks(cfg, b)
    b["geometry"] = b.geometry.simplify(0.5)
    b = b.to_crs("EPSG:4326")
    lon0, lat0 = 9.0, 49.0
    off, xy = [0], []
    for geom in b.geometry:
        poly = max(geom.geoms, key=lambda g: g.area) if geom.geom_type == "MultiPolygon" else geom
        c = np.asarray(poly.exterior.coords)[:-1]
        xy.extend(np.round((c - [lon0, lat0]) * 1e6).astype(int).ravel().tolist())
        off.append(len(xy) // 2)
    _dump(out / "buildings.json", dict(lon0=lon0, lat0=lat0, scale=1e-6, h=b.height.round(1).tolist(),
                                       cell=b.cell.tolist(), off=off, xy=xy, landmarks=marks))

    return u16, devices, b


def export(cfg: Config) -> None:
    P, out = cfg.processed, cfg.web_data_dir
    cells = pd.read_parquet(P("cells.parquet"))
    sites = pd.read_parquet(P("sites.parquet"))
    pr = pd.read_parquet(P("predictions.parquet"))
    metrics = json.loads(P("metrics.json").read_text())
    nS = len(sites)

    tx = pd.read_parquet(P("transmissions.parquet"))
    dev, link = device_stats(tx)
    gstat = link.groupby("gateway_id").agg(devices=("device_id", "nunique"), rssi_med=("rssi_med", "median"))
    u16, devices, b = _write_core(cfg, out, cells, sites, pr, gstat.devices.to_dict())
    weak = {"p_weak16": True} if "p_weak" in pr else {}

    # ---- measured meters
    m = pd.read_parquet(P("meters.parquet"))
    best = link.sort_values("rssi_max", ascending=False).drop_duplicates("device_id")
    m = m.merge(dev[["device_id", "pdr", "n_msgs"]], on="device_id", how="left")
    m = m.merge(best[["device_id", "gateway_id", "rssi_max"]], on="device_id", how="left")
    m["n_gw"] = m.device_id.map(link.groupby("device_id").gateway_id.nunique()).fillna(0).astype(int)
    gname = dict(zip(sites.site_id, sites.name))
    _dump(out / "meters.json", [dict(lon=round(r.lon, 6), lat=round(r.lat, 6), st=r.meter_state, n_gw=r.n_gw,
                                     rssi=None if pd.isna(r.rssi_max) else float(r.rssi_max),
                                     gw=gname.get(r.gateway_id, None if pd.isna(r.gateway_id) else "unknown"),
                                     pdr=None if pd.isna(r.pdr) else round(float(r.pdr), 2),
                                     src=r.coord_source, adr=r.address, mk=str(r.zaehlernummer_id)[1:4],
                                     yr=r.eichjahr, sz=r.zaehlergroesse)
                                for r in m[m.lon.notna()].itertuples()])

    # ---- insights + model + python optimiser reference
    pl = Planner(cfg)
    ins = insights(cfg)
    ins["model"] = metrics
    ins["devices"] = devices
    ins["plan"] = dict(current=pl.summary(pl.existing), greedy_coverage=pl.greedy(5, "coverage"),
                       greedy_redundancy=pl.greedy(5, "redundancy"), greedy_radius=pl.greedy(5, "radius"),
                       device_radius_m=pl.radius, failure=pl.failure_analysis(),
                       min_sites=[{k: v for k, v in pl.min_sites(t).items() if k not in ("curve", "steps")}
                                  for t in (0.8, 0.9, 0.95, 0.98, 0.99)],
                       coverage_curve=pl.min_sites(1.0)["curve"],
                       min_sites_weak=([{k: v for k, v in Planner(cfg, device="weak").min_sites(t).items()
                                         if k not in ("curve", "steps")} for t in (0.8, 0.9, 0.95, 0.98)]
                                       if weak else None),
                       addresses=int(cells.n_addr.sum()))
    gr = json.loads(P("grid_meta.json").read_text()) if P("grid_meta.json").exists() else {}
    ins["data"] = dict(manifest_counts={k: len(v) for k, v in json.loads(P("manifest.json").read_text())["selected"].items()},
                       **gr)
    if P("domain_facts.json").exists():
        facts = json.loads(P("domain_facts.json").read_text())
        ins["hnvg_docs"] = facts
        val = {k: v["value"] for k, v in facts.items()}
        c1, c2 = float(val.get("visit_cost_eur_net") or 92), float(val.get("visit_cost_shaft_eur_net") or 178)
        sh = cells.get("shaft_share", pd.Series(0.0, index=cells.index)).fillna(0).to_numpy()
        w = cells.n_addr.to_numpy(float)
        def case(active):
            cov = 1 - pl.miss(active)
            return dict(readable=float((w * cov).sum()), eur=float((w * cov * (c1 * (1 - sh) + c2 * sh)).sum()))
        steps = ins["plan"]["greedy_coverage"]["steps"]
        best = pl.existing + pl.site_index([s_["site_id"] for s_ in steps[:3]])
        ins["business_case"] = dict(visit_cost=c1, visit_cost_shaft=c2, shaft_addresses=float((w * sh).sum()),
                                    today=case(pl.existing), plus3=case(best),
                                    shaft_p_factor=cfg.raw.get("meter_placement", {}).get("shaft_p_factor", 1.0))
    if P("ablation_placement.json").exists():
        ins["ablation_placement"] = json.loads(P("ablation_placement.json").read_text())
    if P("weather_insights.json").exists():
        ins["weather"] = json.loads(P("weather_insights.json").read_text())
    _dump(out / "insights.json", ins)
    _dump(out / "evidence.json", _evidence(cfg, pl, sites, cells, tx, link, metrics, ins, n_buildings=len(b),
                                           n_pred_links=len(pr)))

    _export_future(cfg, out, pl, nS, u16)

    # ---- optional aerial imagery layer (DOP20 true colour). The DOP20 tree canopy stays in the model
    # rasters (grid.tree) but is not drawn in the app – it cluttered the map.
    if P("grid.npz").exists():
        grid = Grid.load(P("grid.npz"))
        for old in out.glob("aerial_*.jpg"):
            old.unlink()
        _dump(out / "vegetation.json", dict(aerial=_export_aerial(cfg, grid, out)))
    sizes = {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in sorted([*out.glob("*.json"), *out.glob("*.jpg")])}
    print(f"[export] {sizes}")


def export_city(cfg: Config) -> None:
    """Web data for another city (cities/<name>/web_data): same files as Heilbronn, without measured meters."""
    from .city import heilbronn_model_path
    P, out = cfg.processed, cfg.web_data_dir
    cells = pd.read_parquet(P("cells.parquet"))
    sites = pd.read_parquet(P("sites.parquet"))
    pr = pd.read_parquet(P("predictions.parquet"))
    _, devices, b = _write_core(cfg, out, cells, sites, pr, {})
    _dump(out / "meters.json", [])
    city = cfg.raw["city"]
    hm = heilbronn_model_path().parent
    metrics = json.loads((hm / "metrics_transfer.json").read_text()) if (hm / "metrics_transfer.json").exists() else {}
    full = json.loads((hm / "metrics.json").read_text()) if (hm / "metrics.json").exists() else {}
    pl = Planner(cfg)
    plans = {t: pl.min_sites(t) for t in (0.8, 0.9, 0.95, 0.98)}
    weak = Planner(cfg, device="weak") if "p_weak" in pr else None
    gm = json.loads(P("grid_meta.json").read_text())
    cand = sites[sites.kind == "candidate"]
    cv = metrics.get("cv", {})
    sel = metrics.get("selected")
    lon, lat = float(np.average(cells.lon, weights=cells.n_addr)), float(np.average(cells.lat, weights=cells.n_addr))
    ins = dict(
        city=dict(name=city["name"], slug=city["slug"], home=dict(center=[round(lon, 5), round(lat, 5)], zoom=12.9),
                  addresses=int(cells.n_addr.sum()), cells=int(len(cells)), buildings=int(len(b)),
                  cell_m=float(cfg.raw["grid"]["cell_size_m"]),
                  candidates=int(len(cand)), existing=int((sites.kind == "existing").sum()),
                  spacing_m=float(cfg.raw["candidates"]["spacing_m"]), max_range_m=float(cfg.raw["radio"]["max_range_m"]),
                  terrain_source=gm.get("terrain_source"), extent_utm=gm.get("extent_utm"),
                  mounts={k: int(v) for k, v in cand["mount"].value_counts().items()},
                  antenna_median_m=float(cand.h_agl.median()), extrap_share=float((pr.extrap > 0.25).mean()),
                  no_geometry_share=float(pr.no_geometry.mean()),
                  model=dict(selected=sel, logo_auc=cv.get(f"logo/{sel}", {}).get("auc"), spatial_auc=cv.get(f"spatial/{sel}", {}).get("auc"),
                             logo_logloss=cv.get(f"logo/{sel}", {}).get("logloss"), n_links=metrics.get("n_links"),
                             n_devices=metrics.get("n_devices"),
                             heilbronn_logo_auc=full.get("cv", {}).get(f"logo/{full.get('selected')}", {}).get("auc"),
                             heilbronn_logo_logloss=full.get("cv", {}).get(f"logo/{full.get('selected')}", {}).get("logloss"),
                             dropped=["rx_ground_z"])),
        devices=devices, link_budget=None, hardware=cfg.raw.get("hardware"),
        plan=dict(current=pl.summary(pl.existing), device_radius_m=pl.radius,
                  min_sites=[{k: v for k, v in r.items() if k not in ("curve", "steps")} for r in plans.values()],
                  coverage_curve=plans[0.98]["curve"]),
    )
    _dump(out / "insights.json", ins)
    curve = plans[0.98]
    ev = dict(
        city=ins["city"],
        overview=dict(addresses=int(cells.n_addr.sum()), cells=int(len(cells)), buildings=int(len(b)), meters=0, meters_heard=0,
                      receptions=0, messages=0, train_links=metrics.get("n_links"), existing=int((sites.kind == "existing").sum()),
                      candidates=int(len(cand)), coverage_today=curve["baseline"], within300_today=pl.within_radius(pl.existing),
                      coverage_all=curve["maximum"], model=sel, logo_auc=ins["city"]["model"]["logo_auc"],
                      logo_logloss=ins["city"]["model"]["logo_logloss"], week=[],
                      rssi_mae=metrics.get("rssi", {}).get("mae_model"), rssi_mae_textbook=metrics.get("rssi", {}).get("mae_logdistance")),
        plans=[dict(target=t, k_mix=r["k"] if r["reached"] else None, coverage_mix=r["coverage"], greedy_k=r["greedy_k"],
                    steps=([r["baseline"]] + list(r["steps"])) if r["reached"] else [],
                    k_weak=(lambda w: w["k"] if w["reached"] else None)(weak.min_sites(t)) if weak is not None else None,
                    sites=r["sites"]) for t, r in plans.items()],
        curve=[curve["baseline"]] + curve["curve"],
        sites=_city_site_rows(pl, sites, plans),
        devices=devices,
    )
    _dump(out / "evidence.json", ev)
    (out / "future.json").unlink(missing_ok=True)
    if P("grid.npz").exists():
        grid = Grid.load(P("grid.npz"))
        for old in out.glob("aerial_*.jpg"):
            old.unlink()
        try:
            aer = _export_aerial(cfg, grid, out)
        except Exception as e:  # noqa: BLE001 – no DOP20 tiles for this city
            print(f"[export] no aerial photo ({e.__class__.__name__})")
            aer = []
        _dump(out / "vegetation.json", dict(aerial=aer))
    for t, r in plans.items():
        print(f"[export] {city['name']}: {t:.0%} coverage -> {r['k'] if r['reached'] else 'not reachable'} of "
              f"{len(cand)} generated rooftop sites")
    sizes = {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in sorted(out.glob("*.json"))}
    print(f"[export] {out}: {sizes}")


def _city_site_rows(pl: Planner, sites: pd.DataFrame, plans: dict) -> list[dict]:
    P, w = pl.P, pl.w
    rank = {t: {sid: i + 1 for i, sid in enumerate(r["sites"])} for t, r in plans.items()}
    alone, reach50, within = (w[:, None] * P).sum(0), (w[:, None] * (P >= 0.5)).sum(0), (w[:, None] * pl.NEAR).sum(0)
    return [dict(kind=r.kind, id=r.site_id, name=r["name"], mount=r.get("mount"), antenna_m=float(r.h_agl),
                 ground_m=float(r.ground_z) if np.isfinite(r.ground_z) else None, confidence=r.get("confidence"),
                 addr_alone=float(alone[i]), addr_reach50=float(reach50[i]), addr_300m=float(within[i]), addr_new=float(alone[i]),
                 rank_90=rank[0.9].get(r.site_id), rank_95=rank[0.95].get(r.site_id), rank_98=rank[0.98].get(r.site_id),
                 fail_loss_pt=None, receptions=None, meters_heard=None, rssi_med=None)
            for i, r in sites.reset_index(drop=True).iterrows()]
