const API = window.API_BASE.replace(/\/$/, "");
const $ = (id) => document.getElementById(id);

let token = localStorage.getItem("token");
let mode = "login";
let pollTimer = null;

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(API + path, { ...options, headers });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && token) logout();
  if (!res.ok) throw new Error(body.detail || `요청 실패 (${res.status})`);
  return body;
}

// ---------- 인증 ----------

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    mode = tab.dataset.mode;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("auth-submit").textContent = mode === "login" ? "로그인" : "회원가입";
    setMsg("");
  })
);

function setMsg(text, ok = false) {
  $("auth-msg").textContent = text;
  $("auth-msg").classList.toggle("ok", ok);
}

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const creds = { email: form.get("email"), password: form.get("password") };
  $("auth-submit").disabled = true;
  setMsg("처리 중… (서버가 잠들어 있으면 첫 요청에 30초 정도 걸릴 수 있어요)", true);
  try {
    const data = await api(`/auth/${mode}`, { method: "POST", body: JSON.stringify(creds) });
    if (!data.access_token) {
      setMsg("가입 완료. 이메일 인증 후 로그인하세요.", true);
      return;
    }
    token = data.access_token;
    localStorage.setItem("token", token);
    localStorage.setItem("email", creds.email);
    showDashboard();
  } catch (err) {
    setMsg(err.message);
  } finally {
    $("auth-submit").disabled = false;
  }
});

function logout() {
  token = null;
  localStorage.removeItem("token");
  clearInterval(pollTimer);
  $("dash-view").hidden = true;
  $("auth-view").hidden = false;
}
$("logout").addEventListener("click", logout);

// ---------- 대시보드 ----------

function showDashboard() {
  $("auth-view").hidden = true;
  $("dash-view").hidden = false;
  $("user-email").textContent = localStorage.getItem("email") || "";
  refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 3000);
}

async function refresh() {
  try {
    const [sensors, led] = await Promise.all([api("/api/sensors?limit=60"), api("/api/led")]);
    renderSensors(sensors.data);
    renderLed(led.on);
  } catch (err) {
    console.error(err);
  }
}

const fmtTime = (iso) => new Date(iso).toLocaleTimeString("ko-KR");
const fmt = (v, digits = 0) => (v === null || v === undefined ? "-" : Number(v).toFixed(digits));

function renderSensors(rows) {
  const latest = rows[0];
  if (latest) {
    $("v-temp").textContent = fmt(latest.temperature, 1);
    $("v-analog").textContent = fmt(latest.analog);
    $("v-rssi").textContent = fmt(latest.rssi);
    $("last-seen").textContent = new Date(latest.created_at).toLocaleString("ko-KR");
  }
  const online = latest && Date.now() - new Date(latest.created_at).getTime() < 15000;
  $("online-dot").classList.toggle("on", !!online);
  $("online-text").textContent = online ? "온라인" : "오프라인";

  $("rows").innerHTML = rows
    .map((r) => `<tr><td>${fmtTime(r.created_at)}</td><td>${fmt(r.temperature, 1)}</td><td>${fmt(r.analog)}</td><td>${fmt(r.rssi)}</td></tr>`)
    .join("");
  drawChart(rows.slice().reverse());
}

function drawChart(rows) {
  const svg = $("chart");
  const W = 600, H = 180, pad = 24;
  const vals = rows.map((r) => r.temperature).filter((v) => v !== null);
  if (vals.length < 2) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">데이터 수집 중</text>`;
    return;
  }
  let min = Math.min(...vals), max = Math.max(...vals);
  if (max - min < 1) { min -= 0.5; max += 0.5; }
  const x = (i) => pad + (i / (vals.length - 1)) * (W - pad * 2);
  const y = (v) => H - pad - ((v - min) / (max - min)) * (H - pad * 2);
  const points = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  svg.innerHTML = `
    <line class="grid" x1="${pad}" x2="${W - pad}" y1="${y(max)}" y2="${y(max)}"/>
    <line class="grid" x1="${pad}" x2="${W - pad}" y1="${y(min)}" y2="${y(min)}"/>
    <text x="2" y="${y(max) + 4}">${max.toFixed(1)}</text>
    <text x="2" y="${y(min) + 4}">${min.toFixed(1)}</text>
    <polyline class="line" points="${points}"/>`;
}

function renderLed(on) {
  $("led-state").textContent = on ? "켜짐" : "꺼짐";
  $("led-on").disabled = on;
  $("led-off").disabled = !on;
}

async function setLed(on) {
  try {
    const res = await api("/api/led", { method: "POST", body: JSON.stringify({ on }) });
    renderLed(res.on);
  } catch (err) {
    alert(err.message);
  }
}
$("led-on").addEventListener("click", () => setLed(true));
$("led-off").addEventListener("click", () => setLed(false));

// ---------- 시작 ----------
if (token) showDashboard();
else $("auth-view").hidden = false;
