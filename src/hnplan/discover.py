"""Data selector.

Scans the raw folder recursively – including the inside of .zip archives – and picks
exactly the files the project needs (see `datasets` in config.yaml). Everything else
(images, PDFs, licence files, unrelated layers such as parcels or boundary points,
duplicate tiles, other challenges' data) is ignored.

Files inside zips are never extracted: they are addressed through GDAL's /vsizip/
virtual file system or read via `zipfile`, so a 2 GB download stays a 2 GB download.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config

TILE_RE = re.compile(r"_32_(\d{3})_(\d{4})_1_", re.I)
HASH_PREFIX = re.compile(r"^[0-9a-f]{8}-")  # upload prefixes like "84cd939d-"


@dataclass
class Source:
    dataset: str
    container: str        # path of zip (or "" for loose files)
    member: str           # member path inside the zip, or absolute file path
    size: int

    @property
    def name(self) -> str:
        return Path(self.member).name

    @property
    def gdal_path(self) -> str:
        """Path usable by GDAL / pyogrio / rasterio."""
        return f"/vsizip/{self.container}/{self.member}" if self.container else self.member

    def open(self) -> io.BufferedIOBase:
        if self.container:
            return zipfile.ZipFile(self.container).open(self.member)
        return open(self.member, "rb")

    def tile(self) -> tuple[int, int] | None:
        m = TILE_RE.search(self.name)
        return (int(m[1]), int(m[2])) if m else None


def _iter_entries(raw_dir: Path):
    """Yield (container, member, size) for every file, descending into zips."""
    for p in sorted(raw_dir.rglob("*")):
        if p.is_dir():
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for info in z.infolist():
                        if not info.is_dir():
                            yield str(p.resolve()), info.filename, info.file_size
            except zipfile.BadZipFile:
                continue
        else:
            yield "", str(p.resolve()), p.stat().st_size


def _norm(name: str) -> str:
    return HASH_PREFIX.sub("", Path(name).name).lower()


def discover(cfg: Config, verbose: bool = True) -> dict[str, list[Source]]:
    raw_dir = cfg.raw_dir
    if not raw_dir.exists():
        raise FileNotFoundError(f"Raw data folder not found: {raw_dir}")
    specs = {k: (re.compile(v["pattern"], re.I), v) for k, v in cfg.datasets.items()}
    found: dict[str, list[Source]] = {k: [] for k in specs}
    n_seen = 0
    for container, member, size in _iter_entries(raw_dir):
        n_seen += 1
        name = _norm(member)
        for key, (rx, _) in specs.items():
            if rx.search(name) or rx.search(member.replace("\\", "/").lower()):
                found[key].append(Source(key, container, member, size))
                break

    selected: dict[str, list[Source]] = {}
    for key, (_, spec) in specs.items():
        cands = found[key]
        if not spec.get("multi"):
            # several copies of the same CSV (e.g. inside an old .rar extraction and as an
            # upload): keep the largest non-empty one – truncated copies are smaller.
            cands = [c for c in cands if c.size > 0]
            cands = sorted(cands, key=lambda s: s.size, reverse=True)[:1]
        elif key == "lod_gml":
            # one entry per 1 km tile; prefer LoD2 (newer roof detail) over LoD1
            best: dict[tuple, Source] = {}
            for s in cands:
                t = s.tile()
                rank = 2 if s.name.lower().startswith("lod2") else 1
                if t and (t not in best or rank > (2 if best[t].name.lower().startswith("lod2") else 1)):
                    best[t] = s
            cands = list(best.values())
        elif key in ("dom1", "dgm1", "dop20"):
            best = {}
            for s in cands:
                t = s.tile()
                if t and (t not in best or s.name > best[t].name):  # newest flight year wins
                    best[t] = s
            cands = list(best.values())
        elif key == "dlm":  # several layers from one state-wide zip: one entry per layer
            byname: dict[str, Source] = {}
            for s in cands:
                byname.setdefault(s.name.lower(), s)
            cands = list(byname.values())
        else:  # ALKIS layers: one per district zip, drop exact duplicates
            seen, uniq = set(), []
            for s in cands:
                k2 = (Path(s.container).name if s.container else s.member)
                if k2 not in seen:
                    seen.add(k2)
                    uniq.append(s)
            cands = uniq
        if spec.get("required") and not cands:
            raise FileNotFoundError(
                f"Required dataset '{key}' not found in {raw_dir} (pattern {spec['pattern']!r})")
        selected[key] = cands

    manifest = {
        "raw_dir": str(raw_dir),
        "files_scanned": n_seen,
        "selected": {k: [asdict(s) | {"gdal_path": s.gdal_path} for s in v] for k, v in selected.items()},
    }
    cfg.processed("manifest.json").write_text(json.dumps(manifest, indent=1))
    if verbose:
        used = sum(len(v) for v in selected.values())
        print(f"[discover] scanned {n_seen} files, selected {used}, ignored {n_seen - used}")
        for k, v in selected.items():
            mb = sum(s.size for s in v) / 1e6
            extra = ""
            if k in ("lod_gml", "dom1", "dgm1", "dop20") and v:
                xs = [s.tile()[0] for s in v]
                ys = [s.tile()[1] for s in v]
                extra = f"  tiles E{min(xs)}-{max(xs) + 1} N{min(ys)}-{max(ys) + 1} km"
            print(f"   {k:17s} {len(v):4d} file(s) {mb:8.1f} MB{extra}")
    return selected
