/* SurgAnn Annotator — Roboflow 式（工具優先、先畫後選類別）。
 * 不變式：一切以 frame 為準；時間只用於顯示（time = frame / fps）。
 * 後端 API 不變；此檔為介面重寫。 */
"use strict";

// ------------------------------------------------------------- 常數 / 狀態 --
const GUTTER = 150, RULER_H = 20, OVERVIEW_H = 12, LANE_H = 22, ROW_PAD = 5, HEAD_H = 18;
const SHAPE_ICON = { bbox: "▭", polygon: "⬠", point: "•", ellipse: "⬭", polyline: "〰" };
const PALETTE = [
  "#4dc9b0","#e6a23c","#5b9bd5","#c678dd","#98c379","#e06c75","#56b6c2","#d19a66",
  "#7f9cf5","#f471b5","#34d399","#f87171","#60a5fa","#fbbf24","#a78bfa","#4ade80",
  "#fb923c","#38bdf8","#e879f9","#a3e635","#f43f5e","#22d3ee","#facc15","#818cf8",
];
const SPATIAL_TOOLS = ["bbox", "polygon", "point", "ellipse", "polyline"];
// TOOLS.lb / KEYS 皆存 i18n key；顯示時經 window.t() 翻譯（切換語言後 reload 即生效）。
const TOOLS = [
  { k: "select", ic: "⬚", lb: "tool.select", key: "V" },
  { k: "temporal", ic: "⏱", lb: "tool.temporal", key: "T" },
  { sep: true },
  { k: "bbox", ic: "▭", lb: "tool.bbox", key: "B" },
  { k: "polygon", ic: "⬠", lb: "tool.polygon", key: "P" },
  { k: "point", ic: "•", lb: "tool.point", key: "O" },
  { k: "ellipse", ic: "⬭", lb: "tool.ellipse", key: "E" },
  { k: "polyline", ic: "〰", lb: "tool.polyline", key: "L" },
  { sep: true },
  { k: "smart", ic: "✨", lb: "tool.smart", key: "S" },
];
// [左欄按鍵（i18n key 或純 ASCII 字面）, 說明 i18n key]；ASCII 字面經 t() fallback 原樣回傳。
const KEYS = [
  ["V", "k.V"], ["T", "k.T"],
  ["T → Enter", "k.T_enter"],
  ["E", "k.E"],
  ["kk.dblclick", "k.dblclick"],
  ["B / P / O / E / L", "k.BPOEL"],
  ["S", "k.S"],
  ["kk.s_enter", "k.S_enter"],
  ["kk.ctrlz_smart", "k.ctrlz_smart"],
  ["kk.ldbl", "k.ldbl"],
  ["kk.altdrag", "k.alt_drag"],
  ["Space", "k.space"], ["← / →", "k.arrows"],
  ["Ctrl+← / Ctrl+→", "k.ctrl_arrows"], ["Shift+←/→", "k.shift_arrows"],
  ["Enter", "k.enter"], ["Esc", "k.esc"],
  ["kk.zoombtn", "k.zoombtn"],
  ["Del", "k.del"], ["N", "k.N"],
  ["kk.ctrl_click", "k.ctrl_click"],
  ["Ctrl+Z", "k.ctrlz"],
  ["Ctrl+Shift+Z / Ctrl+Y", "k.redo"],
  ["Ctrl+C / Ctrl+V", "k.copy_paste"],
  ["kk.shift_click", "k.shift_click"],
  ["kk.gutter_check", "k.gutter_check"],
  ["kk.multi_del", "k.multi_del"],
  ["kk.multi_relabel", "k.multi_relabel"],
  ["kk.gutter_collapse", "k.gutter_collapse"],
  ["kk.multi_copy", "k.multi_copy"],
  ["kk.eye", "k.eye"],
  ["kk.ctrlwheel_video", "k.ctrl_wheel_video"],
  ["kk.wheel_video", "k.wheel_video"],
  ["kk.wheel_timeline", "k.wheel_timeline"],
  ["kk.ctrlwheel_timeline", "k.ctrl_wheel_timeline"],
  ["kk.splitter", "k.splitter"],
];

const S = {
  cfg: null, video: null, anns: [], curFrame: 0,
  browse: false,       // 純瀏覽模式（URL 無 ?p=）：只播影片與看 metadata，不載任何標註
  project: null,       // 標註隔離的專案（URL ?p= / ?project=；缺 = 瀏覽模式，絕不 fallback）
  annotator: null,     // 目前標註者顯示名（localStorage 記憶；所有 POST 附帶）
  tool: "select",
  view: { start: 0, ppf: 0 }, fitPpf: 0,
  vzoom: { scale: 1, tx: 0, ty: 0 },
  sel: null, editMode: false,   // E：選取後才可進編輯模式（開放拖拉把手/邊界）
  hidden: new Set(),
  hiddenLevels: new Set(),   // 分層隱藏：level（phase/action/event/triplet/spatial）
  hiddenShapes: new Set(),   // 分層隱藏：spatial shape type（bbox/polygon/…）
  hiddenLabels: new Set(),   // 分層隱藏：單一 label（key = `${layer}/${label}`）
  selSet: new Set(),         // shift+左鍵多選（批次複製/刪除/改類別）
  collapsed: new Set(),      // 軌道收合（root/temporal、root/spatial、group/<layer>、shape/<shape>）——只摺疊畫面，不隱藏標註
  draw: null,          // 進行中的 spatial 繪製 {tool, pts, anchor, stage}
  sam: null,           // smart 工具 prompt session {frame, points[], labels[], box, polygon, score, loading, queued, drag, history[]}；換幀/換工具/Esc 即清空
  trace: null,         // 編輯模式 Alt+拖曳重繪 polygon 邊緣 {target, base, pts[[x,y],…], undoBefore}
  pending: null,       // 已畫、待選類別 {kind, shape, points | start,end}
  edit: null,          // 形狀編輯拖曳
  lastClass: { spatial: null, temporal: { layer: "phase", label: null } },
  clip: [], showFrames: false, hoverX: null, drag: null, mouseV: null,
  tmpTemporal: null,   // 時間軸拖曳建立 temporal {start, end}
  autoClass: false,    // N：畫完自動套用上次類別（略過選類別彈窗）
};

const $ = (id) => document.getElementById(id);
const video = $("video"), canvas = $("timeline"), ctx = canvas.getContext("2d");
const overlay = $("overlay"), octx = overlay.getContext("2d"), videoZoom = $("videoZoom");

// ------------------------------------------------------------------- utils --
const fps = () => (S.video ? S.video.fps : 25);
const nFrames = () => (S.video ? S.video.n_frames : 0);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const isSpatialTool = () => SPATIAL_TOOLS.includes(S.tool);
const isDrawishTool = () => isSpatialTool() || S.tool === "smart";   // 會在影片上畫/點的工具
const labelColor = new Map();

