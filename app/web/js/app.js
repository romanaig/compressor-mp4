// Compressor MP4 — UI. One state object; each area of the screen has its own functions.
import { api, query, uploadFile } from "./api.js";
import { clampPct, colorInfo, describeCodec, frameNumber, mb, reduction, shortFps, timecode } from "./format.js";
import { drawSamples, drawTimeMarks, markActiveSample, sampleAt, showPlayed } from "./timeline.js";

const $ = (id) => document.getElementById(id);
const DELAY_MS = 120;             // wait for the user to stop dragging before asking the server
const RENDER_POLL_MS = 500;
const FPS_PRESETS = [12, 15, 24];

const state = {
  config: null,                   // /api/config: extensions, caps, formats, slider steps
  video: null,                    // /api/info of the open video
  t: 0,
  settings: { pct: 50, cap: null, fps: null, format: "mp4" },   // cap/fps null = original
  preview: null,                  // {t, key} of the last preview shown
  busy: false,                    // copy, preview or render in progress
  zoom: null,                     // {fx, fy}: clicked point (0–1)
  playing: false,
  frameSeq: 0,                    // drops frames that arrive late
  sizeRequest: null,
};
const settingsKey = () => JSON.stringify(state.settings);
const videoFps = () => (state.video ? state.video.fps : 1);

// ---------------------------------------------------------------------------
// status, buttons, sliders
// ---------------------------------------------------------------------------

function msg(text, kind = "") {
  $("msg").textContent = text;
  $("msg").className = kind;
  $("statusDot").classList.remove("ok", "error");
  if (kind) $("statusDot").classList.add(kind);
}

function setBusy(yes) {
  state.busy = yes;
  $("statusDot").classList.toggle("busy", yes);
  updateButtons();
}

function updateButtons() {
  const has = !!state.video;
  $("btPreview").disabled = $("btRender").disabled = !has || state.busy;
  for (const id of ["timeline", "btBack", "btForward", "btPlay", "fps"]) $(id).disabled = !has;
}

/** Paints the filled part of the track (CSS --fill). */
function fillSlider(slider) {
  const min = Number(slider.min), max = Number(slider.max), v = Number(slider.value);
  slider.style.setProperty("--fill", `${max > min ? ((v - min) / (max - min)) * 100 : 0}%`);
}

function chip(text, { checked = false, disabled = false, title = "", onClick }) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "chip";
  b.textContent = text;
  b.setAttribute("role", "radio");
  b.setAttribute("aria-checked", String(checked));
  b.disabled = disabled;
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

/** x264 accepts fractional CRF (33.40); VP9 integers only (45). */
const describeCrf = (p) => `CRF ${p.format === "webm" ? p.crf.toFixed(0) : p.crf.toFixed(2)}`;

function markStale() {
  const p = state.preview;
  const stale = p && (Math.abs(p.t - state.t) > 1e-6 || p.key !== settingsKey());
  $("compScreen").classList.toggle("stale", !!stale);
}

// ---------------------------------------------------------------------------
// settings: compression, resolution cap, fps, format
// ---------------------------------------------------------------------------

let paramsTimer = null;
function onSettingsChange() {
  markStale();
  clearTimeout(paramsTimer);
  paramsTimer = setTimeout(showParams, DELAY_MS);
}

function setPct(value, source) {
  state.settings.pct = clampPct(value);
  if (source !== "slider") $("pct").value = state.settings.pct;
  if (source !== "number" || document.activeElement !== $("pctNum")) $("pctNum").value = state.settings.pct.toFixed(2);
  fillSlider($("pct"));
  onSettingsChange();
}

