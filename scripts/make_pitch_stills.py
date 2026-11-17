"""Clean map stills for the web pitch (docs/pitch/pitch.html), rendered by the real app.

    uv run python scripts/make_pitch_stills.py

Writes to docs/pitch/assets/:
  hook_today.jpg / hook_plan.jpg   Heilbronn today vs. the 98 % plan, same camera, no panel/labels
  hook_sites.json                  screen position (in % of the image) of every planned gateway in hook_plan.jpg,
                                   so the page can pulse radio rings exactly from the antennas
  backup.jpg                       one home heard by several gateways while its strongest gateway is switched off
  road_heilbronn.jpg               the 98 % plan for Heilbronn
  road_stuttgart.jpg               the 98 % plan for Stuttgart (open data only)
  unique_*.jpg                     street-level close-ups (backup, future city, Stuttgart) for the "why us" cards
Reuses the clock/CDP machinery of make_demo_video.py.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_demo_video import CDP, CHROME, CLOCK_JS, ROOT, Recorder, wait_http  # noqa: E402

PORT, CITY_PORT, DEBUG_PORT = 8776, 8777, 9343
OUT = ROOT / "docs/pitch/assets"
W, H = 1920, 1080
CLEAN_CSS = """#panel,#map-key,#tooltip,.maplibregl-ctrl,.maplibregl-ctrl-group,.maplibregl-ctrl-attrib,#why{display:none!important}"""
CLEAN_JS = """(() => { const st = document.createElement('style'); st.textContent = %s; document.head.appendChild(st);
  __hn.S.cleanShot = true; __hn.S.lm = 0;
  for (const l of __hn.map.getStyle().layers) if (l.type === 'symbol') __hn.map.setLayoutProperty(l.id, 'visibility', 'none');
  __hn.refresh(); })()""" % json.dumps(CLEAN_CSS)


async def shot(rec: Recorder, name: str, settle_ms=2500):
    for _ in range(int(settle_ms / 40)):            # let the camera finish and tiles draw
        await rec.tick(40)
        await asyncio.sleep(0.03)
    r = await rec.cdp.send("Page.captureScreenshot", format="jpeg", quality=90)
    (OUT / name).write_bytes(base64.b64decode(r["data"]))
    print("   wrote", name)