function fmtTime(frame) {
  const f = frame % Math.round(fps());
  let s = Math.floor(frame / fps());
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(h)}:${p(m)}:${p(s)}.${p(f)}`;
}
function toast(msg, ms = 2200) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms);
}
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) { let d = ""; try { d = (await r.json()).detail || ""; } catch {} throw new Error(`${r.status} ${d}`); }
  return r.status === 204 ? null : r.json();
}
function layerLabels(layer) {
  if (layer === "triplet") return [S.cfg.taxonomy.triplet.segment_label];
  if (layer === "spatial") return (S.cfg.taxonomy.spatial?.labels || []).map(l => l.name);
  return S.cfg.taxonomy[layer] || [];
}
function colorOf(label, layer) {
  const key = `${layer}/${label}`;
  if (layer === "spatial") {
    const l = (S.cfg.taxonomy.spatial?.labels || []).find(x => x.name === label);
    if (l?.color) return l.color;
  }
  if (!labelColor.has(key)) labelColor.set(key, PALETTE[labelColor.size % PALETTE.length]);
  return labelColor.get(key);
}
const temporalLayers = () => S.cfg.layers.filter(l => l !== "spatial");

// --------------------------------------------------------------- frame ------
function setFrame(f) {
  f = clamp(Math.round(f), 0, nFrames() - 1);
  video.currentTime = (f + 0.5) / fps();
}
function frameFromMediaTime(t) { return clamp(Math.floor(t * fps() + 1e-4), 0, nFrames() - 1); }
function frameLoop() {
  const tick = (f) => {
    if (f !== S.curFrame) {
      S.curFrame = f;
      if (S.browse) { updateTransport(); return; }   // 瀏覽模式：只更新播放控制列
      if (S.sam && S.sam.frame !== f) samClear();    // SAM session 綁定幀：換幀即清空
      // 時段標註中：播放頭走到哪、時段就縮放到哪（起點之前 = 往前延伸起點）
      if (S.tmpTemporal && !S.pending) {
        if (f >= S.tmpTemporal.start) S.tmpTemporal.end = f;
        else S.tmpTemporal.start = f;
        updateTmpBar();
      }
      followPlayhead(); updateTransport(); draw(); drawOverlay(); renderRight();
    }
  };
  if (video.requestVideoFrameCallback) {
    const cb = (_n, meta) => { tick(frameFromMediaTime(meta.mediaTime)); video.requestVideoFrameCallback(cb); };
    video.requestVideoFrameCallback(cb);
  } else setInterval(() => tick(frameFromMediaTime(video.currentTime)), 40);
  video.addEventListener("seeked", () => tick(frameFromMediaTime(video.currentTime)));
}

// ------------------------------------------------------------------ 載入 ----
async function boot() {
  const qs = new URLSearchParams(location.search);
  const want = qs.get("v");
  const pid = qs.get("p") || qs.get("project");
  if (!want) { location.href = "/"; return; }
  const vids = await api("GET", "/api/videos");
  const v = vids.find(x => x.video_id === want) || null;
  if (!v) { toast(t("app.video_notfound", want)); setTimeout(() => { location.href = "/"; }, 1500); return; }
  // URL 沒帶專案 = 純瀏覽模式：不 fallback 任何專案、不載任何標註（標註一律走專案入口）
  if (!pid) { await enterBrowseMode(v); return; }
  try { S.cfg = await api("GET", `/api/config?project=${encodeURIComponent(pid)}`); }
  catch (e) {
    toast(t("app.proj_load_fail", pid, e.message), 4000);
    await enterBrowseMode(v); return;
  }
  S.project = pid;
  $("curProject").textContent = pid;
  await initAnnotators();
  buildTools(); wireUI(); frameLoop();
  await loadVideo(v);
}

// ---- 純瀏覽模式（影片庫入口）：播放器 + metadata + 所屬專案清單，無任何標註功能 ----
async function enterBrowseMode(v) {
  S.browse = true; S.project = null; S.video = v;
  document.title = t("app.browse_suffix", v.video_id);
  $("curVideo").textContent = v.video_id;
  const badge = $("curProject");
  badge.textContent = t("app.browse_mode");
  badge.title = t("app.browse_badge_title");
  // 隱藏所有標註 UI（時間軸/工具列/時段列/批次列/分隔線/標註跳轉鈕/標註者選擇器）
  for (const id of ["tools", "splitter", "timelineWrap", "tmpBar", "batchBar",
                    "btnPrevEv", "btnNextEv", "btnPrevKp", "btnNextKp", "zoomBtn"])
    $(id)?.classList.add("hidden");
  $("annotatorSel").classList.add("hidden");
  document.querySelector('label[for="annotatorSel"]')?.classList.add("hidden");
  wireUI(); frameLoop();
  video.src = v.url;
  updateTransport();
  // 右側面板 → 影片資訊 + 所屬專案（標註入口一律經過專案）
  let projects = [], series = [];
  try {
    [projects, series] = await Promise.all([
      api("GET", "/api/projects"), api("GET", "/api/series")]);
  } catch {}
  const sName = (sid) => (series.find(s => s.series_id === sid) || {}).name || sid;
  const projName = (id) => (projects.find(p => p.project_id === id) || {}).name || id;
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const dur = (sec) => { const m = Math.floor(sec / 60), s = Math.round(sec % 60); return `${m}:${String(s).padStart(2, "0")}`; };
  const rows = [
    [t("app.info.video_id"), `<code>${esc(v.video_id)}</code>`],
    [t("app.info.series"), v.series ? `${esc(sName(v.series))}（<code>${esc(v.series)}</code>）` : "—"],
    [t("app.info.duration"), `${dur(v.duration_sec)}（${v.n_frames} frames）`],
    [t("app.info.resolution"), `${v.width}×${v.height}`],
    [t("app.info.fps"), `${v.fps}`],
    [t("app.info.status"), esc(v.status || "—")],
    [t("app.info.ingested_at"), esc((v.ingested_at || "—").slice(0, 19).replace("T", " "))],
  ].map(([k, val]) => `<div class="field"><label>${k}</label><span>${val}</span></div>`).join("");
  const plist = (v.projects || []).map((p) =>
    `<div class="field"><span class="badge ptype">${esc(p)}</span>&nbsp;${esc(projName(p))}` +
    `<span class="spacer"></span><a class="dl-link" href="app.html?v=${encodeURIComponent(v.video_id)}&p=${encodeURIComponent(p)}">${t("app.info.open_here")}</a></div>`
  ).join("") || `<div class="field dim">${t("app.info.no_project")}</div>`;
  $("rightPanel").innerHTML =
    `<div class="panel"><h3>${t("app.info.title")} <span class="dim">${t("app.browse_mode")}</span></h3>${rows}</div>` +
    `<div class="panel"><h3>${t("app.info.projects")}</h3>` +
    `<div class="field dim">${t("app.info.browse_hint")}</div>${plist}</div>`;
}

// ---- 標註者選擇器（名冊 + 新增；選擇記在 localStorage，所有 POST 附帶）----
let annotators = [];
async function initAnnotators() {
  try { annotators = await api("GET", "/api/annotators"); } catch { annotators = []; }
  if (!annotators.length) {   // 名冊空：以後端預設名建立第一位（u001）
    try { annotators = [await api("POST", "/api/annotators", { name: S.cfg.annotator })]; }
    catch { annotators = [{ annotator_id: "", name: S.cfg.annotator }]; }
  }
  const saved = localStorage.getItem("surgann_annotator");
  S.annotator = annotators.some(a => a.name === saved) ? saved : annotators[0].name;
  renderAnnotatorSel();
}
function renderAnnotatorSel() {
  const sel = $("annotatorSel"); sel.innerHTML = "";
  for (const a of annotators) {
    const o = document.createElement("option"); o.value = a.name; o.textContent = a.name;
    sel.appendChild(o);
  }
  const add = document.createElement("option");
  add.value = "__new__"; add.textContent = t("app.new_annotator");
  sel.appendChild(add);
  sel.value = S.annotator;
  sel.onchange = async () => {
    if (sel.value === "__new__") {
      const name = (prompt(t("app.new_annotator_prompt")) || "").trim();
      if (name) {
        try {
          const a = await api("POST", "/api/annotators", { name });
          if (!annotators.some(x => x.name === a.name)) annotators.push(a);
          S.annotator = a.name;
          localStorage.setItem("surgann_annotator", S.annotator);
          toast(t("app.toast.annotator", S.annotator), 1500);
        } catch (e) { toast(e.message); }
      }
      renderAnnotatorSel(); return;
    }
    S.annotator = sel.value;
    localStorage.setItem("surgann_annotator", S.annotator);
    toast(t("app.toast.annotator_set", S.annotator), 2200);
  };
}
// 所有「新增標註」POST 的共同欄位（project_id + annotator）
const annBody = (b) => ({ ...b, project_id: S.project, annotator: S.annotator });
// PATCH / DELETE 單筆標註的 URL：一律帶 ?project=（後端做專案歸屬檢查，不符 404）
const annUrl = (id) => `/api/annotations/${id}?project=${encodeURIComponent(S.project)}`;
async function loadVideo(v) {
  cancelDraw(); samClear(); S.trace = null; S.pending = null; S.sel = null; S.editMode = false; S.tmpTemporal = null;
  S.selSet.clear(); S.hidden.clear(); S.hiddenLevels.clear(); S.hiddenShapes.clear(); S.hiddenLabels.clear(); S.collapsed.clear();
  clearUndo();   // undo 歷史不跨影片
  updateTmpBar(); closeEditPop();
  S.vzoom = { scale: 1, tx: 0, ty: 0 }; applyVzoom();
  S.video = v; S.anns = []; S.curFrame = 0;
  $("curVideo").textContent = v.video_id;
  document.title = t("app.title_video", v.video_id);
  video.src = v.url;
  S.anns = await api("GET", `/api/videos/${v.video_id}/annotations?project=${encodeURIComponent(S.project)}`);
  resizeCanvas();
  S.fitPpf = usableW() / v.n_frames; S.view = { start: 0, ppf: S.fitPpf };
  setTool("select"); updateTransport(); draw(); drawOverlay(); renderRight();
}

// ------------------------------------------------------------ 工具列 --------
function buildTools() {
  const bar = $("tools"); bar.innerHTML = "";
  for (const t of TOOLS) {
    if (t.sep) { const s = document.createElement("div"); s.className = "tool-sep"; bar.appendChild(s); continue; }
    const b = document.createElement("div");
    b.className = "tool"; b.dataset.tool = t.k; b.title = window.t("tool.tip", window.t(t.lb), t.key);
    b.innerHTML = `<span class="ic">${t.ic}</span><span class="lb">${window.t(t.lb)}</span>`;
    // 「時段」是單次性動作鈕（開始/取消錄製），不是常駐工具
    b.onclick = () => (t.k === "temporal" ? toggleTemporalRec() : setTool(t.k));
    bar.appendChild(b);
  }
}
// 工具列 active 狀態：一般工具看 S.tool；「時段」鈕只在錄製中亮起（單次性）
function refreshToolbar() {
  const rec = !!(S.tmpTemporal && !S.pending);
  document.querySelectorAll("#tools .tool").forEach(b =>
    b.classList.toggle("active", b.dataset.tool === "temporal" ? rec : b.dataset.tool === S.tool));
}
function setTool(k) {
  S.tool = k;
  if (S.draw && S.draw.tool !== k) cancelDraw();
  if (k !== "smart") samClear();        // 換工具＝清空 SAM prompt session
  else samCheckStatus();                // 進 smart 工具：非阻斷檢查模型是否就緒
  updateSamBar();                       // 精細度滑桿列只在 smart 工具下顯示
  refreshToolbar();
  overlay.classList.toggle("drawing", isDrawishTool());
  drawOverlay();
}
// T / 時段鈕：開始時段標註（起點＝目前 frame）；錄製中再按一次＝取消。
// 完成（assignClass）或取消後自動回到非啟用狀態；非錄製狀態點時間軸只是 seek。
function toggleTemporalRec() {
  if (S.pending) return;   // 類別選單開著時不動作
  if (S.tmpTemporal) {
    S.tmpTemporal = null; updateTmpBar(); draw();
    toast(t("app.toast.temporal_cancel"), 1200); return;
  }
  video.pause();
  S.tmpTemporal = { start: S.curFrame, end: S.curFrame };
  updateTmpBar(); draw();
  toast(t("app.toast.temporal_start", S.curFrame), 2500);
}

// =========================================================== spatial 繪製 ===
function sizeOverlay() {
  if (!video.videoWidth) return;
  overlay.width = video.videoWidth; overlay.height = video.videoHeight;
  overlay.style.left = `${video.offsetLeft}px`; overlay.style.top = `${video.offsetTop}px`;
  overlay.style.width = `${video.offsetWidth}px`; overlay.style.height = `${video.offsetHeight}px`;
  clampVzoom(); applyVzoom();   // 版面變動後重新夾平移範圍 + 重繪 overlay + 同步捲動 bar
}
video.addEventListener("loadedmetadata", sizeOverlay);
window.addEventListener("resize", sizeOverlay);
function evToVideo(e) {
  const r = overlay.getBoundingClientRect();
  return [clamp((e.clientX - r.left) / r.width * overlay.width, 0, overlay.width),
          clamp((e.clientY - r.top) / r.height * overlay.height, 0, overlay.height)];
}

// keypoint 插值
function shapeAt(a, f) {
  const kps = a.attrs.keypoints;
  if (!kps?.length || f < a.start_frame || f > a.end_frame) return null;
  if (a.attrs.interpolate === false) { const kp = kps.find(k => k.frame === f); return kp ? kp.points : null; }
  if (f <= kps[0].frame) return kps[0].points;
  const last = kps[kps.length - 1];
  if (f >= last.frame) return last.points;
  for (let i = 0; i < kps.length - 1; i++) {
    const k1 = kps[i], k2 = kps[i + 1];
    if (k1.frame <= f && f <= k2.frame) {
      if (k1.points.length !== k2.points.length) return k1.points;
      const t = (f - k1.frame) / (k2.frame - k1.frame);
      return k1.points.map((p, j) => p + (k2.points[j] - p) * t);
    }
  }
  return last.points;
}
function upsertKp(obj, frame, points) {
  const i = obj.keypoints.findIndex(k => k.frame === frame);
  if (i >= 0) obj.keypoints[i] = { frame, points };
  else { obj.keypoints.push({ frame, points }); obj.keypoints.sort((p, q) => p.frame - q.frame); }
}
function tracePath(c, shape, p) {
  c.beginPath();
  if (shape === "bbox") c.rect(p[0], p[1], p[2] - p[0], p[3] - p[1]);
  else if (shape === "ellipse") c.ellipse(p[0], p[1], Math.max(1, p[2]), Math.max(1, p[3]), 0, 0, 6.2832);
  else if (shape === "point") c.arc(p[0], p[1], 6, 0, 6.2832);
  else { c.moveTo(p[0], p[1]); for (let i = 2; i < p.length; i += 2) c.lineTo(p[i], p[i + 1]); if (shape === "polygon") c.closePath(); }
}
function vertexHandles(shape, p) {
  if (shape === "bbox") return [[p[0], p[1]], [p[2], p[1]], [p[2], p[3]], [p[0], p[3]]];
  if (shape === "ellipse") return [[p[0] + p[2], p[1]], [p[0] - p[2], p[1]], [p[0], p[1] + p[3]], [p[0], p[1] - p[3]]];
  if (shape === "point") return [];
  const h = []; for (let i = 0; i < p.length; i += 2) h.push([p[i], p[i + 1]]); return h;
}
function isHidden(a) {
  if (S.hidden.has(a.ann_id)) return true;
  if (S.hiddenLevels.has(a.layer)) return true;
  if (S.hiddenLabels.has(`${a.layer}/${a.label}`)) return true;
  if (a.layer === "spatial" && S.hiddenShapes.has(a.attrs.shape)) return true;
  return false;
}

function drawOverlay() {
  if (!overlay.width) return;
  octx.clearRect(0, 0, overlay.width, overlay.height);
  const lw = 2 / S.vzoom.scale;
  for (const a of S.anns.filter(x => x.layer === "spatial")) {
    if (isHidden(a)) continue;
    const pts = shapeAt(a, S.curFrame); if (!pts) continue;
    const c = colorOf(a.label, "spatial"), seld = a.ann_id === S.sel;
    octx.lineWidth = seld ? lw * 1.7 : lw; octx.strokeStyle = c; octx.fillStyle = c + "26";
    tracePath(octx, a.attrs.shape, pts); if (a.attrs.shape !== "polyline") octx.fill(); octx.stroke();
    octx.font = `${13 / S.vzoom.scale}px system-ui`; octx.fillStyle = c;
    octx.fillText(a.label, (pts[0] ?? 0) + 4, (pts[1] ?? 0) - 5);
    if (seld && S.editMode) { octx.fillStyle = "#fff"; for (const [hx, hy] of vertexHandles(a.attrs.shape, pts)) octx.fillRect(hx - 4 / S.vzoom.scale, hy - 4 / S.vzoom.scale, 8 / S.vzoom.scale, 8 / S.vzoom.scale); }
    if (S.selSet.has(a.ann_id)) { octx.save(); octx.setLineDash([7 / S.vzoom.scale, 4 / S.vzoom.scale]); octx.strokeStyle = "#4da3ff"; octx.lineWidth = lw * 1.8; tracePath(octx, a.attrs.shape, pts); octx.stroke(); octx.restore(); }
  }
  // pending（已畫待選類別）
  if (S.pending?.kind === "spatial") {
    const c = "#ff8c00"; octx.setLineDash([6 / S.vzoom.scale, 4 / S.vzoom.scale]);
    octx.strokeStyle = c; octx.lineWidth = lw; octx.fillStyle = c + "30";
    tracePath(octx, S.pending.shape, S.pending.points); if (S.pending.shape !== "polyline") octx.fill(); octx.stroke();
    octx.setLineDash([]);
  }
  // smart（SAM）prompt session：polygon 預覽虛影 + prompt 點/框 + score + loading
  if (S.sam && S.tool === "smart") {
    const s = S.sam, k = S.vzoom.scale, sc = "#38bdf8";
    if (s.polygon?.length >= 3) {   // 預覽虛影：半透明填色 + 虛線邊（與已落地標註視覺區隔）
      octx.save(); octx.setLineDash([6 / k, 4 / k]);
      octx.strokeStyle = sc; octx.lineWidth = 2 / k; octx.fillStyle = sc + "2e";
      octx.beginPath(); octx.moveTo(s.polygon[0][0], s.polygon[0][1]);
      for (let i = 1; i < s.polygon.length; i++) octx.lineTo(s.polygon[i][0], s.polygon[i][1]);
      octx.closePath(); octx.fill(); octx.stroke(); octx.restore();
      if (s.score != null) {        // score（兩位小數）標在預覽左上角
        const xs = s.polygon.map(p => p[0]), ys = s.polygon.map(p => p[1]);
        octx.font = `${12 / k}px ui-monospace,monospace`; octx.fillStyle = sc;
        octx.fillText(t("app.sam.accept_hint", s.score.toFixed(2)),
          Math.min(...xs) + 4 / k, Math.max(12 / k, Math.min(...ys) - 6 / k));
      }
    }
    const dashRect = (x1, y1, x2, y2) => { octx.save(); octx.setLineDash([5 / k, 3 / k]); octx.strokeStyle = sc; octx.lineWidth = 1.5 / k; octx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1)); octx.restore(); };
    if (s.box) dashRect(s.box[0], s.box[1], s.box[2], s.box[3]);
    if (s.drag) { const [ax, ay] = s.drag.anchor, [dx, dy] = s.drag.cur; if (Math.abs(dx - ax) > 4 && Math.abs(dy - ay) > 4) dashRect(ax, ay, dx, dy); }
    for (let i = 0; i < s.points.length; i++) {   // 正點綠、負點紅
      octx.beginPath(); octx.arc(s.points[i][0], s.points[i][1], 5 / k, 0, 6.2832);
      octx.fillStyle = s.labels[i] === 1 ? "#22c55e" : "#ef4444"; octx.fill();
      octx.lineWidth = 1.5 / k; octx.strokeStyle = "#fff"; octx.stroke();
    }
    if (s.loading && performance.now() - (s.loadingSince || 0) > 150) { octx.font = `${13 / k}px system-ui`; octx.fillStyle = sc; octx.fillText(t("app.sam.inferring"), 8 / k, 18 / k); }
  }
  // 編輯模式 Alt+拖曳重繪：滑鼠軌跡預覽線（放開套用）
  if (S.trace && S.trace.pts.length > 1) {
    const k = S.vzoom.scale;
    octx.save(); octx.strokeStyle = "#f471b5"; octx.lineWidth = 2.5 / k;
    octx.beginPath(); octx.moveTo(S.trace.pts[0][0], S.trace.pts[0][1]);
    for (let i = 1; i < S.trace.pts.length; i++) octx.lineTo(S.trace.pts[i][0], S.trace.pts[i][1]);
    octx.stroke(); octx.restore();
  }
  // 繪製中預覽 + 十字準星
  if (S.draw && S.mouseV) {
    const [mx, my] = S.mouseV; octx.strokeStyle = "#ff8c00"; octx.lineWidth = lw;
    if (S.draw.stage === "drag" && S.draw.anchor) {
      const [ax, ay] = S.draw.anchor;
      if (S.draw.tool === "bbox") octx.strokeRect(Math.min(ax, mx), Math.min(ay, my), Math.abs(mx - ax), Math.abs(my - ay));
      else if (S.draw.tool === "ellipse") { octx.beginPath(); octx.ellipse((ax + mx) / 2, (ay + my) / 2, Math.max(1, Math.abs(mx - ax) / 2), Math.max(1, Math.abs(my - ay) / 2), 0, 0, 6.2832); octx.stroke(); }
    }
    if (S.draw.stage === "poly" && S.draw.pts.length) {
      octx.beginPath(); octx.moveTo(S.draw.pts[0], S.draw.pts[1]);
      for (let i = 2; i < S.draw.pts.length; i += 2) octx.lineTo(S.draw.pts[i], S.draw.pts[i + 1]);
      octx.lineTo(mx, my); octx.stroke(); octx.fillStyle = "#ff8c00";
      for (let i = 0; i < S.draw.pts.length; i += 2) octx.fillRect(S.draw.pts[i] - 3, S.draw.pts[i + 1] - 3, 6, 6);
    }
    octx.strokeStyle = "#ffffff88"; octx.lineWidth = 1 / S.vzoom.scale;
    octx.beginPath(); octx.moveTo(mx, 0); octx.lineTo(mx, overlay.height); octx.moveTo(0, my); octx.lineTo(overlay.width, my); octx.stroke();
  }
}

function normBbox(p) { return [Math.min(p[0], p[2]), Math.min(p[1], p[3]), Math.max(p[0], p[2]), Math.max(p[1], p[3])]; }
function cancelDraw() { S.draw = null; drawOverlay(); }
function beginPending(shape, points, extra) {
  S.pending = { kind: "spatial", shape, points, frame: S.curFrame, ...(extra || {}) };
  S.draw = null; drawOverlay();
  if (S.autoClass && S.lastClass.spatial) { assignClass("spatial", S.lastClass.spatial); return; }  // N：略過選類別
  const r = overlay.getBoundingClientRect();
  openClassPop("spatial", r.left + r.width / 2, r.top + 60);
}
function finishPoly() {
  if (!S.draw) return;
  const need = S.draw.tool === "polygon" ? 6 : 4;
  if (S.draw.pts.length >= need) beginPending(S.draw.tool, S.draw.pts.slice());
  else toast(t("app.toast.pts_insufficient"));
}

// ====================================== smart（SAM 輔助）prompt session ====
// A1 佈局：前端只發 fetch、收 polygon（推論全在後端）。session 綁定目前幀，
// 換幀/換工具/Esc 即清空；Enter 接受 → 走既有「先畫後選類別」流程落地。
function samNewSession() {
  return { frame: S.curFrame, points: [], labels: [], box: null,
           polygon: null, score: null, loading: false, queued: false, drag: null,
           history: [] };   // prompt 動作堆疊（Ctrl+Z 逐步撤銷用）
}
// Ctrl+Z（smart 工具）：撤銷上一個 prompt 動作（點/框）並重發 predict。
// 回傳是否有消化此按鍵；session 無動作時回 false → 放行給全域標註 undo。
function samUndo() {
  const s = S.sam;
  if (!s || !s.history.length) return false;
  const a = s.history.pop();
  if (a.type === "point") { s.points.pop(); s.labels.pop(); }
  else s.box = a.prev;                       // box 撤銷＝還原前一個框（可能為 null）
  if (!s.points.length && !s.box) {          // 提示全撤＝清預覽（session 保留在此幀）
    s.polygon = null; s.score = null;
    updateSamBar(); drawOverlay();
  } else samPredict();
  return true;
}
function samClear() {
  if (!S.sam) return;
  S.sam = null; updateSamUI(); updateSamBar(); drawOverlay();
}
function updateSamUI() { overlay.classList.toggle("sam-loading", !!S.sam?.loading); }
// 點擊落在預覽 polygon 內＝自動負點（把點擊處從遮罩扣掉）；Alt＝強制負點。
// 判定用 canvas isPointInPath（與 hitShape 同法；座標同為影片原生像素）。
function samPointInPreview(x, y) {
  const poly = S.sam?.polygon;
  if (!poly || poly.length < 3) return false;
  octx.save(); octx.beginPath();
  octx.moveTo(poly[0][0], poly[0][1]);
  for (let i = 1; i < poly.length; i++) octx.lineTo(poly[i][0], poly[i][1]);
  octx.closePath();
  const inside = octx.isPointInPath(x, y);
  octx.restore();
  return inside;
}
// ---- 精細度滑桿（polygon 簡化強度 epsilon_px）----
// null＝自動模式（不帶 epsilon_px，後端自動調整到 15–60 頂點）；
// 拖過滑桿＝手動模式（完全尊重滑桿值），記 localStorage 跨 session 沿用。
let samEps = (() => {
  const v = parseFloat(localStorage.getItem("surgann_sam_eps") || "");
  return Number.isFinite(v) ? clamp(v, 0.5, 12) : null;
})();
let samEpsTimer = null;
function updateSamBar() {
  const bar = $("samBar"); if (!bar) return;
  bar.classList.toggle("hidden", S.tool !== "smart" || S.browse);
  if (samEps != null) $("samEps").value = samEps;
  $("samEpsVal").textContent = samEps == null ? t("app.sam_auto") : `${samEps.toFixed(1)}px`;
  $("samEpsAuto").disabled = samEps == null;
  const n = S.sam?.polygon?.length;
  $("samNPts").textContent = n ? t("app.sam.npts", n) : "";
  $("samOk").disabled = !(n >= 3);   // 有可接受的預覽才亮「✓ 確認」
}
$("samEps").addEventListener("input", () => {   // 拖動中 throttle ~120ms 重發 predict
  samEps = clamp(parseFloat($("samEps").value), 0.5, 12);
  localStorage.setItem("surgann_sam_eps", String(samEps));
  updateSamBar();
  if (samEpsTimer) return;
  samEpsTimer = setTimeout(() => { samEpsTimer = null; samPredict(); }, 120);
});
// 拖完滑桿放開＝立刻釋放焦點——否則焦點停在 <input range>，全域快捷鍵
// （含 Enter 確認）會被 keydown 的 INPUT 早退規則吃掉（使用者回報的「Enter 沒反應」根因）
$("samEps").addEventListener("change", () => $("samEps").blur());
$("samEpsAuto").onclick = () => {
  samEps = null; localStorage.removeItem("surgann_sam_eps");
  $("samEpsAuto").blur();   // 釋放焦點：避免之後按 Enter 觸發瀏覽器「重按此鈕」預設行為
  updateSamBar(); samPredict();
};
$("samOk").onclick = () => { $("samOk").blur(); samAccept(); };   // ✓ 確認＝等同 Enter
// 每次 prompt 變更（加點/畫框）→ 整個 session 全量 POST；請求中再點＝排隊最後一次
async function samPredict() {
  const s = S.sam;
  if (!s || (!s.points.length && !s.box)) { drawOverlay(); return; }
  if (s.loading) { s.queued = true; return; }
  s.loading = true; s.loadingSince = performance.now(); updateSamUI(); drawOverlay();
  // 同幀後續點擊實測 ~50–60ms：⏳ 文字延遲 150ms 才顯示，避免快速回應時閃爍
  setTimeout(() => { if (S.sam === s && s.loading) drawOverlay(); }, 180);
  const body = { video_id: S.video.video_id, frame: s.frame,
                 points: s.points, labels: s.labels, box: s.box };
  if (samEps != null) body.epsilon_px = samEps;   // 手動模式才帶；自動模式由後端調頂點數
  try {
    const r = await fetch("/api/sam/predict", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    if (r.ok) {
      const out = await r.json();
      if (S.sam === s) {   // 過期 session（已換幀/清空）的回應直接丟棄
        if (Array.isArray(out.polygon) && out.polygon.length >= 3) { s.polygon = out.polygon; s.score = out.score; }
        else {   // 契約：mask 為空 → polygon:[]（非錯誤）——清除預覽、非阻斷提示
          s.polygon = null; s.score = null;
          toast(t("app.toast.sam_no_shape"), 2400);
        }
      }
    } else if (r.status === 409) {
      toast(t("app.toast.sam_not_ready409"), 3200);
    } else {
      let d = ""; try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      console.error("SAM predict failed", r.status, d);
      toast(t("app.toast.sam_fail", r.status, d), 3200);
    }
  } catch (err) {
    console.error("SAM predict connection failed", err);
    toast(t("app.toast.sam_conn_fail", err.message), 3000);
  } finally {
    if (S.sam === s) {
      s.loading = false; updateSamUI(); updateSamBar();
      if (s.queued) { s.queued = false; samPredict(); } else drawOverlay();
    }
  }
}
// Enter：接受預覽 → 既有先畫後選類別流程（assignClass 落地為標準 spatial polygon）
function samAccept() {
  const s = S.sam;
  if (!s?.polygon || s.polygon.length < 3) { toast(t("app.toast.sam_no_preview"), 2000); return; }
  const flat = [];
  for (const p of s.polygon) flat.push(Math.round(p[0]), Math.round(p[1]));
  S.sam = null; updateSamUI(); updateSamBar();
  beginPending("polygon", flat, { source: "sam" });
}
// 進 smart 工具時的非阻斷就緒檢查（後端 /api/sam/status 永遠回 200）
async function samCheckStatus() {
  try {
    const st = await (await fetch("/api/sam/status")).json();
    if (!st.ready) toast(t("app.toast.sam_not_ready", st.detail), 3200);
  } catch {}
}

// overlay 事件——掛在 document：畫 bbox/polygon 時點在影像範圍外也有效，座標由
// evToVideo() 自動 clamp 到影像邊界（取最近的邊界點）。
// 為何掛 document 而非 videoArea：影像高度貼滿 videoArea 時（左右留黑邊的常見情況），
// 「超出上/下邊界」的點擊會落在 topbar / transport 等 videoArea 之外的元素上，掛在
// videoArea 永遠收不到事件 → 上下 clamp 失效。掛 document 後上下與左右行為一致。
const NON_DRAW_SEL = "button,input,select,textarea,a,canvas,.vsb,#tools,#rightPanel," +
  "#classPop,#editPop,#menuDrop,#splitter,#timelineWrap,#timeDisplay,#tmpBar,#batchBar,#toast,#samBar,.modal-overlay";
// videoArea 之外的點擊要視為繪圖點擊的條件：繪圖工具啟用中，且沒點在任何互動元件上
const outsideDrawOk = (e) => e.button === 0 && isDrawishTool() && !S.pending && !e.target.closest(NON_DRAW_SEL);
const inVideoArea = (e) => $("videoArea").contains(e.target) && !e.target.closest(".vsb,#samBar");
// 左鍵快速連點兩下同一標註（選取工具）＝選取並進入編輯模式（等同 E）。
let ldbl = null;   // 上一次左鍵點中標註 {t, x, y, annId}（client 座標；400ms 內位移 ≤6px 且同一標註＝連點）
document.addEventListener("mousedown", (e) => {
  if (!S.video || S.browse) return;   // 瀏覽模式：無選取/繪製互動
  const inArea = inVideoArea(e);
  if (!inArea && !outsideDrawOk(e)) return;
  // 影像外（黑邊）的點擊只在繪製情境下才處理，避免誤觸選取/清除
  if (inArea && e.target !== overlay && !S.draw && !(isDrawishTool() && !S.pending)) return;
  const [mx, my] = evToVideo(e);
  // 右鍵一律不進繪製/選取流程（右鍵無標註功能；避免誤觸畫框/選取）
  if (e.button === 2) return;
  // shift+左鍵：多選既有形狀（批次複製/刪除）
  if (e.shiftKey && !S.draw && !S.pending) { const hit = hitShape(mx, my); if (hit) { e.preventDefault(); toggleSel(hit.ann_id); return; } }
  // smart（SAM）工具：mousedown 先記起點，mouseup 判定「點」或「拖曳成框」
  if (S.tool === "smart" && !S.pending && e.button === 0) {
    e.preventDefault();
    if (!S.sam || S.sam.frame !== S.curFrame) S.sam = samNewSession();
    S.sam.drag = { anchor: [mx, my], cur: [mx, my], alt: e.altKey };
    drawOverlay(); return;
  }
  // 繪製工具
  if (isSpatialTool() && !S.pending) {
    e.preventDefault();
    if (["bbox", "ellipse"].includes(S.tool)) { S.draw = { tool: S.tool, stage: "drag", anchor: [mx, my], pts: [] }; }
    else if (S.tool === "point") beginPending("point", [mx, my]);
    else { if (!S.draw) S.draw = { tool: S.tool, stage: "poly", pts: [] }; S.draw.pts.push(mx, my); drawOverlay(); }
    return;
  }
  // 選取 / 編輯——編輯（拖本體/頂點把手、Ctrl+增刪頂點）只在編輯模式（選取後按 E）開放
  const a = selAnn();
  if (a && a.layer === "spatial" && S.editMode) {
    const pts = shapeAt(a, S.curFrame);
    if (pts) {
      // Alt+左鍵拖曳（限 polygon）：沿滑鼠軌跡重繪該段邊緣（放開時 splice 套用）
      if (e.altKey && a.attrs.shape === "polygon" && e.button === 0) {
        e.preventDefault();
        S.trace = { target: a, base: pts.slice(), pts: [[mx, my]], undoBefore: snapFields(a) };
        drawOverlay(); return;
      }
      if (e.ctrlKey && ["polygon", "polyline"].includes(a.attrs.shape)) { e.preventDefault(); ctrlVertex(a, pts, mx, my); return; }
      const tol = 10 / S.vzoom.scale, hs = vertexHandles(a.attrs.shape, pts);
      for (let i = 0; i < hs.length; i++) if (Math.hypot(mx - hs[i][0], my - hs[i][1]) < tol) { S.edit = { mode: "vertex", idx: i, base: pts.slice(), start: [mx, my], target: a, shape: a.attrs.shape, undoBefore: snapFields(a) }; e.preventDefault(); return; }
      octx.save(); tracePath(octx, a.attrs.shape, pts); const inside = a.attrs.shape !== "polyline" && octx.isPointInPath(mx, my); octx.restore();
      if (inside || (a.attrs.shape === "point" && Math.hypot(mx - pts[0], my - pts[1]) < tol + 6)) { S.edit = { mode: "body", base: pts.slice(), start: [mx, my], target: a, shape: a.attrs.shape, undoBefore: snapFields(a) }; e.preventDefault(); return; }
    }
  }
  const hit = hitShape(mx, my);
  if (hit) {
    // 左鍵快速連點兩下同一標註（選取工具）＝選取並進入編輯模式（等同 E）。
    // 手勢區隔：第二下在此攔截 return——編輯拖曳（S.edit）只在 editMode 已開的
    // 「下一次」mousedown 才會啟動，連點本身不會誤觸拖曳；繪圖工具走不到此分支。
    if (S.tool === "select" && ldbl && ldbl.annId === hit.ann_id &&
        performance.now() - ldbl.t < 400 &&
        Math.hypot(e.clientX - ldbl.x, e.clientY - ldbl.y) <= 6) {
      ldbl = null; e.preventDefault();
      selectAnn(hit.ann_id);
      if (!S.editMode) toggleEditMode();
      return;
    }
    ldbl = { t: performance.now(), x: e.clientX, y: e.clientY, annId: hit.ann_id };
    selectAnn(hit.ann_id);
  } else {
    ldbl = null;
    if (S.tool === "select") selectAnn(null);
  }
});
document.addEventListener("mousemove", (e) => {
  if (!S.video) return;
  // videoArea 之外只在「繪製中 / 形狀編輯拖曳中」追蹤（座標同樣 clamp 到影像邊緣）
  if (!$("videoArea").contains(e.target) && !S.draw && !S.edit && !S.sam?.drag && !S.trace) return;
  S.mouseV = evToVideo(e);
  if (S.sam?.drag) { S.sam.drag.cur = S.mouseV; drawOverlay(); return; }
  if (S.trace) {   // Alt+拖曳重繪：錄軌跡（取樣間距 ≥2px，座標已 clamp 到影像內）
    const [tx, ty] = S.mouseV, last = S.trace.pts[S.trace.pts.length - 1];
    if (Math.hypot(tx - last[0], ty - last[1]) >= 2) { S.trace.pts.push([tx, ty]); drawOverlay(); }
    return;
  }
  if (S.edit) {
    const { mode, idx, base, start, shape, target } = S.edit, [mx, my] = S.mouseV, dx = mx - start[0], dy = my - start[1];
    let p;
    if (mode === "body") {
      // 整體移動夾制：碰到影像邊界就停在邊界，禁止移出（單一頂點編輯不在此，維持既有 clamp-到-邊界設計）
      const W = overlay.width, H = overlay.height;
      if (shape === "ellipse") {
        p = [clamp(base[0] + dx, base[2], W - base[2]), clamp(base[1] + dy, base[3], H - base[3]), base[2], base[3]];
      } else {
        const xs = base.filter((_, i) => i % 2 === 0), ys = base.filter((_, i) => i % 2 === 1);
        const ddx = clamp(dx, -Math.min(...xs), W - Math.max(...xs));
        const ddy = clamp(dy, -Math.min(...ys), H - Math.max(...ys));
        p = base.map((v, i) => v + (i % 2 ? ddy : ddx));
      }
    }
    else { p = base.slice();
      if (shape === "bbox") { if (idx === 0) { p[0] = mx; p[1] = my; } if (idx === 1) { p[2] = mx; p[1] = my; } if (idx === 2) { p[2] = mx; p[3] = my; } if (idx === 3) { p[0] = mx; p[3] = my; } }
      else if (shape === "ellipse") { if (idx < 2) p[2] = Math.max(2, Math.abs(mx - p[0])); else p[3] = Math.max(2, Math.abs(my - p[1])); }
      else { p[idx * 2] = mx; p[idx * 2 + 1] = my; } }
    S.edit.cur = p; upsertKp(target.attrs, S.curFrame, p);
    if (S.curFrame < target.start_frame) target.start_frame = S.curFrame;
    if (S.curFrame > target.end_frame) target.end_frame = S.curFrame;
    drawOverlay(); return;
  }
  if (S.draw) drawOverlay();
});
window.addEventListener("mouseup", async () => {
  if (S.sam?.drag) {   // smart：放開判定——有拖曳距離＝box prompt（重畫取代）；否則＝加一個點
    const d = S.sam.drag; S.sam.drag = null;
    const [ax, ay] = d.anchor, [cx, cy] = d.cur;
    if (Math.abs(cx - ax) > 4 && Math.abs(cy - ay) > 4) {
      S.sam.history.push({ type: "box", prev: S.sam.box });   // 記前一個框：Ctrl+Z 可還原
      S.sam.box = normBbox([ax, ay, cx, cy]);
    } else {   // 點在預覽 polygon 內＝自動負點（不需 Alt）；Alt＝強制負點（點外面也可）
      const neg = d.alt || samPointInPreview(ax, ay);
      S.sam.points.push([Math.round(ax), Math.round(ay)]); S.sam.labels.push(neg ? 0 : 1);
      S.sam.history.push({ type: "point" });
    }
    samPredict(); return;
  }
  if (S.draw?.stage === "drag" && S.draw.anchor && S.mouseV) {
    const [ax, ay] = S.draw.anchor, [mx, my] = S.mouseV;
    if (Math.abs(mx - ax) > 3 && Math.abs(my - ay) > 3) {
      if (S.draw.tool === "bbox") beginPending("bbox", normBbox([ax, ay, mx, my]));
      else beginPending("ellipse", [(ax + mx) / 2, (ay + my) / 2, Math.abs(mx - ax) / 2, Math.abs(my - ay) / 2]);
    } else cancelDraw();
    return;
  }
  if (S.trace) {   // Alt+拖曳重繪收尾：軌跡 splice 進 polygon（失敗＝不動原形狀）
    const tr = S.trace; S.trace = null;
    const res = traceSplice(tr.base, tr.pts);
    if (res.err) { toast(t("app.toast.trace_notapplied", res.err), 2400); drawOverlay(); return; }
    upsertKp(tr.target.attrs, S.curFrame, res.flat);
    if (S.curFrame < tr.target.start_frame) tr.target.start_frame = S.curFrame;
    if (S.curFrame > tr.target.end_frame) tr.target.end_frame = S.curFrame;
    await persist(tr.target, tr.undoBefore);   // 既有 PATCH＋undo：Ctrl+Z 可回復
    return;
  }
  if (S.edit) { const { target, shape, cur, undoBefore } = S.edit; S.edit = null; if (!cur) return; const fixed = shape === "bbox" ? normBbox(cur) : cur; upsertKp(target.attrs, S.curFrame, fixed); await persist(target, undoBefore); }
});
document.addEventListener("dblclick", (e) => {
  if (S.draw?.stage === "poly" && ($("videoArea").contains(e.target) || outsideDrawOk(e))) { e.preventDefault(); finishPoly(); }
});

function ctrlVertex(a, pts, mx, my) {
  const tol = 10 / S.vzoom.scale, p = pts.slice(), minC = a.attrs.shape === "polygon" ? 6 : 4;
  const before = snapFields(a);   // undo 快照（增/刪頂點皆為一步）
  for (let i = 0; i < p.length; i += 2) if (Math.hypot(mx - p[i], my - p[i + 1]) < tol) { if (p.length - 2 < minC) { toast(t("app.toast.vertex_min")); return; } p.splice(i, 2); upsertKp(a.attrs, S.curFrame, p); persist(a, before); return; }
  let best = { d: Infinity, s: 0 }; const n = p.length / 2, ns = a.attrs.shape === "polygon" ? n : n - 1;
  for (let s = 0; s < ns; s++) { const i1 = s * 2, i2 = ((s + 1) % n) * 2; const d = distToSeg(mx, my, p[i1], p[i1 + 1], p[i2], p[i2 + 1]); if (d < best.d) best = { d, s }; }
  if (best.d > tol * 2.5) { toast(t("app.toast.ctrl_edge_insert")); return; }
  p.splice(best.s * 2 + 2, 0, Math.round(mx), Math.round(my)); upsertKp(a.attrs, S.curFrame, p); persist(a, before);
}
function distToSeg(px, py, x1, y1, x2, y2) { const dx = x2 - x1, dy = y2 - y1, t = clamp(((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy || 1), 0, 1); return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)); }

// ---- Alt+拖曳重繪 polygon 邊緣：splice 演算法（純函式；SPLICE-BEGIN 供測試抽取）----
// Douglas-Peucker 簡化（pts=[[x,y],…]；回傳保留點）
function dpSimplify(pts, eps) {
  if (pts.length < 3) return pts.slice();
  const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [i, j] = stack.pop();
    let maxD = -1, idx = -1;
    for (let k = i + 1; k < j; k++) {
      const d = distToSeg(pts[k][0], pts[k][1], pts[i][0], pts[i][1], pts[j][0], pts[j][1]);
      if (d > maxD) { maxD = d; idx = k; }
    }
    if (maxD > eps) { keep[idx] = 1; stack.push([i, idx], [idx, j]); }
  }
  return pts.filter((_, k) => keep[k]);
}
// 點投影到 polygon（flat）邊界最近點：回傳 {s:邊索引, t:邊上比例, d:距離, x, y}
function projToPoly(x, y, flat) {
  const n = flat.length / 2;
  let best = null;
  for (let s = 0; s < n; s++) {
    const x1 = flat[s * 2], y1 = flat[s * 2 + 1];
    const x2 = flat[((s + 1) % n) * 2], y2 = flat[((s + 1) % n) * 2 + 1];
    const dx = x2 - x1, dy = y2 - y1;
    const t = clamp(((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy || 1), 0, 1);
    const px = x1 + t * dx, py = y1 + t * dy, d = Math.hypot(x - px, y - py);
    if (!best || d < best.d) best = { s, t, d, x: px, y: py };
  }
  return best;
}
// 沿頂點序方向從投影點 A 走到 B 的邊界弧（含兩端投影點）
function arcBetween(flat, A, B) {
  const n = flat.length / 2, out = [[A.x, A.y]];
  if (!(A.s === B.s && B.t >= A.t)) {
    let i = (A.s + 1) % n, guard = 0;
    while (guard++ <= n) {
      out.push([flat[i * 2], flat[i * 2 + 1]]);
      if (i === B.s) break;
      i = (i + 1) % n;
    }
  }
  out.push([B.x, B.y]);
  return out;
}
function distToPolylinePts(x, y, T) {
  let m = Infinity;
  for (let i = 0; i + 1 < T.length; i++) m = Math.min(m, distToSeg(x, y, T[i][0], T[i][1], T[i + 1][0], T[i + 1][1]));
  return m;
}
// 弧到軌跡的平均距離（用內部頂點；無內部頂點＝兩端中點）——判定「使用者在重畫哪段」
function arcScore(arc, T) {
  const pts = arc.length > 2 ? arc.slice(1, -1)
    : [[(arc[0][0] + arc[1][0]) / 2, (arc[0][1] + arc[1][1]) / 2]];
  return pts.reduce((s, p) => s + distToPolylinePts(p[0], p[1], T), 0) / pts.length;
}
// 主函式：polygon（flat）＋滑鼠軌跡（[[x,y],…]）→ 新 flat polygon；失敗回 {err}
function traceSplice(flat, trace) {
  let L = 0;
  for (let i = 1; i < trace.length; i++) L += Math.hypot(trace[i][0] - trace[i - 1][0], trace[i][1] - trace[i - 1][1]);
  if (trace.length < 2 || L < 8) return { err: t("app.toast.trace_short") };
  const T = dpSimplify(trace, 1.5);
  const A = projToPoly(T[0][0], T[0][1], flat), B = projToPoly(T[T.length - 1][0], T[T.length - 1][1], flat);
  if (A.d > 40 || B.d > 40) return { err: t("app.toast.trace_far") };
  // 邊界切成兩段弧；取代「離軌跡較近」的那段（平手取較短弧＝頂點較少者）
  const T2 = [[A.x, A.y], ...T.slice(1, -1), [B.x, B.y]];   // 軌跡端點貼齊投影點
  const arcAB = arcBetween(flat, A, B), arcBA = arcBetween(flat, B, A);
  const dAB = arcScore(arcAB, T2), dBA = arcScore(arcBA, T2);
  const replaceAB = dAB < dBA || (dAB === dBA && arcAB.length <= arcBA.length);
  // 保留弧一律轉成 PB → PA 方向，與 T2（PA → PB）首尾相接
  const kept = replaceAB ? arcBA : arcAB.slice().reverse();
  const pts = [...T2, ...kept.slice(1, -1)];
  const out = [];   // 去重複點（<0.5px 視為同點）＋首尾去重
  for (const p of pts) { const q = out[out.length - 1]; if (!q || Math.hypot(p[0] - q[0], p[1] - q[1]) > .5) out.push(p); }
  if (out.length > 1 && Math.hypot(out[0][0] - out[out.length - 1][0], out[0][1] - out[out.length - 1][1]) <= .5) out.pop();
  if (out.length < 3) return { err: t("app.toast.trace_fewpts") };
  let area = 0;
  for (let i = 0; i < out.length; i++) { const j = (i + 1) % out.length; area += out[i][0] * out[j][1] - out[j][0] * out[i][1]; }
  if (Math.abs(area / 2) < 1) return { err: t("app.toast.trace_zeroarea") };
  const flat2 = [];
  for (const p of out) flat2.push(Math.round(p[0]), Math.round(p[1]));
  return { flat: flat2 };
}
// ---- SPLICE-END ----
function hitShape(mx, my) {
  const anns = S.anns.filter(x => x.layer === "spatial" && !isHidden(x));
  for (let i = anns.length - 1; i >= 0; i--) { const pts = shapeAt(anns[i], S.curFrame); if (!pts) continue; const sh = anns[i].attrs.shape;
    if (sh === "point") { if (Math.hypot(mx - pts[0], my - pts[1]) < 12) return anns[i]; }
    else if (sh === "polyline") { for (let j = 0; j + 3 < pts.length; j += 2) if (distToSeg(mx, my, pts[j], pts[j + 1], pts[j + 2], pts[j + 3]) < 8) return anns[i]; }
    else { octx.save(); tracePath(octx, sh, pts); const hit = octx.isPointInPath(mx, my); octx.restore(); if (hit) return anns[i]; } }
  return null;
}

// ================================================= class selector（浮動）===
let cpState = null;   // {kind, tab, hi}
// 預設反白＝上一次使用的類別（找不到則第一項）→ 開選單後直接 Enter 即以預設 label 建立
function cpDefaultHi() {
  const last = cpState.kind === "spatial" ? S.lastClass.spatial : S.lastClass.temporal.label;
  const i = layerLabels(cpLayer()).indexOf(last);
  return i >= 0 ? i : 0;
}
function openClassPop(kind, x, y, opts = {}) {
  cpState = { kind, tab: kind === "temporal" ? (opts.layer || S.lastClass.temporal.layer) : "spatial", hi: 0, batch: !!opts.batch };
  cpState.hi = cpDefaultHi();
  const pop = $("classPop"); pop.classList.remove("hidden");
  const tabs = $("cpTabs");
  if (opts.batch) tabs.style.display = "none";     // 批次模式：層已鎖定，不給切換
  else if (kind === "temporal") {
    tabs.style.display = "flex"; tabs.innerHTML = "";
    for (const l of temporalLayers()) { const b = document.createElement("button"); b.textContent = l; b.dataset.layer = l; if (l === cpState.tab) b.classList.add("active"); b.onclick = () => { cpState.tab = l; cpState.hi = cpDefaultHi(); renderCpTabs(); renderCpList(); }; tabs.appendChild(b); }
  } else tabs.style.display = "none";
  $("cpSearch").value = ""; renderCpList();
  // 定位（避免超出視窗）
  const w = 220, h = 320;
  pop.style.left = `${clamp(x - w / 2, 8, innerWidth - w - 8)}px`;
  pop.style.top = `${clamp(y, 8, innerHeight - h - 8)}px`;
  setTimeout(() => $("cpSearch").focus(), 0);
}
function closeClassPop() { $("classPop").classList.add("hidden"); cpState = null; }
function renderCpTabs() { $("cpTabs").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.layer === cpState.tab)); }
function cpLayer() { return cpState.kind === "spatial" ? "spatial" : cpState.tab; }
function renderCpList() {
  const q = $("cpSearch").value.trim().toLowerCase();
  const layer = cpLayer();
  let items = layerLabels(layer).filter(l => l.toLowerCase().includes(q));
  const last = cpState.kind === "spatial" ? S.lastClass.spatial : S.lastClass.temporal.label;
  const host = $("cpList"); host.innerHTML = "";
  cpState.rows = [];
  items.forEach((label, i) => {
    const it = document.createElement("div"); it.className = "cp-item"; if (i === cpState.hi) it.classList.add("hi");
    it.innerHTML = `<span class="dot" style="background:${colorOf(label, layer)}"></span>${label}${label === last ? ' <span class="dim">↩</span>' : ''}`;
    it.onclick = () => assignClass(layer, label);
    host.appendChild(it); cpState.rows.push({ label });
  });
  if (q && !items.some(l => l.toLowerCase() === q)) {
    const it = document.createElement("div"); it.className = "cp-item cp-new"; if (cpState.hi >= items.length) it.classList.add("hi");
    it.textContent = t("app.cp.add_new", $("cpSearch").value.trim(), layer);
    it.onclick = () => createAndAssign(layer, $("cpSearch").value.trim());
    host.appendChild(it); cpState.rows.push({ label: $("cpSearch").value.trim(), isNew: true });
  }
}
async function createAndAssign(layer, name) {
  try { const res = await api("POST", "/api/labels", { layer, name, project_id: S.project }); S.cfg.taxonomy = res.taxonomy; assignClass(layer, name); }
  catch (e) { toast(e.message); }
}
async function assignClass(layer, label) {
  if (cpState?.batch) { closeClassPop(); await batchRelabel(layer, label); return; }   // 批次改類別
  if (!S.pending) { closeClassPop(); return; }
  try {
    let created;
    if (S.pending.kind === "spatial") {
      // SAM 產出：與手畫 polygon 完全同構，僅 interpolate:false（各幀頂點數不保證一致，
      // 不做跨幀插值）＋ source:'sam' 供日後統計；下游（時間軸/匯出/編輯/undo）零特例。
      const attrs = { shape: S.pending.shape,
                      interpolate: S.pending.source !== "sam",
                      keypoints: [{ frame: S.pending.frame, points: S.pending.points }] };
      if (S.pending.source) attrs.source = S.pending.source;
      created = await api("POST", `/api/videos/${S.video.video_id}/annotations`, annBody({
        layer: "spatial", label, start_frame: S.pending.frame, end_frame: S.pending.frame,
        attrs }));
      S.lastClass.spatial = label;
    } else {
      created = await api("POST", `/api/videos/${S.video.video_id}/annotations`, annBody({
        layer, label, start_frame: S.pending.start, end_frame: S.pending.end, attrs: {} }));
      S.lastClass.temporal = { layer, label };
    }
    S.anns.push(created); S.pending = null; S.tmpTemporal = null;
    updateTmpBar();   // 完成後「時段」鈕自動回到非啟用（單次性）
    pushUndo(t("app.undo.create", label), [{ type: "create", ann: snapAnn(created) }]);
    closeClassPop(); selectAnn(created.ann_id); toast(t("app.toast.annotated", label), 1200);
  } catch (e) { toast(e.message); }
}

// ================================================= temporal（時間軸建立）===
function finishTemporal() {
  if (!S.tmpTemporal) return;
  video.pause();
  let { start, end } = S.tmpTemporal; if (end < start) [start, end] = [end, start];
  S.pending = { kind: "temporal", start, end };
  updateTmpBar();
  const r = $("timelineWrap").getBoundingClientRect();
  openClassPop("temporal", r.left + r.width / 2, r.top - 330);
}
function updateTmpBar() {
  refreshToolbar();   // 「時段」鈕 active 狀態與錄製狀態同步
  const bar = $("tmpBar");
  if (!S.tmpTemporal || S.pending) { bar.classList.add("hidden"); return; }
  let { start, end } = S.tmpTemporal; if (end < start) [start, end] = [end, start];
  $("tmpInfo").textContent =
    t("app.tmp.info", fmtTime(start), fmtTime(end), start, end, ((end - start + 1) / fps()).toFixed(2));
  bar.classList.remove("hidden");
}

// ================================================= 時間軸（flat tracks）====
function usableW() { return canvas.clientWidth - GUTTER; }
function resizeCanvas() { const d = devicePixelRatio || 1; canvas.width = canvas.clientWidth * d; canvas.height = canvas.clientHeight * d; ctx.setTransform(d, 0, 0, d, 0, 0); }
const fX = (f) => GUTTER + (f - S.view.start) * S.view.ppf;
const xF = (x) => S.view.start + (x - GUTTER) / S.view.ppf;

let gutterHits = [];   // 時間軸 gutter 的點擊區（眼睛/收合/全選鈕，draw() 時填）
// 三層樹狀分軌：TEMPORAL / SPATIAL（root）→ phase/action/event/triplet 或 shape type → label 軌。
// 收合（S.collapsed）只是不輸出底下的列（畫面變短），不影響標註顯示（isHidden 不看它）。
function tracks() {
  const rows = [];
  const tLayers = temporalLayers().filter(l => S.anns.some(a => a.layer === l));
  if (tLayers.length) {
    rows.push({ kind: "root", root: "temporal", title: "TEMPORAL", ckey: "root/temporal" });
    if (!S.collapsed.has("root/temporal")) for (const layer of tLayers) {
      rows.push({ kind: "group", level: layer, ckey: `group/${layer}` });
      if (S.collapsed.has(`group/${layer}`)) continue;
      const labels = [...new Set(S.anns.filter(a => a.layer === layer).map(a => a.label))].sort();
      for (const lb of labels) rows.push({ kind: "track", layer, label: lb, title: lb });
    }
  }
  const spat = S.anns.filter(a => a.layer === "spatial");
  if (spat.length) {
    rows.push({ kind: "root", root: "spatial", title: "SPATIAL", ckey: "root/spatial" });
    if (!S.collapsed.has("root/spatial")) for (const shp of SPATIAL_TOOLS) {
      const arr = spat.filter(a => a.attrs.shape === shp); if (!arr.length) continue;
      rows.push({ kind: "shape", level: "spatial", shape: shp, title: `${SHAPE_ICON[shp] || ""} ${shp}`, ckey: `shape/${shp}` });
      if (S.collapsed.has(`shape/${shp}`)) continue;
      const labels = [...new Set(arr.map(a => a.label))].sort();
      for (const lb of labels) rows.push({ kind: "track", layer: "spatial", label: lb, shape: shp, title: lb });
    }
  }
  return rows;
}
function layout() {
  const rows = []; const rects = []; let y = OVERVIEW_H + RULER_H;
  for (const tr of tracks()) {
    if (tr.kind !== "track") { rows.push({ ...tr, y, h: HEAD_H }); y += HEAD_H; continue; }
    const anns = S.anns.filter(a => a.layer === tr.layer && a.label === tr.label && (tr.shape == null || a.attrs.shape === tr.shape));
    const sorted = anns.slice().sort((p, q) => p.start_frame - q.start_frame || p.end_frame - q.end_frame);
    const laneEnd = [], lane = new Map();
    for (const a of sorted) { let li = laneEnd.findIndex(e => a.start_frame > e); if (li < 0) { li = laneEnd.length; laneEnd.push(-1); } laneEnd[li] = a.end_frame; lane.set(a.ann_id, li); }
    const nL = Math.max(1, laneEnd.length), h = nL * LANE_H + ROW_PAD;
    rows.push({ ...tr, y, h });
    for (const a of sorted) rects.push({ a, lane: lane.get(a.ann_id), rowY: y });
    y += h;
  }
  return { rows, rects, totalH: y };
}
function niceTickStep() { const want = 90 / S.view.ppf, F = Math.round(fps()); return [1, 5, F, 5 * F, 10 * F, 30 * F, 60 * F, 300 * F, 600 * F, 1800 * F].find(c => c >= want) || 3600 * F; }

function draw() {
  if (!S.video) return;
  const { rows, rects, totalH } = layout();
  const wrapH = canvas.parentElement.clientHeight, target = Math.max(wrapH, totalH + 6);
  if (Math.abs(canvas.clientHeight - target) > 2) { canvas.style.height = `${target}px`; resizeCanvas(); }
  const W = canvas.clientWidth, H = canvas.clientHeight; ctx.clearRect(0, 0, W, H);
  // 概覽
  ctx.fillStyle = "#20252b"; ctx.fillRect(GUTTER, 0, W - GUTTER, OVERVIEW_H);
  const ovPpf = (W - GUTTER) / nFrames(); ctx.fillStyle = "#555";
  for (const a of S.anns) ctx.fillRect(GUTTER + a.start_frame * ovPpf, 2, Math.max(1, (a.end_frame - a.start_frame + 1) * ovPpf), OVERVIEW_H - 4);
  const vw = usableW() / S.view.ppf; ctx.strokeStyle = "#ff8c00"; ctx.lineWidth = 1;
  ctx.strokeRect(GUTTER + S.view.start * ovPpf + .5, .5, Math.max(2, vw * ovPpf), OVERVIEW_H - 1);
  // 時間尺
  ctx.fillStyle = "#262b31"; ctx.fillRect(GUTTER, OVERVIEW_H, W - GUTTER, RULER_H);
  const step = niceTickStep(), first = Math.floor(S.view.start / step) * step;
  ctx.fillStyle = "#8a949e"; ctx.font = "10px ui-monospace,monospace"; ctx.strokeStyle = "#333b44";
  for (let f = first; f <= S.view.start + vw; f += step) { const x = fX(f); if (x < GUTTER) continue; ctx.beginPath(); ctx.moveTo(x + .5, OVERVIEW_H + RULER_H - 5); ctx.lineTo(x + .5, totalH); ctx.stroke(); ctx.fillText(S.showFrames ? `f${f}` : fmtTime(f), x + 3, OVERVIEW_H + 13); }
  if (S.view.ppf >= 5) { ctx.strokeStyle = "#2c333b"; for (let f = Math.max(0, Math.floor(S.view.start)); f <= S.view.start + vw + 1; f++) { const x = fX(f); if (x < GUTTER) continue; ctx.beginPath(); ctx.moveTo(x + .5, OVERVIEW_H + RULER_H); ctx.lineTo(x + .5, totalH); ctx.stroke(); } }
  // 軌道 / 群組標頭（gutter：▸/▾ 收合、👁 分層隱藏、☑ 全選類別）
  gutterHits = [];
  for (const [i, r] of rows.entries()) {
    const isTrack = r.kind === "track";
    ctx.fillStyle = isTrack ? (i % 2 ? "#242930" : "#262c33")
      : (r.kind === "root" ? "#161a1f" : r.kind === "shape" ? "#20262d" : "#1b2026");
    ctx.fillRect(GUTTER, r.y, W - GUTTER, r.h);
    ctx.fillStyle = r.kind === "root" ? "#14181d" : "#191d22"; ctx.fillRect(0, r.y, GUTTER, r.h);
    let indent, color, font, kind, key, hidden = false, title, arrowX = null;
    if (r.kind === "root") {
      arrowX = 4; indent = 16; color = "#b6c0ca"; font = "700 10px system-ui"; title = r.title;
      if (r.root === "spatial") { kind = "level"; key = "spatial"; hidden = S.hiddenLevels.has(key); }
    } else if (r.kind === "group") { arrowX = 12; indent = 24; color = "#9aa4ae"; font = "700 10px system-ui"; kind = "level"; key = r.level; hidden = S.hiddenLevels.has(key); title = r.level.toUpperCase(); }
    else if (r.kind === "shape") { arrowX = 20; indent = 32; color = "#aeb6bf"; font = "11px system-ui"; kind = "shape"; key = r.shape; hidden = S.hiddenShapes.has(key); title = r.title; }
    else { indent = r.layer === "spatial" ? 42 : 32; color = "#c6cdd5"; font = "11px system-ui"; kind = "label"; key = `${r.layer}/${r.label}`; hidden = S.hiddenLabels.has(key); title = r.title; }
    const baseY = r.y + (isTrack ? Math.min(15, r.h - 6) : 13);
    if (arrowX != null) {   // 收合鈕（▸/▾）：整列左半（眼睛鈕以外）皆可點
      ctx.fillStyle = "#8a949e"; ctx.font = "10px system-ui";
      ctx.fillText(S.collapsed.has(r.ckey) ? "▸" : "▾", arrowX, baseY);
      gutterHits.push({ x: 0, y: r.y, w: GUTTER - 26, h: r.h, act: "collapse", ckey: r.ckey });
    }
    const rightW = isTrack ? 44 : 24;   // track 列多留 ☑ 的空間
    ctx.fillStyle = color; ctx.font = font;
    ctx.save(); ctx.beginPath(); ctx.rect(indent, r.y, GUTTER - indent - rightW, r.h); ctx.clip();
    ctx.fillText(title, indent, baseY); ctx.restore();
    if (isTrack) {          // ☑ 全選此類別軌的所有標註（加入多選集合）
      ctx.font = "12px system-ui"; ctx.globalAlpha = .85;
      ctx.fillText("☑", GUTTER - 40, baseY); ctx.globalAlpha = 1;
      gutterHits.push({ x: GUTTER - 44, y: r.y, w: 20, h: r.h, act: "selectall", layer: r.layer, label: r.label, shape: r.shape ?? null });
    }
    if (kind) {
      ctx.font = "12px system-ui"; ctx.globalAlpha = hidden ? 1 : .85;
      ctx.fillText(hidden ? "🚫" : "👁", GUTTER - 20, baseY); ctx.globalAlpha = 1;
      gutterHits.push({ x: GUTTER - 24, y: r.y, w: 24, h: r.h, act: "eye", kind, key });
    }
    ctx.strokeStyle = "#333b44"; ctx.beginPath(); ctx.moveTo(0, r.y + r.h + .5); ctx.lineTo(W, r.y + r.h + .5); ctx.stroke();
  }
  // 標註
  for (const { a, lane, rowY } of rects) {
    const x = fX(a.start_frame), w = Math.max(5, (a.end_frame - a.start_frame + 1) * S.view.ppf);
    if (x + w < GUTTER || x > W) continue;
    const y = rowY + 3 + lane * LANE_H, c = colorOf(a.label, a.layer);
    ctx.globalAlpha = isHidden(a) ? 0.25 : 1;
    ctx.fillStyle = c + (a.ann_id === S.sel ? "ff" : "b8");
    ctx.fillRect(Math.max(GUTTER, x), y, w - Math.max(0, GUTTER - x), LANE_H - 5);
    if (a.ann_id === S.sel) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.strokeRect(Math.max(GUTTER, x) + .5, y + .5, w - Math.max(0, GUTTER - x) - 1, LANE_H - 6); }
    if (S.selSet.has(a.ann_id)) { ctx.save(); ctx.setLineDash([4, 2]); ctx.strokeStyle = "#4da3ff"; ctx.lineWidth = 2; ctx.strokeRect(Math.max(GUTTER, x) + 1, y + 1, w - Math.max(0, GUTTER - x) - 2, LANE_H - 7); ctx.setLineDash([]); ctx.restore(); }
    if (w > 30 && a.layer !== "spatial") { ctx.fillStyle = "#10131a"; ctx.font = "11px system-ui"; ctx.save(); ctx.beginPath(); ctx.rect(Math.max(GUTTER, x), y, w, LANE_H - 5); ctx.clip(); ctx.fillText(a.label, Math.max(GUTTER, x) + 4, y + 12); ctx.restore(); }
    if (a.layer === "spatial" && a.attrs.keypoints) { ctx.fillStyle = "#2b2f36"; for (const kp of a.attrs.keypoints) { const kx = fX(kp.frame + .5); if (kx < GUTTER) continue; const ky = y + (LANE_H - 5) / 2; ctx.beginPath(); ctx.moveTo(kx, ky - 4); ctx.lineTo(kx + 4, ky); ctx.lineTo(kx, ky + 4); ctx.lineTo(kx - 4, ky); ctx.closePath(); ctx.fill(); } }
    ctx.globalAlpha = 1;
  }
  // 建立中的 temporal
  if (S.tmpTemporal) { let { start, end } = S.tmpTemporal; if (end < start) [start, end] = [end, start]; const x = fX(start), w = Math.max(2, (end - start + 1) * S.view.ppf); ctx.fillStyle = "#ff8c0055"; ctx.fillRect(Math.max(GUTTER, x), OVERVIEW_H + RULER_H, w - Math.max(0, GUTTER - x), totalH - OVERVIEW_H - RULER_H); ctx.setLineDash([5, 3]); ctx.strokeStyle = "#ff8c00"; ctx.lineWidth = 1.5; ctx.strokeRect(Math.max(GUTTER, x) + .5, OVERVIEW_H + RULER_H + .5, w - Math.max(0, GUTTER - x) - 1, totalH - OVERVIEW_H - RULER_H - 1); ctx.setLineDash([]); }
  // 播放頭
  const px = fX(S.curFrame + .5); if (px >= GUTTER) { ctx.strokeStyle = "#ff8c00"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(px, OVERVIEW_H); ctx.lineTo(px, Math.max(totalH, H)); ctx.stroke(); }
  if (S.hoverX != null && S.hoverX > GUTTER) { const f = clamp(Math.round(xF(S.hoverX)), 0, nFrames() - 1); const lbl = S.showFrames ? `f${f}` : fmtTime(f); ctx.font = "10px ui-monospace,monospace"; const tw = ctx.measureText(lbl).width + 10; ctx.fillStyle = "#000a"; ctx.fillRect(S.hoverX - tw / 2, OVERVIEW_H + RULER_H, tw, 15); ctx.fillStyle = "#fff"; ctx.fillText(lbl, S.hoverX - tw / 2 + 5, OVERVIEW_H + RULER_H + 11); }
}
function followPlayhead() { const vw = usableW() / S.view.ppf; if (S.curFrame < S.view.start || S.curFrame > S.view.start + vw) S.view.start = clamp(S.curFrame - vw * .15, 0, Math.max(0, nFrames() - vw)); }
function setZoom(ppf, af, ax) { ppf = clamp(ppf, S.fitPpf, 40); if (af == null) { af = S.curFrame; ax = GUTTER + usableW() / 2; } S.view.ppf = ppf; S.view.start = clamp(af - (ax - GUTTER) / ppf, 0, Math.max(0, nFrames() - usableW() / ppf)); updateTransport(); draw(); }

// 時間軸互動
canvas.addEventListener("mousedown", (e) => {
  const mx = e.offsetX, my = e.offsetY;
  if (my < OVERVIEW_H) { S.drag = { type: "overview" }; panOverview(mx); return; }
  if (mx < GUTTER) {
    // 後推入的 hit（眼睛/☑）優先於整列的收合區
    const g = [...gutterHits].reverse().find(h => mx >= h.x && mx <= h.x + h.w && my >= h.y && my <= h.y + h.h);
    if (!g) return;
    if (g.act === "eye") toggleHide(g.kind, g.key);                      // 分層隱藏
    else if (g.act === "collapse") { if (S.collapsed.has(g.ckey)) S.collapsed.delete(g.ckey); else S.collapsed.add(g.ckey); draw(); }
    else if (g.act === "selectall") selectAllOf(g.layer, g.label, g.shape);   // 全選類別 → 多選集合
    return;
  }
  // shift+左鍵：多選時間軸標註（批次複製/刪除）
  if (e.shiftKey && my > OVERVIEW_H) { const hit = hitTimeline(mx, my); if (hit) { toggleSel(hit.ann_id); return; } }
  // 時段標註中（按 T / 時段鈕開始）：點時間軸＝時段縮放到該處且影片跳到該 frame。
  // 非標註中點時間軸只是 seek，絕不會產生標註。
  if (S.tmpTemporal && !S.pending && my > OVERVIEW_H + RULER_H) {
    const f = clamp(Math.round(xF(mx)), 0, nFrames() - 1);
    if (f >= S.tmpTemporal.start) S.tmpTemporal.end = f;
    else S.tmpTemporal.start = f;
    setFrame(f); updateTmpBar(); draw(); return;
  }
  // 選取物的邊緣拖曳（極窄如 1-frame：左右半邊各拉一側，保證抓得到）——僅編輯模式（選取後按 E）開放
  const a = selAnn();
  if (a && S.editMode) { const x1 = fX(a.start_frame), x2 = fX(a.end_frame + 1); const { rects } = layout(); const rc = rects.find(r => r.a.ann_id === a.ann_id); if (rc) { const y = rc.rowY + 3 + rc.lane * LANE_H; if (my >= y && my <= y + LANE_H - 5) {
    if (x2 - x1 < 14) { if (mx >= x1 - 8 && mx <= x2 + 8) { S.drag = { type: "edge", edge: mx <= (x1 + x2) / 2 ? "start" : "end", a, undoBefore: snapFields(a) }; return; } }
    else { if (Math.abs(mx - x1) < 6) { S.drag = { type: "edge", edge: "start", a, undoBefore: snapFields(a) }; return; } if (Math.abs(mx - x2) < 6) { S.drag = { type: "edge", edge: "end", a, undoBefore: snapFields(a) }; return; } }
  } } }
  // 點選標註＝選取 + 影片同步跳到該標註的起點
  const hit = hitTimeline(mx, my); if (hit) { selectAnn(hit.ann_id); setFrame(hit.start_frame); return; }
  setFrame(xF(mx));
});
function hitTimeline(mx, my) { const { rects } = layout(); for (let i = rects.length - 1; i >= 0; i--) { const { a, lane, rowY } = rects[i]; const x = fX(a.start_frame), w = Math.max(5, (a.end_frame - a.start_frame + 1) * S.view.ppf), pad = w < 10 ? 4 : 0, y = rowY + 3 + lane * LANE_H; if (mx >= x - pad && mx <= x + w + pad && my >= y && my <= y + LANE_H - 5) return a; } return null; }
function panOverview(mx) { const ovPpf = (canvas.clientWidth - GUTTER) / nFrames(), vw = usableW() / S.view.ppf; S.view.start = clamp((mx - GUTTER) / ovPpf - vw / 2, 0, Math.max(0, nFrames() - vw)); draw(); }
canvas.addEventListener("mousemove", (e) => {
  const mx = e.offsetX; S.hoverX = mx;
  if (S.drag?.type === "overview") { panOverview(mx); return; }
  if (S.drag?.type === "edge") {
    let f = clamp(Math.round(xF(mx)), 0, nFrames() - 1); const a = S.drag.a;
    if (a.layer === "spatial" && a.attrs.keypoints?.length) { const kf = a.attrs.keypoints.map(k => k.frame); if (S.drag.edge === "start") f = Math.min(f, Math.min(...kf)); else f = Math.max(f, Math.max(...kf)); }
    if (S.drag.edge === "start") a.start_frame = Math.min(f, a.end_frame); else a.end_frame = Math.max(f, a.start_frame);
    S.drag.dirty = true;
    if (f !== S.curFrame) setFrame(f);   // 影片同步跟到拖拉中的 frame
    draw(); renderRight(); return;
  }
  draw();
});
canvas.addEventListener("mouseleave", () => { S.hoverX = null; draw(); });
window.addEventListener("mouseup", () => {
  if (S.drag?.type === "edge" && S.drag.dirty) persist(S.drag.a, S.drag.undoBefore);
  S.drag = null;
});
canvas.addEventListener("wheel", (e) => {
  e.preventDefault(); if (!S.video) return;
  const panX = (d) => { const vw = usableW() / S.view.ppf; S.view.start = clamp(S.view.start + d / S.view.ppf * 1.5, 0, Math.max(0, nFrames() - vw)); draw(); };
  if (e.ctrlKey) { setZoom(S.view.ppf * (e.deltaY < 0 ? 1.3 : 1 / 1.3), xF(e.offsetX), e.offsetX); return; }
  if (e.shiftKey) { panX(e.deltaY); return; }              // Shift+滾輪 = 水平移動
  if (e.deltaX) panX(e.deltaX);                            // 觸控板橫滑 = 水平移動
  if (e.deltaY) $("timelineWrap").scrollTop += e.deltaY;   // 滾輪 = 時間軸上下捲動
}, { passive: false });
// 縮放鈕：單鈕循環 100% → 200% → 400% → 800% → frame 級（ppf≥5）→ 回 100%；鈕面顯示目前級別
$("zoomBtn").onclick = () => {
  if (S.view.ppf >= 5) { setZoom(S.fitPpf); return; }   // frame 級 → 回 100%
  const Z = S.view.ppf / S.fitPpf, next = [1, 2, 4, 8].find(p => p > Z * 1.01);
  setZoom(next ? next * S.fitPpf : 5);                  // 800% 之後 → frame 級
};

// 影片/時間軸 分隔線：按住上下拖曳調整兩者高度比例
(function wireSplitter() {
  const sp = $("splitter"), wrap = $("timelineWrap");
  let drag = null;
  sp.addEventListener("mousedown", (e) => {
    e.preventDefault();
    drag = { y: e.clientY, h: wrap.clientHeight };
    sp.classList.add("dragging"); document.body.style.cursor = "ns-resize";
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const maxH = $("stage").clientHeight - 170;   // 影片區至少留 ~170px
    wrap.style.height = `${clamp(drag.h + (drag.y - e.clientY), 90, Math.max(90, maxH))}px`;
    resizeCanvas(); draw(); sizeOverlay();
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null; sp.classList.remove("dragging"); document.body.style.cursor = "";
    resizeCanvas(); draw(); sizeOverlay();
  });
})();

// ============================================================ undo / redo ==
// linear 堆疊（上限 50 步）。entry = { label, ops[] }；op 三種：
//   create {ann}                    — 該步新增了 ann；undo=刪除、redo=以原 ann_id 重建
//   delete {ann}                    — 該步刪除了 ann；undo=重建、redo=刪除
//   update {ann_id, before, after}  — PATCH 類；undo=回寫 before、redo=回寫 after
// undo/redo 皆打 API 真正落庫，完成後重新拉整份標註列表刷新畫面。
// 批次操作（批次刪除/批次改類別/貼上/生成 event）＝單一 entry，一次 Ctrl+Z 全部還原。
const UNDO_MAX = 50, undoStack = [], redoStack = [];
let undoBusy = false;
const snapAnn = (a) => JSON.parse(JSON.stringify({
  ann_id: a.ann_id, video_id: a.video_id, layer: a.layer, label: a.label,
  start_frame: a.start_frame, end_frame: a.end_frame, attrs: a.attrs || {},
  project_id: a.project_id || S.project, annotator: a.annotator || S.annotator }));
const snapFields = (a) => JSON.parse(JSON.stringify({
  label: a.label, start_frame: a.start_frame, end_frame: a.end_frame, attrs: a.attrs || {} }));
function pushUndo(label, ops) {
  if (!ops.length) return;
  undoStack.push({ label, ops });
  if (undoStack.length > UNDO_MAX) undoStack.shift();
  redoStack.length = 0;   // 新操作使 redo 分支失效（linear）
}
function clearUndo() { undoStack.length = 0; redoStack.length = 0; }
async function refreshAnns() {
  if (!S.video) return;
  try { S.anns = await api("GET", `/api/videos/${S.video.video_id}/annotations?project=${encodeURIComponent(S.project)}`); }
  catch (e) { toast(t("app.toast.reload_fail", e.message), 3000); return; }
  const ids = new Set(S.anns.map(a => a.ann_id));
  if (S.sel && !ids.has(S.sel)) S.sel = null;
  for (const id of [...S.selSet]) if (!ids.has(id)) S.selSet.delete(id);
  for (const id of [...S.hidden]) if (!ids.has(id)) S.hidden.delete(id);
  draw(); drawOverlay(); renderRight();
}
async function applyStep(entry, dir) {
  const ops = dir === "undo" ? [...entry.ops].reverse() : entry.ops;
  for (const op of ops) {
    if (op.type === "update") {
      await api("PATCH", annUrl(op.ann_id), dir === "undo" ? op.before : op.after);
    } else if ((op.type === "create") === (dir === "undo")) {
      // undo(create) / redo(delete) → 刪除；404（已不存在）視為成功
      try { await api("DELETE", annUrl(op.ann.ann_id)); }
      catch (e) { if (!String(e.message).startsWith("404")) throw e; }
    } else {
      // undo(delete) / redo(create) → 以完整快照重建；帶原 ann_id，後端不衝突即沿用
      // 重建也必須帶 project_id/annotator（快照有存；舊快照 fallback 目前值）
      await api("POST", `/api/videos/${op.ann.video_id}/annotations`, {
        ann_id: op.ann.ann_id, layer: op.ann.layer, label: op.ann.label,
        start_frame: op.ann.start_frame, end_frame: op.ann.end_frame, attrs: op.ann.attrs,
        project_id: op.ann.project_id || S.project,
        annotator: op.ann.annotator || S.annotator });
    }
  }
}
async function doUndo() {
  if (undoBusy) return;
  const entry = undoStack.pop();
  if (!entry) { toast(t("app.toast.no_undo"), 1200); return; }
  undoBusy = true;
  try { await applyStep(entry, "undo"); redoStack.push(entry); toast(t("app.toast.undone", entry.label), 1600); }
  catch (e) { toast(t("app.toast.undo_fail", e.message), 3000); }
  finally { undoBusy = false; await refreshAnns(); }
}
async function doRedo() {
  if (undoBusy) return;
  const entry = redoStack.pop();
  if (!entry) { toast(t("app.toast.no_redo"), 1200); return; }
  undoBusy = true;
  try { await applyStep(entry, "redo"); undoStack.push(entry); toast(t("app.toast.redone", entry.label), 1600); }
  catch (e) { toast(t("app.toast.redo_fail", e.message), 3000); }
  finally { undoBusy = false; await refreshAnns(); }
}

// ============================================================ 選取 / 存 ====
function selAnn() { return S.anns.find(a => a.ann_id === S.sel) || null; }
function selectAnn(id) { if (id !== S.sel) S.editMode = false; S.sel = id; S.selSet.clear(); draw(); drawOverlay(); renderRight(); }
// E（有選取時）：進入/離開編輯模式——未選取＝純瀏覽不可誤拖；選取後按 E 才開放
// 拖拉把手（spatial 頂點/本體）與 temporal 邊界。與 Ctrl+Z 相容（編輯仍走 persist→undo）。
function toggleEditMode() {
  const a = selAnn();
  if (!a) return;
  S.editMode = !S.editMode;
  toast(S.editMode
    ? (a.layer === "spatial" ? t("app.edit.spatial_on") : t("app.edit.temporal_on"))
    : t("app.edit.off"), 2000);
  draw(); drawOverlay();
}
function toggleSel(id) { if (S.selSet.has(id)) S.selSet.delete(id); else S.selSet.add(id); S.sel = S.selSet.size ? id : null; draw(); drawOverlay(); renderRight(); }
// ☑ 全選某類別（軌）的所有標註：加入多選集合；已全在集合中則再按一次＝取消
function selectAllOf(layer, label, shape) {
  const ids = S.anns.filter(a => a.layer === layer && a.label === label && (shape == null || a.attrs.shape === shape)).map(a => a.ann_id);
  if (!ids.length) return;
  const allIn = ids.every(id => S.selSet.has(id));
  if (allIn) ids.forEach(id => S.selSet.delete(id)); else ids.forEach(id => S.selSet.add(id));
  S.sel = S.selSet.size ? ids[0] : null;
  toast(allIn ? t("app.toast.deselect_label", label) : t("app.toast.select_label", label, ids.length), 1400);
  draw(); drawOverlay(); renderRight();
}
function clearSelSet() { if (!S.selSet.size) return; S.selSet.clear(); draw(); drawOverlay(); renderRight(); }
// 批次操作列（多選集合非空時顯示；renderRight() 每次同步）
function updateBatchBar() {
  const bar = $("batchBar");
  if (!S.selSet.size) { bar.classList.add("hidden"); return; }
  $("batchCount").textContent = S.selSet.size;
  bar.classList.remove("hidden");
}
// 批次改類別：僅支援同一層（label 集合依層而異）
function openBatchClassPop() {
  const sel = S.anns.filter(a => S.selSet.has(a.ann_id));
  if (!sel.length) return;
  const layers = [...new Set(sel.map(a => a.layer))];
  if (layers.length > 1) { toast(t("app.toast.batch_cross_layer", layers), 3000); return; }
  const r = $("batchBar").getBoundingClientRect();
  openClassPop(layers[0] === "spatial" ? "spatial" : "temporal", r.left + 200, r.top - 340, { batch: true, layer: layers[0] });
}
async function batchRelabel(layer, label) {
  const ids = [...S.selSet]; let n = 0; const ops = [];
  for (const id of ids) {
    const a = S.anns.find(x => x.ann_id === id);
    if (!a || a.layer !== layer) continue;
    const before = snapFields(a);
    try { const u = await api("PATCH", annUrl(id), { label }); Object.assign(a, u); n++; ops.push({ type: "update", ann_id: id, before, after: snapFields(a) }); }
    catch (e) { toast(e.message); }
  }
  pushUndo(t("app.undo.batch_relabel", n, label), ops);
  toast(t("app.toast.batch_relabeled", n, label), 1800);
  draw(); drawOverlay(); renderRight();
}
function toggleHide(kind, key) {
  const set = kind === "level" ? S.hiddenLevels : kind === "shape" ? S.hiddenShapes : kind === "label" ? S.hiddenLabels : S.hidden;
  if (set.has(key)) set.delete(key); else set.add(key);
  draw(); drawOverlay(); renderRight();
}
async function deleteMany(ids) {
  if (!ids.length) return;
  if (!confirm(t("app.confirm.del_many", ids.length))) return;
  const done = [];   // 成功刪除者的完整快照（單一 undo entry，一次 Ctrl+Z 全部復原）
  for (const id of ids) {
    const a = S.anns.find(x => x.ann_id === id);
    try { await api("DELETE", annUrl(id)); if (a) done.push({ type: "delete", ann: snapAnn(a) }); }
    catch (e) { toast(e.message); }
  }
  const set = new Set(done.map(op => op.ann.ann_id)); S.anns = S.anns.filter(x => !set.has(x.ann_id));
  set.forEach(id => { if (S.sel === id) S.sel = null; S.selSet.delete(id); });
  pushUndo(t("app.undo.del_many", done.length), done);
  toast(t("app.toast.del_many", done.length), 1500); draw(); drawOverlay(); renderRight();
}
// persist(a, before)：a 已被就地修改；before = 手勢開始前的 snapFields 快照（供 undo 回寫）
async function persist(a, before) { if (!a.ann_id) return; try { const u = await api("PATCH", annUrl(a.ann_id), { start_frame: a.start_frame, end_frame: a.end_frame, attrs: a.attrs }); Object.assign(a, u); if (before) pushUndo(t("app.undo.modify"), [{ type: "update", ann_id: a.ann_id, before, after: snapFields(a) }]); } catch (e) { toast(e.message); } draw(); drawOverlay(); renderRight(); }
async function patchSel(fields) { const a = selAnn(); if (!a) return; const before = snapFields(a); try { const u = await api("PATCH", annUrl(a.ann_id), fields); Object.assign(a, u); pushUndo(fields.label !== undefined ? t("app.undo.relabel") : t("app.undo.modify"), [{ type: "update", ann_id: a.ann_id, before, after: snapFields(a) }]); } catch (e) { toast(e.message); } draw(); drawOverlay(); renderRight(); }
async function deleteAnn(id) { const a = S.anns.find(x => x.ann_id === id); if (!a) return; const snap = snapAnn(a); try { await api("DELETE", annUrl(id)); S.anns = S.anns.filter(x => x.ann_id !== id); if (S.sel === id) S.sel = null; pushUndo(t("app.undo.del_one", snap.label), [{ type: "delete", ann: snap }]); draw(); drawOverlay(); renderRight(); } catch (e) { toast(e.message); } }

// ==================================== 標註編輯彈窗（時間軸雙擊，影片同步）==
let editId = null;
function closeEditPop() { editId = null; $("editPop").classList.add("hidden"); }
function openEditPop(a, x, y) {
  editId = a.ann_id; buildEditPop(a);
  const pop = $("editPop"); pop.classList.remove("hidden");
  const w = 300, h = 230;
  pop.style.left = `${clamp(x - w / 2, 8, innerWidth - w - 8)}px`;
  pop.style.top = `${clamp(y - h - 16, 8, innerHeight - h - 8)}px`;
}
function buildEditPop(a) {
  const pop = $("editPop");
  const opts = layerLabels(a.layer).map(l => `<option ${l === a.label ? "selected" : ""}>${l}</option>`).join("");
  const len = a.end_frame - a.start_frame + 1;
  pop.innerHTML =
    `<div class="ep-head"><span class="dot" style="background:${colorOf(a.label, a.layer)}"></span>` +
    `<b>${a.label}</b>&nbsp;<span class="dim">${a.layer}</span><span class="spacer"></span>` +
    `<button id="epClose" class="licon" title="${t("app.ep.close")}">✕</button></div>` +
    `<div class="field"><label>${t("app.ep.label")}</label><select id="epLabel">${opts}</select></div>` +
    `<div class="field"><label>${t("app.ep.start")}</label><input id="epStart" type="number" min="0" value="${a.start_frame}">` +
    `<button id="epStartHere" title="${t("app.ep.sethere")}">⌖</button><span class="dim">${fmtTime(a.start_frame)}</span></div>` +
    `<div class="field"><label>${t("app.ep.end")}</label><input id="epEnd" type="number" min="0" value="${a.end_frame}">` +
    `<button id="epEndHere" title="${t("app.ep.sethere")}">⌖</button><span class="dim">${fmtTime(a.end_frame)}</span></div>` +
    `<div class="field dim">${t("app.ep.len", len, (len / fps()).toFixed(2))}</div>` +
    `<div class="field"><button id="epDel" class="danger">${t("app.ep.del")}</button></div>`;
  const patchAndSeek = async (fields, seekTo) => { selectAnn(a.ann_id); await patchSel(fields); if (seekTo != null) setFrame(seekTo); refreshEditPop(); };
  pop.querySelector("#epClose").onclick = closeEditPop;
  pop.querySelector("#epLabel").onchange = (e) => patchAndSeek({ label: e.target.value });
  pop.querySelector("#epStart").onchange = (e) => { const v = parseInt(e.target.value, 10); if (!Number.isNaN(v)) patchAndSeek({ start_frame: v }, v); };
  pop.querySelector("#epEnd").onchange = (e) => { const v = parseInt(e.target.value, 10); if (!Number.isNaN(v)) patchAndSeek({ end_frame: v }, v); };
  pop.querySelector("#epStartHere").onclick = () => patchAndSeek({ start_frame: S.curFrame });
  pop.querySelector("#epEndHere").onclick = () => patchAndSeek({ end_frame: S.curFrame });
  pop.querySelector("#epDel").onclick = () => { const id = a.ann_id; closeEditPop(); deleteAnn(id); };
}
function refreshEditPop() {
  if (!editId) return;
  if ($("editPop").contains(document.activeElement)) return;   // 使用者輸入中不重繪
  const a = S.anns.find(x => x.ann_id === editId);
  if (!a) { closeEditPop(); return; }
  buildEditPop(a);
}
canvas.addEventListener("dblclick", (e) => {
  const hit = hitTimeline(e.offsetX, e.offsetY);
  if (hit) {
    // 雙擊＝編輯既有標註；快速雙擊在 temporal 工具下會誤觸「開始錄製」，此處取消之
    if (S.tmpTemporal && !S.pending) { S.tmpTemporal = null; updateTmpBar(); }
    selectAnn(hit.ann_id); setFrame(hit.start_frame); openEditPop(hit, e.clientX, e.clientY);
  }
});

// ============================================================ 右側面板 =====
// 只列「目前播放頭 frame 底下」的標註；label 後括號＝該 label 全片總數；分層隱藏（level/shape/label/單一）
function renderRight() {
  refreshEditPop(); updateBatchBar();
  const list = $("layersList"); list.innerHTML = "";
  const cf = S.curFrame;
  const atFrame = (a) => a.layer === "spatial" ? !!shapeAt(a, cf) : (a.start_frame <= cf && cf <= a.end_frame);
  const cur = S.anns.filter(atFrame);
  $("layersCount").textContent = t("app.layers_count", cur.length);
  const totalCount = new Map();
  for (const a of S.anns) { const k = `${a.layer}/${a.label}`; totalCount.set(k, (totalCount.get(k) || 0) + 1); }
  const eyeBtn = (hidden, on) => { const b = document.createElement("button"); b.className = "licon"; b.textContent = hidden ? "🚫" : "👁"; b.title = t("app.licon.hide"); b.onclick = (e) => { e.stopPropagation(); on(); }; return b; };
  const addGroupHeader = (title, hidden, kind, key, sub) => {
    const h = document.createElement("div"); h.className = "grp-head" + (sub ? " grp-sub" : ""); if (hidden) h.classList.add("hid");
    const t = document.createElement("span"); t.className = "grp-title"; t.textContent = title; h.appendChild(t);
    h.appendChild(eyeBtn(hidden, () => toggleHide(kind, key)));
    list.appendChild(h);
  };
  const addLabelHeader = (layer, label) => {
    const key = `${layer}/${label}`, hidden = S.hiddenLabels.has(key);
    const h = document.createElement("div"); h.className = "lbl-head"; if (hidden) h.classList.add("hid");
    h.innerHTML = `<span class="dot" style="background:${colorOf(label, layer)}"></span>` +
      `<span class="lname">${label} <span class="lkind">(${totalCount.get(key) || 0})</span></span>`;
    const selBtn = document.createElement("button"); selBtn.className = "licon"; selBtn.textContent = "☑";
    selBtn.title = t("app.licon.selectall");
    selBtn.onclick = (e) => { e.stopPropagation(); selectAllOf(layer, label, null); };
    h.appendChild(selBtn);
    h.appendChild(eyeBtn(hidden, () => toggleHide("label", key)));
    list.appendChild(h);
  };
  const addItem = (a) => {
    const it = document.createElement("div"); it.className = "layer-item indent"; if (a.ann_id === S.sel) it.classList.add("sel"); if (S.selSet.has(a.ann_id)) it.classList.add("multi"); if (isHidden(a)) it.classList.add("hid");
    const meta = a.layer === "spatial" ? `${a.attrs.shape} · f${a.start_frame}–${a.end_frame}` : `${fmtTime(a.start_frame)}–${fmtTime(a.end_frame)}`;
    it.innerHTML = `<span class="lname"><span class="lkind">${meta}</span></span>`;
    it.append(
      eyeBtn(S.hidden.has(a.ann_id), () => { if (S.hidden.has(a.ann_id)) S.hidden.delete(a.ann_id); else S.hidden.add(a.ann_id); draw(); drawOverlay(); renderRight(); }),
    );
    const del = document.createElement("button"); del.className = "licon"; del.textContent = "🗑"; del.title = t("app.licon.del"); del.onclick = (e) => { e.stopPropagation(); deleteAnn(a.ann_id); };
    it.append(del);
    it.onclick = (e) => { if (e.shiftKey) { toggleSel(a.ann_id); return; } selectAnn(a.ann_id); if (a.layer === "spatial") setFrame(a.attrs.keypoints[0].frame); else setFrame(a.start_frame); };
    list.appendChild(it);
  };
  // temporal：level 群組 → label → 單一標註
  for (const layer of temporalLayers()) {
    const arr = cur.filter(a => a.layer === layer); if (!arr.length) continue;
    addGroupHeader(layer.toUpperCase(), S.hiddenLevels.has(layer), "level", layer);
    for (const lb of [...new Set(arr.map(a => a.label))].sort()) {
      addLabelHeader(layer, lb);
      for (const a of arr.filter(x => x.label === lb).sort((p, q) => p.start_frame - q.start_frame)) addItem(a);
    }
  }
  // spatial：level 群組 → shape type 群組 → label → 單一標註
  const sp = cur.filter(a => a.layer === "spatial");
  if (sp.length) {
    addGroupHeader("SPATIAL", S.hiddenLevels.has("spatial"), "level", "spatial");
    for (const shp of SPATIAL_TOOLS) {
      const arr = sp.filter(a => a.attrs.shape === shp); if (!arr.length) continue;
      addGroupHeader(`${SHAPE_ICON[shp] || ""} ${shp}`, S.hiddenShapes.has(shp), "shape", shp, true);
      for (const lb of [...new Set(arr.map(a => a.label))].sort()) {
        addLabelHeader("spatial", lb);
        for (const a of arr.filter(x => x.label === lb)) addItem(a);
      }
    }
  }
  renderDetail();
}
function renderDetail() {
  const box = $("detailBox"), a = selAnn();
  $("btnPrevKp").classList.toggle("hidden", !(a && a.layer === "spatial"));
  $("btnNextKp").classList.toggle("hidden", !(a && a.layer === "spatial"));
  if (!a) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden"); box.innerHTML = `<h3>${t("app.detail.sel", a.label)} <span class="dim">${a.layer}</span></h3>`;
  const field = (html) => { const d = document.createElement("div"); d.className = "field"; d.innerHTML = html; box.appendChild(d); return d; };
  // start/end
  const se = field(`<label>Start</label><input id="dStart" type="number" min="0" value="${a.start_frame}"><button id="dStartHere" title="${t("app.ep.sethere")}">⌖</button>`);
  const en = field(`<label>End</label><input id="dEnd" type="number" min="0" value="${a.end_frame}"><button id="dEndHere" title="${t("app.ep.sethere")}">⌖</button>`);
  se.querySelector("#dStart").onchange = (e) => patchSel({ start_frame: parseInt(e.target.value, 10) });
  en.querySelector("#dEnd").onchange = (e) => patchSel({ end_frame: parseInt(e.target.value, 10) });
  se.querySelector("#dStartHere").onclick = () => patchSel({ start_frame: S.curFrame });
  en.querySelector("#dEndHere").onclick = () => patchSel({ end_frame: S.curFrame });
  // triplet
  if (a.layer === "triplet") for (const k of ["instrument", "verb", "target"]) {
    const opts = ['<option value="">—</option>'].concat(S.cfg.taxonomy.triplet[k].map(v => `<option ${a.attrs[k] === v ? "selected" : ""}>${v}</option>`));
    const d = field(`<label>${k}</label><select>${opts.join("")}</select>`);
    d.querySelector("select").onchange = (e) => patchSel({ attrs: { ...a.attrs, [k]: e.target.value } });
  }
  // spatial
  if (a.layer === "spatial") {
    const d = field(`<label>${t("app.detail.interp")}</label><input type="checkbox" id="dInterp" ${a.attrs.interpolate !== false ? "checked" : ""}>`);
    d.querySelector("#dInterp").onchange = (e) => patchSel({ attrs: { ...a.attrs, interpolate: e.target.checked } });
    const kpNote = document.createElement("div"); kpNote.className = "field dim"; kpNote.textContent = t("app.detail.kp_hint", a.attrs.keypoints.length); box.appendChild(kpNote);
    for (const kp of a.attrs.keypoints) { const row = document.createElement("div"); row.className = "kp-row"; const fr = document.createElement("span"); fr.className = "kp-frame"; fr.textContent = `◆ f${kp.frame}`; fr.onclick = () => setFrame(kp.frame); row.appendChild(fr); if (a.attrs.keypoints.length > 1) { const x = document.createElement("button"); x.textContent = "✕"; x.onclick = () => patchSel({ attrs: { ...a.attrs, keypoints: a.attrs.keypoints.filter(k => k.frame !== kp.frame) } }); row.appendChild(x); } box.appendChild(row); }
  }
  // temporal：抽樣生成 event
  if (a.layer !== "spatial") { const d = field(`<label>${t("app.detail.sample")}</label><button id="dExtract">${t("app.detail.gen_event")}</button>`); d.querySelector("#dExtract").onclick = () => openExtract(a); }
  // 動作
  const act = field(`<button id="dJump">${t("app.detail.jump")}</button><button id="dHide">${isHidden(a) ? t("app.detail.show") : t("app.detail.hide")}</button><button id="dDel" class="danger">${t("app.detail.del")}</button>`);
  act.querySelector("#dJump").onclick = () => setFrame(a.start_frame);
  act.querySelector("#dHide").onclick = () => { if (S.hidden.has(a.ann_id)) S.hidden.delete(a.ann_id); else S.hidden.add(a.ann_id); draw(); drawOverlay(); renderRight(); };
  act.querySelector("#dDel").onclick = () => deleteAnn(a.ann_id);
}

// ============================================================ transport =====
function updateTransport() {
  $("btnPlay").textContent = video.paused ? "▶" : "⏸";
  $("timeDisplay").textContent = S.showFrames ? `f${S.curFrame} / f${nFrames() - 1}` : `${fmtTime(S.curFrame)} / ${fmtTime(Math.max(0, nFrames() - 1))}`;
  const Z = Math.round(S.view.ppf / S.fitPpf * 100); $("zoomBtn").textContent = S.view.ppf >= 5 ? "frame" : `${Z}%`;
}
$("btnPlay").onclick = () => (video.paused ? video.play() : video.pause());
$("btnRewind").onclick = () => setFrame(0);
$("btnEnd").onclick = () => setFrame(nFrames() - 1);
$("btnPrevF").onclick = () => setFrame(S.curFrame - 1);
$("btnNextF").onclick = () => setFrame(S.curFrame + 1);
$("speed").onchange = (e) => { video.playbackRate = parseFloat(e.target.value); };
$("timeDisplay").onclick = () => { S.showFrames = !S.showFrames; updateTransport(); draw(); };
$("gotoFrame").onchange = (e) => { const f = parseInt(e.target.value, 10); if (!Number.isNaN(f)) setFrame(f); e.target.value = ""; };
video.addEventListener("play", updateTransport); video.addEventListener("pause", updateTransport);
// 上一個/下一個標註：只在「顯示中」的標註間跳轉（跳過被眼睛鈕隱藏的；收合不算隱藏）
const navStarts = () => [...new Set(S.anns.filter(a => !isHidden(a)).map(a => a.start_frame))].sort((x, y) => x - y);
function gotoPrevAnn() { const f = navStarts().reverse().find(x => x < S.curFrame); if (f != null) setFrame(f); }
function gotoNextAnn() { const f = navStarts().find(x => x > S.curFrame); if (f != null) setFrame(f); }
$("btnPrevEv").onclick = gotoPrevAnn;
$("btnNextEv").onclick = gotoNextAnn;
function selSpatial() { const a = selAnn(); return a && a.layer === "spatial" ? a : null; }
$("btnPrevKp").onclick = () => { const a = selSpatial(); if (!a) return; const f = a.attrs.keypoints.map(k => k.frame).reverse().find(x => x < S.curFrame); if (f != null) setFrame(f); };
$("btnNextKp").onclick = () => { const a = selSpatial(); if (!a) return; const f = a.attrs.keypoints.map(k => k.frame).find(x => x > S.curFrame); if (f != null) setFrame(f); };

// ============================================================ 影片縮放 =====
// 平移 clamp：捲動不得超出影像邊緣；縮放後影像在該軸仍小於視窗時鎖定置中（不可捲）
// 兩軸可平移範圍（單一事實來源：clampVzoom 與平移捲動 bar 共用同一套邊界定義）
function vzoomBounds() {
  const k = S.vzoom.scale, area = $("videoArea");
  const ax = (off, len, view) => ({ lo: view - (off + len) * k, hi: -off * k });  // [影像底/右緣貼視窗, 影像頂/左緣貼視窗]
  return { x: ax(video.offsetLeft, video.offsetWidth, area.clientWidth),
           y: ax(video.offsetTop, video.offsetHeight, area.clientHeight) };
}
function clampVzoom() {
  const z = S.vzoom;
  if (z.scale <= 1.001) { z.tx = 0; z.ty = 0; return; }
  if (!video.videoWidth) return;
  const b = vzoomBounds();
  const one = (t, { lo, hi }) => (lo > hi ? (lo + hi) / 2 : clamp(t, lo, hi));
  z.tx = one(z.tx, b.x); z.ty = one(z.ty, b.y);
}
$("videoArea").addEventListener("wheel", (e) => {
  if (!S.video) return; e.preventDefault(); const z = S.vzoom;
  if (e.ctrlKey) { const r = videoZoom.getBoundingClientRect(), qx = e.clientX - r.left, qy = e.clientY - r.top, k = clamp(z.scale * (e.deltaY < 0 ? 1.2 : 1 / 1.2), 1, 10) / z.scale; z.tx += qx * (1 - k); z.ty += qy * (1 - k); z.scale *= k; if (z.scale <= 1.001) { z.scale = 1; z.tx = 0; z.ty = 0; } }
  else if (z.scale > 1.001) {   // 100% 時滾輪不動畫面；放大後才可捲動，且不超出影像邊緣
    if (e.shiftKey) z.tx -= e.deltaY; else z.ty -= e.deltaY;
    clampVzoom();
  } else return;
  applyVzoom();
}, { passive: false });
$("videoArea").addEventListener("dblclick", (e) => { if (e.target.closest(".vsb") || isSpatialTool() || S.sel || S.draw || S.pending) return; S.vzoom = { scale: 1, tx: 0, ty: 0 }; applyVzoom(); });
function applyVzoom() { const z = S.vzoom; videoZoom.style.transform = `translate(${z.tx}px,${z.ty}px) scale(${z.scale})`; drawOverlay(); updateVBars(); }

// -------- zoom >100% 的平移捲動 bar（自繪 thumb；scale ≤1 一律隱藏）-----------
// thumb 位置/長度反映目前視窗在影像上的位置；拖曳 thumb＝平移畫面，
// 平移量一律經 clampVzoom() 夾在影像邊緣（與滾輪捲動同一套 clamp）。
function updateVBars() {
  const bx = $("vsbX"), by = $("vsbY"); if (!bx || !by) return;
  const z = S.vzoom;
  if (z.scale <= 1.001 || !video.videoWidth) { bx.classList.add("hidden"); by.classList.add("hidden"); return; }
  const b = vzoomBounds();
  const set = (bar, bb, t, horiz) => {
    const range = bb.hi - bb.lo;               // 可平移距離（px）；≤0＝該軸影像未超出視窗（置中鎖定）
    if (range <= 1) { bar.classList.add("hidden"); return; }
    bar.classList.remove("hidden");
    const area = $("videoArea"), view = horiz ? area.clientWidth : area.clientHeight;
    const trackLen = horiz ? bar.clientWidth : bar.clientHeight;
    const len = clamp(trackLen * view / (view + range), 24, trackLen);   // thumb 長度 ∝ 可見比例（view+range＝影像縮放後長度）
    const pos = (bb.hi - t) / range * (trackLen - len);
    const th = bar.firstElementChild;
    if (horiz) { th.style.width = `${len}px`; th.style.left = `${pos}px`; }
    else { th.style.height = `${len}px`; th.style.top = `${pos}px`; }
  };
  set(bx, b.x, z.tx, true); set(by, b.y, z.ty, false);
}
(function wireVsb() {
  for (const [id, axis] of [["vsbX", "x"], ["vsbY", "y"]]) {
    const bar = $(id), horiz = axis === "x";
    let drag = null;
    bar.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();          // 不觸發繪圖 / 選取
      if (e.button !== 0) return;
      const b = vzoomBounds()[axis], range = b.hi - b.lo;
      if (range <= 0) return;
      const th = bar.firstElementChild;
      const trackLen = horiz ? bar.clientWidth : bar.clientHeight;
      const thumbLen = horiz ? th.offsetWidth : th.offsetHeight;
      const r = bar.getBoundingClientRect();
      const pos = horiz ? e.clientX - r.left : e.clientY - r.top;
      if (e.target !== th) {                            // 點軌道：thumb 中心跳到點擊處
        const frac = clamp((pos - thumbLen / 2) / Math.max(1, trackLen - thumbLen), 0, 1);
        if (horiz) S.vzoom.tx = b.hi - frac * range; else S.vzoom.ty = b.hi - frac * range;
        clampVzoom(); applyVzoom();
      }
      drag = { pos, t: horiz ? S.vzoom.tx : S.vzoom.ty, k: range / Math.max(1, trackLen - thumbLen) };
      bar.classList.add("dragging");
    });
    document.addEventListener("mousemove", (e) => {
      if (!drag) return;
      const r = bar.getBoundingClientRect();
      const pos = horiz ? e.clientX - r.left : e.clientY - r.top;
      const t = drag.t - (pos - drag.pos) * drag.k;     // thumb 往右/下 → 影像往左/上（t 變小）
      if (horiz) S.vzoom.tx = t; else S.vzoom.ty = t;
      clampVzoom(); applyVzoom();                       // 統一夾在影像邊緣
    });
    document.addEventListener("mouseup", () => { if (drag) { drag = null; bar.classList.remove("dragging"); } });
  }
})();

// ============================================================ 複製 / 貼上 ==
function copyShapes(all) {
  const src = all ? S.anns.filter(a => a.layer === "spatial" && shapeAt(a, S.curFrame)) : [selAnn()].filter(a => a && a.layer === "spatial" && shapeAt(a, S.curFrame));
  S.clip = src.map(a => ({ kind: "spatial", label: a.label, shape: a.attrs.shape, points: shapeAt(a, S.curFrame).slice() }));
  toast(S.clip.length ? t("app.toast.copied_shapes", S.clip.length) : t("app.toast.no_copy_shapes"));
}
// 多選複製：spatial 複製當前 frame 形狀；temporal 以選取中最早的 start 為基準，貼上時整組平移到目前 frame
function copyMulti() {
  const sel = S.anns.filter(a => S.selSet.has(a.ann_id));
  const temporal = sel.filter(a => a.layer !== "spatial");
  const base = temporal.length ? Math.min(...temporal.map(a => a.start_frame)) : S.curFrame;
  S.clip = [];
  for (const a of sel) {
    if (a.layer === "spatial") { const pts = shapeAt(a, S.curFrame); if (pts) S.clip.push({ kind: "spatial", label: a.label, shape: a.attrs.shape, points: pts.slice() }); }
    else S.clip.push({ kind: "temporal", layer: a.layer, label: a.label, dStart: a.start_frame - base, len: a.end_frame - a.start_frame });
  }
  toast(S.clip.length ? t("app.toast.copied_multi", S.clip.length) : t("app.toast.no_copy_spatial"), 2600);
}
async function pasteShapes() {
  if (!S.clip.length) { toast(t("app.toast.clip_empty")); return; }
  let last = null, n = 0; const ops = [];
  for (const c of S.clip) {
    try {
      let a;
      if (c.kind === "temporal") {
        const start = clamp(S.curFrame + c.dStart, 0, nFrames() - 1);
        const end = clamp(start + c.len, 0, nFrames() - 1);
        a = await api("POST", `/api/videos/${S.video.video_id}/annotations`, annBody({ layer: c.layer, label: c.label, start_frame: start, end_frame: end, attrs: {} }));
      } else {
        a = await api("POST", `/api/videos/${S.video.video_id}/annotations`, annBody({ layer: "spatial", label: c.label, start_frame: S.curFrame, end_frame: S.curFrame, attrs: { shape: c.shape, interpolate: true, keypoints: [{ frame: S.curFrame, points: c.points.slice() }] } }));
      }
      S.anns.push(a); last = a; n++; ops.push({ type: "create", ann: snapAnn(a) });
    } catch (e) { toast(e.message); }
  }
  pushUndo(t("app.undo.paste", n), ops);
  if (last) { if (n === 1) selectAnn(last.ann_id); toast(t("app.toast.pasted", n), 1500); draw(); drawOverlay(); renderRight(); }
}
function toggleAutoClass() {
  if (!S.lastClass.spatial) { toast(t("app.toast.no_lastclass")); return; }
  S.autoClass = !S.autoClass;
  if (S.autoClass && !isSpatialTool()) setTool("bbox");
  toast(S.autoClass ? t("app.toast.autoclass_on", S.lastClass.spatial) : t("app.toast.autoclass_off"), 2800);
}

// ============================================================ extract ======
let extractA = null;
function openExtract(a) {
  extractA = a; const ev = layerLabels("event"); if (!ev.length) { toast(t("app.toast.no_event_label")); return; }
  $("extractLabel").innerHTML = ev.map(l => `<option>${l}</option>`).join("");
  $("extractDesc").textContent = t("app.extract.desc", a.layer, a.label, a.start_frame, a.end_frame);
  updateExtractPreview(); openModal("extractOverlay");
}
function updateExtractPreview() { if (!extractA) return; const sec = parseFloat($("extractInterval").value), step = Math.max(1, Math.round(sec * fps())); const n = Math.floor((extractA.end_frame - extractA.start_frame) / step) + 1; $("extractPreview").textContent = t("app.extract.preview", n, sec); }
async function runExtract() { if (!extractA) return; try { const res = await api("POST", `/api/videos/${S.video.video_id}/generate-events`, annBody({ label: $("extractLabel").value, interval_sec: parseFloat($("extractInterval").value), start_frame: extractA.start_frame, end_frame: extractA.end_frame })); S.anns.push(...res.created); pushUndo(t("app.undo.gen_event", res.count), res.created.map(c => ({ type: "create", ann: snapAnn(c) }))); closeModal("extractOverlay"); toast(t("app.toast.gen_event", res.count), 2000); draw(); renderRight(); } catch (e) { toast(e.message); } }

// ============================================================ modal / 選單 =
function openModal(id) { $(id).classList.remove("hidden"); }
function closeModal(id) { $(id).classList.add("hidden"); }
function wireUI() {
  const drop = $("menuDrop");
  $("menuBtn").onclick = (e) => { e.stopPropagation(); drop.classList.toggle("hidden"); };
  document.addEventListener("click", () => drop.classList.add("hidden"));
  drop.addEventListener("click", (e) => e.stopPropagation());
  $("menuKeys").onclick = () => { drop.classList.add("hidden"); openModal("keysOverlay"); };
  $("keysTable").innerHTML = KEYS.map(([k, d]) => `<tr><td><kbd>${t(k)}</kbd></td><td>${t(d)}</td></tr>`).join("");
  $("libBtn").onclick = () => location.href =
    S.project ? `project.html?p=${encodeURIComponent(S.project)}` : "/";   // 回到來源專案頁
  document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => closeModal(b.dataset.close));
  document.querySelectorAll(".modal-overlay").forEach(ov => ov.addEventListener("mousedown", (e) => { if (e.target === ov) ov.classList.add("hidden"); }));
  $("extractInterval").onchange = updateExtractPreview; $("extractGo").onclick = runExtract;
  $("tmpOk").onclick = () => finishTemporal();
  $("tmpCancel").onclick = () => cancelPending();
  // 批次操作列（多選集合）
  $("batchLabel").onclick = () => openBatchClassPop();
  $("batchDel").onclick = () => deleteMany([...S.selSet]);
  $("batchClear").onclick = () => selectAnn(null);
  document.addEventListener("mousedown", (e) => { if (editId && !$("editPop").contains(e.target)) closeEditPop(); });
  // class popup 搜尋
  $("cpSearch").addEventListener("input", () => { cpState.hi = 0; renderCpList(); });
  $("cpSearch").addEventListener("keydown", (e) => {
    if (!cpState) return; const n = cpState.rows.length;
    if (e.key === "ArrowDown") { e.preventDefault(); cpState.hi = Math.min(n - 1, cpState.hi + 1); renderCpList(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cpState.hi = Math.max(0, cpState.hi - 1); renderCpList(); }
    else if (e.key === "Enter") { e.preventDefault(); const row = cpState.rows[cpState.hi]; if (!row) return; if (row.isNew) createAndAssign(cpLayer(), row.label); else assignClass(cpLayer(), row.label); }
    else if (e.key === "Escape") { e.preventDefault(); cancelPending(); }
  });
}
function cancelPending() { S.pending = null; S.tmpTemporal = null; closeClassPop(); updateTmpBar(); draw(); drawOverlay(); }

// ============================================================ 快捷鍵 =======
window.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
    // 例外：焦點停在 samBar 控件（拖完滑桿的殘留焦點）時，Enter/Esc 仍要作用——
    // blur 後 Enter＝接受預覽、Esc＝清空 session（修復「按 Enter 沒反應」）
    if (document.activeElement.closest?.("#samBar") && (e.key === "Enter" || e.key === "Escape")) {
      e.preventDefault(); document.activeElement.blur();
      if (e.key === "Enter") { if (S.tool === "smart" && S.sam) samAccept(); }
      else samClear();
    }
    return;
  }
  const modal = document.querySelector(".modal-overlay:not(.hidden)");
  if (modal) { if (e.key === "Escape") modal.classList.add("hidden"); return; }
  if (S.browse) {   // 瀏覽模式：只保留播放類快捷鍵，標註快捷鍵全部停用
    if (e.key === " ") { e.preventDefault(); $("btnPlay").onclick(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); setFrame(S.curFrame - (e.shiftKey ? 10 : 1)); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setFrame(S.curFrame + (e.shiftKey ? 10 : 1)); }
    else if (e.key === "Home") setFrame(0);
    else if (e.key === "End") setFrame(nFrames() - 1);
    return;
  }
  if (!$("classPop").classList.contains("hidden")) { if (e.key === "Escape") cancelPending(); return; }
  if (e.ctrlKey && !e.altKey) {
    const k = e.key.toLowerCase();
    if (k === "c") { e.preventDefault(); if (S.selSet.size) copyMulti(); else copyShapes(e.shiftKey); }
    else if (k === "v") { e.preventDefault(); pasteShapes(); }
    else if (k === "z" && !e.shiftKey) {
      e.preventDefault();
      // 繪製 polygon/polyline 中：Ctrl+Z = 移除上一個頂點（繪圖工具慣例，優先於全域 undo）
      if (S.draw?.stage === "poly" && S.draw.pts.length) { S.draw.pts.splice(-2); if (!S.draw.pts.length) cancelDraw(); else drawOverlay(); }
      else if (S.tool === "smart" && samUndo()) { /* smart prompt 逐步撤銷；session 空時放行 */ }
      else doUndo();
    }
    else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); doRedo(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); setFrame(S.curFrame - 1); }    // Ctrl+← = 上一 frame
    else if (e.key === "ArrowRight") { e.preventDefault(); setFrame(S.curFrame + 1); }   // Ctrl+→ = 下一 frame
    return;
  }
  const k0 = e.key.toLowerCase();
  if (k0 === "t" && !e.repeat) { toggleTemporalRec(); return; }                 // T＝開始/取消時段標註（單次性）
  if (k0 === "e" && !e.repeat && S.sel) { toggleEditMode(); return; }           // 有選取時 E＝編輯模式；無選取時往下＝橢圓工具
  const T = { v: "select", b: "bbox", p: "polygon", o: "point", e: "ellipse", l: "polyline", s: "smart" };
  if (T[k0] && !e.repeat) { setTool(T[k0]); toast(t("app.toast.tool", T[k0]), 800); return; }
  if (e.key === "n" || e.key === "N") { toggleAutoClass(); return; }
  switch (e.key) {
    case " ": e.preventDefault(); $("btnPlay").onclick(); break;
    case "ArrowLeft": e.preventDefault(); if (e.shiftKey) setFrame(S.curFrame - 10); else gotoPrevAnn(); break;
    case "ArrowRight": e.preventDefault(); if (e.shiftKey) setFrame(S.curFrame + 10); else gotoNextAnn(); break;
    case "Home": setFrame(0); break;
    case "End": setFrame(nFrames() - 1); break;
    case "Enter":
      if (editId) { e.preventDefault(); closeEditPop(); }        // 編輯視窗開著時 Enter = 關閉，不新增
      else if (S.draw?.stage === "poly") { e.preventDefault(); finishPoly(); }
      else if (S.tool === "smart" && S.sam) { e.preventDefault(); samAccept(); }
      else if (S.tmpTemporal && !S.pending) { e.preventDefault(); finishTemporal(); }
      break;
    case "Escape": if (editId) closeEditPop(); else if (S.draw) cancelDraw(); else if (S.trace) { S.trace = null; drawOverlay(); } else if (S.sam) samClear(); else if (S.pending || S.tmpTemporal) cancelPending(); else if (S.editMode) toggleEditMode(); else if (S.sel || S.selSet.size) selectAnn(null); break;
    case "Delete": case "Backspace": if (S.selSet.size) deleteMany([...S.selSet]); else if (S.sel) deleteAnn(S.sel); break;
  }
});

// ------------------------------------------------------------------ 啟動 ----
window.addEventListener("resize", () => { resizeCanvas(); if (S.video) { S.fitPpf = usableW() / nFrames(); S.view.ppf = Math.max(S.view.ppf, S.fitPpf); draw(); } sizeOverlay(); });
resizeCanvas();
boot().catch(e => toast(t("app.toast.boot_fail", e.message), 8000));