async function showParams() {
  if (!state.video) { $("params").textContent = "—"; return; }
  const key = settingsKey();
  try {
    const p = await api("/api/params" + query({ v: state.video.name, ...state.settings }));
    if (key !== settingsKey()) return;                       // changed while the request was in flight
    $("params").textContent = `${describeCrf(p)} · ${p.format === "webm" ? "Opus" : "AAC"} ${p.audio_kbps} kbps\n` +
                              `output ${p.width}×${p.height}${p.fps ? ` · ${shortFps(p.fps)} fps` : ""}`;
  } catch (e) {
    $("params").textContent = e.message;
  }
}

function drawPctMarks() {
  const marks = [["Min", 0], ...state.config.height_steps.map(([pct, side]) => [`${side}p (${pct}%)`, pct]), ["100%", 100]];
  $("pctMarks").replaceChildren(...marks.map(([text, pct]) => {
    const s = document.createElement("span");
    s.textContent = text;
    s.style.left = `${pct}%`;
    return s;
  }));
}

/** 1080p…144p buttons: cap on the shorter side. The button matching the original shows as checked when there is no cap. */
function buildCaps() {
  const v = state.video;
  const short = v ? Math.min(v.width, v.height) : null;
  const caps = state.config.caps;                           // largest to smallest
  const equivalent = short ? caps.filter((c) => c >= short).pop() : null;
  const pick = (cap) => () => {
    state.settings.cap = cap === null || cap >= short ? null : cap;
    buildCaps();
    onSettingsChange();
  };
  const chips = caps.map((c) => chip(`${c}p`, {
    checked: state.settings.cap === c || (state.settings.cap === null && c === equivalent),
    disabled: !short || (c > short && c !== equivalent),
    title: short && c > short ? "Never upscales" : `Shorter side up to ${c} px`,
    onClick: pick(c),
  }));
  if (short && short > caps[0]) {                           // video larger than 1080p: "Orig" keeps the size
    chips.unshift(chip("Orig", { checked: state.settings.cap === null, onClick: pick(null) }));
  }
  $("caps").replaceChildren(...chips);
}

function setFps(value) {
  const source = state.video && state.video.fps;
  state.settings.fps = value === null || !source || value >= source - 0.01 ? null : Math.max(state.config.min_fps, value);
  buildFps();
  onSettingsChange();
}

function buildFps() {
  const source = state.video ? state.video.fps : null;
  const current = state.settings.fps;
  const slider = $("fps");
  if (source) {
    slider.min = state.config.min_fps;
    slider.max = Math.ceil(source);
    slider.value = current ?? slider.max;
  }
  fillSlider(slider);
  const value = $("fpsValue");
  value.textContent = source ? `${shortFps(current ?? source)} fps ` : "—";
  if (source && current === null) value.append(Object.assign(document.createElement("small"), { textContent: "(Original)" }));
  $("fpsPresets").replaceChildren(
    ...FPS_PRESETS.map((f) => chip(`${f} fps`, {
      checked: current === f, disabled: !source || f >= source - 0.01,
      title: source && f >= source ? "Never raises the frame rate" : "", onClick: () => setFps(f) })),
    chip(`Orig (${source ? Math.round(source) : "—"})`, { checked: !!source && current === null, disabled: !source,
      onClick: () => setFps(null) }));
}

function buildFormats() {
  const formats = state.config.formats;
  $("formats").replaceChildren(...Object.entries(formats).map(([key, f]) => chip(f.extension.toUpperCase(), {
    checked: state.settings.format === key, title: f.label,
    onClick: () => { state.settings.format = key; buildFormats(); onSettingsChange(); } })));
  const current = formats[state.settings.format];
  $("codec").textContent = current.label;
  $("formatLabel").textContent = state.settings.format === "mp4" ? "MP4 (Default)" : `${state.settings.format.toUpperCase()} · ${current.label}`;
}

// ---------------------------------------------------------------------------
// time: timeline, raw frame, ⏪ ▶ ⏩
// ---------------------------------------------------------------------------

