// Shared by every page. Server is the source of truth; clients only render.
const FMS = (() => {
  let skew = 0, state = null, ws = null, role = null;
  const subs = [];
  const LABEL = { auto: "Auto", transition: "Transition", shift1: "Shift 1", shift2: "Shift 2",
                  shift3: "Shift 3", shift4: "Shift 4", endgame: "Endgame" };

  const now = () => Date.now() / 1000 + skew;
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onopen = () => setConn(true);
    ws.onmessage = (e) => {
      state = JSON.parse(e.data);
      skew = state.now - Date.now() / 1000; // LAN latency is ms; good enough for a match clock
      subs.forEach((f) => f(state));
    };
    ws.onclose = () => { setConn(false); setTimeout(connect, 1000); };
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);

  function setConn(on) { const el = $(".conn"); if (el) el.classList.toggle("on", on); }

  function tokenKey() { return "fms_token_" + role; }

  async function api(path, body, method = "POST") {
    const r = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", "X-FMS-Token": localStorage.getItem(tokenKey()) || "" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && role) { localStorage.removeItem(tokenKey()); }
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }

  // Where are we in the match right now?
  function phase(cur, t = now()) {
    if (!cur || !cur.periods) return { name: "idle", label: cur ? "Ready" : "No match", remaining: null };
    const ps = cur.periods;
    const end = ps[ps.length - 1].end;
    if (t < ps[0].start) return { name: "pre", label: "Starting", remaining: ps[0].start - t };
    for (let i = 0; i < ps.length; i++) {
      const p = ps[i];
      if (t >= p.start && t < p.end) {
        return { ...p, label: LABEL[p.name], remaining: p.end - t, next: ps[i + 1] || null,
                 clock: p.name === "auto" ? p.end - t : end - t };
      }
    }
    if (t < ps[1].start) return { name: "pause", label: "Auto over", remaining: ps[1].start - t,
                                  next: ps[1], clock: end - ps[1].start };
    return { name: "post", label: cur.status === "running" ? "Counting last fuel" : "Match over", remaining: null, clock: 0 };
  }

  const mmss = (s) => {
    if (s == null) return "–:––";
    s = Math.max(0, Math.ceil(s));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };

  function toast(msg, err = false) {
    const el = document.createElement("div");
    el.className = "toast" + (err ? " err" : "");
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), err ? 4000 : 2200);
  }

  // Gate a page behind a PIN. Resolves once logged in.
  function gate(r, extraField) {
    role = r;
    return new Promise((resolve) => {
      if (localStorage.getItem(tokenKey())) return resolve();
      const box = document.createElement("div");
      box.className = "gate panel";
      box.innerHTML = `<h2>Enter the ${esc(r)} PIN</h2>
        ${extraField ? `<input id="gx" placeholder="${esc(extraField)}" autocomplete="off">` : ""}
        <input id="gp" type="password" inputmode="numeric" placeholder="PIN" autocomplete="off">
        <button class="primary" id="gb" style="width:100%">Log in</button>`;
      document.body.appendChild(box);
      const go = async () => {
        try {
          const j = await api("/api/login", { role: r, pin: $("#gp").value });
          localStorage.setItem(tokenKey(), j.token);
          if (extraField) localStorage.setItem("fms_name", $("#gx").value.trim() || "ref");
          box.remove();
          resolve();
        } catch (e) { toast(e.message, true); }
      };
      $("#gb").onclick = go;
      $("#gp").onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
  }

  return { connect, on: (f) => subs.push(f), api, phase, mmss, now, toast, gate, $, esc, LABEL,
           get state() { return state; }, setRole: (r) => (role = r) };
})();

// periodic re-render hook for clocks (state only arrives on change / 2 Hz)
function everyFrame(fn) { const loop = () => { fn(); setTimeout(loop, 100); }; loop(); }
