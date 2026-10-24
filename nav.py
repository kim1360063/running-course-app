"""러닝 내비게이션 화면 (휴대폰 브라우저 안에서 직접 돌아가는 HTML).

서버를 거치지 않고 1초마다 GPS 위치로 아래를 계산해서 보여준다.
- 코스 위 진행 거리, 다음 갈림길까지 거리와 방향
- 현재 페이스 (최근 약 30초 기준)
- 소모 칼로리: 현재 페이스로 예상 심박을 보정한 뒤 Keytel 공식으로 1초씩 누적
  (예상 심박 = 코스 예측 심박 + 7.6 x (목표 페이스 - 현재 페이스), 데이터 분석에서 얻은 기울기)
'시연 모드'를 켜면 GPS 대신 가상 러너가 목표 페이스로 코스를 따라 달린다 (10배속).
"""
import json

ICON = {0: "⬅", 1: "➡", 2: "⬅", 3: "➡", 4: "↖", 5: "↗", 7: "🔄", 8: "🔄", 9: "↩", 12: "↖", 13: "↗"}


def build_html(course, turns, user, pace, color, demo=False):
    data = {
        "route": [[c[1], c[0]] for c in course["coords"]],          # [위도, 경도]
        "turns": [{"km": km, "msg": msg, "icon": ICON.get(t, "⬆")} for _, km, msg, _, _, t in turns],
        "distKm": course["dist_km"],
        "targetKcal": course["kcal"],
        "hrPred": course["hr"],
        "pace": pace,
        "weight": user["weight"], "age": user["age"], "male": bool(user["male"]),
        "color": color,
        "demo": bool(demo),
    }
    return TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))


TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
  :root { --bg:#111; --panel:#1c1c1c; --ink:#f4f4f2; --sub:#a9a9a2; --accent:#ffd23f; --warn:#e5484d; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--ink);
               font-family:-apple-system, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif; }
  #wrap { position:relative; height:100%; display:flex; flex-direction:column; }
  #turn { display:flex; align-items:center; gap:14px; padding:12px 16px; background:#0f3d2e; min-height:64px; }
  #turn.off { background:var(--warn); }
  #turn .icon { font-size:40px; line-height:1; width:48px; text-align:center; }
  #turn .dist { font-size:26px; font-weight:800; }
  #turn .msg { font-size:16px; color:#e7f3ee; }
  #map { flex:1; min-height:300px; }
  #stats { display:grid; grid-template-columns:repeat(3,1fr); background:var(--panel); padding:10px 8px 6px; }
  .stat { text-align:center; }
  .stat .v { font-size:26px; font-weight:800; font-variant-numeric:tabular-nums; }
  .stat .l { font-size:12px; color:var(--sub); }
  #bar { height:6px; background:#333; } #bar div { height:100%; width:0; background:var(--accent); transition:width .5s; }
  #foot { display:flex; justify-content:space-between; padding:6px 14px 10px; background:var(--panel); color:var(--sub); font-size:13px; }
  #start { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center;
           gap:12px; background:rgba(0,0,0,.72); z-index:1000; text-align:center; padding:16px; }
  #start button { font-size:20px; font-weight:800; padding:14px 34px; border:0; border-radius:999px;
                  background:var(--accent); color:#111; }
  #start p { margin:0; color:#ddd; font-size:14px; }
  .me { width:18px; height:18px; border-radius:50%; background:#2a78d6; border:3px solid #fff; box-shadow:0 0 0 6px rgba(42,120,214,.3); }
</style></head>
<body><div id="wrap">
  <div id="turn"><div class="icon" id="tIcon">⬆</div><div><div class="dist" id="tDist">출발 준비</div><div class="msg" id="tMsg">시작 버튼을 눌러주세요</div></div></div>
  <div id="map"></div>
  <div id="bar"><div id="barFill"></div></div>
  <div id="stats">
    <div class="stat"><div class="v" id="sPace">–</div><div class="l">현재 페이스 /km</div></div>
    <div class="stat"><div class="v" id="sKcal">0</div><div class="l">소모 칼로리 kcal</div></div>
    <div class="stat"><div class="v" id="sDist">0.00</div><div class="l">달린 거리 km</div></div>
  </div>
  <div id="foot"><span id="fTime">00:00</span><span id="fGoal"></span><span id="fMode"></span></div>
  <div id="start"><button id="go">▶ 안내 시작</button><p id="startMsg"></p></div>
</div>
<script>
const D = __DATA__;
const $ = id => document.getElementById(id);

// ---------- 코스를 미터 단위 평면으로 바꿔서 누적 거리 계산 ----------
const lat0 = D.route[0][0], kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110540;
const P = D.route.map(([la, lo]) => [lo * kx, la * ky]);
const cum = [0];
for (let i = 1; i < P.length; i++) cum.push(cum[i-1] + Math.hypot(P[i][0]-P[i-1][0], P[i][1]-P[i-1][1]));
const total = cum[cum.length - 1];
const turnsM = D.turns.map(t => ({...t, m: t.km * 1000}));

// 위치를 코스 위로 투영 → (코스 위 진행 거리, 코스에서 떨어진 거리)
let lastIdx = 0;
function project(la, lo) {
  const p = [lo * kx, la * ky];
  let best = {d: 1e18, along: 0, idx: lastIdx};
  const search = (a, b) => {
    for (let i = Math.max(0, a); i < Math.min(P.length - 1, b); i++) {
      const A = P[i], B = P[i+1], dx = B[0]-A[0], dy = B[1]-A[1];
      const L2 = dx*dx + dy*dy || 1e-9;
      let t = ((p[0]-A[0])*dx + (p[1]-A[1])*dy) / L2; t = Math.max(0, Math.min(1, t));
      const d = Math.hypot(A[0] + t*dx - p[0], A[1] + t*dy - p[1]);
      if (d < best.d) best = {d, along: cum[i] + t * Math.sqrt(L2), idx: i};
    }
  };
  search(lastIdx - 10, lastIdx + 80);              // 지금 위치 근처부터 (같은 길을 되돌아오는 코스 대비)
  if (best.d > 40) search(0, P.length);            // 많이 벗어났으면 전체에서 다시 찾기
  lastIdx = best.idx;
  return best;
}

// ---------- 칼로리 (Keytel) ----------
function kcalPerMin(hr) {
  const v = D.male ? (-55.0969 + 0.6309*hr + 0.1988*D.weight + 0.2017*D.age) / 4.184
                   : (-20.4022 + 0.4472*hr - 0.1263*D.weight + 0.074*D.age) / 4.184;
  return Math.max(v, 0);
}
const fmtPace = p => { if (!isFinite(p) || p <= 0 || p > 30) return "–"; let m = Math.floor(p), s = Math.round((p-m)*60); if (s === 60) { m++; s = 0; } return `${m}'${String(s).padStart(2,"0")}"`; };
const fmtTime = s => `${String(Math.floor(s/60)).padStart(2,"0")}:${String(Math.floor(s%60)).padStart(2,"0")}`;

// ---------- 지도 ----------
let map = null, me = null, done = null;
if (window.L) {
  map = L.map("map", {zoomControl: false, attributionControl: true}).setView(D.route[0], 17);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "© OpenStreetMap"}).addTo(map);
  L.polyline(D.route, {color: D.color, weight: 7, opacity: .9}).addTo(map);
  done = L.polyline([], {color: "#8a8a85", weight: 8, opacity: .9}).addTo(map);   // 이미 달린 부분은 회색
  me = L.marker(D.route[0], {icon: L.divIcon({className: "", html: '<div class="me"></div>', iconSize: [18,18], iconAnchor: [9,9]})}).addTo(map);
  map.fitBounds(L.polyline(D.route).getBounds(), {padding: [20, 20]});
}

// ---------- 상태 ----------
let started = false, t0 = null, lastT = null, kcal = 0, hist = [], finished = false, bestAlong = 0;
$("fGoal").textContent = `목표 ${Math.round(D.targetKcal)}kcal · ${D.distKm.toFixed(1)}km`;
$("fMode").textContent = D.demo ? "🎬 시연 모드 (10배속)" : "📍 GPS";
$("startMsg").textContent = D.demo ? "가상 러너가 목표 페이스로 코스를 달려요" : "위치 권한을 허용하고 화면을 켜둔 채 사용하세요";