function showTime() {
  const v = state.video;
  const fps = videoFps();
  const total = Object.assign(document.createElement("span"), { className: "total", textContent: ` / ${timecode(v ? v.duration : 0, fps)}` });
  $("timecode").replaceChildren(timecode(state.t, fps), total);
  $("frameInfo").textContent = v ? `FRAME ${frameNumber(state.t, fps)} · ${fps.toFixed(2)} FPS` : "FRAME 0 · — FPS";
  if (!v) return;
  showPlayed($("played"), state.t, v.duration);
  const i = sampleAt(state.t, v);
  markActiveSample($("sampleMarks"), i);
  $("activeSample").textContent = i >= 0 ? `Active sample: #${i + 1} @ ${timecode(v.samples[i], fps)}` : "";
}

let frameTimer = null;
function setTime(value, immediate) {
  const duration = state.video ? state.video.duration : 0;
  state.t = Math.min(duration, Math.max(0, Number(value) || 0));
  if (Number($("timeline").value) !== state.t) $("timeline").value = state.t;
  showTime();
  markStale();
  clearTimeout(frameTimer);
  frameTimer = setTimeout(loadRawFrame, immediate ? 0 : DELAY_MS);
}

function loadRawFrame() {
  if (!state.video) return;
  const seq = ++state.frameSeq;
  const t = state.t;
  const img = new Image();
  img.onload = () => {
    if (seq !== state.frameSeq) return;                     // arrived late: a newer request exists
    $("rawImg").src = img.src;
    $("rawImg").classList.add("loaded");
    $("rawEmpty").hidden = true;
    $("rawTime").textContent = timecode(t, videoFps());
  };
  img.onerror = () => { if (seq === state.frameSeq) msg("Could not read that frame.", "error"); };
  img.src = "/api/frame" + query({ v: state.video.name, t: t.toFixed(3), _: seq });
}

function stepFrame(direction) {
  if (!state.video) return;
  pause();
  const fps = videoFps();
  setTime((frameNumber(state.t, fps) + direction) / fps, true);
}

async function togglePlay() {
  if (!state.video) return;
  if (state.playing) { pause(); return; }
  exitZoom();
  const video = $("rawVideo");
  const source = "/api/video" + query({ v: state.video.name });
  if (video.dataset.source !== source) { video.src = source; video.dataset.source = source; }
  video.currentTime = state.t;
  try {
    await video.play();
  } catch (_) {
    const ext = state.video.name.split(".").pop().toUpperCase();
    msg(`The browser cannot play ${ext}. Use ⏪ ⏩ or the timeline.`, "error");
    return;
  }
  state.playing = true;
  video.hidden = false;
  $("rawScreen").classList.add("playing");
  $("btPlay").querySelector("use").setAttribute("href", "#i-pause");
  requestAnimationFrame(followVideo);
}

function followVideo() {
  if (!state.playing) return;
  state.t = $("rawVideo").currentTime;
  $("timeline").value = state.t;
  $("rawTime").textContent = timecode(state.t, videoFps());
  showTime();
  markStale();
  requestAnimationFrame(followVideo);
}

/** Stops the video and goes back to the exact frame (ffmpeg PNG), the same one the preview will use. */
function pause() {
  if (!state.playing) return;
  const video = $("rawVideo");
  video.pause();
  state.playing = false;
  video.hidden = true;
  $("rawScreen").classList.remove("playing");
  $("btPlay").querySelector("use").setAttribute("href", "#i-play");
  const fps = videoFps();
  setTime(frameNumber(video.currentTime, fps) / fps, true);
}

// ---------------------------------------------------------------------------
// preview, size estimate, render
// ---------------------------------------------------------------------------

