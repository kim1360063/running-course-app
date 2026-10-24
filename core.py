"""러닝 코스 추천 앱의 계산 부분 (화면과 분리해서 테스트하기 쉽게).

예측 평균 심박 = 최근 평소 심박 + LightGBM(평소 대비 페이스, 거리, 1km당 오르막,
                 가장 긴 오르막, 경사, 성별, 최근 평소 심박, 최근 평소 페이스)
칼로리 = Keytel 공식(예측 심박, 몸무게, 나이, 성별) x 운동 시간
"""
import numpy as np
import pandas as pd
import requests

FEATURES = ['pace_rel4', 'dist_km', 'ascent_per_km', 'longest_climb_gain', 'grade_p90',
            'gender_m', 'hr_recent', 'pace_recent']
ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"


# ---------------------------------------------------------------- 코스 지형 계산
def haversine(lat1, lon1, lat2, lon2):
    """두 좌표 사이 거리 (m)"""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * np.arcsin(np.sqrt(a))


def route_features(coords):
    """코스 좌표 [[경도, 위도, 고도], ...] → 학습 때와 같은 방식으로 지형 특성 계산"""
    c = np.asarray(coords, float)
    lon, lat = c[:, 0], c[:, 1]
    ele = c[:, 2] if c.shape[1] > 2 else np.zeros(len(c))
    seg = haversine(lat[:-1], lon[:-1], lat[1:], lon[1:])
    cum = np.r_[0, np.cumsum(seg)]
    dist_km = cum[-1] / 1000

    ele_s = pd.Series(ele).rolling(5, center=True, min_periods=1).median().to_numpy()
    grid = np.arange(0, cum[-1], 50)
    if len(grid) < 3:
        grid = np.linspace(0, cum[-1], 3)
    d = np.diff(np.interp(grid, cum, ele_s))
    step = max(grid[1] - grid[0], 1e-9)
    grade = d / step * 100

    best_gain = cur_gain = 0.0
    for g, dd in zip(grade, d):
        if g > 2:
            cur_gain += dd
            best_gain = max(best_gain, cur_gain)
        else:
            cur_gain = 0.0

    ascent = float(d[d > 0].sum())
    return {
        'dist_km': dist_km,
        'ascent_m': ascent,
        'ascent_per_km': ascent / max(dist_km, 1e-9),
        'grade_p90': float(np.percentile(grade, 90)),
        'longest_climb_gain': best_gain,
    }


# ---------------------------------------------------------------- 심박 · 칼로리
def predict_hr(model, terrain, pace, user):
    """예측 평균 심박 = 최근 평소 심박 + 모델 예측(평소 대비)"""
    row = {
        'pace_rel4': pace - user['pace_recent'],
        'dist_km': terrain['dist_km'],
        'ascent_per_km': terrain['ascent_per_km'],
        'longest_climb_gain': terrain['longest_climb_gain'],
        'grade_p90': terrain['grade_p90'],
        'gender_m': 1 if user['male'] else 0,
        'hr_recent': user['hr_recent'],
        'pace_recent': user['pace_recent'],
    }
    X = pd.DataFrame([row])[FEATURES]
    return float(user['hr_recent'] + model.predict(X)[0])


def keytel_kcal_per_min(hr, weight, age, male):
    """Keytel(2005) 심박 기반 분당 칼로리"""
    if male:
        v = (-55.0969 + 0.6309 * hr + 0.1988 * weight + 0.2017 * age) / 4.184
    else:
        v = (-20.4022 + 0.4472 * hr - 0.1263 * weight + 0.074 * age) / 4.184
    return max(v, 0.0)


def evaluate(model, terrain, pace, user):
    """코스 하나 평가 → 시간, 예상 심박, 칼로리와 범위"""
    minutes = terrain['dist_km'] * pace
    hr = predict_hr(model, terrain, pace, user)
    err = user['hr_error']
    k = lambda h: keytel_kcal_per_min(h, user['weight'], user['age'], user['male']) * minutes
    return {**terrain, 'minutes': minutes, 'hr': hr, 'hr_lo': hr - err, 'hr_hi': hr + err,
            'kcal': k(hr), 'kcal_lo': k(hr - err), 'kcal_hi': k(hr + err)}


def estimate_distance(model, target_kcal, pace, user):
    """평지라고 가정하고 목표 칼로리에 필요한 거리 추정 (km)"""
    dist = 4.0
    for _ in range(4):
        flat = {'dist_km': dist, 'ascent_m': 0, 'ascent_per_km': 0, 'grade_p90': 0, 'longest_climb_gain': 0}
        r = evaluate(model, flat, pace, user)
        per_km = r['kcal'] / dist
        if per_km <= 0:
            break
        dist = float(np.clip(target_kcal / per_km, 0.5, 30))
    return dist


