// Shared WebSocket client and helpers. Paths stay relative so the app works behind the device reverse proxy too.
const FRAME_LEASE_MS = 400;  // organiser rule: a frame is "fresh" for 400 ms
const FRAME_HOLD_MS = 2000;  // a held frame may be shown, labelled, for up to 2 s

const handlers = {};
function on(type, fn) { (handlers[type] ||= []).push(fn); }
function dispatch(msg) { (handlers[msg.type] || []).forEach(fn => fn(msg)); }

function connectSocket() {
  const url = new URL("ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(url);
  ws.onmessage = e => dispatch(JSON.parse(e.data));
  ws.onclose = () => { dispatch({ type: "socket", up: false }); setTimeout(connectSocket, 1500); };
  ws.onopen = () => dispatch({ type: "socket", up: true });
}

const $ = sel => document.querySelector(sel);
const fmt = (v, digits = 1, unit = "") =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : `${Number(v).toFixed(digits)}${unit}`;

// Status LED: state is ok | warn | bad | off; the text always says the state too, so color is never the only cue.
function led(el, text, state) {
  if (!el) return;
  el.textContent = text;
  el.title = text;
  el.dataset.state = state || "off";
}

function renderStatus(s) {
  const state = v => (v === "connected" ? "ok" : v && v.startsWith("connect") ? "warn" : v === "not configured" ? "off" : "bad");
  led($("#st-reader"), `reader ${s.reader}`, state(s.reader));
  led($("#st-writer"), `writer ${s.writer}`, state(s.writer));
}

// Camera panel that honours the frame lease: fresh → shown, held → labelled, expired → cleared.
function cameraView(imgSel, labelSel, emptySel, name) {
  const img = $(imgSel), label = $(labelSel), empty = $(emptySel);
  let last = null;
  function update() {
    if (!last) return;
    const sinceArrival = Date.now() - last.seen;
    if (sinceArrival > FRAME_HOLD_MS) {
      img.style.visibility = "hidden";
      empty.style.display = "";
      label.textContent = `${name} · no fresh frame`;
      label.dataset.state = "none";
      return;
    }
    const fresh = sinceArrival <= last.lease;
    const ageS = ((last.source_age_ms || 0) + sinceArrival) / 1000;
    // Spans let compact views drop the redundant index and role; freshness and age always stay visible.
    const parts = [`cam ${last.camera_index + 1}`, `idx ${last.camera_index}`, last.role, fresh ? "fresh" : "held", `${ageS.toFixed(1)} s old`];
    const nodes = [Object.assign(document.createElement("b"), { textContent: name })];
    parts.forEach((text, i) => {
      nodes.push(" ", Object.assign(document.createElement("span"), { className: i === 1 ? "idx" : i === 2 ? "role" : "", textContent: text }));
    });
    label.replaceChildren(...nodes);
    label.title = [name, ...parts].join(" · ");
    label.dataset.state = fresh ? "fresh" : "held";
  }
  setInterval(update, 100);
  // Lease and ages are relative to arrival: the publisher's clock runs ~160 ms behind, so expires_utc_ms can't be
  // compared with local time. The server stamps age_ms on every frame it sends.
  return frame => {
    const lease = Math.min(FRAME_LEASE_MS, Math.max(0, (frame.expires_utc_ms - frame.published_utc_ms) || FRAME_LEASE_MS));
    last = { ...frame, lease, seen: Date.now() - (frame.age_ms || 0) };
    img.src = `data:image/jpeg;base64,${frame.jpeg_base64}`;
    img.style.visibility = "";
    empty.style.display = "none";
    update();
  };
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => null);
  if (data === null || (!res.ok && typeof data.error !== "string")) throw new Error(`HTTP ${res.status}`);
  return data;
}

// Operator control state (target, Auto, estimate, decision, pins): polled, because only widths/decisions ride the socket.
// A poll that started before a POST finished is dropped so it can't show the old state over the new one.
const CONTROL_TIMEOUT_MS = 3000;
let controlGen = 0, polling = false;

const controlState = async res => {
  const c = res.ok ? await res.json() : null;
  if (!c || typeof c.auto !== "boolean") throw new Error(`no control state (HTTP ${res.status})`);
  return c;
};

async function pollControl() {
  if (polling) return false;
  polling = true;
  const gen = controlGen;
  try {
    const c = await controlState(await fetch("api/control", { signal: AbortSignal.timeout(CONTROL_TIMEOUT_MS) }));
    if (gen === controlGen) dispatch({ type: "control", ...c });
    return true;
  } catch {
    if (gen === controlGen) dispatch({ type: "control", offline: true });
    return false;
  } finally { polling = false; }
}

// Resolves to the server's state, or {error}; {error, lost} when the answer never came and the state was re-read.
async function postControl(body) {
  try {
    const res = await fetch("api/control", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: AbortSignal.timeout(CONTROL_TIMEOUT_MS) });
    const data = await res.json();
    controlGen++;
    if (!res.ok && typeof data.error === "string") return data;
    if (!res.ok || typeof data.auto !== "boolean") throw new Error(`HTTP ${res.status}`);
    dispatch({ type: "control", ...data });
    return data;
  } catch {
    controlGen++;
    polling = false;
    return (await pollControl()) ? { error: "response lost, state re-read", lost: true } : { error: "app offline" };
  }
}
function startControlPoll(ms = 2000) { pollControl(); setInterval(pollControl, ms); }

function setupSettings() {
  const dlg = $("#settings");
  if (!dlg) return;
  $("#open-settings").onclick = async () => {
    const s = await api("api/settings");
    for (const k of ["host", "port", "reader_user", "writer_user", "controller_id"]) dlg.querySelector(`[name=${k}]`).value = s[k] ?? "";
    for (const k of ["reader_password", "writer_password"]) {
      const el = dlg.querySelector(`[name=${k}]`);
      el.value = "";
      el.placeholder = s[k] ? "stored — leave empty to keep" : "not set";
    }
    dlg.showModal();
  };
  dlg.querySelector("form").onsubmit = async e => {
    e.preventDefault();
    await api("api/settings", Object.fromEntries(new FormData(e.target)));
    dlg.close();
  };
  $("#close-settings").onclick = () => dlg.close();
}

api("api/version").then(v => ($("#version").textContent = `v${v.version}`)).catch(() => {});

// Per-viewer layout preferences only; never camera data.
const prefs = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(`em.${key}`)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`em.${key}`, JSON.stringify(value)); } catch { /* private mode: layout just isn't remembered */ }
  },
};

// Resize handle: pointer drag, arrow keys (±1, ±5 with Shift) and double-click reset.
function draggable(handle, { onDrag, onKey, onReset }) {
  handle.addEventListener("pointerdown", e => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add("dragging");
    const move = ev => onDrag(ev);
    const up = () => {
      handle.removeEventListener("pointermove", move);
      document.body.classList.remove("dragging");
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, { once: true });
    handle.addEventListener("pointercancel", up, { once: true });
  });
  if (onKey) handle.addEventListener("keydown", e => {
    const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
    if (step) { e.preventDefault(); onKey(step * (e.shiftKey ? 5 : 1)); }
  });
  if (onReset) handle.addEventListener("dblclick", onReset);
}
on("snapshot", m => renderStatus(m.status));
on("status", renderStatus);
on("socket", m => { if (!m.up) led($("#st-reader"), "app offline", "bad"); });