async function runPreview() {
  if (!state.video || state.busy) return;
  pause();
  const request = { v: state.video.name, t: state.t, ...state.settings };
  const key = settingsKey();
  setBusy(true);
  $("compScreen").classList.add("busy");
  msg("Generating preview…");
  try {
    const r = await api("/api/preview", request);
    const p = r.params;
    $("compImg").src = r.img;
    $("compImg").classList.add("loaded");
    $("compEmpty").hidden = true;
    state.preview = { t: request.t, key };                   // browser's own value: avoids rounding differences
    $("compTime").textContent = `${timecode(r.t, videoFps())} · ${r.pct.toFixed(2)} %`;
    $("compRes").textContent = ` · ${p.width}×${p.height}${p.fps ? ` · ${shortFps(p.fps)} fps` : ""}`;
    $("compCaption").textContent = `${p.codec.split(" / ")[0]} · ${describeCrf(p)}`;
    if (state.zoom) applyZoom();
    msg("Preview ready.", "ok");
    estimateSize(request);
  } catch (e) {
    msg("Preview failed: " + e.message, "error");
  } finally {
    setBusy(false);
    $("compScreen").classList.remove("busy");
    markStale();
  }
}

function clearSize(note) {
  const v = state.video;
  $("estOriginal").textContent = v ? mb(v.bytes) : "—";
  $("estSize").textContent = $("estReduction").textContent = "—";
  $("sizeBar").style.width = "0%";
  $("estNew").textContent = "New: —";
  $("estSaved").textContent = "Saved: —";
  $("estNote").textContent = note;
}

function showSize(r) {
  const original = r.original_bytes, estimated = r.estimate_bytes;
  $("estOriginal").textContent = mb(original);
  $("estSize").textContent = `≈ ${mb(estimated)}`;
  $("estReduction").textContent = `${reduction(r.reduction_pct)} at ${r.pct.toFixed(2)} %`;
  $("sizeBar").style.width = `${Math.min(100, (estimated / original) * 100)}%`;
  $("estNew").textContent = `New: ${mb(estimated)}`;
  $("estSaved").textContent = `Saved: ${mb(Math.max(0, original - estimated))}`;
  $("estNote").textContent = `${r.samples} ${r.samples === 1 ? "clip" : "clips"} of the video · typical error ±10 %`;
  $("compCaption").textContent = `${r.params.codec.split(" / ")[0]} · ≈ ${mb(estimated)} (est.)`;
}

/** Separate from the preview: the image shows first; the size comes later (it encodes 6 clips). */
async function estimateSize(request) {
  const mine = state.sizeRequest = JSON.stringify(request, ["v", "pct", "cap", "fps", "format"]);
  clearSize("Estimate: calculating size…");
  $("estSize").textContent = "calculating…";
  try {
    const { t: _t, ...withoutTime } = request;
    const r = await api("/api/estimate", withoutTime);
    if (mine === state.sizeRequest) showSize(r);           // otherwise: another setting was requested meanwhile
  } catch (e) {
    if (mine === state.sizeRequest) clearSize("Estimate failed: " + e.message);
  }
}

async function runRender() {
  if (!state.video || state.busy) return;
  pause();
  setBusy(true);
  $("btOutput").classList.remove("highlight");
  $("progress").style.width = "0%";
  msg(`Rendering ${state.settings.pct.toFixed(2)} %…`);
  try {
    const { job } = await api("/api/render", { v: state.video.name, ...state.settings });
    const j = await followRender(job);
    msg(`Done: output/${j.output} · ${mb(j.bytes)} · ${reduction(j.reduction_pct)}`, "ok");
    $("btOutput").classList.add("highlight");
  } catch (e) {
    msg("Render failed: " + e.message, "error");
  } finally {
    setBusy(false);
  }
}

async function followRender(job) {
  while (true) {
    await new Promise((ok) => setTimeout(ok, RENDER_POLL_MS));
    const j = await api("/api/render" + query({ job }));
    $("progress").style.width = j.progress + "%";
    if (j.state === "done") return j;
    if (j.state === "error") throw new Error(j.error);
    msg(`Rendering… ${j.progress.toFixed(2)} %`);
  }
}

