"""Record the pitch demo video of the planner, fully automatic.

    uv run python scripts/make_demo_video.py            -> docs/demo.mp4 (1920x1080, 25 fps, ~2 min)
    uv run python scripts/make_demo_video.py --fast     -> docs/demo_preview.mp4 (960x540, 12 fps, quick check)

How it works
  * starts the web app on a spare port and a headless Chrome, and drives the real app through the
    Chrome DevTools Protocol (window.__hn hook in web/app.js)
  * a fake clock is injected into the page before any script runs: requestAnimationFrame and
    performance.now only move when the recorder says so. Every frame = advance the clock by 1/fps,
    let map + deck.gl draw, take a screenshot. Smooth and deterministic on any computer, however slow.
  * captions (with chapter labels) and title / end cards are overlaid in the page and fade on the same clock
  * ffmpeg (must be on PATH) encodes the frames to H.264 MP4 – inserts straight into PowerPoint
All numbers in the captions are read from web/data/evidence.json (and cities/stuttgart/web_data/evidence.json for the
Stuttgart chapter, served on a second port), so the video always matches the data.
"""
from __future__ import annotations

import argparse
import re
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
HOLD_S, HOLD_LONG = 1.2, 0.8   # extra seconds per caption (+ more for two-line captions) – set by --hold
CAPLOG: list[dict] = []        # video time of every caption -> docs/demo_captions.json
PORT, CITY_PORT, DEBUG_PORT = 8766, 8767, 9333
CHROME = os.environ.get("CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")

# Injected before any page script: a clock that only moves when the recorder ticks it.
CLOCK_JS = """(() => {
  let now = 0, id = 0; const q = new Map(), hooks = [];
  Object.defineProperty(performance, 'now', { value: () => now, configurable: true });
  window.requestAnimationFrame = (cb) => { const i = ++id; q.set(i, cb); return i; };
  window.cancelAnimationFrame = (i) => { q.delete(i); };
  window.__onTick = (f) => hooks.push(f);
  window.__tick = (ms) => {
    now += ms;
    const cbs = [...q.values()]; q.clear();
    for (const cb of cbs) { try { cb(now); } catch (e) { console.error(e); } }
    for (const h of hooks) { try { h(now, ms); } catch (e) { console.error(e); } }
    return true;
  };
})();"""

