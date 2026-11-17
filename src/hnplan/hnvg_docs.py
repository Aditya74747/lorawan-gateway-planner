"""Facts from HNVG / Stadtwerke Heilbronn documents that change how we model water meters.

Only four documents from the HNVG website are relevant (the data selector picks exactly these
by file name, see `datasets:` in config.yaml):

  tab_wasser            TAB Wasser – meter right behind the house entry, connection straight
                        from the street main by the shortest route  -> meter on the street-facing
                        basement wall
  water_conditions      Ergänzende Bedingungen AVBWasserV – meter shaft at the property line if the
                        pipe on the property exceeds 40 m; remote-readable meters allowed; annual
                        manual reading
  water_service_prices  Montagepreise Wasserversorgung – price of a manual reading visit
                        (normal vs. shaft)
  water_tariff          Preisblatt AVBWasserV – drinking-water price (leak-detection value)

Every fact has a default (config.yaml -> domain) so the pipeline still runs without the PDFs;
the output records whether each value was read from a document or fell back to the default.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from pypdf import PdfReader

from .config import Config
from .discover import Source


@dataclass
class Rule:
    key: str
    dataset: str
    pattern: str            # regex; group(1) is the value for numeric facts
    kind: str = "number"    # number | flag
    unit: str = ""
    meaning: str = ""


RULES = [
    Rule("shaft_pipe_length_m", "water_conditions",
         r"Anschlussleitung auf dem anzuschlie\w*\s+Grundst\w+\s+(\d+)\s*m\s+\w*berschreitet", unit="m",
         meaning="pipe length on the property above which a meter shaft/cabinet at the property line can be required"),
    Rule("remote_reading_allowed", "water_conditions", r"fernauslesbaren\s+Wasserz\w+", "flag",
         meaning="the utility may install remotely readable water meters"),
    Rule("manual_reading_annual", "water_conditions", r"grunds\w+\s+j\w+hrlich\s+im\s+rollierenden", "flag",
         meaning="meters are read manually once a year (rolling schedule)"),
    Rule("meter_at_house_entry", "tab_wasser", r"so\s+nah\s+wie\s+m\w+glich\s+im\s+Bereich\s+der\s+HAE", "flag",
         meaning="meter is mounted as close as possible to the house entry of the connection pipe"),
    Rule("connection_shortest_path", "tab_wasser", r"auf\s+dem\s+k\w+rzesten\s+Wege", "flag",
         meaning="house connection runs straight from the street main by the shortest route"),
    Rule("meter_min_height_mm", "tab_wasser", r"Montageh\w+he\s+mindestens\s+(\d+)\s*mm", unit="mm",
         meaning="minimum mounting height of the meter above the floor"),
    Rule("visit_cost_eur_net", "water_service_prices",
         r"Kontrollablesung auf Wunsch des Kunden \(zus\w+tzliche Abrechnung\)\s+(\d+,\d+)", unit="EUR",
         meaning="price of one manual check reading (net)"),
    Rule("visit_cost_shaft_eur_net", "water_service_prices",
         r"\(zus\w+tzliche Abrechnung\)\s+Schacht\s+(\d+,\d+)", unit="EUR",
         meaning="price of one manual check reading of a meter in a shaft (net)"),
    Rule("water_price_eur_m3_net", "water_tariff",
         r"Trinkwasser\s+betr\w+gt\s+Euro/m\S*\s+(\d+,\d+)", unit="EUR/m3",
         meaning="drinking-water price (net) – value of detecting a leak early"),
]


def pdf_text(src: Source) -> str:
    with src.open() as fh:
        reader = PdfReader(fh)
        text = " ".join((p.extract_text() or "") for p in reader.pages)
    return re.sub(r"\s+", " ", text)


def extract_facts(cfg: Config, selected: dict[str, list[Source]]) -> dict:
    defaults = cfg.raw.get("domain", {})
    texts: dict[str, tuple[str, str]] = {}
    for ds in {r.dataset for r in RULES}:
        if selected.get(ds):
            src = selected[ds][0]
            try:
                texts[ds] = (src.name, pdf_text(src))
            except Exception as e:  # unreadable PDF -> defaults
                print(f"[docs] could not read {src.name}: {e}")
    facts = {}
    for r in RULES:
        name, text = texts.get(r.dataset, ("", ""))
        m = re.search(r.pattern, text, re.I) if text else None
        if m and r.kind == "number":
            value, found = float(m.group(1).replace(",", ".")), True
        elif m:
            value, found = True, True
        else:
            value, found = defaults.get(r.key), False
        snippet = text[max(0, m.start() - 60): m.end() + 40].strip() if m else ""
        facts[r.key] = dict(value=value, unit=r.unit, meaning=r.meaning, found_in_document=found,
                            source=name or f"default (config.yaml domain.{r.key})", evidence=snippet)
    used = sorted({v["source"] for v in facts.values() if v["found_in_document"]})
    print(f"[docs] {sum(v['found_in_document'] for v in facts.values())}/{len(facts)} facts read from: {used or 'no documents'}")
    cfg.processed("domain_facts.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False))
    return facts


def load_facts(cfg: Config) -> dict[str, float | bool]:
    """Plain {key: value} view, falling back to config defaults if extraction never ran."""
    p = cfg.processed("domain_facts.json")
    if p.exists():
        return {k: v["value"] for k, v in json.loads(p.read_text()).items()}
    return dict(cfg.raw.get("domain", {}))