async def stills(rec: Recorder, base: str, city_base: str | None):
    js = rec.cdp.js
    planned = "__hn.S.planInfo && __hn.S.plan.length > 0"
    await rec.load(base + "/", "window.__hn && __hn.ready()", "the 3D map")
    await js(CLEAN_JS)
    cam = "{center:[9.198,49.139],zoom:12.95,pitch:46,bearing:-12}"
    await js(f"__hn.map.jumpTo({cam})")
    await shot(rec, "hook_today.jpg", 9000)  # long enough for the landmark intro to fade out
    await js("__hn.setPlanMode('target'); __hn.setTarget(0.98); __hn.run()")
    await rec.settle(planned, "the optimiser")
    await js(f"__hn.map.stop(); __hn.map.jumpTo({cam})")
    await shot(rec, "hook_plan.jpg", 4000)
    pts = await js("""__hn.S.plan.map(i => { const s = typeof i === 'number' ? __hn.sites[i] : i;
        const p = __hn.map.project([s.lon, s.lat]); return [+(100 * p.x / innerWidth).toFixed(2), +(100 * p.y / innerHeight).toFixed(2)]; })
        .filter(([x, y]) => x > 0 && x < 100 && y > 0 && y < 100)""", value=True)
    (OUT / "hook_sites.json").write_text(json.dumps(pts), encoding="utf-8")
    print(f"   {len(pts)} planned gateways in view")
    # wide plan view for the "what comes next" scene
    await js("__hn.map.jumpTo({center:[9.19,49.137],zoom:12.15,pitch:45,bearing:-8})")
    await shot(rec, "road_heilbronn.jpg", 4000)
    # backup: strongest gateway of one home switched off, the others still hear it
    b = await js("window.__bk = __hn.pickBackup(9.192, 49.136); __bk", value=True)
    if b:
        await js("__hn.setMode('red'); __hn.toggle(__bk.strongest); __hn.refresh(); __hn.showLinks(__bk.lon, __bk.lat);"
                 f"__hn.map.jumpTo({{center:[{b['lon']},{b['lat'] - 0.0015}],zoom:14.2,pitch:50,bearing:-10}})")
        await shot(rec, "backup.jpg", 4000)
        # close-up for the "unique" card: the home, its links and the switched-off gateway at street level
        await js(f"__hn.map.jumpTo({{center:[{b['lon']},{b['lat'] - 0.0008}],zoom:15.6,pitch:62,bearing:-18}})")
        await shot(rec, "unique_backup.jpg", 4000)
        await js("__hn.toggle(__bk.strongest); __hn.showLinks(null); __hn.setMode('cov'); __hn.refresh()")
    # future city close-up: planned districts built at worst-case height, covered by the future-proof plan
    if (await js("!!(__hn.setScenario)", value=True)):
        await js("__hn.setScenario('future'); __hn.refresh(); __hn.growFuture(1500)")
        await js("__hn.setPlanMode('target'); __hn.setTarget(0.98); __hn.run()")
        await rec.settle("__hn.S.scen === 'future' && " + planned, "the future plan")
        for _ in range(100):                      # the optimise button zooms out to all sites after a moment – let that run first
            await rec.tick(40)
            await asyncio.sleep(0.03)
        await js("__hn.map.stop(); __hn.map.jumpTo({center:[9.1705,49.1715],zoom:14.9,pitch:62,bearing:-20})")
        await shot(rec, "unique_future.jpg", 5000)
    if city_base:
        await rec.load(city_base + "/", "window.__hn && __hn.ready()", "the Stuttgart map")
        await js(CLEAN_JS)
        await js("__hn.setPlanMode('target'); __hn.setTarget(0.98); __hn.run()")
        await rec.settle(planned, "the Stuttgart plan", timeout=180)
        await js("const h = __hn.CITY.home; __hn.map.stop(); __hn.map.jumpTo({center:h.center,zoom:11.55,pitch:42,bearing:-8})")
        await shot(rec, "road_stuttgart.jpg", 5000)
        await js("__hn.map.jumpTo({center:[9.1795,48.7745],zoom:15.1,pitch:62,bearing:-22})")
        await shot(rec, "unique_stuttgart.jpg", 5000)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="hn_stills_"))
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    procs = [subprocess.Popen([sys.executable, "-m", "hnplan.cli", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    city = (ROOT / "cities/stuttgart/web_data/insights.json").exists()
    if city:
        procs.append(subprocess.Popen([sys.executable, "-m", "hnplan.cli", "city", "stuttgart", "serve", "--port", str(CITY_PORT)],
                                      cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}", f"--window-size={W},{H}",
                                   "--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--hide-scrollbars",
                                   "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                                   f"--user-data-dir={tmp / 'profile'}", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        wait_http(f"http://127.0.0.1:{PORT}/api/health")
        if city:
            wait_http(f"http://127.0.0.1:{CITY_PORT}/api/health")
        targets = json.loads(wait_http(f"http://127.0.0.1:{DEBUG_PORT}/json/list"))
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=None) as ws:
            cdp = CDP(ws)
            pump = asyncio.create_task(cdp.loop())
            await cdp.send("Page.enable")
            await cdp.send("Runtime.enable")
            await cdp.send("Page.addScriptToEvaluateOnNewDocument", source=CLOCK_JS)
            await cdp.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
            await stills(Recorder(cdp, tmp, 25), f"http://127.0.0.1:{PORT}",
                         f"http://127.0.0.1:{CITY_PORT}" if city else None)
            pump.cancel()
    finally:
        for p in procs:
            p.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
