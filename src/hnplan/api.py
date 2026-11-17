"""FastAPI server: serves the 3D web app and a small planning API."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import PROJECT_ROOT, load_config



class PlanRequest(BaseModel):
    k: int = 3
    objective: str = "coverage"
    disabled: list[str] = []


def make_app(cfg=None) -> FastAPI:
    """cfg=None: Heilbronn (config.yaml, web/data). A city Config: same app, data from cities/<name>/web_data."""
    app = FastAPI(title="LoRaWAN Planner", version="0.2.0")
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @lru_cache(maxsize=1)
    def planner():
        from .optimize import Planner
        return Planner(cfg or load_config())

    @app.get("/api/health")
    def health():
        return {"status": "ok", "city": (cfg.raw.get("city") or {}).get("name") if cfg else "Heilbronn"}

    @app.post("/api/plan")
    def plan(req: PlanRequest):
        pl = planner()
        try:
            dis = pl.site_index(req.disabled)
        except KeyError as e:
            raise HTTPException(400, f"unknown site {e}")
        return pl.greedy(max(0, min(req.k, 20)), req.objective, disabled=dis)

    @app.get("/api/failure")
    def failure():
        return planner().failure_analysis()

    @app.get("/api/summary")
    def summary(active: str = ""):
        pl = planner()
        ids = [s for s in active.split(",") if s]
        idx = pl.site_index(ids) if ids else pl.existing
        return pl.summary(idx)

    if cfg is not None:  # city data first, then the shared app files
        app.mount("/data", StaticFiles(directory=cfg.web_data_dir), name="city-data")
    app.mount("/", StaticFiles(directory=Path(PROJECT_ROOT) / "web", html=True), name="web")
    return app


app = make_app()
