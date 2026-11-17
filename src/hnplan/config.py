"""Configuration loading and project paths."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path

    @property
    def raw_dir(self) -> Path:
        return self._p(self.raw["paths"]["raw"])

    @property
    def processed_dir(self) -> Path:
        p = self._p(self.raw["paths"]["processed"])
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def web_data_dir(self) -> Path:
        p = self._p(self.raw["paths"]["web_data"])
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def datasets(self) -> dict[str, dict]:
        return self.raw["datasets"]

    def section(self, name: str) -> dict:
        return self.raw.get(name, {})

    def _p(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    def processed(self, name: str) -> Path:
        return self.processed_dir / name


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, root=path.resolve().parent)