OVERLAY_CSS = """
#vcap{position:fixed;left:calc(50% + 185px);bottom:54px;transform:translateX(-50%);z-index:50;max-width:1120px;
  padding:16px 28px 18px;border-radius:16px;background:rgba(10,12,16,.88);border:1px solid rgba(255,255,255,.12);
  color:#eceef1;font:600 29px/1.32 Geist,Segoe UI,sans-serif;letter-spacing:-.01em;text-align:center;
  box-shadow:0 18px 50px rgba(0,0,0,.55);opacity:0}
#vcap .ch{display:block;font:700 14px Geist,Segoe UI,sans-serif;letter-spacing:.16em;color:#7fb0ff;margin-bottom:6px}
#vcap b{color:#9cc2ff}
#vcap.q{border-color:rgba(245,165,36,.45);background:rgba(18,14,8,.9)}
#vcap.q .ch{color:#f5a524}
#vcap.q b{color:#ffd28a}
#vcur{position:fixed;left:0;top:0;z-index:70;width:30px;height:30px;pointer-events:none;opacity:0;filter:drop-shadow(0 3px 6px rgba(0,0,0,.6))}
#vrip{position:fixed;z-index:69;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;border:3px solid #9cc2ff;pointer-events:none;opacity:0}
#vcard{position:fixed;inset:0;z-index:60;display:grid;place-items:center;text-align:center;opacity:0;pointer-events:none;
  background:radial-gradient(ellipse at 50% 38%,#152647 0%,#0a0d14 55%,#08090b 100%)}
#vcard .k{font:700 20px Geist,Segoe UI,sans-serif;color:#7fb0ff;letter-spacing:.16em;text-transform:uppercase}
#vcard h1{font:700 78px/1.06 Geist,Segoe UI,sans-serif;color:#fff;letter-spacing:-.03em;margin:20px 0 22px}
#vcard p{font:500 30px/1.45 Geist,Segoe UI,sans-serif;color:#aab2be;margin:0}
#vcard .row{display:flex;gap:26px;justify-content:center;margin-top:44px}
#vcard .st{min-width:300px;padding:22px 26px;border-radius:18px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1)}
#vcard .st b{display:block;font:700 56px/1 Geist,Segoe UI,sans-serif;color:#fff;letter-spacing:-.03em}
#vcard .st b span{color:#4c8dff}
#vcard .st i{display:block;font:500 21px/1.35 Geist,Segoe UI,sans-serif;color:#9aa3ae;font-style:normal;margin-top:10px}
"""
OVERLAY_JS = """(() => { if (document.getElementById('vcap')) return;
  const st = document.createElement('style'); st.textContent = %s; document.head.appendChild(st);
  const c = document.createElement('div'); c.id = 'vcap'; document.body.appendChild(c);
  const k = document.createElement('div'); k.id = 'vcard'; document.body.appendChild(k);
  const cur = document.createElement('div'); cur.id = 'vcur';
  cur.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30"><path d="M4 2l15 11-6.5 1.2L16 21l-2.6 1.2-3.4-6.6L5 20z" fill="#fff" stroke="#0b0d10" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  document.body.appendChild(cur);
  const rip = document.createElement('div'); rip.id = 'vrip'; document.body.appendChild(rip);
  let cx = innerWidth * 0.62, cy = innerHeight * 0.55, fx = cx, fy = cy, tx = cx, ty = cy, cT = 0, cD = 1, cvis = 0, rT = -1;
  window.__cur = (x, y, ms) => { fx = cx; fy = cy; tx = x; ty = y; cT = 0; cD = Math.max(ms, 1); cvis = 1; };
  window.__curHide = () => { cvis = 0; };
  window.__click = () => { rT = 0; };
  let ct = 0, co = 0, kt = 0, ko = 0, pending = null;
  let pq = false;  // question style travels with the text, so a fading caption never changes colour
  window.__cap = (h, q) => { if (h && co > 0.05 && c.innerHTML !== h) { pending = h; pq = !!q; ct = 0; }
    else { ct = h ? 1 : 0; if (h) { c.innerHTML = h; c.classList.toggle('q', !!q); } } };
  window.__card = (h, instant) => { kt = h ? 1 : 0; if (h) k.innerHTML = h; if (instant) { ko = kt; k.style.opacity = ko; } };
  const step = (o, t, ms, dur) => o + Math.sign(t - o) * Math.min(Math.abs(t - o), ms / dur);
  __onTick((now, ms) => {
    co = step(co, ct, ms, 380);
    if (pending && co === 0) { c.innerHTML = pending; c.classList.toggle('q', pq); pending = null; ct = 1; }
    ko = step(ko, kt, ms, 600);
    c.style.opacity = co; k.style.opacity = ko;
    cT = Math.min(cD, cT + ms); const e = cT / cD, s = e < .5 ? 2 * e * e : 1 - Math.pow(-2 * e + 2, 2) / 2;
    cx = fx + (tx - fx) * s; cy = fy + (ty - fy) * s;
    cur.style.transform = `translate(${cx - 4}px,${cy - 2}px)`;
    cur.style.opacity = Math.max(0, Math.min(1, (parseFloat(cur.style.opacity) || 0) + (cvis ? 1 : -1) * ms / 250));
    if (rT >= 0) { rT += ms; const r = Math.min(1, rT / 450); rip.style.left = cx + 'px'; rip.style.top = cy + 'px';
      rip.style.transform = `scale(${1 + 2.6 * r})`; rip.style.opacity = 1 - r; if (r >= 1) rT = -1; }
  });
})()""" % json.dumps(OVERLAY_CSS)


