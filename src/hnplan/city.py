"""Any city: the same planner without operator data.

Folder layout (one folder per city):

    cities/<name>/
        city.yaml        optional overrides of config.yaml (name, centre, candidate spacing, range …)
        raw/             LGL Open GeoData for the area: LoD2 + DOM1 + DGM1 tiles, hk_bw.zip (house coordinates),
                         optional bdlm_bw_shp.zip (forest/water), dop20rgbi tiles (aerial photo),
                         optional gateways.csv (name;lat;lon) for gateways that already exist
        processed/       cache (written by the pipeline)
        web_data/        what the browser loads (written by `hnplan city <name> export`)

What is different from Heilbronn:
  * no measurements -> the reception model trained on the Heilbronn week is TRANSFERRED: it only uses geometry
    (distance, antenna height, buildings/terrain on the path) and the meter maker, all available for any city
  * no address list from the operator -> every official house coordinate (Hauskoordinaten) is a meter
  * no candidate sites -> the planner proposes its own: one rooftop per `candidates.spacing_m` square that has
    addresses, the highest roof in that square (top-level surface, as HNVG mounts gateways); antenna = roof + mast
  * no existing gateways unless raw/gateways.csv lists them
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import yaml

from . import io as hio
from .buildings import build_3d_buildings, load_dlm
from .config import PROJECT_ROOT, Config
from .discover import discover
from .rasters import Grid, _tiles_extent, build_grid
from .sites import site_heights

CITIES = PROJECT_ROOT / "cities"
OPERATOR_DATA = ("transmissions", "transmitter_meta", "mapping", "gateway_meta", "candidates", "addresses",
                 "alkis_buildings", "tab_wasser", "water_conditions", "water_service_prices", "water_tariff", "weather", "xplan")
CITY_DEFAULTS = {
    "city": {"name": None, "municipalities": None},
    "candidates": {"spacing_m": 400, "min_roof_m": 6, "min_footprint_m2": 60, "min_addresses": 5, "prefix": "S"},
    "radio": {"max_range_m": 4000},
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def city_dir(name: str) -> Path:
    return CITIES / name


def city_config(name: str) -> Config:
    d = city_dir(name)
    if not (d / "raw").exists():
        raise SystemExit(f"cities/{name}/raw not found – create it and put the city's data there (see cities/README.md)")
    base = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    over = yaml.safe_load((d / "city.yaml").read_text(encoding="utf-8")) if (d / "city.yaml").exists() else {}
    raw = _merge(_merge(base, CITY_DEFAULTS), over)
    raw["paths"] = {"raw": str(d / "raw"), "processed": str(d / "processed"), "web_data": str(d / "web_data")}
    for k in OPERATOR_DATA:  # the city run never needs operator files
        raw["datasets"].pop(k, None)
    raw["datasets"]["gateways_csv"] = {"pattern": r"(^|/)gateways\.csv$", "required": False}
    raw["city"]["name"] = raw["city"].get("name") or name.replace("_", " ").title()
    raw["city"]["slug"] = name
    raw["meter_placement"] = {"enabled": False}
    return Config(raw=raw, root=PROJECT_ROOT)


def heilbronn_model_path() -> Path:
    """Transfer model: trained on the Heilbronn week without absolute ground elevation (see model.TRANSFER_DROP).
    Trained on first use, and again whenever the Heilbronn model is newer."""
    from .config import load_config
    from .model import TRANSFER_DROP, train
    hcfg = load_config()
    main, tr = hcfg.processed("model.joblib"), hcfg.processed("model_transfer.joblib")
    if not main.exists() or not hcfg.processed("train_links.parquet").exists():
        raise SystemExit("The Heilbronn model is missing – run `uv run hnplan all` once in the main project first.")
    if not tr.exists() or tr.stat().st_mtime < main.stat().st_mtime:
        print(f"[city] training the transfer model on the Heilbronn measurements (without {', '.join(TRANSFER_DROP)}) ...")
        train(hcfg, drop=TRANSFER_DROP, tag="transfer")
    return tr


# ---------------------------------------------------------------------------------------------------------------
def generate_candidates(buildings: gpd.GeoDataFrame, cells: pd.DataFrame, ccfg: dict, crs) -> pd.DataFrame:
    """One rooftop candidate per spacing x spacing square with addresses: the highest roof (ground + height)."""
    sp = float(ccfg["spacing_m"])
    b = buildings[(buildings.height >= float(ccfg["min_roof_m"])) &
                  (buildings.geometry.area >= float(ccfg["min_footprint_m2"]))].copy()
    c = b.geometry.representative_point()
    b["x"], b["y"] = c.x.to_numpy(), c.y.to_numpy()
    b["top"] = b.ground_z.fillna(0) + b.height
    b["sq"] = list(zip((b.x // sp).astype(int), (b.y // sp).astype(int)))
    demand = cells.assign(sq=list(zip((cells.x // sp).astype(int), (cells.y // sp).astype(int)))).groupby("sq").n_addr.sum()
    ok = set(demand[demand >= int(ccfg["min_addresses"])].index)
    best = b[b.sq.isin(ok)].sort_values("top", ascending=False).drop_duplicates("sq").sort_values(["y", "x"], ascending=[False, True])
    pre = ccfg.get("prefix", "S")
    s = pd.DataFrame(dict(site_id=[f"{pre}{i + 1:03d}" for i in range(len(best))], x=best.x.to_numpy(), y=best.y.to_numpy(),
                          roof_h=best.height.round(1).to_numpy(), kind="candidate"))
    s["name"] = "Rooftop " + s.site_id
    p = gpd.GeoSeries(gpd.points_from_xy(s.x, s.y), crs=crs).to_crs("EPSG:4326")
    s["lon"], s["lat"] = p.x.to_numpy(), p.y.to_numpy()
    return s


def _existing(sel, crs) -> pd.DataFrame | None:
    if not sel.get("gateways_csv"):
        return None
    with sel["gateways_csv"][0].open() as fh:
        g = pd.read_csv(fh, sep=None, engine="python")
    g.columns = [c.strip().lower() for c in g.columns]
    g = g.rename(columns={"latitude": "lat", "longitude": "lon"})
    g["site_id"] = g.get("id", pd.Series([f"GW{i + 1}" for i in range(len(g))]))
    g["name"] = g.get("name", g["site_id"])
    g["kind"] = "existing"
    p = gpd.GeoSeries(gpd.points_from_xy(g.lon, g.lat), crs="EPSG:4326").to_crs(crs)
    g["x"], g["y"] = p.x.to_numpy(), p.y.to_numpy()
    return g[["site_id", "name", "lat", "lon", "x", "y", "kind"]]


def prepare_city(cfg: Config, force: bool = False) -> None:
    t0 = time.time()
    crs, P, r = cfg.raw["crs"], cfg.processed, cfg.raw["radio"]
    sel = discover(cfg)
    name = cfg.raw["city"]["name"]
    for need in ("lod_gml", "dom1", "house_coords"):
        if not sel.get(need):
            raise SystemExit(f"[city] {name}: no '{need}' files in {cfg.raw_dir} – see cities/README.md")
    if force or not P("buildings.parquet").exists():
        print(f"[city] {name}: parsing 3D building models ...")
        build_3d_buildings(sel["lod_gml"], crs).to_parquet(P("buildings.parquet"))
    b = gpd.read_parquet(P("buildings.parquet"))
    l, t, rr, btm = _tiles_extent(sel["dom1"])
    print(f"[city] 3D buildings: {len(b):,}; area {(rr - l) / 1000:.0f} x {(t - btm) / 1000:.0f} km")
    dlm = None
    if sel.get("dlm"):
        if force or not P("dlm.parquet").exists():
            d = load_dlm(sel["dlm"], (l - 1000, btm - 1000, rr + 1000, t + 1000), crs)
            if d is not None:
                d.to_parquet(P("dlm.parquet"))
        dlm = gpd.read_parquet(P("dlm.parquet")) if P("dlm.parquet").exists() else None
    if force or not P("grid.npz").exists():
        print("[city] building 5 m planning rasters ...")
        build_grid(sel["dom1"], sel.get("dgm1", []), b, None, cfg.raw["grid"]["raster_res_m"], dlm=dlm,
                   dop=sel.get("dop20")).save(P("grid.npz"))
    grid = Grid.load(P("grid.npz"))
    (P("grid_meta.json")).write_text(json.dumps(dict(terrain_source=grid.terrain_source, res_m=grid.res,
                                                     extent_utm=[l, btm, rr, t])))

    # ---- every official address point inside the surface model = one water meter
    if force or not P("house_coords.parquet").exists():
        print("[city] reading Hauskoordinaten (state-wide file, clipped to the area) ...")
        hio.load_house_coords(sel["house_coords"][0], (l, btm, rr, t)).to_parquet(P("house_coords.parquet"))
    hk = pd.read_parquet(P("house_coords.parquet"))
    mun = cfg.raw["city"].get("municipalities")
    if mun:
        hk = hk[hk.gmd.isin(mun)]
    hk = hk[np.isfinite(grid.sample(grid.surface, hk.x.to_numpy(), hk.y.to_numpy()))]
    cs = cfg.raw["grid"]["cell_size_m"]
    hk = hk.assign(cx=(hk.x // cs).astype(int), cy=(hk.y // cs).astype(int))
    cells = hk.groupby(["cx", "cy"]).agg(n_addr=("address", "size"), mx=("x", "mean"), my=("y", "mean"),
                                         example=("address", "first"), gmd=("gmd", "first")).reset_index()
    hk = hk.merge(cells[["cx", "cy", "mx", "my"]], on=["cx", "cy"])
    hk["dd"] = np.hypot(hk.x - hk.mx, hk.y - hk.my)
    rep = hk.sort_values("dd").drop_duplicates(["cx", "cy"])[["cx", "cy", "x", "y", "address"]]
    cells = cells.merge(rep, on=["cx", "cy"]).reset_index(drop=True)
    cells["example"] = cells["address"]
    cells["cell_id"] = np.arange(len(cells))
    cells["shaft_share"], cells["setback_med"] = 0.0, np.nan
    cells["px"], cells["py"] = cells.x, cells.y
    p = gpd.GeoSeries(gpd.points_from_xy(cells.x, cells.y), crs=crs).to_crs("EPSG:4326")
    cells["lon"], cells["lat"] = p.x.to_numpy(), p.y.to_numpy()
    cells.drop(columns=["address"]).to_parquet(P("cells.parquet"))
    print(f"[city] addresses (= meters): {int(cells.n_addr.sum()):,} in {len(cells):,} cells of {cs} m "
          f"({', '.join(hk.gmd.value_counts().index[:4])})")

    # ---- gateway sites: existing (optional list) + generated rooftop candidates
    cand = generate_candidates(b, cells, cfg.raw["candidates"], crs)
    ex = _existing(sel, crs)
    sites = pd.concat([x for x in (ex, cand) if x is not None], ignore_index=True)
    sites = site_heights(sites, grid, r)
    sites.to_parquet(P("sites.parquet"))
    print(f"[city] sites: {0 if ex is None else len(ex)} existing, {len(cand)} generated rooftop candidates "
          f"(one per {cfg.raw['candidates']['spacing_m']:.0f} m square), median antenna {sites.h_agl.median():.0f} m  "
          f"[{time.time() - t0:.0f}s]")


def predict_city(cfg: Config) -> None:
    import joblib

    from .model import _predict
    b = joblib.load(heilbronn_model_path())
    out = _predict(cfg, b, pd.read_parquet(cfg.processed("pred_links.parquet")), pd.read_parquet(cfg.processed("cells.parquet")),
                   write_devices=True)
    out.to_parquet(cfg.processed("predictions.parquet"))
    print(f"[city] {len(out):,} links predicted with the Heilbronn model ({out.no_geometry.mean():.1%} via distance fallback), "
          f"mean P {out.p.mean():.3f}; extrapolating links (> 25 % outside training range): {(out.extrap > 0.25).mean():.1%}")


def run(name: str, step: str, force: bool = False, port: int = 8001, host: str = "127.0.0.1") -> None:
    cfg = city_config(name)
    if step in ("prepare", "all"):
        prepare_city(cfg, force)
    if step in ("features", "all"):
        from .pipeline import build_prediction
        build_prediction(cfg)
    if step in ("predict", "all"):
        predict_city(cfg)
    if step in ("export", "all"):
        from .export import export_city
        export_city(cfg)
    if step == "serve":
        import uvicorn

        from .api import make_app
        print(f"[city] {cfg.raw['city']['name']}: http://{host}:{port}")
        uvicorn.run(make_app(cfg), host=host, port=port)