function update(la, lo, tSec) {
  if (finished) return;
  if (t0 === null) { t0 = tSec; lastT = tSec; }
  const pr = project(la, lo);
  const along = Math.max(bestAlong - 30, Math.min(pr.along, total));   // GPS 튐으로 뒤로 크게 가지 않게
  bestAlong = Math.max(bestAlong, along);
  const dt = Math.max(0, tSec - lastT); lastT = tSec;

  // 현재 페이스: 최근 30초 동안 코스 위 진행 거리 기준
  hist.push([tSec, bestAlong]); while (hist.length > 2 && tSec - hist[0][0] > 30) hist.shift();
  const dA = hist[hist.length-1][1] - hist[0][1], dT = hist[hist.length-1][0] - hist[0][0];
  const pace = dA > 5 ? (dT / 60) / (dA / 1000) : NaN;

  // 칼로리: 움직이는 동안만, 현재 페이스로 예상 심박 보정
  if (isFinite(pace) && pace < 20 && dt > 0 && dt < 30) {
    const hr = Math.max(80, Math.min(200, D.hrPred + 7.6 * (D.pace - pace)));
    kcal += kcalPerMin(hr) * dt / 60;
  }

  // 화면 갱신
  $("sPace").textContent = fmtPace(pace);
  $("sKcal").textContent = Math.round(kcal);
  $("sDist").textContent = (bestAlong / 1000).toFixed(2);
  $("fTime").textContent = fmtTime(tSec - t0);
  $("barFill").style.width = `${Math.min(100, kcal / D.targetKcal * 100)}%`;

  const turnBox = $("turn");
  if (pr.d > 40) {
    turnBox.classList.add("off");
    $("tIcon").textContent = "⚠"; $("tDist").textContent = `코스에서 ${Math.round(pr.d)}m 벗어났어요`;
    $("tMsg").textContent = "지도에서 색 있는 선으로 돌아가세요";
  } else {
    turnBox.classList.remove("off");
    const next = turnsM.find(t => t.m > bestAlong + 5);
    if (next) {
      const left = next.m - bestAlong;
      $("tIcon").textContent = next.icon;
      $("tDist").textContent = left < 25 ? "지금" : `${Math.round(left / 10) * 10}m 후`;
      $("tMsg").textContent = next.msg;
    } else {
      $("tIcon").textContent = "🏁";
      $("tDist").textContent = `${Math.max(0, Math.round((total - bestAlong) / 10) * 10)}m 후 도착`;
      $("tMsg").textContent = "출발점으로 돌아가요";
    }
  }
  if (map) {
    me.setLatLng([la, lo]);
    map.panTo([la, lo], {animate: true, duration: .5});
    const k = cum.findIndex(c => c >= bestAlong);
    done.setLatLngs(D.route.slice(0, (k < 0 ? D.route.length : k) + 1));
  }
  if (bestAlong >= total - 30 && tSec - t0 > 60) {
    finished = true;
    $("tIcon").textContent = "🎉"; $("tDist").textContent = "완주!";
    $("tMsg").textContent = `${(bestAlong/1000).toFixed(2)}km · ${Math.round(kcal)}kcal · ${fmtTime(tSec - t0)}`;
  }
}

// 코스 위 진행 거리(m) → 위도·경도 (시연 모드용)
function pointAt(m) {
  let i = cum.findIndex(c => c >= m); if (i <= 0) return D.route[i < 0 ? D.route.length - 1 : 0];
  const t = (m - cum[i-1]) / ((cum[i] - cum[i-1]) || 1);
  return [D.route[i-1][0] + t * (D.route[i][0] - D.route[i-1][0]), D.route[i-1][1] + t * (D.route[i][1] - D.route[i-1][1])];
}

async function start() {
  if (started) return; started = true;
  $("start").style.display = "none";
  try { if (navigator.wakeLock) await navigator.wakeLock.request("screen"); } catch (e) {}   // 화면 꺼짐 방지
  if (map) map.setZoom(18);
  if (D.demo) {
    const speed = 1000 / (D.pace * 60);   // m/s
    let simT = 0;
    const timer = setInterval(() => {
      simT += 10;                          // 1초마다 가상 시간 10초
      const wobble = Math.sin(simT / 40) * 0.25;   // 페이스가 조금씩 오르내리게
      const m = Math.min(total, speed * simT * (1 + wobble * 0.15));
      const [la, lo] = pointAt(m);
      update(la, lo, simT);
      if (finished) clearInterval(timer);
    }, 1000);
    window.__simTimer = timer;
  } else if (navigator.geolocation) {
    navigator.geolocation.watchPosition(
      pos => update(pos.coords.latitude, pos.coords.longitude, pos.timestamp / 1000),
      err => { $("tDist").textContent = "위치를 가져올 수 없어요"; $("tMsg").textContent = "위치 권한을 허용해주세요"; },
      {enableHighAccuracy: true, maximumAge: 1000, timeout: 15000});
  } else {
    $("tDist").textContent = "이 브라우저는 위치를 지원하지 않아요";
  }
}
$("go").addEventListener("click", start);
</script></body></html>"""
