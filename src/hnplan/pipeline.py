"""End-to-end data preparation: raw files -> cached, analysis-ready tables."""
from __future__ import annotations

import json
import time

import geopandas as gpd
import numpy as np
import pandas as pd

from . import io as hio
from .buildings import build_3d_buildings, load_alkis, load_dlm
from .config import Config
from .discover import discover
from .features import FeatureBuilder
from .geocode import AddressIndex
from .hnvg_docs import extract_facts
from .meter_placement import MeterPlacer
from .rasters import Grid, _tiles_extent, build_grid
from .sites import site_heights


def _xy(df, crs):
    pts = gpd.GeoSeries(gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326").to_crs(crs)
    return pts.x.to_numpy(), pts.y.to_numpy()


def _lonlat(x, y, crs):
    p = gpd.GeoSeries(gpd.points_from_xy(x, y), crs=crs).to_crs("EPSG:4326")
    return p.x.to_numpy(), p.y.to_numpy()


def prepare(cfg: Config, force: bool = False) -> None:
    """Select data, build buildings/rasters, geocode, estimate site heights."""
    t0 = time.time()
    crs = cfg.raw["crs"]
    sel = discover(cfg)
    P = cfg.processed

    # ---------------- 3D buildings + ALKIS ----------------
    if force or not P("buildings.parquet").exists():
        print("[prepare] parsing 3D building models ...")
        b = build_3d_buildings(sel["lod_gml"], crs)
        b.to_parquet(P("buildings.parquet"))
    b = gpd.read_parquet(P("buildings.parquet"))
    print(f"[prepare] 3D buildings: {len(b):,}")

    if force or not P("alkis_buildings.parquet").exists():
        ab = load_alkis(sel["alkis_buildings"], ["oid", "lagebeztxt", "gebnutzbez"], crs)
        ab.to_parquet(P("alkis_buildings.parquet"))
        lu = load_alkis(sel["alkis_landuse"], ["oid", "nutzart"], crs) if sel["alkis_landuse"] else None
        if lu is not None:
            lu.to_parquet(P("landuse.parquet"))
    ab = gpd.read_parquet(P("alkis_buildings.parquet"))
    lu = gpd.read_parquet(P("landuse.parquet")) if P("landuse.parquet").exists() else None

    l, t, r, btm = _tiles_extent(sel["dom1"])
    bbox = (l - 1000, btm - 1000, r + 1000, t + 1000)
    if sel.get("dlm") and (force or not P("dlm.parquet").exists()):
        print(f"[prepare] Basis-DLM: {', '.join(s.name for s in sel['dlm'])} (clipped to the planning area)")
        d = load_dlm(sel["dlm"], bbox, crs)
        if d is not None:
            d.to_parquet(P("dlm.parquet"))
    dlm = gpd.read_parquet(P("dlm.parquet")) if sel.get("dlm") and P("dlm.parquet").exists() else None

    hk = None
    if sel.get("house_coords"):
        if force or not P("house_coords.parquet").exists():
            print("[prepare] reading Hauskoordinaten (state-wide file, clipped to the planning area) ...")
            hio.load_house_coords(sel["house_coords"][0], bbox).to_parquet(P("house_coords.parquet"))
        hk = pd.read_parquet(P("house_coords.parquet"))

    # ---------------- rasters ----------------
    if force or not P("grid.npz").exists():
        print("[prepare] building 5 m planning rasters ...")
        grid = build_grid(sel["dom1"], sel["dgm1"], b, lu, cfg.raw["grid"]["raster_res_m"], dlm=dlm,
                          dop=sel.get("dop20"))
        grid.save(P("grid.npz"))
    grid = Grid.load(P("grid.npz"))
    print(f"[prepare] terrain source: {grid.terrain_source}")
    save_json(P("grid_meta.json"), dict(terrain_source=grid.terrain_source, res_m=grid.res,
                                        extent_utm=[grid.x0, grid.y0 - grid.shape[0] * grid.res,
                                                    grid.x0 + grid.shape[1] * grid.res, grid.y0]))

    # ---------------- HNVG documents -> facts, meter placement rules ----------------
    facts = {k: v["value"] for k, v in extract_facts(cfg, sel).items()}
    pc = cfg.raw.get("meter_placement", {"enabled": False})
    placer = MeterPlacer(b, lu, pc, facts) if pc.get("enabled") else None

    def place(df, xcol="x", ycol="y"):
        cols = ["px", "py", "fdx", "fdy", "setback_m", "shaft_likely", "placed"]
        if placer is None:
            res = pd.DataFrame(dict(px=df[xcol], py=df[ycol], fdx=np.nan, fdy=np.nan, setback_m=np.nan,
                                    shaft_likely=0.0, placed=False), index=df.index)
        else:
            res = placer.place(df[xcol].to_numpy(), df[ycol].to_numpy())
            res.index = df.index
        return pd.concat([df.drop(columns=[c for c in cols if c in df]), res[cols]], axis=1)

    # ---------------- challenge tables ----------------
    addr = hio.load_addresses(sel["addresses"][0])
    if hk is not None:
        # same street names exist in neighbouring towns -> only use points of the address list's cities
        cities = set(addr["city"].dropna().str.strip()) if "city" in addr else set()
        hk = hk[hk["gmd"].isin(cities)] if cities & set(hk["gmd"]) else hk
        print(f"[prepare] Hauskoordinaten: {len(hk):,} official address points ({', '.join(sorted(cities)) or 'all'})")
    idx = AddressIndex(ab, hk)
    meters = hio.load_meters(sel["transmitter_meta"][0], sel["mapping"][0])
    meters["x"], meters["y"] = _xy(meters.fillna({"lat": 0, "lon": 0}), crs)
    meters.loc[meters.lat.isna(), ["x", "y"]] = np.nan
    meters["coord_source"] = np.where(meters.x.notna(), "provided", None)
    broken = meters.x.isna()
    if broken.any():
        gc = idx.lookup(meters.loc[broken, "address"])
        meters.loc[broken, ["x", "y"]] = gc[["x", "y"]].to_numpy()
        meters.loc[broken & meters.x.notna(), "coord_source"] = "geocoded:" + gc["quality"].astype(str)
    print(f"[prepare] meters: {len(meters)}, broken coordinates {int(broken.sum())}, "
          f"repaired {int((broken & meters.x.notna()).sum())}")
    meters["lon"], meters["lat"] = _lonlat(meters.x.fillna(0), meters.y.fillna(0), crs)
    meters.loc[meters.x.isna(), ["lon", "lat"]] = np.nan
    meters = place(meters)
    print(f"[prepare] meters placed on street-facing wall: {int(meters.placed.sum())}/{int(meters.x.notna().sum())}, "
          f"likely in a shaft: {int(meters.shaft_likely.sum())}")
    meters.to_parquet(P("meters.parquet"))

    sites = pd.concat([hio.load_gateways(sel["gateway_meta"][0]), hio.load_candidates(sel["candidates"][0])],
                      ignore_index=True)
    sites["x"], sites["y"] = _xy(sites, crs)
    r = cfg.raw["radio"]
    sites = site_heights(sites, grid, r)
    print("[prepare] gateway mounting: " + ", ".join(f"{k} {v}" for k, v in sites["mount"].value_counts().items()))
    sites.to_parquet(P("sites.parquet"))
    print(f"[prepare] sites: {int((sites.kind == 'existing').sum())} existing, "
          f"{int((sites.kind == 'candidate').sum())} candidates "
          f"({int((~sites.in_surface_model).sum())} outside surface model)")

    tx = hio.load_transmissions(sel["transmissions"][0])
    tx.to_parquet(P("transmissions.parquet"))

    # ---------------- addresses -> planning cells ----------------
    gc = idx.lookup(addr["street"])
    addr = pd.concat([addr, gc], axis=1)
    print(f"[prepare] addresses: {len(addr):,} – exact {int((addr.quality == 'exact').sum()):,}, "
          f"street-level {int((addr.quality == 'street').sum()):,}, unmatched {int(addr.x.isna().sum()):,}")
    addr = addr[addr.x.notna()].copy()
    addr = place(addr)
    print(f"[prepare] addresses placed: {int(addr.placed.sum()):,}, likely meter shaft: {int(addr.shaft_likely.sum()):,} "
          f"(set back > {facts.get('shaft_pipe_length_m')} m from the street)")
    cs = cfg.raw["grid"]["cell_size_m"]
    addr["cx"], addr["cy"] = (addr.x // cs).astype(int), (addr.y // cs).astype(int)
    cells = addr.groupby(["cx", "cy"]).agg(n_addr=("street", "size"), mx=("x", "mean"), my=("y", "mean"),
                                           example=("street", "first"), shaft_share=("shaft_likely", "mean"),
                                           setback_med=("setback_m", "median")).reset_index()
    # representative point = the real address point closest to the cell mean (inside a building)
    addr = addr.merge(cells[["cx", "cy", "mx", "my"]], on=["cx", "cy"])
    addr["dd"] = np.hypot(addr.x - addr.mx, addr.y - addr.my)
    rep = addr.sort_values("dd").drop_duplicates(["cx", "cy"])[
        ["cx", "cy", "x", "y", "px", "py", "fdx", "fdy", "setback_m", "shaft_likely", "placed"]]
    cells = cells.merge(rep, on=["cx", "cy"]).reset_index(drop=True)
    cells["cell_id"] = np.arange(len(cells))
    cells["lon"], cells["lat"] = _lonlat(cells.x, cells.y, crs)
    cells.to_parquet(P("cells.parquet"))
    addr[["street", "plz", "x", "y", "px", "py", "setback_m", "shaft_likely", "quality", "cx", "cy"]].to_parquet(P("addresses.parquet"))
    print(f"[prepare] planning cells ({cs} m): {len(cells):,}  [{time.time() - t0:.0f}s]")
    prepare_future(cfg, sel, force, grid, b, lu, sites, cells)


FUTURE_FILES = ("grid_future.npz", "future_buildings.parquet", "future_areas.parquet", "future_cells.parquet",
                "future_summary.json", "pred_links_future.parquet", "predictions_future.parquet")


def prepare_future(cfg: Config, sel, force, grid, b, lu, sites, cells) -> None:
    """Future city scenario from XPlanung plans (see future.py). Removes stale outputs when no plans are given."""
    from .future import build_future, future_cells, load_plan_zones
    P = cfg.processed
    if not sel.get("xplan"):
        for f in FUTURE_FILES:
            P(f).unlink(missing_ok=True)
        return
    if force or not P("plan_zones.parquet").exists():
        print(f"[prepare] spatial plans (XPlanung): {len(sel['xplan'])} files ...")
        meta = json.loads(P("grid_meta.json").read_text())
        z = load_plan_zones(sel["xplan"], meta["extent_utm"], cfg.raw["crs"])
        if z is None or not len(z):
            print("[prepare] no plan zones inside the planning area")
            return
        z.to_parquet(P("plan_zones.parquet"))
    z = gpd.read_parquet(P("plan_zones.parquet"))
    fgrid, fb, areas, summary = build_future(grid, z, b, lu, None, sites, cfg.raw.get("future", {}))
    if fb is None or not len(fb):
        print("[prepare] future city: no open building land found")
        return
    fgrid.save(P("grid_future.npz"))
    fb.to_parquet(P("future_buildings.parquet"))
    areas.to_parquet(P("future_areas.parquet"))
    fc = future_cells(fb, cfg.raw["grid"]["cell_size_m"], int(cells.cell_id.max()) + 1)
    fc["lon"], fc["lat"] = _lonlat(fc.x, fc.y, cfg.raw["crs"])
    fc.to_parquet(P("future_cells.parquet"))
    summary["future_cells"], summary["future_meters"] = int(len(fc)), int(fc.n_addr.sum())
    save_json(P("future_summary.json"), summary)
    print(f"[prepare] future city: {summary['open_areas']} open building areas ({summary['open_ha']:.0f} ha), "
          f"{summary['future_buildings']:,} future buildings / meters in {len(fc)} cells")


def device_stats(tx: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per device (delivery ratio via frame counters) and per device x gateway statistics."""
    msgs = tx.drop_duplicates("id")
    d = msgs.groupby("device_id").agg(n_msgs=("id", "size"), fmin=("fcnt", "min"), fmax=("fcnt", "max"),
                                      sf_med=("sf", "median"), toa_ms=("toa_ms", "median"))
    d["expected"] = (d.fmax - d.fmin + 1).clip(lower=d.n_msgs)
    d["pdr"] = d.n_msgs / d.expected
    d.loc[d.expected > 500, "pdr"] = np.nan  # counter resets -> unreliable
    link = tx.groupby(["device_id", "gateway_id"]).agg(n_rx=("id", "nunique"), rssi_med=("rssi", "median"),
                                                       snr_med=("snr", "median"), rssi_max=("rssi", "max"))
    return d.reset_index(), link.reset_index()


def build_training(cfg: Config) -> pd.DataFrame:
    P = cfg.processed
    grid = Grid.load(P("grid.npz"))
    meters = pd.read_parquet(P("meters.parquet"))
    sites = pd.read_parquet(P("sites.parquet"))
    tx = pd.read_parquet(P("transmissions.parquet"))
    dev, link = device_stats(tx)
    gws = sites[sites.kind == "existing"].reset_index(drop=True)

    heard_any = set(tx.device_id)
    m = meters[meters.x.notna()].copy()
    m["heard"] = m.device_id.isin(heard_any)
    # eligible = actually transmitting during the week, or flagged active ("A") but silent
    elig = m[m.heard | (m.meter_state == "A")].reset_index(drop=True)
    print(f"[train-data] meters with coords {len(m)}, eligible {len(elig)} "
          f"(heard {int(elig.heard.sum())}, active-but-silent {int((~elig.heard).sum())})")

    fb = FeatureBuilder(grid, cfg.raw["radio"], cfg.raw["grid"])
    wall = cfg.raw.get("meter_placement", {}).get("use_for_model", False)
    rx = fb.rx_points(elig.px.fillna(elig.x) if wall else elig.x, elig.py.fillna(elig.y) if wall else elig.y, elig)
    ri = np.repeat(np.arange(len(elig)), len(gws))
    ti = np.tile(np.arange(len(gws)), len(elig))
    X = fb.links(rx, ri, gws, ti)
    L = pd.DataFrame(dict(zaehlernummer_id=elig.zaehlernummer_id.to_numpy()[ri],
                          device_id=elig.device_id.to_numpy()[ri], meter_state=elig.meter_state.to_numpy()[ri],
                          rx_x=elig.x.to_numpy()[ri], rx_y=elig.y.to_numpy()[ri],
                          site_id=gws.site_id.to_numpy()[ti], site_name=gws.name.to_numpy()[ti]))
    L = pd.concat([L, X], axis=1)
    L = L.merge(link.rename(columns={"gateway_id": "site_id"}), on=["device_id", "site_id"], how="left")
    L = L.merge(dev[["device_id", "n_msgs", "pdr", "sf_med"]], on="device_id", how="left")
    L["n_rx"] = L.n_rx.fillna(0)
    L["heard"] = (L.n_rx > 0).astype(int)
    L["rx_share"] = (L.n_rx / L.n_msgs).fillna(0).clip(0, 1)
    L["block"] = ((L.rx_x // 1000).astype(int).astype(str) + "_" + (L.rx_y // 1000).astype(int).astype(str))
    L.to_parquet(P("train_links.parquet"))
    print(f"[train-data] links {len(L):,}, positives {int(L.heard.sum()):,} ({L.heard.mean():.1%})")
    return L


def build_prediction(cfg: Config) -> pd.DataFrame:
    P = cfg.processed
    grid = Grid.load(P("grid.npz"))
    cells = pd.read_parquet(P("cells.parquet"))
    sites = pd.read_parquet(P("sites.parquet"))
    fb = FeatureBuilder(grid, cfg.raw["radio"], cfg.raw["grid"])
    wall = cfg.raw.get("meter_placement", {}).get("use_for_model", False)
    rx = fb.rx_points(cells.px.fillna(cells.x) if wall else cells.x, cells.py.fillna(cells.y) if wall else cells.y, cells)
    d = np.hypot(rx["x"][:, None] - sites.x.to_numpy()[None, :],
                 rx["y"][:, None] - sites.y.to_numpy()[None, :])
    ri, ti = np.nonzero(d <= cfg.raw["radio"]["max_range_m"])
    t0 = time.time()
    X = fb.links(rx, ri, sites, ti)
    X.insert(0, "cell_idx", ri)
    X.insert(1, "site_idx", ti)
    X.to_parquet(P("pred_links.parquet"))
    print(f"[predict-data] {len(X):,} cell->site links in range  [{time.time() - t0:.0f}s]")
    if P("grid_future.npz").exists() and P("future_cells.parquet").exists():
        build_prediction_future(cfg, X, rx, cells, sites)
    return X


def future_cell_table(cfg: Config) -> pd.DataFrame:
    """Today's cells followed by the future cells (same column set) – row order = cell_idx in the future scenario."""
    P = cfg.processed
    cells = pd.read_parquet(P("cells.parquet")).assign(future=False)
    fc = pd.read_parquet(P("future_cells.parquet"))
    return pd.concat([cells, fc[[c for c in fc.columns if c in cells.columns]]], ignore_index=True)


def build_prediction_future(cfg: Config, X_today, rx_today, cells, sites) -> None:
    """Links in the future city: today's cells + new cells, on the grid with planned buildings.
    Only links whose path touches a cell that changed are recomputed – all others are identical to today."""
    P = cfg.processed
    fgrid = Grid.load(P("grid_future.npz"))
    grid = Grid.load(P("grid.npz"))
    fc = pd.read_parquet(P("future_cells.parquet"))
    fb = FeatureBuilder(fgrid, cfg.raw["radio"], cfg.raw["grid"])
    t0 = time.time()
    # which existing links cross changed raster cells? sample each path on the change mask
    changed = (fgrid.bheight != grid.bheight)
    from scipy import ndimage
    near = ndimage.binary_dilation(changed, iterations=int(np.ceil(60 / grid.res)))  # + 60 m: rx_bdens / rx_bldg_h
    ci, si = X_today.cell_idx.to_numpy(), X_today.site_idx.to_numpy()
    sx, sy = sites.x.to_numpy(), sites.y.to_numpy()
    hit = np.zeros(len(ci), bool)
    for s0 in range(0, len(ci), 10_000):
        a, t = ci[s0:s0 + 10_000], si[s0:s0 + 10_000]
        x0, y0, x1, y1 = rx_today["x"][a], rx_today["y"][a], sx[t], sy[t]
        k = np.linspace(0, 1, 400)[None, :]
        r, c, ok = grid.rc((x0[:, None] + k * (x1 - x0)[:, None]).ravel(), (y0[:, None] + k * (y1 - y0)[:, None]).ravel())
        hit[s0:s0 + 10_000] = (near[r, c] & ok).reshape(len(a), -1).any(1)
    wall = cfg.raw.get("meter_placement", {}).get("use_for_model", False)
    rxf = fb.rx_points(cells.px.fillna(cells.x) if wall else cells.x, cells.py.fillna(cells.y) if wall else cells.y, cells)
    Xc = X_today.copy()
    if hit.any():
        Xn = fb.links(rxf, ci[hit], sites, si[hit])
        Xc.loc[hit, Xn.columns] = Xn.to_numpy()
    # new cells -> all sites in range
    rxn = fb.rx_points(fc.x, fc.y, None)
    d = np.hypot(rxn["x"][:, None] - sx[None, :], rxn["y"][:, None] - sy[None, :])
    ri, ti = np.nonzero(d <= cfg.raw["radio"]["max_range_m"])
    Xf = fb.links(rxn, ri, sites, ti)
    Xf.insert(0, "cell_idx", ri + len(cells))
    Xf.insert(1, "site_idx", ti)
    Xall = pd.concat([Xc, Xf], ignore_index=True)
    Xall.to_parquet(P("pred_links_future.parquet"))
    print(f"[predict-data] future city: {int(hit.sum()):,} of {len(ci):,} links cross planned buildings (recomputed), "
          f"{len(Xf):,} links for {len(fc)} new cells  [{time.time() - t0:.0f}s]")


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