// ---------------------------------------------------------------------------
// synced 1:1 zoom
// ---------------------------------------------------------------------------

const imageBoxes = () => document.querySelectorAll(".screen .img");

function applyZoom() {
  const w = state.video.width, h = state.video.height;
  for (const id of ["rawImg", "compImg"]) {                  // compressed scaled up to the original size
    $(id).style.width = w + "px";
    $(id).style.height = h + "px";
  }
  for (const box of imageBoxes()) {
    box.scrollLeft = state.zoom.fx * w - box.clientWidth / 2;
    box.scrollTop = state.zoom.fy * h - box.clientHeight / 2;
  }
}

function exitZoom() {
  if (!state.zoom) return;
  state.zoom = null;
  document.querySelector(".screens").classList.remove("zoom");
  for (const id of ["rawImg", "compImg"]) { $(id).style.width = ""; $(id).style.height = ""; }
}

function toggleZoom(e) {
  if (!state.video || !e.target.classList.contains("loaded")) return;
  if (state.zoom) { exitZoom(); return; }
  const r = e.target.getBoundingClientRect();
  state.zoom = { fx: (e.clientX - r.left) / r.width, fy: (e.clientY - r.top) / r.height };
  document.querySelector(".screens").classList.add("zoom");
  applyZoom();
}

let syncing = false;
function syncScroll(e) {
  if (!state.zoom || syncing) return;
  syncing = true;
  for (const other of imageBoxes()) {
    if (other !== e.currentTarget) { other.scrollLeft = e.currentTarget.scrollLeft; other.scrollTop = e.currentTarget.scrollTop; }
  }
  requestAnimationFrame(() => { syncing = false; });
}

// ---------------------------------------------------------------------------
// open a video: drag and drop
// ---------------------------------------------------------------------------

async function send(file) {
  await configReady;
  if (state.busy) { msg("Wait for the preview/render to finish.", "error"); return; }
  const extensions = state.config.extensions;
  const ext = (file.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
  if (!extensions.includes(ext)) {                          // refuse before copying (large files take a while)
    msg(`Not opened: format not accepted (${ext || "no extension"}). Use: ${extensions.join(", ")}`, "error");
    return;
  }
  pause();
  setBusy(true);
  $("progress").style.width = "0%";
  msg(`Copying ${file.name} to input/…`);
  try {
    const info = await uploadFile(file, (p) => {
      $("progress").style.width = p + "%";
      msg(`Copying… ${p.toFixed(2)} %`);
    });
    setBusy(false);
    openVideo(info);
  } catch (e) {
    setBusy(false);
    msg("Not opened: " + e.message, "error");
  }
}

function openVideo(v) {
  exitZoom();
  state.video = v;
  state.preview = null;
  state.sizeRequest = null;
  state.settings.cap = null;                                // cap and fps belonged to the previous video
  state.settings.fps = null;
  $("rawVideo").removeAttribute("src");
  $("rawVideo").dataset.source = "";

  const name = Object.assign(document.createElement("b"), { textContent: v.name });
  $("fileInfo").replaceChildren(name, ` · ${v.width}×${v.height} · ${shortFps(v.fps)} fps · ${timecode(v.duration)} · ${mb(v.bytes)}${v.audio ? "" : " · no audio"}`);
  $("timeline").max = v.duration;
  $("timeline").step = (1 / v.fps).toFixed(4);
  $("timeline").value = 0;
  drawTimeMarks($("timeMarks"), v.duration);
  drawSamples($("sampleMarks"), v);
  const n = v.samples.length;
  $("samplesText").lastChild.textContent = n === 1 ? "1 sample (short video) for a quick estimate"
                                                   : `${n} samples spread out for a quick estimate`;
  $("colorInfo").textContent = colorInfo(v);
  $("rawCaption").textContent = `Original · ${describeCodec(v)}`;
  $("rawRes").textContent = ` · ${v.width}×${v.height}`;

  $("compImg").classList.remove("loaded");
  $("compImg").removeAttribute("src");
  $("compEmpty").hidden = false;
  $("compTime").textContent = $("compRes").textContent = $("compCaption").textContent = "";
  clearSize("Press Preview to estimate the size.");
  $("progress").style.width = "0%";
  $("btOutput").classList.remove("highlight");
  buildCaps();
  buildFps();
  msg("Video ready. Pick a frame and press Preview.", "ok");
  updateButtons();
  setTime(0, true);
  setPct(state.settings.pct, "end");
}

let dragDepth = 0;                                          // dragenter/leave fire for every child element
function onDrag(e) {
  e.preventDefault();
  dragDepth += e.type === "dragenter" ? 1 : -1;
  if (dragDepth <= 0) dragDepth = 0;
  document.body.classList.toggle("dragging", dragDepth > 0);
}

function onDrop(e) {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  const file = e.dataTransfer && e.dataTransfer.files[0];
  if (file) send(file);
}

// ---------------------------------------------------------------------------
// keyboard: P preview, R render, space plays, ← → one frame
// ---------------------------------------------------------------------------

function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const target = e.target;
  if (target instanceof HTMLInputElement && target.type !== "range") return;   // typing the %
  const key = e.key.toLowerCase();
  if (key === "p") { e.preventDefault(); runPreview(); }
  else if (key === "r") { e.preventDefault(); runRender(); }
  else if (e.key === " " && !(target instanceof HTMLButtonElement)) { e.preventDefault(); togglePlay(); }
  else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !(target instanceof HTMLInputElement)) {
    e.preventDefault();
    stepFrame(e.key === "ArrowLeft" ? -1 : 1);
  }
}

