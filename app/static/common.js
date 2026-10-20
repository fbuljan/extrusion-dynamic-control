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

function pill(el, text, state) {
  el.textContent = text;
  el.className = `pill ${state || ""}`;
}

function renderStatus(s) {
  const cls = v => (v === "connected" ? "ok" : v && v.startsWith("connect") ? "warn" : v === "not configured" ? "" : "bad");
  if ($("#st-reader")) pill($("#st-reader"), `reader: ${s.reader}`, cls(s.reader));
  if ($("#st-writer")) pill($("#st-writer"), `writer: ${s.writer}`, cls(s.writer));
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
      label.classList.add("held");
      return;
    }
    const fresh = sinceArrival <= last.lease;
    const ageS = ((last.source_age_ms || 0) + sinceArrival) / 1000;
    label.textContent = `${name} · cam ${last.camera_index + 1} (idx ${last.camera_index}) · ${last.role} · `
      + `${fresh ? "fresh" : "held"} · ${ageS.toFixed(1)} s old`;
    label.classList.toggle("held", !fresh);
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
  return res.json();
}

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

api("api/version").then(v => ($("#version").textContent = `v${v.version}`));
on("snapshot", m => renderStatus(m.status));
on("status", renderStatus);
on("socket", m => { if (!m.up && $("#st-reader")) pill($("#st-reader"), "app offline", "bad"); });
