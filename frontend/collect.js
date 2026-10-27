const API = window.API_BASE.replace(/\/$/, "");
const $ = (id) => document.getElementById(id);
const PAGE = 120;

let token = localStorage.getItem("token");
if (!token) location.href = "index.html";

const state = {
  labels: [],                 // [{label, count}]
  current: localStorage.getItem("collect.label") || null,
  tab: "",                    // 갤러리 필터 ("" = 전체)
  images: [],
  selected: new Set(),
  bursting: false,
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  if (options.body) headers["Content-Type"] = "application/json";
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) return logout();
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `요청 실패 (${res.status})`);
  }
  return res;
}
const apiJson = async (path, options) => (await api(path, options)).json();

function logout() {
  localStorage.removeItem("token");
  location.href = "index.html";
}
$("logout").addEventListener("click", logout);
$("user-email").textContent = localStorage.getItem("email") || "";

function setMsg(text, ok = false) {
  $("ctrl-msg").textContent = text;
  $("ctrl-msg").classList.toggle("ok", ok);
}

// ---------- 실시간 카메라 (long-poll) ----------

let lastSeq = 0;
let frameTimes = [];

async function cameraLoop() {
  while (true) {
    try {
      const res = await api(`/api/camera/frame?after=${lastSeq}`);
      if (res && res.status === 200) {
        const blob = await res.blob();
        const old = $("cam").src;
        $("cam").src = URL.createObjectURL(blob);
        if (old) URL.revokeObjectURL(old);
        lastSeq = Number(res.headers.get("X-Frame-Seq")) || lastSeq;
        const age = Number(res.headers.get("X-Frame-Age")) || 0;
        const now = performance.now();
        frameTimes = frameTimes.filter((t) => now - t < 3000).concat(now);
        $("cam-empty").hidden = true;
        setCamStatus(age < 10, age < 10 ? `${(frameTimes.length / 3).toFixed(1)} fps` : `마지막 프레임 ${age.toFixed(0)}초 전`);
        $("cam-info").textContent = `${$("cam").naturalWidth || "-"}×${$("cam").naturalHeight || "-"} · ${(blob.size / 1024).toFixed(1)} KB · #${lastSeq}`;
      } else {
        setCamStatus(false, "기기 오프라인");
      }
    } catch (err) {
      setCamStatus(false, "서버 연결 실패");
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

function setCamStatus(online, text) {
  $("cam-dot").classList.toggle("on", online);
  $("cam-status").textContent = text;
}

// ---------- 클래스 ----------

function savedLabels() {
  try { return JSON.parse(localStorage.getItem("collect.labels") || "[]"); } catch { return []; }
}

async function loadLabels() {
  const data = await apiJson("/api/labels");
  const counts = Object.fromEntries(data.labels.map((l) => [l.label, l.count]));
  const names = [...new Set([...savedLabels(), ...Object.keys(counts)])];
  state.labels = names.map((label) => ({ label, count: counts[label] || 0 }));
  if (!state.current || !names.includes(state.current)) state.current = names[0] || null;
  renderLabels();
}

function renderLabels() {
  $("labels").innerHTML = state.labels
    .map((l, i) => `
      <li class="${l.label === state.current ? "active" : ""}" data-label="${l.label}">
        <span class="key">${i < 9 ? i + 1 : ""}</span>
        <span class="name">${l.label}</span>
        <span class="count">${l.count}</span>
      </li>`)
    .join("") || `<li class="muted empty">클래스를 추가하세요</li>`;
  $("cur-label").textContent = state.current || "-";
  $("shot").disabled = $("burst").disabled = !state.current;

  const total = state.labels.reduce((s, l) => s + l.count, 0);
  $("gallery-tabs").innerHTML = [{ label: "", count: total }, ...state.labels]
    .map((l) => `<button class="tab ${l.label === state.tab ? "active" : ""}" data-tab="${l.label}">${l.label || "전체"} <span class="muted">${l.count}</span></button>`)
    .join("");
}

function selectLabel(label) {
  state.current = label;
  localStorage.setItem("collect.label", label);
  renderLabels();
}

$("labels").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-label]");
  if (li) selectLabel(li.dataset.label);
});

$("add-label").addEventListener("submit", (e) => {
  e.preventDefault();
  const label = e.target.label.value.trim();
  const saved = savedLabels();
  if (!saved.includes(label)) localStorage.setItem("collect.labels", JSON.stringify([...saved, label]));
  if (!state.labels.some((l) => l.label === label)) state.labels.push({ label, count: 0 });
  e.target.reset();
  selectLabel(label);
});

function bumpCount(label, delta) {
  const l = state.labels.find((x) => x.label === label);
  if (l) l.count = Math.max(0, l.count + delta);
}

// ---------- 촬영 ----------

async function captureOne() {
  const label = state.current;
  const row = await apiJson("/api/captures", { method: "POST", body: JSON.stringify({ label }) });
  flash();
  bumpCount(label, 1);
  renderLabels();
  if (!state.tab || state.tab === label) {
    state.images.unshift(row);
    $("grid").prepend(thumb(row));
  }
  return row;
}

function flash() {
  const f = $("flash");
  f.classList.remove("on");
  void f.offsetWidth;
  f.classList.add("on");
}

$("shot").addEventListener("click", async () => {
  if (state.bursting || !state.current) return;
  $("shot").disabled = true;
  try {
    await captureOne();
    setMsg(`${state.current} 저장됨`, true);
  } catch (err) {
    setMsg(err.message);
  } finally {
    $("shot").disabled = false;
  }
});

