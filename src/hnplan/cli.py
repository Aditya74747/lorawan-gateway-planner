"""Command line interface:  uv run hnplan <command>

  discover   list which files from data/raw are used (and that nothing required is missing)
  prepare    buildings, rasters, geocoding, site heights (cached in data/processed)
  features   line-of-sight features for training links + all planning links
  train      cross-validate physics / boosted / neural models, fit the final ensemble
  benchmark  compare ~14 learners on the same honest splits (uv run --group bench ...)
  predict    reception probability for every address cell x gateway site
  optimize   best new gateway sites (greedy), failure analysis
  weather    fetch / read weather and test its effect on signal strength
  export     write web/data/*.json for the 3D app
  all        prepare -> features -> train -> predict -> export
  serve      start the web app + API (http://localhost:8000)
  city       any other city from cities/<name>/ – e.g.  hnplan city stuttgart all   then   hnplan city stuttgart serve
             (steps: prepare | features | predict | export | all | serve; default port 8001)
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="hnplan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["discover", "prepare", "features", "train", "benchmark", "predict",
                                        "optimize", "weather", "export", "all", "serve", "city"])
    ap.add_argument("args", nargs="*", help="city: <name> [prepare|features|predict|export|all|serve]")
    ap.add_argument("--models", default=None, help="benchmark: comma-separated subset of models")
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="rebuild cached buildings/rasters")
    ap.add_argument("-k", type=int, default=None, help="number of new gateways (optimize)")
    ap.add_argument("--target", type=float, default=None,
                    help="optimize: fewest candidate sites reaching this coverage, e.g. 0.95 (or 95)")
    ap.add_argument("--objective", default=None, choices=["coverage", "redundancy", "radius"],
                    help="radius = provider rule: devices within radio.device_radius_m of a gateway")
    ap.add_argument("--future", action="store_true",
                    help="optimize: plan for the future city (planned buildings + new meters from the land-use plans)")
    ap.add_argument("--fetch", action="store_true", help="weather: download from Open-Meteo")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=None)
    a = ap.parse_args(argv)
    if a.command == "city":
        if not a.args:
            ap.error("usage: hnplan city <name> [prepare|features|predict|export|all|serve]")
        from .city import run
        run(a.args[0], a.args[1] if len(a.args) > 1 else "all", force=a.force, port=a.port or 8001,
            host=a.host if a.host != "0.0.0.0" else "127.0.0.1")
        return
    cfg = load_config(a.config)

    if a.command == "discover":
        from .discover import discover
        discover(cfg)
    if a.command in ("prepare", "all"):
        from .pipeline import prepare
        prepare(cfg, force=a.force)
    if a.command in ("features", "all"):
        from .pipeline import build_prediction, build_training
        build_training(cfg)
        build_prediction(cfg)
    if a.command in ("train", "all"):
        from .model import train
        train(cfg)
    if a.command == "benchmark":
        from .benchmark import benchmark
        benchmark(cfg, a.models.split(",") if a.models else None)
    if a.command in ("predict", "all"):
        from .model import predict
        predict(cfg)
    if a.command == "optimize":
        from .optimize import Planner
        pl = Planner(cfg, scenario="future" if a.future else "today")
        oc = cfg.raw["optimizer"]
        if a.target is not None:
            t = a.target / 100 if a.target > 1 else a.target
            r = pl.min_sites(t, a.objective or "coverage")
            print(f"[optimize] target {t:.1%}: {r['k']} of {len(pl.candidates)} approved sites -> "
                  f"{r['coverage']:.2%} (today {r['baseline']:.2%}, all sites {r['maximum']:.2%})"
                  + ("" if r["reached"] else "  – target NOT reachable, all sites listed"))
            print("   build order:", ", ".join(r["sites"]))
        else:
            res = pl.greedy(a.k or oc["default_k"], a.objective or oc["objective"])
            print(json.dumps(dict(plan=res, failure=pl.failure_analysis()), indent=1))
    if a.command == "weather":
        _weather(cfg, a.fetch)
    if a.command in ("export", "all"):
        from .export import export
        export(cfg)
    if a.command == "serve":
        import uvicorn
        uvicorn.run("hnplan.api:app", host=a.host, port=a.port or 8000)


def _weather(cfg, fetch: bool) -> None:
    import pandas as pd
    from .discover import discover
    from .pipeline import save_json
    from .weather import analyse, fetch_open_meteo, read_weather_csv
    tx = pd.read_parquet(cfg.processed("transmissions.parquet"))
    sel = discover(cfg, verbose=False)
    if fetch:
        w = cfg.raw["weather"]
        df = fetch_open_meteo(w["latitude"], w["longitude"], str(tx.ts.min().date()), str(tx.ts.max().date()))
    elif sel.get("weather"):
        df = read_weather_csv(sel["weather"][0].open().read())
    else:
        sys.exit("No weather data: run with --fetch or put an Open-Meteo CSV into data/raw")
    res = analyse(cfg, df)
    save_json(cfg.processed("weather_insights.json"), res)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