// ---------------------------------------------------------------------------
// wiring and startup
// ---------------------------------------------------------------------------

function bindEvents() {
  $("pct").addEventListener("input", (e) => setPct(e.target.value, "slider"));
  $("pctNum").addEventListener("input", (e) => { if (e.target.value !== "") setPct(e.target.value, "number"); });
  $("pctNum").addEventListener("change", (e) => setPct(e.target.value, "end"));
  $("pctNum").addEventListener("blur", (e) => setPct(e.target.value, "end"));
  $("fps").addEventListener("input", (e) => setFps(Number(e.target.value)));
  $("timeline").addEventListener("input", (e) => { pause(); setTime(e.target.value, false); });
  $("timeline").addEventListener("change", (e) => setTime(e.target.value, true));
  $("btPreview").addEventListener("click", runPreview);
  $("btRender").addEventListener("click", runRender);
  $("btOutput").addEventListener("click", () => api("/api/open_output", {}).catch((e) => msg(e.message, "error")));
  $("btBack").addEventListener("click", () => stepFrame(-1));
  $("btForward").addEventListener("click", () => stepFrame(1));
  $("btPlay").addEventListener("click", togglePlay);
  $("rawVideo").addEventListener("ended", pause);
  $("rawImg").addEventListener("click", toggleZoom);
  $("compImg").addEventListener("click", toggleZoom);
  for (const box of imageBoxes()) box.addEventListener("scroll", syncScroll);
  window.addEventListener("dragenter", onDrag);
  window.addEventListener("dragleave", onDrag);
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", onDrop);
  window.addEventListener("keydown", onKey);
}

async function loadConfig() {
  state.config = await api("/api/config");
  $("version").textContent = `v${state.config.version} · ffmpeg ${state.config.ffmpeg.split("-")[0]}`;
  drawPctMarks();
  buildCaps();
  buildFps();
  buildFormats();
  setPct(state.settings.pct, "end");
}

bindEvents();
showTime();
updateButtons();
const configReady = loadConfig().catch((e) => msg("Server did not respond: " + e.message, "error"));
window.compressor = { send };                               // used by the automated tests (tests/)