# ---------------------------------------------------------------- 코스 만들기
def get_round_trip(api_key, lat, lon, length_m, seed):
    """OpenRouteService: 출발점으로 돌아오는 걷기·러닝 코스
    → (좌표 [[경도, 위도, 고도], ...], 길 정보 {'surface': [...], 'waytype': [...]})"""
    body = {"coordinates": [[lon, lat]], "elevation": True, "extra_info": ["surface", "waytype"],
            "options": {"round_trip": {"length": int(length_m), "points": 5, "seed": int(seed)}}}
    r = requests.post(ORS_URL, json=body, timeout=30,
                      headers={"Authorization": api_key, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"코스 생성 실패 ({r.status_code}): {r.text[:200]}")
    feat = r.json()["features"][0]
    props = feat.get("properties", {})
    extras = {k: v.get("summary", []) for k, v in props.get("extras", {}).items()}
    # 갈림길 안내용: 꺾는 방향(type), 그 구간 거리, 길 이름, 시작 좌표 번호
    extras["steps"] = [{"type": s.get("type"), "distance": s.get("distance", 0), "name": s.get("name", ""),
                        "wp": s.get("way_points", [0])[0]}
                       for seg in props.get("segments", []) for s in seg.get("steps", [])]
    return feat["geometry"]["coordinates"], extras


# ---------------------------------------------------------------- 코스 특징
# OpenRouteService 길 정보 코드표
UNPAVED = {2, 8, 9, 10, 11, 12, 15, 16, 17, 18}    # 비포장, 자갈, 흙, 모래, 잔디 등
BIG_ROAD = {1, 2}                                   # 큰 도로, 일반 도로
WALKWAY = {4, 6, 7}                                 # 오솔길, 자전거길, 보행로
STEPS = 8                                           # 계단


def share(summary, codes):
    """길 정보 요약에서 해당 코드들이 차지하는 비율(%)"""
    return sum(s.get("amount", 0) for s in summary if int(s.get("value", -1)) in codes)


def count_crossings(coords, timeout=20):
    """OpenStreetMap에서 코스 15m 이내의 횡단보도·신호등 개수 (실패하면 None)"""
    c = np.asarray(coords, float)
    s, w = c[:, 1].min() - 0.001, c[:, 0].min() - 0.001
    n, e = c[:, 1].max() + 0.001, c[:, 0].max() + 0.001
    q = (f'[out:json][timeout:{timeout}];(node["highway"="crossing"]({s},{w},{n},{e});'
         f'node["highway"="traffic_signals"]({s},{w},{n},{e}););out;')
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=timeout + 5)
        nodes = r.json()["elements"]
    except Exception:  # noqa: BLE001 - 없어도 앱은 동작
        return None
    if not nodes:
        return {"crossings": 0, "signals": 0}
    # 위도·경도를 미터 단위 평면으로 바꿔서 점과 선분 사이 거리 계산
    k = np.cos(np.radians(c[:, 1].mean()))
    P = np.c_[(c[:, 0]) * 111320 * k, c[:, 1] * 110540]
    A, B = P[:-1], P[1:]
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-9)
    counts = {"crossings": 0, "signals": 0}
    for nd in nodes:
        p = np.array([nd["lon"] * 111320 * k, nd["lat"] * 110540])
        t = np.clip(((p - A) * AB).sum(1) / L2, 0, 1)
        dist = np.sqrt((((A + t[:, None] * AB) - p) ** 2).sum(1)).min()
        if dist <= 15:
            counts["signals" if nd.get("tags", {}).get("highway") == "traffic_signals" else "crossings"] += 1
    return counts


