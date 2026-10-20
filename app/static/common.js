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
  setInterval(() => {
    if (!last) return;
    const age = Date.now() - last.seen;
    if (age > FRAME_HOLD_MS) {
      img.style.visibility = "hidden";
      empty.style.display = "";
      label.textContent = `${name} · no fresh frame`;
      label.classList.add("held");
    } else {
      label.textContent = `${name} · cam ${last.camera_index + 1} · ${last.role}` + (age > FRAME_LEASE_MS ? ` · held ${(age / 1000).toFixed(1)}s` : "");
      label.classList.toggle("held", age > FRAME_LEASE_MS);
    }
  }, 200);
  // Ages use the browser's clock (arrival time), since the device clock may differ; snapshot frames carry their age.
  return frame => {
    last = { ...frame, seen: Date.now() - (frame.age_ms || 0) };
    img.src = `data:image/jpeg;base64,${frame.jpeg_base64}`;
    img.style.visibility = "";
    empty.style.display = "none";
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
    for (const k of ["host", "port", "reader_user", "writer_user"]) dlg.querySelector(`[name=${k}]`).value = s[k] ?? "";
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

on("snapshot", m => renderStatus(m.status));
on("status", renderStatus);
on("socket", m => { if (!m.up && $("#st-reader")) pill($("#st-reader"), "app offline", "bad"); });
