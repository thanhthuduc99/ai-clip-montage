const $ = (s) => document.querySelector(s);
const CHUNK = 8 * 1024 * 1024;

let brands = [];
let sources = [];
let voices = [];
let draft = null;
let renderJobId = null;
let srcPoll = null;
let renderPoll = null;

const currentBrand = () => $("#brandSelect").value || "";
const brandById = (id) => brands.find((b) => b.id === id);
const readySources = () => sources.filter((s) => s.status === "ready");
const srcById = (id) => sources.find((s) => s.id === id);

/** Tốc độ đọc của giọng đang chọn. Mỗi giọng một khác, Vivibe nhanh gần gấp đôi
 *  Edge, nên dùng chung một hằng số là ước sai thời lượng đoạn. */
function currentWps() {
  const v = voices.find((x) => x.id === ($("#voice") || {}).value);
  return (v && v.wps) || 3.3;
}
const speakSeconds = (vo) =>
  String(vo || "").trim().split(/\s+/).filter(Boolean).length / currentWps();

function icon(name, size = 16, stroke = 1.5) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** Thẻ trong lưới hiện lần lượt thay vì bật ra cùng lúc. Trần 12 thẻ để lưới
 *  dài không phải đợi cả giây mới thấy thẻ cuối. */
function stagger(el, i) {
  el.classList.add("reveal");
  el.style.animationDelay = Math.min(i, 12) * 45 + "ms";
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = icon(kind === "err" ? "alert" : "check") + `<span>${esc(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 3200);
}

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"),
                 headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.success === false) throw new Error(data.error || `Lỗi ${r.status}`);
  return data;
}

// ── Trạng thái có đồng hồ đếm ─────────────────────────────────────
function setStatus(el, kind, html) {
  el.className = "status " + kind;
  el.innerHTML = html;
}

function startClock(el) {
  const t0 = Date.now();
  const span = el.querySelector(".elapsed");
  if (!span) return () => {};
  const id = setInterval(() => {
    span.textContent = `${Math.round((Date.now() - t0) / 1000)}s`;
  }, 1000);
  return () => clearInterval(id);
}

// ── Modal ─────────────────────────────────────────────────────────
function confirmDialog(title, body, okLabel = "Xóa") {
  return new Promise((resolve) => {
    const dlg = $("#dlgConfirm");
    $("#cfTitle").textContent = title;
    $("#cfBody").textContent = body;
    $("#cfOk").textContent = okLabel;
    const ok = () => { cleanup(); dlg.close(); resolve(true); };
    const cancel = () => { if (!done) { cleanup(); resolve(false); } };
    let done = false;
    function cleanup() { done = true; $("#cfOk").removeEventListener("click", ok);
                         dlg.removeEventListener("close", cancel); }
    $("#cfOk").addEventListener("click", ok);
    dlg.addEventListener("close", cancel, { once: true });
    dlg.showModal();
  });
}

// ── Điều hướng ────────────────────────────────────────────────────
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => showPage(b.dataset.page);
});

function showPage(name) {
  document.querySelectorAll("nav button").forEach((x) => x.removeAttribute("aria-current"));
  document.querySelectorAll(".page").forEach((x) => x.classList.remove("active"));
  document.querySelector(`nav button[data-page="${name}"]`).setAttribute("aria-current", "page");
  $("#page-" + name).classList.add("active");
  if (name === "sources") loadSources();
  if (name === "create") refreshCreateHint();
  if (name === "output") loadVideos();
}

// ── Thương hiệu ───────────────────────────────────────────────────
async function loadBrands(keepId) {
  const data = await api("/brands");
  brands = data.brands || [];
  const sel = $("#brandSelect");
  const want = keepId || sel.value;
  sel.innerHTML = brands.length
    ? brands.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")
    : '<option value="">Chưa có thương hiệu</option>';
  if (want && brands.some((b) => b.id === want)) sel.value = want;
  $("#btnBrandEdit").disabled = !brands.length;
  paintBrandContext();
}

function paintBrandContext() {
  const b = brandById(currentBrand());
  const el = $("#brandContext");
  if (!b) {
    el.textContent = "Chưa có thương hiệu nào. Bấm dấu cộng trên thanh trên cùng để tạo.";
    return;
  }
  el.textContent = b.context
    ? b.context
    : "Thương hiệu này chưa có bối cảnh. Bấm nút bút chì để mô tả, AI sẽ viết đúng hơn.";
}

$("#brandSelect").onchange = () => { paintBrandContext(); loadSources(); };

function openBrandDialog(brand) {
  const dlg = $("#dlgBrand");
  $("#dlgBrandTitle").textContent = brand ? "Sửa thương hiệu" : "Thêm thương hiệu";
  $("#brandName").value = brand ? brand.name : "";
  $("#brandCtx").value = brand ? (brand.context || "") : "";
  $("#btnBrandDel").hidden = !brand;
  $("#btnBrandSave").onclick = async () => {
    const name = $("#brandName").value.trim();
    if (!name) { toast("Nhập tên thương hiệu.", "err"); $("#brandName").focus(); return; }
    try {
      const payload = { name, context: $("#brandCtx").value.trim() };
      if (brand) payload.id = brand.id;
      const res = await api("/brands/save", payload);
      dlg.close();
      await loadBrands(res.brand.id);
      loadSources();
      toast(brand ? "Đã lưu thương hiệu." : `Đã tạo "${name}".`, "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  $("#btnBrandDel").onclick = async () => {
    const ok = await confirmDialog("Xóa thương hiệu",
      `Xóa "${brand.name}"? Phải xóa hết clip của thương hiệu này trước.`);
    if (!ok) return;
    try {
      await api("/brands/delete", { id: brand.id });
      dlg.close();
      await loadBrands();
      loadSources();
      toast("Đã xóa thương hiệu.", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  dlg.showModal();
  $("#brandName").focus();
}

$("#btnBrandNew").onclick = () => openBrandDialog(null);
$("#btnBrandEdit").onclick = () => {
  const b = brandById(currentBrand());
  if (b) openBrandDialog(b);
};

// ── Upload ────────────────────────────────────────────────────────
$("#btnUpload").onclick = async () => {
  const files = Array.from($("#fileInput").files || []);
  if (!files.length) return toast("Chọn file clip trước.", "err");
  if (!currentBrand()) return toast("Tạo thương hiệu trước khi tải clip.", "err");

  const btn = $("#btnUpload");
  const bar = $("#upProg i");
  btn.disabled = true;
  $("#upProg").hidden = false;
  const total = files.reduce((s, f) => s + f.size, 0);
  let sent = 0;

  try {
    const init = await api("/uploads/init", {
      files: files.map((f, i) => ({ field: "clip" + i, name: f.name, size: f.size, type: f.type })),
    });
    const session = init.upload;
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      const meta = session.files[i];
      setStatus($("#upStatus"), "busy",
        `${icon("loader")}<span class="spin"></span> Đang tải ${i + 1}/${files.length}: ${esc(f.name)}`);
      let offset = 0;
      while (offset < f.size) {
        const slice = f.slice(offset, offset + CHUNK);
        const r = await fetch(`/uploads/${session.id}/${meta.id}`, {
          method: "PUT",
          headers: { "X-Upload-Offset": String(offset), "Content-Type": "application/octet-stream" },
          body: await slice.arrayBuffer(),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Lỗi khi tải lên");
        offset += slice.size;
        sent += slice.size;
        bar.style.width = Math.round((sent / total) * 100) + "%";
      }
    }
    const done = await api(`/uploads/${session.id}/complete`, { brand: currentBrand() });
    setStatus($("#upStatus"), "busy",
      `${icon("loader")} Đã thêm ${done.sources.length} clip. AI đang xem nội dung...`);
    $("#fileInput").value = "";
    await loadSources();
    startSourcePolling();
  } catch (e) {
    toast(e.message, "err");
    setStatus($("#upStatus"), "err", `${icon("alert")} ${esc(e.message)}`);
  } finally {
    btn.disabled = false;
    setTimeout(() => { $("#upProg").hidden = true; bar.style.width = "0"; }, 600);
  }
};

// ── Kho clip ──────────────────────────────────────────────────────
const STATUS_TEXT = { pending: "chờ", analyzing: "đang xem", ready: "xong", error: "lỗi" };

async function loadSources() {
  const data = await api("/sources?brand=" + encodeURIComponent(currentBrand()));
  sources = data.sources;
  $("#srcCount").textContent = sources.length ? `(${sources.length})` : "";
  refreshCreateHint();

  const grid = $("#srcGrid");
  grid.innerHTML = "";
  $("#srcEmpty").innerHTML = sources.length ? "" : emptyState(
    "image",
    currentBrand() ? "Kho clip đang trống" : "Chưa chọn thương hiệu",
    currentBrand()
      ? "Tải vài clip quay sẵn lên. AI sẽ xem từng clip rồi mô tả lại, sau đó tự chọn clip khớp với kịch bản."
      : "Tạo thương hiệu ở thanh trên cùng trước, rồi mới tải clip lên.");

  sources.forEach((s, idx) => {
    const el = document.createElement("div");
    el.className = "src";
    stagger(el, idx);
    const tags = (s.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("");
    const dur = s.duration ? `${s.duration.toFixed(1)}s` : "";
    const used = s.times_used > 2 ? "badge hot" : "badge";
    el.innerHTML = `
      <button class="well wide" data-open aria-label="Xem clip ${esc(s.original_name)}"
              style="border:0;padding:0;width:100%;cursor:pointer">
        <img src="${s.thumb_url}" alt="Khung hình đầu của ${esc(s.original_name)}" loading="lazy">
        <span class="play">${icon("play", 28, 1.2)}</span>
      </button>
      <div class="body">
        <div class="meta">
          <span class="pill ${s.status}"><span class="dot"></span>${STATUS_TEXT[s.status] || s.status}</span>
          <span class="${used}">${dur}${dur && s.times_used ? " · " : ""}${s.times_used ? "dùng " + s.times_used : ""}</span>
        </div>
        <div class="desc${s.error ? " err" : ""}">${
          esc(s.description) || (s.error ? esc(s.error) : "Đang chờ AI xem clip...")}</div>
        <div class="tags">${tags}</div>
        <div class="name" title="${esc(s.original_name)}">${esc(s.original_name)}</div>
        <div class="acts">
          <button class="btn ghost sm" data-edit>${icon("pencil", 14)} Sửa</button>
          <button class="btn ghost sm" data-re title="Cho AI xem lại clip">${icon("refresh", 14)}</button>
          <button class="btn danger sm" data-del title="Xóa clip">${icon("trash", 14)}</button>
        </div>
      </div>`;

    el.querySelector("[data-open]").onclick = () => window.open(s.video_url, "_blank");
    el.querySelector("[data-edit]").onclick = () => openClipDialog(s);
    el.querySelector("[data-re]").onclick = async () => {
      try {
        await api("/sources/reanalyze", { id: s.id });
        toast("Đã xếp hàng cho AI xem lại.", "ok");
        await loadSources();
        startSourcePolling();
      } catch (e) { toast(e.message, "err"); }
    };
    el.querySelector("[data-del]").onclick = async () => {
      const ok = await confirmDialog("Xóa clip", `Xóa "${s.original_name}" khỏi kho?`);
      if (!ok) return;
      try {
        await api("/sources/delete", { id: s.id });
        await loadSources();
        toast("Đã xóa clip.", "ok");
      } catch (e) { toast(e.message, "err"); }
    };
    grid.appendChild(el);
  });
}

function emptyState(ic, title, text, btnLabel, btnFn) {
  const id = "es" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => { if (btnFn) { const b = document.getElementById(id); if (b) b.onclick = btnFn; } }, 0);
  return `<div class="empty">
    ${icon(ic, 40, 1.2)}
    <h3>${esc(title)}</h3>
    <p>${esc(text)}</p>
    ${btnLabel ? `<button class="btn" id="${id}">${esc(btnLabel)}</button>` : ""}
  </div>`;
}

function startSourcePolling() {
  if (srcPoll) clearInterval(srcPoll);
  srcPoll = setInterval(async () => {
    await loadSources();
    if (!sources.some((s) => s.status === "pending" || s.status === "analyzing")) {
      clearInterval(srcPoll);
      srcPoll = null;
      const bad = sources.filter((s) => s.status === "error").length;
      setStatus($("#upStatus"), bad ? "err" : "ok",
        bad ? `${icon("alert")} ${bad} clip phân tích lỗi, xem chi tiết trên thẻ clip.`
            : `${icon("check")} AI đã xem xong toàn bộ clip.`);
      setTimeout(() => setStatus($("#upStatus"), "", ""), 6000);
    }
  }, 4000);
}

function openClipDialog(s) {
  const dlg = $("#dlgClip");
  $("#clipThumb").src = s.thumb_url;
  $("#clipThumb").alt = "Khung hình của " + s.original_name;
  $("#clipMeta").textContent =
    [s.original_name, s.duration ? s.duration.toFixed(1) + "s" : "",
     s.width ? `${s.width}x${s.height}` : ""].filter(Boolean).join(" · ");
  $("#clipDesc").value = s.description || "";
  $("#clipTags").value = (s.tags || []).join(", ");
  $("#btnClipSave").onclick = async () => {
    try {
      await api("/sources/update", {
        id: s.id,
        description: $("#clipDesc").value.trim(),
        tags: $("#clipTags").value,
      });
      dlg.close();
      await loadSources();
      toast("Đã lưu mô tả clip.", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
  dlg.showModal();
  $("#clipDesc").focus();
}

// ── Giọng đọc ─────────────────────────────────────────────────────
async function loadVoices() {
  const data = await api("/voices");
  voices = data.voices || [];
  const sel = $("#voice");
  sel.innerHTML = voices.map((v) =>
    `<option value="${v.id}"${v.ready ? "" : " disabled"}>${esc(v.label)}${
      v.ready ? "" : " — thiếu key"}</option>`).join("");
  const usable = voices.find((v) => v.id === data.default && v.ready)
    || voices.find((v) => v.ready);
  if (usable) sel.value = usable.id;
  paintVoiceHint();
}

function paintVoiceHint() {
  const v = voices.find((x) => x.id === $("#voice").value);
  if (!v) { $("#voiceHint").textContent = ""; return; }
  $("#voiceHint").textContent = v.words
    ? `Giọng này đọc ${v.wps} từ mỗi giây, AI sẽ viết lời cho vừa thời lượng. `
      + "Phụ đề chia đều theo số chữ nên có thể lệch nhẹ với tiếng đọc."
    : `Giọng này đọc ${v.wps} từ mỗi giây và trả về mốc thời gian từng chữ, `
      + "phụ đề khớp chính xác với tiếng đọc.";
}

$("#voice").onchange = () => {
  paintVoiceHint();
  // Đổi giọng là đổi tốc độ đọc, mọi ước tính thời lượng đoạn phải tính lại.
  if (draft) renderDraft();
};

// ── Tạo video ─────────────────────────────────────────────────────
function refreshCreateHint() {
  const b = brandById(currentBrand());
  const n = readySources().length;
  $("#createHint").textContent = b
    ? `${b.name} · ${n} clip sẵn sàng trong kho`
    : "Chưa chọn thương hiệu.";
  $("#btnGenerate").disabled = !b || n < 3;
  if (b && n < 3) {
    $("#createHint").textContent =
      `${b.name} · mới có ${n} clip sẵn sàng, cần ít nhất 3 clip để AI dựng được kịch bản.`;
  }
}

$("#btnGenerate").onclick = async () => {
  const topic = $("#topic").value.trim();
  if (!topic) { toast("Nhập chủ đề trước.", "err"); $("#topic").focus(); return; }
  if (!currentBrand()) return toast("Chưa chọn thương hiệu.", "err");

  const btn = $("#btnGenerate");
  btn.disabled = true;
  $("#draftCard").hidden = true;
  $("#genSkeleton").hidden = false;
  setStatus($("#genStatus"), "busy",
    `${icon("loader")} AI đang viết kịch bản và chọn clip, thường mất 15 đến 30 giây <span class="elapsed">0s</span>`);
  const stop = startClock($("#genStatus"));

  try {
    await loadSources();
    const data = await api("/draft-generate", {
      topic,
      extra_prompt: $("#extraPrompt").value.trim(),
      brand: currentBrand(),
      seconds: parseInt($("#seconds").value, 10),
      voice: $("#voice").value,
    });
    draft = data.draft;
    renderDraft();
    setStatus($("#genStatus"), "ok", `${icon("check")} Xong. Xem lại rồi bấm render.`);
    $("#draftCard").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    setStatus($("#genStatus"), "err", `${icon("alert")} ${esc(e.message)}`);
    toast(e.message, "err");
  } finally {
    stop();
    $("#genSkeleton").hidden = true;
    btn.disabled = false;
  }
};

/** Các clip của một đoạn theo thứ tự chiếu: clip chính rồi tới clip bù. */
const chainOf = (seg) =>
  [seg.clip_id, ...(seg.fill_ids || [])].filter(Boolean);

/** Clip đang bị đoạn khác dùng — không cho đoạn này lấy trùng. */
function clipsUsedElsewhere(exceptIndex) {
  const set = new Set();
  (draft.segments || []).forEach((s, i) => {
    if (i !== exceptIndex) chainOf(s).forEach((id) => set.add(id));
  });
  return set;
}

// Giống MIN_FILL_GAP trong server.py: thiếu dưới ngần này giây thì không chèn
// clip mới, vì clip hiện chưa tới một giây rồi cắt trông như lỗi dựng.
const MIN_FILL_GAP = 1.5;

/** Tự nối thêm clip chưa dùng cho tới khi đủ thời lượng lời đọc. */
function autoFill(i) {
  const seg = draft.segments[i];
  const need = speakSeconds(seg.vo) + 0.25;
  const used = clipsUsedElsewhere(i);
  let have = chainOf(seg).reduce(
    (t, id) => t + ((srcById(id) || {}).duration || 0), 0);
  if (!seg.clip_id) return;
  seg.fill_ids = seg.fill_ids || [];
  const pool = readySources()
    .filter((s) => !used.has(s.id) && !chainOf(seg).includes(s.id))
    .sort((a, b) => (a.times_used - b.times_used) || (b.duration - a.duration));
  while (have < need - MIN_FILL_GAP && pool.length) {
    const extra = pool.shift();
    seg.fill_ids.push(extra.id);
    have += extra.duration || 0;
  }
}

function paintChain(i, box) {
  const seg = draft.segments[i];
  const chain = chainOf(seg);
  const need = speakSeconds(seg.vo) + 0.25;
  let have = chain.reduce((t, id) => t + ((srcById(id) || {}).duration || 0), 0);

  let html = '<div class="chain">';
  let left = need;
  chain.forEach((id, k) => {
    const s = srcById(id);
    if (!s) return;
    const shown = Math.max(0, Math.min(s.duration || 0, left));
    left -= shown;
    const cut = shown > 0.05 && shown < (s.duration || 0) - 0.05;
    html += `<div class="chain-item" data-slot="${k}">
      <span class="well wide"><img src="${s.thumb_url}" alt="Clip ${k + 1} của đoạn ${i + 1}"></span>
      ${k > 0 ? `<button class="drop" data-drop="${k}" aria-label="Bỏ clip này">${icon("x", 12, 2)}</button>` : ""}
      <div class="lbl">
        <b>${k === 0 ? "Clip chính" : "Nối thêm " + k}</b>
        <span>${shown.toFixed(1)}s${cut ? " (cắt từ " + s.duration.toFixed(1) + "s)" : ""}</span>
      </div>
    </div>`;
  });
  html += "</div>";

  if (!chain.length) {
    html = `<div class="chain-note">${icon("alert", 13)}<span>Chưa có clip, đoạn này sẽ ra nền trơn.</span></div>`;
  } else if (have < need - MIN_FILL_GAP) {
    const times = Math.ceil(need / Math.max(0.5, have));
    html += `<div class="warn-loop" style="margin-top:8px">${icon("repeat", 13)}
      <span>Còn thiếu ${(need - have).toFixed(1)}s so với lời đọc, hết clip rảnh nên
      clip cuối sẽ lặp ${times} lần. Rút ngắn lời hoặc thêm clip vào kho.</span></div>`;
  } else if (chain.length > 1) {
    html += `<div class="chain-note" style="margin-top:8px">${icon("layers", 13)}
      <span>Đoạn dài ${need.toFixed(1)}s nên ghép ${chain.length} clip, clip cuối cắt bớt phần dư.</span></div>`;
  }
  box.innerHTML = html;

  box.querySelectorAll("[data-drop]").forEach((b) => {
    b.onclick = () => {
      const slot = parseInt(b.dataset.drop, 10);
      seg.fill_ids.splice(slot - 1, 1);
      paintChain(i, box);
    };
  });
  box.querySelectorAll(".chain-item").forEach((el) => {
    el.querySelector(".well").onclick = () =>
      openPicker(i, parseInt(el.dataset.slot, 10), box);
    el.querySelector(".well").style.cursor = "pointer";
  });
}

function renderDraft() {
  $("#draftCard").hidden = false;
  $("#draftTitle").value = draft.title || "";
  $("#draftSub").value = draft.hook_sub || "";
  const list = $("#segList");
  list.innerHTML = "";

  (draft.segments || []).forEach((seg, i) => {
    const el = document.createElement("div");
    el.className = "seg";
    stagger(el, i);
    el.innerHTML = `
      <div>
        <div class="num"><span class="idx">${i + 1}</span>
          ${i === 0 ? "Mở đầu" : i === draft.segments.length - 1 ? "Kết" : "Đoạn giữa"}
          <span class="badge" data-secs></span></div>
        <label class="sr-only" for="vo${i}">Lời đọc đoạn ${i + 1}</label>
        <textarea id="vo${i}" data-vo rows="3">${esc(seg.vo)}</textarea>
        <div class="scene">Cảnh: ${esc(seg.scene) || "chưa có"}</div>
        <div class="reason" style="margin-top:6px">${esc(seg.match_reason || "")}</div>
      </div>
      <div class="clipbox">
        <div data-chain></div>
        <button class="btn ghost sm" data-add style="width:100%;margin-top:8px">
          ${icon("plus", 14)} Nối thêm clip</button>
      </div>`;

    const box = el.querySelector("[data-chain]");
    const secs = el.querySelector("[data-secs]");
    const paintSecs = () => { secs.textContent = `đọc ~${speakSeconds(draft.segments[i].vo).toFixed(1)}s`; };

    const ta = el.querySelector("[data-vo]");
    let typing = null;
    ta.oninput = () => {
      draft.segments[i].vo = ta.value;
      paintSecs();
      // Lời dài ra thì cần thêm clip; đợi ngừng gõ rồi mới tính lại cho đỡ giật.
      clearTimeout(typing);
      typing = setTimeout(() => { autoFill(i); paintChain(i, box); }, 500);
    };
    el.querySelector("[data-add]").onclick = () =>
      openPicker(i, chainOf(draft.segments[i]).length, box);

    paintSecs();
    paintChain(i, box);
    list.appendChild(el);
  });
  loadBgmList();
}

/** slot 0 = clip chính, slot >= 1 = clip nối thêm thứ slot.
 *  slot bằng độ dài chuỗi nghĩa là thêm clip mới vào cuối. */
function openPicker(segIndex, slot, box) {
  const dlg = $("#dlgPicker");
  const grid = $("#pickerGrid");
  const seg = draft.segments[segIndex];
  const chain = chainOf(seg);
  const adding = slot >= chain.length;
  $("#pickerTitle").textContent = adding
    ? `Nối thêm clip cho đoạn ${segIndex + 1}`
    : slot === 0
      ? `Đổi clip chính của đoạn ${segIndex + 1}`
      : `Đổi clip nối thứ ${slot} của đoạn ${segIndex + 1}`;
  $("#pickerFilter").value = "";
  $("#btnPickClear").textContent = adding ? "Đóng" : "Bỏ clip này khỏi đoạn";
  $("#btnPickClear").hidden = adding;

  const busy = clipsUsedElsewhere(segIndex);
  const inChain = new Set(chain.filter((_, k) => k !== slot));

  function paint(filter = "") {
    const q = filter.trim().toLowerCase();
    const list = readySources().filter((s) =>
      !q || (s.description + " " + (s.tags || []).join(" ")).toLowerCase().includes(q));
    grid.innerHTML = "";
    if (!list.length) {
      grid.innerHTML = `<p class="field-hint">Không có clip nào khớp.</p>`;
      return;
    }
    list.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pick";
      b.setAttribute("aria-pressed", String(s.id === chain[slot]));
      const flag = busy.has(s.id) ? "đoạn khác" : inChain.has(s.id) ? "đã trong đoạn" : "";
      b.innerHTML = `
        <span class="well wide"><img src="${s.thumb_url}" alt="" loading="lazy"></span>
        ${flag ? `<span class="flag">${flag}</span>` : ""}
        <span class="cap">${esc((s.description || s.original_name).slice(0, 90))}
          <br><span style="color:var(--fg-muted)">${s.duration ? s.duration.toFixed(1) + "s" : ""}</span></span>`;
      b.onclick = () => { apply(s.id); dlg.close(); };
      grid.appendChild(b);
    });
  }

  function apply(clipId) {
    seg.fill_ids = seg.fill_ids || [];
    if (slot === 0) {
      seg.clip_id = clipId;
      seg.match_reason = clipId ? "Bạn chọn tay" : "";
      // Đổi clip chính sang clip ngắn hơn thì phải bù lại, không thì đoạn hụt hình.
      autoFill(segIndex);
    } else if (adding) {
      if (clipId) seg.fill_ids.push(clipId);
    } else if (clipId) {
      seg.fill_ids[slot - 1] = clipId;
      autoFill(segIndex);
    } else {
      seg.fill_ids.splice(slot - 1, 1);
    }
    paintChain(segIndex, box);
  }

  $("#pickerFilter").oninput = (e) => paint(e.target.value);
  $("#btnPickClear").onclick = () => { apply(""); dlg.close(); };
  paint();
  dlg.showModal();
  $("#pickerFilter").focus();
}

async function loadBgmList() {
  try {
    const data = await api("/bgm-list");
    const sel = $("#bgm");
    const keep = sel.value;
    sel.innerHTML = '<option value="random">Ngẫu nhiên</option><option value="none">Không nhạc</option>'
      + (data.files || []).map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
    sel.value = keep;
    if (!(data.files || []).length) {
      sel.title = "Thư mục assets/music đang trống, video sẽ không có nhạc nền.";
    }
  } catch (e) { /* không có nhạc thì thôi */ }
}

// ── Render ────────────────────────────────────────────────────────
$("#btnRender").onclick = async () => {
  if (!draft) return;
  const missing = draft.segments.filter((s) => !s.clip_id).length;
  if (missing) {
    const ok = await confirmDialog("Có đoạn chưa gán clip",
      `${missing} đoạn chưa có clip, những đoạn đó sẽ hiện nền trơn. Vẫn render?`, "Vẫn render");
    if (!ok) return;
  }
  const seen = new Set();
  for (const s of draft.segments) {
    for (const id of chainOf(s)) {
      if (seen.has(id)) {
        toast("Một clip đang bị dùng ở hai chỗ, đổi lại trước khi render.", "err");
        return;
      }
      seen.add(id);
    }
  }
  const btn = $("#btnRender");
  btn.disabled = true;
  try {
    draft.title = $("#draftTitle").value.trim();
    draft.hook_sub = $("#draftSub").value.trim();
    await api("/draft-update", {
      id: draft.id, title: draft.title, hook_sub: draft.hook_sub,
      segments: draft.segments, voice: $("#voice").value,
    });
    const data = await api("/draft-render", {
      id: draft.id, transition: $("#transition").value, bgm: $("#bgm").value,
      voice: $("#voice").value,
    });
    renderJobId = data.job_id;
    $("#btnCancelRender").hidden = false;
    setStatus($("#renderStatus"), "busy",
      `${icon("loader")} Đang render, có thể chuyển sang tab khác <span class="elapsed">0s</span>`);
    const stop = startClock($("#renderStatus"));
    pollRender(stop);
  } catch (e) {
    toast(e.message, "err");
    setStatus($("#renderStatus"), "err", `${icon("alert")} ${esc(e.message)}`);
    btn.disabled = false;
  }
};

$("#btnCancelRender").onclick = async () => {
  if (!renderJobId) return;
  const ok = await confirmDialog("Hủy render", "Dừng việc render đang chạy?", "Hủy render");
  if (!ok) return;
  try {
    await api("/jobs/cancel", { id: renderJobId });
    toast("Đã yêu cầu dừng, chờ vài giây.", "ok");
  } catch (e) { toast(e.message, "err"); }
};

async function pollRender(stopClock) {
  if (renderPoll) clearTimeout(renderPoll);
  try {
    const data = await api("/jobs");
    const job = (data.jobs || []).find((j) => j.id === renderJobId);
    if (!job) { renderPoll = setTimeout(() => pollRender(stopClock), 3000); return; }

    if (job.status === "done") {
      stopClock();
      setStatus($("#renderStatus"), "ok", `${icon("check")} Render xong.`);
      $("#btnRender").disabled = false;
      $("#btnCancelRender").hidden = true;
      toast("Video đã xong, xem ở tab Video đã xong.", "ok");
      loadVideos();
      return;
    }
    if (job.status === "failed" || job.status === "cancelled") {
      stopClock();
      setStatus($("#renderStatus"), job.status === "cancelled" ? "" : "err",
        `${icon(job.status === "cancelled" ? "x" : "alert")} ${
          job.status === "cancelled" ? "Đã hủy render." : esc(job.error || "Render lỗi.")}`);
      $("#btnRender").disabled = false;
      $("#btnCancelRender").hidden = true;
      return;
    }
    renderPoll = setTimeout(() => pollRender(stopClock), 3000);
  } catch (e) {
    renderPoll = setTimeout(() => pollRender(stopClock), 5000);
  }
}

// ── Video đã xong ─────────────────────────────────────────────────
async function loadVideos() {
  const data = await api("/videos");
  const vids = data.videos || [];
  $("#vidCount").textContent = vids.length ? `(${vids.length})` : "";
  $("#vidEmpty").innerHTML = vids.length ? "" : emptyState(
    "film", "Chưa có video nào",
    "Sang tab Tạo video, nhập chủ đề rồi để AI dựng kịch bản cho bạn.",
    "Sang tab Tạo video", () => showPage("create"));

  const grid = $("#vidGrid");
  grid.innerHTML = "";
  vids.forEach((v, idx) => {
    const el = document.createElement("div");
    el.className = "vid";
    stagger(el, idx);
    el.innerHTML = `
      <span class="well tall">
        <video src="${v.preview_url}" poster="${v.thumb_url}" controls preload="none"></video>
      </span>
      <div class="body">
        <div class="title">${esc(v.title) || "(chưa đặt tiêu đề)"}</div>
        <div class="sub">${v.duration ? Math.round(v.duration) + " giây" : ""}${
          v.bgm ? " · nhạc " + esc(v.bgm) : " · không nhạc"}</div>
        <div class="acts">
          <button class="btn sm" data-big>${icon("expand", 14)} Phóng to</button>
          <a class="btn ghost sm" href="${v.video_url}?download=1" download>${icon("download", 14)} Tải</a>
          <button class="btn danger sm" data-del title="Xóa video">${icon("trash", 14)}</button>
        </div>
      </div>`;
    el.querySelector("[data-big]").onclick = () => openViewer(v);
    el.querySelector("[data-del]").onclick = async () => {
      const ok = await confirmDialog("Xóa video", `Xóa "${v.title || "video này"}"?`);
      if (!ok) return;
      try {
        await api("/videos/delete", { id: v.id });
        loadVideos();
        toast("Đã xóa video.", "ok");
      } catch (e) { toast(e.message, "err"); }
    };
    grid.appendChild(el);
  });
}

/** Xem video to hết màn hình. Dùng bản gốc chứ không phải bản xem nhanh 480p. */
function openViewer(v) {
  const dlg = $("#dlgVideo");
  const vid = $("#bigVideo");
  vid.src = v.video_url;
  vid.poster = v.thumb_url;
  $("#bigTitle").textContent = [v.title || "(chưa đặt tiêu đề)",
    v.duration ? Math.round(v.duration) + " giây" : ""].filter(Boolean).join(" · ");
  $("#bigDownload").href = v.video_url + "?download=1";
  dlg.showModal();
}

$("#btnCloseVideo").onclick = () => $("#dlgVideo").close();
// Bấm ra ngoài khung hình cũng đóng, như mọi trình xem ảnh khác.
$("#dlgVideo").addEventListener("click", (e) => {
  if (e.target === $("#dlgVideo")) $("#dlgVideo").close();
});
// Dừng tiếng khi đóng, không thì video chạy ngầm.
$("#dlgVideo").addEventListener("close", () => {
  const vid = $("#bigVideo");
  vid.pause();
  vid.removeAttribute("src");
  vid.load();
});

// ── Khởi động ─────────────────────────────────────────────────────
loadVoices()
  .then(loadBrands)
  .then(loadSources)
  .then(() => { if (sources.some((s) => s.status !== "ready")) startSourcePolling(); })
  .catch((e) => toast(e.message, "err"));