async function runBurst() {
  if (state.bursting) { state.bursting = false; return; }
  if (!state.current) return;
  const count = Math.max(1, Math.min(500, Number($("burst-count").value) || 1));
  const interval = Math.max(0, Number($("burst-interval").value) || 0);
  state.bursting = true;
  $("burst").textContent = "중지";
  $("shot").disabled = true;
  $("burst-progress").hidden = false;
  let done = 0, failed = 0;
  for (let i = 0; i < count && state.bursting; i++) {
    const started = performance.now();
    try { await captureOne(); done++; } catch (err) { failed++; setMsg(err.message); if (failed >= 3) break; }
    $("burst-progress").firstElementChild.style.width = `${((i + 1) / count) * 100}%`;
    $("burst").textContent = `중지 (${i + 1}/${count})`;
    const wait = interval - (performance.now() - started);
    if (wait > 0 && i < count - 1) await new Promise((r) => setTimeout(r, wait));
  }
  state.bursting = false;
  $("burst").innerHTML = "연속 촬영 <kbd>B</kbd>";
  $("shot").disabled = false;
  $("burst-progress").hidden = true;
  if (done) setMsg(`${state.current} ${done}장 저장${failed ? `, ${failed}장 실패` : ""}`, !failed);
}
$("burst").addEventListener("click", runBurst);

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input")) return;
  if (e.code === "Space") { e.preventDefault(); $("shot").click(); }
  else if (e.key === "b" || e.key === "B") runBurst();
  else if (e.key === "Escape") $("viewer").hidden = true;
  else if (/^[1-9]$/.test(e.key) && state.labels[e.key - 1]) selectLabel(state.labels[e.key - 1].label);
});

// ---------- 갤러리 ----------

function thumb(img) {
  const el = document.createElement("figure");
  el.className = "thumb" + (state.selected.has(img.id) ? " selected" : "");
  el.dataset.id = img.id;
  el.innerHTML = `
    <img src="${img.url}" loading="lazy" alt="${img.label}">
    <figcaption>${img.label}</figcaption>
    <button class="zoom" title="크게 보기">⤢</button>`;
  return el;
}

async function loadImages(reset = true) {
  if (reset) { state.images = []; state.selected.clear(); }
  const q = new URLSearchParams({ limit: PAGE, offset: state.images.length });
  if (state.tab) q.set("label", state.tab);
  const { images } = await apiJson(`/api/images?${q}`);
  state.images.push(...images);
  if (reset) $("grid").innerHTML = "";
  images.forEach((img) => $("grid").append(thumb(img)));
  $("more").hidden = images.length < PAGE;
  if (!state.images.length) $("grid").innerHTML = `<p class="muted empty">이미지가 없습니다</p>`;
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  $("sel-info").textContent = n ? `${n}개 선택` : "";
  $("del-sel").disabled = n === 0;
  $("sel-all").textContent = n && n === state.images.length ? "선택 해제" : "전체 선택";
  $("dl").textContent = state.tab ? `${state.tab} ZIP` : "전체 ZIP";
}

$("gallery-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  state.tab = b.dataset.tab;
  renderLabels();
  loadImages();
});

$("grid").addEventListener("click", (e) => {
  const fig = e.target.closest(".thumb");
  if (!fig) return;
  const img = state.images.find((i) => i.id === fig.dataset.id);
  if (e.target.closest(".zoom")) {
    $("viewer-img").src = img.url;
    $("viewer-cap").textContent = `${img.label} · ${new Date(img.created_at).toLocaleString("ko-KR")}`;
    $("viewer").hidden = false;
    return;
  }
  state.selected.has(img.id) ? state.selected.delete(img.id) : state.selected.add(img.id);
  fig.classList.toggle("selected");
  renderSelection();
});
$("viewer").addEventListener("click", () => ($("viewer").hidden = true));

$("sel-all").addEventListener("click", () => {
  const all = state.selected.size !== state.images.length;
  state.selected = new Set(all ? state.images.map((i) => i.id) : []);
  document.querySelectorAll(".thumb").forEach((f) => f.classList.toggle("selected", all));
  renderSelection();
});

$("del-sel").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  $("del-sel").disabled = true;
  try {
    const { deleted } = await apiJson("/api/images/delete", { method: "POST", body: JSON.stringify({ ids }) });
    for (const img of state.images.filter((i) => state.selected.has(i.id))) bumpCount(img.label, -1);
    state.images = state.images.filter((i) => !state.selected.has(i.id));
    document.querySelectorAll(".thumb.selected").forEach((f) => f.remove());
    state.selected.clear();
    renderLabels();
    renderSelection();
    setMsg(`${deleted}장 삭제됨`, true);
  } catch (err) {
    setMsg(err.message);
    renderSelection();
  }
});

$("more").addEventListener("click", () => loadImages(false));

$("dl").addEventListener("click", async () => {
  const btn = $("dl");
  const label = state.tab;
  btn.disabled = true;
  btn.textContent = "압축 중…";
  try {
    const res = await api(`/api/images/download${label ? `?label=${encodeURIComponent(label)}` : ""}`);
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "images.zip";
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    setMsg(err.message);
  } finally {
    btn.disabled = false;
    renderSelection();
  }
});

// ---------- 시작 ----------
loadLabels().then(() => loadImages()).catch((err) => setMsg(err.message));
cameraLoop();
