import json

import folium
import lightgbm as lgb
import streamlit as st
from streamlit_folium import st_folium

import core

st.set_page_config(page_title="칼로리 맞춤 러닝 코스", page_icon="🏃", layout="wide")

DEFAULT_START = (35.1760, 126.9058)          # 시작 지도 위치 (광주)
COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]   # 코스 A, B, C
NAMES = ["A", "B", "C"]


@st.cache_resource
def load():
    model = lgb.Booster(model_file="hr_model.txt")
    with open("app_defaults.json", encoding="utf-8") as f:
        defaults = json.load(f)
    return model, defaults


def get_api_key():
    try:
        return st.secrets["ORS_API_KEY"]
    except Exception:  # noqa: BLE001 - Secrets가 없으면 화면에서 입력
        return st.sidebar.text_input("OpenRouteService API 키", type="password")


def pace_input(label, default, key):
    """분/초로 페이스 입력 → 분/km (소수)"""
    st.markdown(f"**{label}**")
    c1, c2 = st.columns(2)
    m = c1.number_input("분", 3, 12, int(default), key=f"{key}_m")
    s0 = min(55, int(round((default - int(default)) * 60 / 5) * 5))
    s = c2.number_input("초", 0, 55, s0, step=5, key=f"{key}_s")
    return m + s / 60


model, defaults = load()
if "start" not in st.session_state:
    st.session_state.start = DEFAULT_START
if "results" not in st.session_state:
    st.session_state.results = None

# ------------------------------------------------------------------ 사이드바: 입력
with st.sidebar:
    st.header("🎯 목표")
    target = st.number_input("목표 소모 칼로리 (kcal)", 50, 1500, 300, step=10)
    pace = pace_input("목표 페이스 (분/km)", 6.0, "pace")

    st.header("🙋 내 정보")
    gender = st.radio("성별", ["남성", "여성"], horizontal=True)
    male = gender == "남성"
    gkey = "male" if male else "female"
    age = st.number_input("나이", 10, 90, 25)
    weight = st.number_input("몸무게 (kg)", 30, 200, 65)

    st.header("📈 최근 러닝 기록")
    pace_recent = pace_input("최근 평소 페이스 (분/km)", float(defaults["pace_recent_median"][gkey]), "prec")
    know_hr = st.checkbox("최근 러닝 평균 심박을 알아요 (스마트워치 등)", value=True)
    if know_hr:
        hr_recent = st.number_input("최근 러닝 평균 심박 (bpm)", 80, 200, int(defaults["hr_recent_median"][gkey]))
        hr_error = defaults["hr_error_known"]
    else:
        hr_recent = float(defaults["hr_recent_median"][gkey])
        hr_error = defaults["hr_error_unknown"]
        st.caption(f"성별 평균({hr_recent:.0f}bpm)으로 추정해요. 입력하면 예측 범위가 ±{defaults['hr_error_unknown']} → "
                   f"±{defaults['hr_error_known']}bpm으로 좁아져요.")
    api_key = get_api_key()

user = dict(male=male, weight=weight, age=age, hr_recent=hr_recent, pace_recent=pace_recent, hr_error=hr_error)

# ------------------------------------------------------------------ 본문
st.title("🏃 칼로리 맞춤 러닝 코스")
st.write("목표 칼로리와 페이스를 정하고, **지도에서 출발 위치를 클릭**한 뒤 **코스 찾기**를 누르세요. "
         "출발점으로 돌아오는 코스 중 목표 칼로리에 가장 가까운 코스를 추천해요.")

left, right = st.columns([3, 2])
with left:
    lat, lon = st.session_state.start
    m = folium.Map(location=[lat, lon], zoom_start=14, tiles="OpenStreetMap")
    folium.Marker([lat, lon], tooltip="출발", icon=folium.Icon(color="red", icon="play")).add_to(m)
    res = st.session_state.results
    if res:
        for i, r in enumerate(res):
            folium.PolyLine([(c[1], c[0]) for c in r["coords"]], color=COLORS[i], weight=5, opacity=0.85,
                            tooltip=f"코스 {NAMES[i]}: {r['dist_km']:.1f}km · 약 {r['kcal']:.0f}kcal").add_to(m)
    out = st_folium(m, height=520, use_container_width=True, returned_objects=["last_clicked"], key="map")
    if out and out.get("last_clicked"):
        new = (out["last_clicked"]["lat"], out["last_clicked"]["lng"])
        if new != st.session_state.start:
            st.session_state.start = new
            st.session_state.results = None
            st.rerun()

with right:
    st.subheader("출발 위치")
    st.caption(f"위도 {lat:.5f}, 경도 {lon:.5f}")
    st.markdown(f"목표 **{target}kcal** · 페이스 **{core.fmt_pace(pace)}/km** "
                f"(평소보다 {'빠르게' if pace < pace_recent else '느리게' if pace > pace_recent else '같게'})")

    if st.button("🔎 코스 찾기", type="primary"):
        if not api_key:
            st.error("OpenRouteService API 키가 필요해요. 사이드바에 입력하거나 Secrets에 설정하세요.")
        else:
            with st.spinner("코스를 만들고 칼로리를 계산하는 중... (10~20초)"):
                found, errors = core.find_courses(model, api_key, lat, lon, target, pace, user)
            st.session_state.results = found
            if not found:
                st.error("코스를 만들지 못했어요. 출발 위치를 큰길이나 공원 근처로 바꿔보세요.")
                if errors:
                    st.caption(errors[0])
            st.rerun()

    res = st.session_state.results
    if res:
        for i, r in enumerate(res):
            with st.container(border=True):
                st.markdown(f"<span style='color:{COLORS[i]};font-size:1.3em'>●</span> "
                            f"**코스 {NAMES[i]}**", unsafe_allow_html=True)
                c1, c2, c3 = st.columns(3)
                c1.metric("예상 칼로리", f"{r['kcal']:.0f} kcal")
                c2.metric("거리", f"{r['dist_km']:.1f} km")
                c3.metric("시간", f"{r['minutes']:.0f} 분")
                st.caption(f"칼로리 범위 {r['kcal_lo']:.0f}~{r['kcal_hi']:.0f}kcal · "
                           f"예상 평균 심박 {r['hr']:.0f}bpm ({r['hr_lo']:.0f}~{r['hr_hi']:.0f}) · "
                           f"오르막 {r['ascent_m']:.0f}m")

st.divider()
st.caption("예측 모델: LightGBM · 학습 데이터 UCSD FitRec(Endomondo) 러닝 기록 약 6.6만 건, 사용자 683명 · "
           "처음 보는 사용자 기준 평균 심박 오차 5.3bpm, 90%가 ±12bpm 이내 · 칼로리는 Keytel(2005) 공식으로 계산 · "
           "코스: OpenRouteService · 건강 상태에 따라 실제와 다를 수 있으니 참고용으로 사용하세요.")
