const API = window.API_BASE.replace(/\/$/, "");
const $ = (id) => document.getElementById(id);
const MAX_HISTORY = 300;
const PALETTE = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2", "#ca8a04"];
const UNCERTAIN_COLOR = "#9ca3af";

let token = localStorage.getItem("token");
if (!token) location.href = "index.html";
$("user-email").textContent = localStorage.getItem("email") || "";

let history = [];      // [{t, label, top, confidence, scores, nn_ms, ...}] 오래된 것 → 최신
let classes = [];      // 점수에 나온 클래스 이름 (정렬)
let lastSeq = 0;
let recvTimes = [];

async function api(path) {
  const res = await fetch(API + path, { headers: { Authorization: `Bearer ${token}` } });
  if (res.status === 401) logout();
  if (!res.ok) throw new Error(`요청 실패 (${res.status})`);
  return res;
}

function logout() {
  localStorage.removeItem("token");
  location.href = "index.html";
}
$("logout").addEventListener("click", logout);

const colorOf = (label) =>
  label === "uncertain" ? UNCERTAIN_COLOR : PALETTE[Math.max(0, classes.indexOf(label)) % PALETTE.length];
const pct = (v) => `${(v * 100).toFixed(1)}%`;
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString("ko-KR");

function learnClasses(scores) {
  const names = Object.keys(scores || {});
  if (names.some((n) => !classes.includes(n))) {
    classes = [...new Set([...classes, ...names])].sort();
    $("legend").innerHTML = [...classes, "uncertain"]
      .map((c) => `<span><i style="background:${colorOf(c)}"></i>${c}</span>`)
      .join("");
  }
}

// ---------- 실시간 (long-poll) ----------

async function liveLoop() {
  while (true) {
    try {
      const res = await api(`/api/infer/live?after=${lastSeq}`);
      if (res.status === 200) {
        const data = await res.json();
        lastSeq = data.seq;
        $("cam").src = `data:image/jpeg;base64,${data.image}`;
        $("cam-empty").hidden = true;
        const now = performance.now();
        recvTimes = recvTimes.filter((t) => now - t < 3000).concat(now);
        const online = data.age < 10;
        setStatus(online, online ? "추론 중" : `마지막 수신 ${data.age.toFixed(0)}초 전`);
        $("t-fps").textContent = (recvTimes.length / 3).toFixed(1);
        $("cam-info").textContent = `${(data.image.length * 0.75 / 1024).toFixed(1)} KB · #${data.seq}`;
        if (data.result) {
          const item = { t: Date.now() / 1000 - data.age, seq: data.seq, ...data.result };
          showCurrent(item);
          if (!history.length || history[history.length - 1].seq !== item.seq) {
            history.push(item);
            if (history.length > MAX_HISTORY) history.shift();
            renderHistory();
          }
        } else {
          $("badge").hidden = true;
          $("v-label").textContent = "추론 결과 없음";
          $("v-conf").textContent = "수집 펌웨어가 실행 중일 수 있습니다";
        }
      } else {
        setStatus(false, "기기 오프라인");
      }
    } catch (err) {
      setStatus(false, "서버 연결 실패");
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

function setStatus(online, text) {
  $("cam-dot").classList.toggle("on", online);
  $("cam-status").textContent = text;
}

function showCurrent(r) {
  learnClasses(r.scores);
  const color = colorOf(r.label);
  $("badge").hidden = false;
  $("badge").textContent = `${r.label} ${pct(r.confidence)}`;
  $("badge").style.background = color;
  $("v-label").textContent = r.label;
  $("v-label").style.color = color;
  $("v-conf").textContent = r.label === "uncertain" ? `최고 ${r.top} ${pct(r.confidence)}` : pct(r.confidence);
  $("bars").innerHTML = classes
    .map((c) => {
      const v = r.scores[c] ?? 0;
      return `<div class="bar"><span class="bar-name">${c}</span>
        <span class="bar-track"><span style="width:${v * 100}%;background:${colorOf(c)}"></span></span>
        <span class="bar-val">${pct(v)}</span></div>`;
    })
    .join("");
  $("t-dsp").textContent = r.dsp_ms;
  $("t-nn").textContent = r.nn_ms;
  $("t-loop").textContent = r.loop_ms;
}

// ---------- 기록 ----------

function renderHistory() {
  $("hist-n").textContent = history.length;
  $("timeline").innerHTML = history
    .map((h) => `<i style="background:${colorOf(h.label)}" title="${fmtTime(h.t)} ${h.label} ${pct(h.confidence)}"></i>`)
    .join("");

  const counts = {};
  for (const h of history) counts[h.label] = (counts[h.label] || 0) + 1;
  $("dist").innerHTML = [...classes, "uncertain"]
    .filter((c) => counts[c])
    .map((c) => `<span><i style="background:${colorOf(c)}"></i>${c} <strong>${counts[c]}</strong> <span class="muted">${pct(counts[c] / history.length)}</span></span>`)
    .join("");

  $("rows").innerHTML = history
    .slice(-15)
    .reverse()
    .map((h) => `<tr>
      <td>${fmtTime(h.t)}</td>
      <td><i class="chip" style="background:${colorOf(h.label)}"></i>${h.label}</td>
      <td>${pct(h.confidence)}</td>
      <td class="muted">${classes.map((c) => `${c} ${(h.scores[c] ?? 0).toFixed(2)}`).join(" · ")}</td>
      <td>${h.nn_ms}</td></tr>`)
    .join("");
}

async function loadHistory() {
  try {
    const data = await (await api("/api/infer/history?limit=300")).json();
    history = data.history;
    history.forEach((h) => learnClasses(h.scores));
    lastSeq = 0;
    renderHistory();
  } catch (err) {
    console.error(err);
  }
}

loadHistory().then(liveLoop);