class CDP:
    """Minimal Chrome DevTools Protocol client."""

    def __init__(self, ws):
        self.ws, self.n, self.pending = ws, 0, {}

    async def loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            fut = self.pending.pop(msg.get("id"), None)
            if fut:
                fut.set_result(msg)

    async def send(self, method, timeout=300, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        res = await asyncio.wait_for(fut, timeout)
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result", {})

    async def js(self, expr, timeout=300, value=False):
        """Run JS in the page. Actions return nothing (e.g. map.flyTo returns the whole map object, which can't be
        serialised); value=True returns the result of an expression (used for wait conditions)."""
        if not value:
            expr = f"(() => {{ {expr}; }})()"
        r = await self.send("Runtime.evaluate", timeout=timeout, expression=expr, awaitPromise=True, returnByValue=value)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error in: {expr[:80]}… -> {r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")


class Recorder:
    def __init__(self, cdp: CDP, frames: Path, fps: int):
        self.cdp, self.frames, self.fps, self.k, self.t0 = cdp, frames, fps, 0, time.time()

    async def tick(self, ms):
        await self.cdp.js(f"window.__tick && __tick({ms})")

    async def wait(self, cond: str, what: str, timeout=120, tick_ms=40):
        """Advance the clock in small steps (real time passes for network/compute) until cond is true."""
        end = time.time() + timeout
        while time.time() < end:
            if await self.cdp.js(f"!!({cond})", value=True):
                return
            if tick_ms:
                await self.tick(tick_ms)
            await asyncio.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {what}")

    async def settle(self, cond: str, what: str, timeout=60):
        """Wait in REAL time without moving the clock (e.g. the optimiser computing), so no animation is skipped."""
        await self.wait(cond, what, timeout, tick_ms=0)

    async def load(self, url: str, ready: str, what: str):
        await self.cdp.send("Page.navigate", url=url)
        await self.wait(ready, what, timeout=180)
        await self.cdp.js(OVERLAY_JS)
        for _ in range(60):                 # let map tiles arrive and draw (clock moves 1.2 s, real ~3 s)
            await self.tick(20)
            await asyncio.sleep(0.05)

    async def record(self, seconds: float):
        for _ in range(int(round(seconds * self.fps))):
            await self.tick(1000 / self.fps)
            shot = await self.cdp.send("Page.captureScreenshot", format="jpeg", quality=92)
            (self.frames / f"{self.k:05d}.jpg").write_bytes(base64.b64decode(shot["data"]))
            self.k += 1
            if self.k % (5 * self.fps) == 0:
                el = time.time() - self.t0
                print(f"   {self.k / self.fps:5.0f} s of video  ({el:.0f} s elapsed)", flush=True)


# ----------------------------------------------------------------------------- the story
def future_gap():
    """New meters in the planned districts that today's 98 % plan would not hear (expected count)."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from hnplan.config import load_config
        from hnplan.optimize import Planner
        cfg = load_config()
        pl, pf = Planner(cfg), Planner(cfg, scenario="future")
        ia = pf.existing + pf.site_index(pl.min_sites(0.98)["sites"])
        fut = pf.cells["future"].to_numpy().astype(bool)
        return int(round((pf.w[fut] * pf.miss(ia)[fut]).sum())), int(pf.w[fut].sum())
    except Exception as e:  # noqa: BLE001 – the video still works without this number
        print(f"   (future gap not computed: {e})")
        return None, None


async def story_pitch(rec: Recorder, base: str, city_base: str | None):
    """1-minute pitch cut: no captions, no cursor – three beats the presenter narrates live.
    0:00 Heilbronn today + 71 approved sites -> all 71 -> the optimal 27
    0:20 future city: planned districts rise, the plan adapts
    0:40 Stuttgart from open data: red -> 67 rooftops -> flyover"""
    js = rec.cdp.js
    E = json.loads((ROOT / "web/data/evidence.json").read_text(encoding="utf-8"))
    has_future = bool((E.get("future") or {}).get("plans"))
    planned = "__hn.S.planInfo && __hn.S.plan.length > 0"
    # ---- Heilbronn today: red streets, 71 grey candidate rings
    await rec.load(base + "/", "window.__hn && __hn.ready()", "the 3D map")
    await js("__hn.map.jumpTo({center:[9.19,49.13],zoom:11.7,pitch:40,bearing:-30});"
             "__hn.map.easeTo({center:[9.186,49.128],zoom:12.25,pitch:52,bearing:-16,duration:5500})")
    await rec.record(5.6)
    # ---- all 71 switched on
    await js("__hn.allSites()")
    await rec.record(4.2)
    # ---- the optimal 27 for 98 %
    await js("__hn.setPlanMode('target'); __hn.setTarget(0.98); __hn.run()")
    await rec.settle(planned, "the optimiser")
    await js("__hn.map.stop(); __hn.map.easeTo({center:[9.19,49.136],zoom:12.0,pitch:52,bearing:-4,duration:11000,easing:t=>t})")
    await rec.record(11.0)
    # ---- backup: one address heard by several gateways; its strongest one fails, the others still catch it
    b = await js("window.__bk = __hn.pickBackup(9.192, 49.136); __bk", value=True)
    if b:
        await js("__hn.setMode('red'); __hn.refresh(); __hn.map.stop();"
                 f"__hn.map.flyTo({{center:[{b['lon']},{b['lat'] - 0.004}],"
                 "zoom:14.6,pitch:58,bearing:-10,duration:2600})")
        await rec.record(2.9)
        await js("__hn.showLinks(__bk.lon, __bk.lat)")
        await rec.record(2.6)
        await js("__hn.toggle(__bk.strongest); __hn.showLinks(__bk.lon, __bk.lat)")
        await rec.record(3.4)
        await js("__hn.toggle(__bk.strongest); __hn.showLinks(null); __hn.setMode('cov'); __hn.refresh();"
                 "__hn.map.easeTo({center:[9.19,49.14],zoom:12.6,pitch:52,bearing:-8,duration:1600})")
        await rec.record(1.5)
        print(f"   backup address heard by {b['n']} gateways, strongest {b['strongest']}")
    else:
        print("   (no backup address found, scene skipped)")
    # ---- the future city
    if has_future:
        NORTH = (9.1705, 49.1765)
        await js("__hn.setScenario('future'); __hn.refresh();"
                 f"__hn.map.flyTo({{center:[{NORTH[0]},{NORTH[1]}],zoom:14.4,pitch:60,bearing:-20,duration:2600}})")
        await rec.record(2.4)
        await js("__hn.growFuture(3200)")
        await rec.record(7.0)
        k_today = await js("__hn.S.plan.length", value=True)
        await js("document.getElementById('btn-opt').click()")
        # wait for the NEW plan (the button runs after a short timeout and then zooms out to all sites)
        await rec.settle(f"__hn.S.scen === 'future' && __hn.S.plan.length > 0 && __hn.S.plan.length !== {k_today}", "the future-proof plan")
        await asyncio.sleep(0.3)
        await js(f"__hn.map.stop(); __hn.map.flyTo({{center:[{NORTH[0] + 0.004},{NORTH[1] - 0.012}],zoom:13.0,pitch:55,bearing:-10,duration:4500}})")
        await rec.record(8.0)
    # ---- Stuttgart
    if city_base:
        await rec.load(city_base + "/", "window.__hn && __hn.ready()", "the Stuttgart map")
        await js("const h = __hn.CITY.home; __hn.map.jumpTo({center:h.center,zoom:11.4,pitch:40,bearing:-28});"
                 "__hn.map.easeTo({center:h.center,zoom:12.0,pitch:52,bearing:-16,duration:4800})")
        await rec.record(5.0)
        await js("__hn.setPlanMode('target'); __hn.setTarget(0.98); __hn.run()")
        await rec.settle(planned, "the Stuttgart plan", timeout=180)
        await js("const h = __hn.CITY.home; __hn.map.stop(); __hn.map.easeTo({center:h.center,zoom:11.8,pitch:52,bearing:-4,duration:7500,easing:t=>t})")
        await rec.record(7.4)
        await js("__hn.map.flyTo({center:[9.1795,48.7775],zoom:14.4,pitch:62,bearing:-22,duration:6000})")
        await rec.record(7.0)


async def story(rec: Recorder, base: str, city_base: str | None):
    js = rec.cdp.js
    E = json.loads((ROOT / "web/data/evidence.json").read_text(encoding="utf-8"))
    O = E["overview"]
    plan = {round(p["target"] * 100): p for p in E["plans"]}
    k98, nC = plan[98]["k_mix"], O["candidates"]
    today = 100 * O["coverage_today"]
    FU = E.get("future") or {}
    f98 = next((p for p in FU.get("plans", []) if round(p["target"] * 100) == 98), None)
    FS = FU.get("summary", {})
    gap, newm = future_gap() if f98 else (None, None)

    # pacing for live narration: each new caption stays HOLD s longer (added to the next recorded stretch)
    HOLD = HOLD_S
    bonus = [0.0]
    base_record = rec.record

    async def record(sec):
        extra, bonus[0] = bonus[0], 0.0
        await base_record(sec + extra)
    rec.record = record

    async def say(ch, html, q=False):
        if html:
            # long (two-line) captions get one more second to read and narrate
            plain = re.sub(r"<[^>]+>", "", html.replace("<br>", " "))
            bonus[0] += HOLD + (HOLD_LONG if ("<br>" in html or len(plain) > 80) else 0.0)
            CAPLOG.append(dict(t=round(rec.k / rec.fps, 2), kind="question" if q else "answer", label=ch, text=plain))
        await js(f"__cap({json.dumps(f'<span class=ch>{ch}</span>{html}')}, {str(q).lower()})")
    ask = lambda html: say("QUESTION", html, q=True)  # noqa: E731
    card = lambda html, instant=False: js(f"__card({json.dumps(html)}, {str(instant).lower()})")  # noqa: E731
    kpi = lambda: js("document.getElementById('s-cov').textContent", value=True)  # noqa: E731

    async def cursor_to(expr: str, ms=900):
        """expr -> [x, y] in page pixels; the cursor glides there on the video clock."""
        x, y = await js(expr, value=True)
        await js(f"__cur({x}, {y}, {ms})")
        await rec.record(ms / 1000 + 0.15)

    async def click(selector: str, action: str | None = None, ms=900):
        await js(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block:'center'}})")
        await cursor_to(f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
                        f" return [r.left + r.width * 0.5, r.top + r.height * 0.55]; }})()", ms)
        await js("__click()")
        await js(action or f"document.querySelector({json.dumps(selector)}).click()")
        await rec.record(0.45)

    async def point_map(lon, lat, ms=900, dy=-34):
        await cursor_to(f"(() => {{ const p = __hn.map.project([{lon}, {lat}]); return [p.x, p.y + {dy}]; }})()", ms)

    # ---- hook
    await rec.load(base + "/", "window.__hn && __hn.ready()", "the 3D map")
    await card(f"""<div><div class="k">Future City Hackathon 2026 · HNVG challenge</div>
        <h1>Which gateways does<br>Heilbronn really need?</h1>
        <p>HNVG wants to read every water meter by radio (LoRaWAN).</p></div>""", instant=True)
    await js("__hn.map.jumpTo({center:[9.205,49.124],zoom:12.2,pitch:45,bearing:-30})")
    await rec.record(3.2)
    await card("")
    await js("__hn.map.easeTo({center:[9.207,49.124],zoom:13.1,pitch:55,bearing:-18,duration:3000}); __hn.playLandmarks()")
    await say("REAL DATA", "This is the real Heilbronn, here with <b>Hochschule Heilbronn</b> highlighted.<br>Every building comes from the state's 3D city model")
    await rec.record(4.0)
    await say("TODAY", f"<b>{O['existing']} gateways</b> hear only <b>{today:.0f} %</b> of {O['addresses']:,} basement water meters.<br>Red means not heard")
    await js("__hn.map.easeTo({center:[9.183,49.124],zoom:12.3,pitch:52,bearing:-12,duration:4500})")
    await rec.record(3.8)

    # ---- why not build all 71?
    await ask(f"HNVG has approved <b>{nC} new sites</b>. Why not simply build all of them?")
    await rec.record(2.2)
    await js("__hn.allSites(); __hn.map.easeTo({center:[9.19,49.138],zoom:11.95,pitch:50,bearing:-14,duration:2500})")
    await rec.record(1.0)
    await say("ALL 71", f"All {nC} sites give <b>{await kpi()}</b> coverage.<br>But that means {nC} gateways to buy, mount, power and maintain")
    await rec.record(3.8)

    # ---- the fewest for 98 %
    await ask("So what is the <b>fewest</b> that still reaches <b>98 %</b>?")
    await js("__hn.setPlanMode('target'); __hn.setTarget(0.98)")
    await rec.record(0.8)
    await click("#btn-opt")
    await rec.settle("__hn.S.planInfo && __hn.S.plan.length > 0", "the optimiser")
    await js("__curHide(); __hn.map.stop(); __hn.map.flyTo({center:[9.19,49.138],zoom:11.95,pitch:50,bearing:-16,duration:1800})")
    await say("THE ANSWER", f"<b>{k98} of {nC}</b> sites are enough for 98 %.<br><b>{nC - k98} sites never need to be built</b>. The numbers show the build order")
    await js("__hn.map.easeTo({bearing:-4,duration:6000,easing:t=>t})")
    await rec.record(4.6)

    # ---- can we trust it?
    await ask("But can we trust a computer model?")
    await js("__hn.map.easeTo({center:[9.215,49.128],zoom:12.55,pitch:58,bearing:-24,duration:2500})")
    await rec.record(1.8)
    await js("__hn.S.showMet = true; __hn.refresh(); __hn.showLinks(9.242, 49.131)")
    await say("EVIDENCE", f"It learned from <b>{O['receptions']:,} real radio receptions</b> of {O['meters']} meters (dots).<br>Every path is traced through"
                          f" {O['buildings']:,} buildings. Tested on gateways it had <b>never seen</b>: AUC {O['logo_auc']:.2f}")
    await rec.record(5.0)
    await js("__hn.showLinks(null); __hn.S.showMet = false; __hn.refresh()")

    # ---- what if a gateway fails? (on today's network: WT Böckingen is the most critical gateway)
    wt = "__hn.sites.find(x => x.name.includes('WT B'))"
    lon, lat = await js(f"[{wt}.lon, {wt}.lat]", value=True)
    await ask("And what if a gateway <b>fails</b>?")
    await js("__hn.reset()")
    await js(f"__hn.map.stop(); __hn.map.flyTo({{center:[{lon},{lat - 0.004}],zoom:13.1,pitch:55,bearing:-12,duration:2200}})")
    await rec.record(1.8)
    before = await kpi()
    await point_map(lon, lat)
    await js(f"__click(); __hn.toggle({wt}.id)")
    await rec.record(0.6)
    await say("GATEWAY DOWN", f"Today's network loses <b>WT Böckingen</b>, its most critical gateway:<br>coverage drops from {before} to <b>{await kpi()}</b>. Whole streets go red")
    await rec.record(3.6)
    await js("__hn.setPlanMode('target'); __hn.setTarget(0.98)")
    await click("#btn-opt")
    await rec.settle("__hn.S.planInfo && __hn.S.plan.length > 0", "re-plan without the failed gateway")
    k_fail = await js("__hn.S.plan.length", value=True)
    await js(f"__curHide(); __hn.map.stop(); __hn.map.flyTo({{center:[{lon + 0.012},{lat - 0.002}],zoom:12.4,pitch:52,bearing:-14,duration:2200}})")
    await say("RE-PLANNED", f"One click: the planner works around the failure.<br><b>{k_fail} sites</b> bring it back to <b>{await kpi()}</b>")
    await rec.record(3.6)
    await js("__hn.reset(); __hn.setPlanMode('target'); __hn.setTarget(0.98)")

    # ---- today's 98 % plan (needed for the future chapter)
    await js("__hn.run()")
    await rec.settle("__hn.S.planInfo && __hn.S.plan.length > 0", "the 98 % plan")
    await js("__hn.map.stop(); __hn.map.easeTo({center:[9.19,49.138],zoom:11.95,pitch:50,bearing:-14,duration:1600})")
    await rec.record(1.0)

    # ---- the future
    if f98:
        NORTH = (9.1705, 49.1765)
        await ask("Great for today. But Heilbronn is <b>growing</b>.<br>What about tomorrow?")
        await js("__hn.map.easeTo({bearing:-8,duration:3500,easing:t=>t})")
        await rec.record(2.2)
        await click("#scen [data-scen=future]", "__hn.setScenario('future'); __hn.refresh()")
        await js(f"__curHide(); __hn.map.flyTo({{center:[{NORTH[0]},{NORTH[1]}],zoom:14.5,pitch:60,bearing:-20,duration:2600}})")
        await rec.record(1.8)
        await js("__hn.growFuture(3200)")
        await say("THE FUTURE CITY", f"Heilbronn's official land use plans: <b>{FS.get('future_buildings', 0):,} new buildings</b> rise on today's farmland."
                                     "<br>Each one is a new water meter")
        await rec.record(4.4)
        lost = (f"<b>{gap:,} of {newm:,} new homes</b> could not be read" if gap else "the new districts are only partly heard")
        await say("THE GAP", f"With today's plan, {lost}.<br>Coverage falls to <b>{await kpi()}</b>")
        await js("__hn.map.easeTo({bearing:-6,duration:4500,easing:t=>t})")
        await rec.record(3.8)
        await click("#btn-opt")
        await rec.settle("__hn.S.scen === 'future' && __hn.S.plan.length > 0", "the future-proof plan")
        kf = await js("__hn.S.plan.length", value=True)
        await js(f"__curHide(); __hn.map.stop(); __hn.map.flyTo({{center:[{NORTH[0]},{NORTH[1] - 0.004}],zoom:13.6,pitch:56,bearing:-12,duration:2200}})")
        await say("FUTURE PROOF", f"Plan once for both: <b>{kf} sites</b>. New sites appear next to the new districts.<br>"
                                  f"<b>{await kpi()}</b> tomorrow, and already today")
        await rec.record(4.6)

    # ---- any city
    if city_base:
        C = json.loads((ROOT / "cities/stuttgart/web_data/evidence.json").read_text(encoding="utf-8"))
        CO = C["overview"]
        await ask("Does this only work for Heilbronn?")
        await rec.record(1.4)
        await rec.load(city_base + "/", "window.__hn && __hn.ready()", "the Stuttgart map")
        await js("__cap('<span class=ch>QUESTION</span>Does this only work for Heilbronn?', true)")
        await js("const h = __hn.CITY.home; __hn.map.jumpTo({center:h.center,zoom:11.6,pitch:40,bearing:-25});"
                 "__hn.map.easeTo({center:h.center,zoom:12.15,pitch:52,bearing:-15,duration:3500})")
        await rec.record(1.6)
        await say("STUTTGART", f"No operator data, only open state data: <b>{CO['addresses']:,} addresses</b>, {CO['buildings']:,} buildings.<br>"
                               f"The planner proposes its own <b>{CO['candidates']} rooftop sites</b>")
        await rec.record(3.6)
        await js("__hn.setPlanMode('target'); __hn.setTarget(0.98)")
        await click("#btn-opt")
        await rec.settle("__hn.S.planInfo && __hn.S.plan.length > 0", "the Stuttgart plan", timeout=180)
        ks = await js("__hn.S.plan.length", value=True)
        await js("__curHide(); const h = __hn.CITY.home; __hn.map.stop(); __hn.map.flyTo({center:h.center,zoom:11.75,pitch:50,bearing:-12,duration:2200})")
        await say("ANY CITY", f"<b>{ks} rooftops</b> reach 98 % of Stuttgart.<br>Drop a city's open data into a folder and get the plan in minutes")
        await js("__hn.map.easeTo({bearing:-2,duration:6000,easing:t=>t})")
        await rec.record(4.4)
        await js("__hn.map.flyTo({center:[9.1795,48.7775],zoom:14.3,pitch:62,bearing:-20,duration:3000})")
        await rec.record(2.0)

    # ---- end card: the journey
    await js("__cap('')")
    CAPLOG.append(dict(t=round(rec.k / rec.fps, 2), kind="card", label="END", text="Fewer gateways. Every meter read."))
    stg = (f"""<div class="st"><b><span>{ks}</span> rooftops</b><i>any city:<br>Stuttgart from open data</i></div>""" if city_base else "")
    fut = (f"""<div class="st"><b><span>{kf}</span> sites</b><i>ready for the<br>new districts</i></div>""" if f98 else "")
    await card(f"""<div><div class="k">Heilbronn LoRaWAN Planner</div>
        <h1 style="font-size:64px">Fewer gateways. Every meter read.</h1>
        <div class="row">
          <div class="st"><b><span>{k98}</span> of {nC}</b><i>sites reach 98 %,<br>{nC - k98} never built</i></div>
          <div class="st"><b>1 click</b><i>replans when<br>a gateway fails</i></div>
          {fut}{stg}
        </div>
        <p style="margin-top:34px;font-size:22px">Trained on {O['receptions']:,} real receptions · all evidence in an open dashboard</p></div>""")
    await rec.record(5.0)


# ----------------------------------------------------------------------------- orchestration
def wait_http(url, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.read()
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(url)


async def main(args):
    global HOLD_S, HOLD_LONG
    if args.hold is not None:
        HOLD_S = HOLD_LONG = args.hold
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH – install it (winget install Gyan.FFmpeg) and retry")
    w, h = (960, 540) if args.fast else (1920, 1080)
    fps = 12 if args.fast else args.fps
    tmp = Path(tempfile.mkdtemp(prefix="hn_video_"))
    frames = tmp / "frames"
    frames.mkdir()
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    server = subprocess.Popen([sys.executable, "-m", "hnplan.cli", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # GPU rendering (Direct3D via ANGLE): ~0.1 s per frame. Software rendering takes ~7 s per frame –
    # only as a fallback for computers without a usable graphics card (--software).
    city = None
    if not args.no_city and (ROOT / "cities/stuttgart/web_data/insights.json").exists():
        city = subprocess.Popen([sys.executable, "-m", "hnplan.cli", "city", "stuttgart", "serve", "--port", str(CITY_PORT)],
                                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gl = (["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] if args.software else
          ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-gpu-rasterization"])
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={DEBUG_PORT}", f"--window-size={w},{h}",
                               *gl, "--hide-scrollbars", "--mute-audio",
                               "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                               f"--user-data-dir={tmp / 'profile'}", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            await cdp.send("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
            rec = Recorder(cdp, frames, fps)
            print(f"[video] recording {w}x{h} @ {fps} fps …", flush=True)
            run_story = story_pitch if args.pitch else story
            await run_story(rec, f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{CITY_PORT}" if city else None)
            (ROOT / "docs").mkdir(exist_ok=True)
            if not args.pitch:
                (ROOT / "docs/demo_captions.json").write_text(json.dumps(dict(duration=round(rec.k / fps, 2), captions=CAPLOG),
                                                                         indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"[video] {rec.k} frames ({rec.k / fps:.0f} s) captured in {time.time() - rec.t0:.0f} s", flush=True)
            pump.cancel()
        rec_len = rec.k / fps
        out = ROOT / (args.out or ("docs/demo_preview.mp4" if args.fast else "docs/demo_pitch.mp4" if args.pitch else "docs/demo.mp4"))
        out.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", str(frames / "%05d.jpg"),
                        "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0, rec_len - 1.2):.2f}:d=1.2,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
                        "-crf", "21", "-movflags", "+faststart", str(out)], check=True)
        print(f"[video] written {out} ({out.stat().st_size / 1e6:.1f} MB)")
    finally:
        chrome.terminate()
        server.terminate()
        if city:
            city.terminate()
        try:
            chrome.wait(10)
        except Exception:
            chrome.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="quick low-res preview (960x540, 12 fps)")
    ap.add_argument("--pitch", action="store_true", help="1-minute pitch cut without captions -> docs/demo_pitch.mp4")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--software", action="store_true", help="render without the graphics card (very slow, fallback)")
    ap.add_argument("--hold", type=float, default=None, help="extra seconds each caption stays (default 0.3)")
    ap.add_argument("--no-city", action="store_true", help="skip the Stuttgart chapter")
    ap.add_argument("--out", default=None, help="output path relative to the project (default docs/demo.mp4)")
    asyncio.run(main(ap.parse_args()))