def describe(r, others):
    """코스 특징 문장 목록"""
    tags = []
    if r["grade_p90"] >= 6 or r["longest_climb_gain"] >= 25:
        tags.append(f"⛰️ 가파른 언덕 있음 (한 번에 {r['longest_climb_gain']:.0f}m 오르막)")
    elif r["ascent_per_km"] < 5 and r["grade_p90"] < 3:
        tags.append("🟢 평지 위주라 편하게 뛸 수 있어요")
    else:
        tags.append("〽️ 완만한 오르막·내리막이 섞여 있어요")

    ex = r.get("extras", {})
    if ex.get("surface"):
        un = share(ex["surface"], UNPAVED)
        if un >= 10:
            tags.append(f"🟫 비포장 구간 약 {un:.0f}% — 미끄럼·발목 주의")
    if ex.get("waytype"):
        if share(ex["waytype"], {STEPS}) > 0:
            tags.append("🪜 계단 구간이 있어요")
        if share(ex["waytype"], WALKWAY) >= 50:
            tags.append("🌳 보행로·산책로 위주")
        elif share(ex["waytype"], BIG_ROAD) >= 40:
            tags.append("🚗 차도 옆 구간이 많아요")

    cx = r.get("cross")
    if cx is not None:
        total = cx["crossings"] + cx["signals"]
        mine = [o["cross"]["crossings"] + o["cross"]["signals"] for o in others if o.get("cross") is not None]
        if len(mine) > 1 and total == min(mine) and mine.count(total) == 1:
            tags.append(f"🚦 횡단보도·신호 가장 적음 (약 {total}곳)")
        elif total / max(r["dist_km"], 0.1) >= 3:
            tags.append(f"🚦 횡단보도·신호 많음 (약 {total}곳) — 신호 대기 주의")
        else:
            tags.append(f"🚦 횡단보도·신호 약 {total}곳")
    return tags


TURN = {0: "좌회전", 1: "우회전", 2: "왼쪽으로 크게 꺾기", 3: "오른쪽으로 크게 꺾기",
        4: "왼쪽으로 살짝", 5: "오른쪽으로 살짝", 7: "로터리 진입", 8: "로터리 나가기",
        9: "유턴", 12: "왼쪽 길 유지", 13: "오른쪽 길 유지"}


def turn_list(r):
    """갈림길 안내 [(번호, 몇 km 지점, 안내 문장, 위도, 경도), ...]"""
    steps = r.get("extras", {}).get("steps") or []
    c = np.asarray(r["coords"], float)
    seg = haversine(c[:-1, 1], c[:-1, 0], c[1:, 1], c[1:, 0])
    cum = np.r_[0, np.cumsum(seg)]
    out = []
    for s in steps:
        t = s.get("type")
        if t not in TURN:
            continue
        i = min(int(s.get("wp", 0)), len(c) - 1)
        name = (s.get("name") or "").strip()
        msg = TURN[t] + (f" → {name}" if name and name != "-" else "")
        out.append((len(out) + 1, cum[i] / 1000, msg, float(c[i, 1]), float(c[i, 0])))
    return out


def km_marks(coords):
    """1km마다 위치 [(km, 위도, 경도), ...]"""
    c = np.asarray(coords, float)
    seg = haversine(c[:-1, 1], c[:-1, 0], c[1:, 1], c[1:, 0])
    cum = np.r_[0, np.cumsum(seg)]
    out = []
    for km in range(1, int(cum[-1] // 1000) + 1):
        d = km * 1000
        out.append((km, float(np.interp(d, cum, c[:, 1])), float(np.interp(d, cum, c[:, 0]))))
    return out


def find_courses(model, api_key, lat, lon, target_kcal, pace, user, n_seeds=4, top=3,
                 route_fn=get_round_trip, cross_fn=count_crossings):
    """목표 칼로리에 가장 가까운 순환 코스 찾기
    1) 평지 기준 필요한 거리 추정 → 2) 코스 후보 여러 개 생성 → 3) 칼로리 예측
    4) 목표와 5% 넘게 차이 나면 길이를 비율만큼 조정해 한 번 더 생성 → 5) 가까운 순으로 top개
    6) 고른 코스의 횡단보도 수와 특징 문장 추가"""
    dist = estimate_distance(model, target_kcal, pace, user)
    found, errors = [], []
    for seed in range(1, n_seeds + 1):
        length = dist * 1000
        for _ in range(2):
            try:
                coords, extras = route_fn(api_key, lat, lon, length, seed)
            except Exception as e:          # noqa: BLE001 - 사용자에게 메시지로 보여줌
                errors.append(str(e))
                break
            res = evaluate(model, route_features(coords), pace, user)
            res.update(coords=coords, extras=extras, seed=seed)
            found.append(res)
            ratio = target_kcal / max(res['kcal'], 1e-9)
            if abs(ratio - 1) <= 0.05:
                break
            length *= ratio
    found.sort(key=lambda r: abs(r['kcal'] - target_kcal))
    picked, seen = [], set()
    for r in found:                         # 같은 seed의 코스는 하나만
        if r['seed'] in seen:
            continue
        picked.append(r); seen.add(r['seed'])
        if len(picked) == top:
            break
    for r in picked:
        r['cross'] = cross_fn(r['coords'])
    for r in picked:
        r['tags'] = describe(r, picked)
    return picked, errors


def fmt_pace(p):
    m = int(p); s = int(round((p - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""
