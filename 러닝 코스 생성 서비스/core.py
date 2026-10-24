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
    """OpenRouteService: 출발점으로 돌아오는 걷기·러닝 코스 [[경도, 위도, 고도], ...]"""
    body = {"coordinates": [[lon, lat]], "elevation": True,
            "options": {"round_trip": {"length": int(length_m), "points": 5, "seed": int(seed)}}}
    r = requests.post(ORS_URL, json=body, timeout=30,
                      headers={"Authorization": api_key, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"코스 생성 실패 ({r.status_code}): {r.text[:200]}")
    return r.json()["features"][0]["geometry"]["coordinates"]


def find_courses(model, api_key, lat, lon, target_kcal, pace, user, n_seeds=4, top=3,
                 route_fn=get_round_trip):
    """목표 칼로리에 가장 가까운 순환 코스 찾기
    1) 평지 기준 필요한 거리 추정 → 2) 코스 후보 여러 개 생성 → 3) 칼로리 예측
    4) 목표와 5% 넘게 차이 나면 길이를 비율만큼 조정해 한 번 더 생성 → 5) 가까운 순으로 top개"""
    dist = estimate_distance(model, target_kcal, pace, user)
    found, errors = [], []
    for seed in range(1, n_seeds + 1):
        length = dist * 1000
        for _ in range(2):
            try:
                coords = route_fn(api_key, lat, lon, length, seed)
            except Exception as e:          # noqa: BLE001 - 사용자에게 메시지로 보여줌
                errors.append(str(e))
                break
            res = evaluate(model, route_features(coords), pace, user)
            res.update(coords=coords, seed=seed)
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
    return picked, errors


def fmt_pace(p):
    m = int(p); s = int(round((p - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}'{s:02d}\""
