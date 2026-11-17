/* Connection watch (map + data page): the planner server runs only while its launcher window is open.
   When that window is closed the server stops – show it, and hide the notice again if it comes back. */
(function () {
  if (location.protocol === "file:") return;
  const bar = document.createElement("div");
  bar.className = "offline";
  bar.setAttribute("role", "alert");
  bar.hidden = true;
  bar.innerHTML = `<span class="offline-dot"></span><div><b>Disconnected – the planner has stopped.</b>
    Its window was closed. Double-click <code>start-planner.bat</code> (Heilbronn) or <code>start-city.bat</code> (other cities)
    to start it again; this page reconnects by itself.</div>`;
  const add = () => document.body.appendChild(bar);
  document.body ? add() : document.addEventListener("DOMContentLoaded", add);
  let missed = 0;
  async function beat() {
    let ok = false;
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 2500);
      ok = (await fetch("api/health", { cache: "no-store", signal: ctl.signal })).ok;
      clearTimeout(t);
    } catch (e) { ok = false; }
    missed = ok ? 0 : missed + 1;
    bar.hidden = missed < 2;
    document.documentElement.classList.toggle("is-offline", missed >= 2);
  }
  setInterval(beat, 4000);
})();
