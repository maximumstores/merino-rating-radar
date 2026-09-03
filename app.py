import datetime
import json
import os
import re
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from dotenv import load_dotenv
from sklearn.linear_model import LinearRegression

from collector import (
    check_asin,
    clean_db_trash,
    delete_asin_completely,
    ensure_schema,
    extract_asin,
    finish_run,
    get_tracked_asins,
    save_to_db,
    start_run,
)

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(
    page_title="Rating Radar",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ==================== ДИЗАЙН ====================
PALETTE = {
    "ok": "#1f8a4c",
    "warn": "#c77800",
    "risk": "#d13438",
    "none": "#8e8e93",
    "accent": "#0071e3",
    "ink": "#1d1d1f",
    "muted": "#6e6e73",
}
STATUS_COLOR = {"ОК": PALETTE["ok"], "Внимание": PALETTE["warn"],
                "Риск": PALETTE["risk"], "Нет данных": PALETTE["none"]}

st.markdown(
    """
<style>
    .stApp { background: #f5f5f7;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif; }
    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1480px; }
    h1, h2, h3 { font-weight: 650 !important; letter-spacing: -0.02em !important; color: #1d1d1f !important; }
    h3 { margin-top: 0.4rem !important; }
    div[data-testid="stForm"], div[data-testid="stExpander"] {
        border: 1px solid #e5e5ea !important; border-radius: 12px !important; background: #fff !important; }
    div[data-testid="stTabs"] button { font-weight: 600; font-size: 15px; }
    .kpi { background:#fff; border:1px solid #e5e5ea; border-radius:14px; padding:16px 18px;
           box-shadow: 0 1px 2px rgba(0,0,0,.03); }
    .kpi .lbl { font-size:12px; color:#6e6e73; text-transform:uppercase; letter-spacing:.06em; }
    .kpi .val { font-size:30px; font-weight:700; color:#1d1d1f; line-height:1.15; margin-top:4px; }
    .kpi .sub { font-size:12px; color:#6e6e73; margin-top:4px; }
    .badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px;
             font-weight:600; color:#fff; }
    .card-asin { font-family: ui-monospace, Menlo, monospace; font-weight:600; font-size:14px; }
    .muted { color:#6e6e73; font-size:13px; }
</style>
""",
    unsafe_allow_html=True,
)

PLOTLY_LAYOUT = dict(
    template="plotly_white",
    font=dict(family="-apple-system, BlinkMacSystemFont, Helvetica Neue, sans-serif", size=12, color=PALETTE["ink"]),
    margin=dict(l=10, r=10, t=30, b=10),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    hoverlabel=dict(bgcolor="#fff", font_size=12),
)


def style_fig(fig, height=320, **kw):
    fig.update_layout(**PLOTLY_LAYOUT, height=height, **kw)
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor="#ececf0", zeroline=False)
    return fig


def kpi(col, label, value, sub="", color=None):
    v_style = f"color:{color}" if color else ""
    col.markdown(
        f"<div class='kpi'><div class='lbl'>{label}</div>"
        f"<div class='val' style='{v_style}'>{value}</div>"
        f"<div class='sub'>{sub}</div></div>",
        unsafe_allow_html=True,
    )


def badge(text):
    return f"<span class='badge' style='background:{STATUS_COLOR.get(text, PALETTE['none'])}'>{text}</span>"


clean_db_trash()

TIMEZONES = {
    "Киев (EEST / EET)": "Europe/Kyiv",
    "UTC": "UTC",
    "Берлин / Париж (CET)": "Europe/Berlin",
    "Лондон (BST / GMT)": "Europe/London",
    "Нью-Йорк (EDT / EST)": "America/New_York",
}
MARKET_DOMAINS = {
    "US": "amazon.com", "BE": "amazon.com.be", "NL": "amazon.nl", "DE": "amazon.de",
    "UK": "amazon.co.uk", "FR": "amazon.fr", "IT": "amazon.it", "ES": "amazon.es",
}
VALID_SOURCES = tuple(MARKET_DOMAINS.keys())


# ==================== ДАННЫЕ ====================
def _conn():
    return psycopg2.connect(DATABASE_URL)


def get_last_run():
    try:
        conn = _conn()
        row = pd.read_sql(
            "SELECT started_at, finished_at, asin_count, ok_count, status "
            "FROM collection_runs ORDER BY started_at DESC LIMIT 1", conn)
        conn.close()
        return row.iloc[0] if not row.empty else None
    except Exception:
        return None


def get_runs_history(limit=60):
    try:
        conn = _conn()
        df = pd.read_sql(
            "SELECT started_at, finished_at, asin_count, ok_count, status "
            f"FROM collection_runs ORDER BY started_at DESC LIMIT {int(limit)}", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def get_asin_markets_map(all_tracked):
    if not DATABASE_URL:
        return {a: "BE" for a in all_tracked}
    try:
        conn = _conn()
        df_src = pd.read_sql(
            "SELECT DISTINCT ON (asin) asin, source FROM asin_metrics ORDER BY asin, created_at DESC", conn)
        conn.close()
        db_map = dict(zip(df_src["asin"], df_src["source"]))
        return {a: (db_map.get(a) if db_map.get(a) in MARKET_DOMAINS else "BE") for a in all_tracked}
    except Exception:
        return {a: "BE" for a in all_tracked}


def get_full_history():
    if not DATABASE_URL:
        return pd.DataFrame()
    try:
        conn = _conn()
        df = pd.read_sql(
            """
            SELECT asin, source, rating, review_count, histogram_json, image_url, bsr, note, created_at
            FROM asin_metrics
            WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
            ORDER BY created_at ASC;
            """, conn)
        conn.close()
        df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
        df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
        df["review_count"] = pd.to_numeric(df["review_count"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


def parse_hist(raw):
    if not raw:
        return None
    try:
        h = json.loads(raw) if isinstance(raw, str) else raw
        return {str(k): int(v) for k, v in h.items()}
    except Exception:
        return None


def run_collection(items, label="Прогон"):
    ensure_schema()
    run_id = start_run(len(items))
    progress = st.progress(0.0, text=f"0/{len(items)}")
    log_box = st.empty()
    log_lines = []
    ok = 0
    for i, item in enumerate(items, 1):
        def _log(msg, _lines=log_lines):
            _lines.append(msg)
            log_box.code("\n".join(_lines[-15:]))
        try:
            res = check_asin(item, log=_log)
            save_to_db(res)
            if res.get("source") in VALID_SOURCES:
                ok += 1
        except Exception as e:
            _log(f"Ошибка {item}: {e}")
        progress.progress(i / len(items), text=f"{i}/{len(items)}")
    finish_run(run_id, ok, "done")
    st.session_state["last_auto_run"] = time.time()
    st.success(f"{label} завершён: {ok}/{len(items)} успешно.")
    st.rerun()


# ==================== ШАПКА ====================
hdr_l, hdr_r = st.columns([3, 2])
with hdr_l:
    st.title("📡 Rating Radar")
    st.markdown("<div class='muted'>Мониторинг качества листингов и аналитика портфеля</div>", unsafe_allow_html=True)
with hdr_r:
    last_run = get_last_run()
    if last_run is not None:
        started_kyiv = pd.to_datetime(last_run["started_at"]).tz_convert(ZoneInfo("Europe/Kyiv"))
        status_label = "завершён" if last_run["status"] == "done" else "в процессе"
        st.info(
            f"Последний сбор: **{started_kyiv:%d.%m.%Y %H:%M}** (Киев) · {status_label} · "
            f"валидных **{int(last_run['ok_count'] or 0)} / {int(last_run['asin_count'] or 0)}**")
    else:
        st.warning("История сборов пуста")

tracked = get_tracked_asins()
asin_market_map = get_asin_markets_map(tracked)
full_df = get_full_history()

# ==================== РАСЧЁТ ТЕКУЩИХ МЕТРИК ====================
def build_calc_df(df):
    if df.empty:
        return pd.DataFrame()
    latest = df.sort_values("created_at").groupby("asin").last().reset_index()
    latest = latest.sort_values("created_at", ascending=False)

    # дельты vs предыдущий замер
    prev = (df.sort_values("created_at").groupby("asin").nth(-2)
            .reset_index()[["asin", "rating", "review_count"]]
            .rename(columns={"rating": "prev_rating", "review_count": "prev_reviews"}))
    latest = latest.merge(prev, on="asin", how="left")

    rows = []
    for _, r in latest.iterrows():
        rating = float(r["rating"]) if pd.notnull(r["rating"]) else None
        cnt = int(r["review_count"]) if pd.notnull(r["review_count"]) else None
        source = str(r["source"]) if pd.notnull(r["source"]) else "none"
        h = parse_hist(r["histogram_json"])
        bad_pct = (h.get("1", 0) + h.get("2", 0)) if h else 0
        five_pct = h.get("5", 0) if h else None

        margin = None
        if rating is not None and cnt is not None and rating > 4.0:
            margin = max(0, int((cnt * (rating - 4.0)) / 3.0))

        if source == "none" or rating is None:
            status = "Нет данных"
        elif rating <= 4.2:
            status = "Риск"
        elif rating == 4.3 or bad_pct > 15:
            status = "Внимание"
        else:
            status = "ОК"

        trend = "—"
        if bad_pct > 20 or (rating is not None and rating < 4.2):
            trend = "↓"
        elif bad_pct < 8 and rating is not None and rating >= 4.5:
            trend = "↑"

        d_rating = (rating - float(r["prev_rating"])) if (rating is not None and pd.notnull(r["prev_rating"])) else None
        d_reviews = (cnt - int(r["prev_reviews"])) if (cnt is not None and pd.notnull(r["prev_reviews"])) else None

        img = r["image_url"]
        if not (isinstance(img, str) and img.startswith("http")):
            img = None
        bsr = r["bsr"] if pd.notnull(r["bsr"]) and r["bsr"] else "—"

        rows.append({
            "Выбор": False,
            "raw_asin": str(r["asin"]),
            "Статус": status,
            "raw_created_at": r["created_at"],
            "ASIN": f"https://www.{MARKET_DOMAINS.get(source, 'amazon.com.be')}/dp/{r['asin']}",
            "Фото": img,
            "Источник": source,
            "Рейтинг": rating,
            "Δ Рейтинг": d_rating,
            "Отзывы": cnt,
            "Δ Отзывы": d_reviews,
            "bad_pct": bad_pct,
            "five_pct": five_pct,
            "1–2★ %": f"{bad_pct}%" if h else "—",
            "Тренд": trend,
            "margin": margin,
            "Запас (до 4.0)": f"{margin} ед." if margin is not None else "—",
            "BSR": bsr,
            "Комментарий": r["note"] if pd.notnull(r["note"]) else "",
        })
    return pd.DataFrame(rows)


calc_df = build_calc_df(full_df)

# ==================== KPI ====================
if not calc_df.empty:
    n_total = len(calc_df)
    n_risk = int((calc_df["Статус"] == "Риск").sum())
    n_warn = int((calc_df["Статус"] == "Внимание").sum())
    n_ok = int((calc_df["Статус"] == "ОК").sum())
    n_none = int((calc_df["Статус"] == "Нет данных").sum())
    avg_r = calc_df["Рейтинг"].mean()
    w_avg = None
    if calc_df["Отзывы"].fillna(0).sum() > 0:
        m = calc_df.dropna(subset=["Рейтинг", "Отзывы"])
        w_avg = (m["Рейтинг"] * m["Отзывы"]).sum() / m["Отзывы"].sum()
    tot_reviews = int(calc_df["Отзывы"].fillna(0).sum())
    new_reviews = int(calc_df["Δ Отзывы"].fillna(0).clip(lower=0).sum())

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    kpi(k1, "Позиций", n_total, f"отслеживается {len(tracked)}")
    kpi(k2, "Средний рейтинг", f"{avg_r:.2f}" if pd.notnull(avg_r) else "—",
        f"взвеш. по отзывам {w_avg:.2f}" if w_avg else "")
    kpi(k3, "ОК", n_ok, f"{n_ok / n_total:.0%} портфеля", PALETTE["ok"])
    kpi(k4, "Внимание", n_warn, "4.3★ или >15% негатива", PALETTE["warn"])
    kpi(k5, "Риск", n_risk, "≤ 4.2★", PALETTE["risk"])
    kpi(k6, "Отзывов всего", f"{tot_reviews:,}".replace(",", " "),
        f"+{new_reviews} с прошлого замера" if new_reviews else f"нет данных: {n_none}")
    st.markdown("<br>", unsafe_allow_html=True)

# ==================== ГЛОБАЛЬНЫЕ ФИЛЬТРЫ ====================
fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1.5, 1.5, 1.6, 1.2])
all_asins = calc_df["raw_asin"].tolist() if not calc_df.empty else []
all_sources = sorted(calc_df["Источник"].dropna().unique().tolist()) if not calc_df.empty else []

with fc1:
    sel_asins = st.multiselect("Фильтр ASIN", options=all_asins, default=[], placeholder="Все ASIN")
with fc2:
    sel_sources = st.multiselect("Источник", options=all_sources, default=[], placeholder="Все")
with fc3:
    sel_status = st.multiselect("Статус", options=["ОК", "Внимание", "Риск", "Нет данных"], default=[],
                                placeholder="Все")
with fc4:
    selected_tz_label = st.selectbox("Часовой пояс", options=list(TIMEZONES.keys()), index=0, key="sel_tz_val")
    selected_tz = TIMEZONES[selected_tz_label]
    tz_short = selected_tz_label.split(" ")[0]
with fc5:
    period_days = st.selectbox("Период истории", options=[7, 14, 30, 60, 90, 365], index=2,
                               format_func=lambda d: f"{d} дн.")

if not calc_df.empty:
    filtered_df = calc_df[
        calc_df["raw_asin"].isin(sel_asins if sel_asins else all_asins)
        & calc_df["Источник"].isin(sel_sources if sel_sources else all_sources)
        & calc_df["Статус"].isin(sel_status if sel_status else ["ОК", "Внимание", "Риск", "Нет данных"])
    ].copy()
    filtered_df["Время сбора"] = filtered_df["raw_created_at"].apply(
        lambda dt: pd.to_datetime(dt).tz_convert(ZoneInfo(selected_tz)).strftime("%d.%m.%Y %H:%M")
        if pd.notnull(dt) else "—")
    f_asins = filtered_df["raw_asin"].tolist()

    hist_df = full_df[full_df["asin"].isin(f_asins)].copy()
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=period_days)
    hist_df = hist_df[hist_df["created_at"] >= cutoff]
    hist_df["created_local"] = hist_df["created_at"].dt.tz_convert(ZoneInfo(selected_tz))
else:
    filtered_df = pd.DataFrame()
    hist_df = pd.DataFrame()

# ==================== ВКЛАДКИ ====================
tab_port, tab_an, tab_fc, tab_ops = st.tabs(["📋 Портфель", "📊 Аналитика", "📈 Прогноз", "⚙️ Сбор и управление"])

# ---------- ПОРТФЕЛЬ ----------
with tab_port:
    if filtered_df.empty:
        st.warning("В базе нет сохранённых метрик под текущие фильтры")
    else:
        hc, vc = st.columns([3, 1])
        hc.markdown(f"### Сводный отчёт <span class='muted'>· {len(filtered_df)} из {len(calc_df)} позиций</span>",
                    unsafe_allow_html=True)
        view_mode = vc.radio("Вид", options=["Таблица", "Карточки"], horizontal=True, label_visibility="collapsed")

        if view_mode == "Таблица":
            cols_order = ["Выбор", "Статус", "Время сбора", "ASIN", "Фото", "Источник", "Рейтинг", "Δ Рейтинг",
                          "Отзывы", "Δ Отзывы", "1–2★ %", "Тренд", "Запас (до 4.0)", "BSR", "Комментарий"]
            display_tbl = filtered_df[cols_order]
            if sel_asins:
                display_tbl = display_tbl.assign(Выбор=filtered_df["raw_asin"].isin(sel_asins))

            edited_df = st.data_editor(
                display_tbl,
                column_config={
                    "Выбор": st.column_config.CheckboxColumn("✓", default=False, width="small"),
                    "Статус": st.column_config.TextColumn("Статус", width="small", disabled=True),
                    "Время сбора": st.column_config.TextColumn(f"Сбор ({tz_short})", width="medium", disabled=True),
                    "ASIN": st.column_config.LinkColumn("ASIN", width="medium", disabled=True,
                                                        display_text=r"/dp/([A-Z0-9]{10})"),
                    "Фото": st.column_config.ImageColumn("Фото", width="small"),
                    "Источник": st.column_config.TextColumn("Ист.", width="small", disabled=True),
                    "Рейтинг": st.column_config.ProgressColumn("Рейтинг", format="%.2f", min_value=1.0, max_value=5.0,
                                                               width="medium"),
                    "Δ Рейтинг": st.column_config.NumberColumn("Δ★", format="%+.2f", width="small", disabled=True),
                    "Отзывы": st.column_config.NumberColumn("Отзывы", width="small", disabled=True),
                    "Δ Отзывы": st.column_config.NumberColumn("Δ отз.", format="%+d", width="small", disabled=True),
                    "1–2★ %": st.column_config.TextColumn("1–2★", width="small", disabled=True),
                    "Тренд": st.column_config.TextColumn("Тренд", width="small", disabled=True),
                    "Запас (до 4.0)": st.column_config.TextColumn("Запас", width="small", disabled=True),
                    "BSR": st.column_config.TextColumn("BSR", width="small", disabled=True),
                    "Комментарий": st.column_config.TextColumn("Комментарий", width="large", disabled=True),
                },
                use_container_width=True, hide_index=True, key="table_editor",
            )

            selected_asins = [extract_asin(u) for u in edited_df.loc[edited_df["Выбор"] == True, "ASIN"] if extract_asin(u)]

            a1, a2, a3, a4 = st.columns([3, 1, 1, 1])
            if selected_asins:
                a1.markdown(f"**Выбрано: {len(selected_asins)}** · `{', '.join(selected_asins)}`")
            else:
                a1.caption("Отметьте строки галочкой для массовых действий")
            if a2.button("↻ Обновить выбранные", use_container_width=True, disabled=not selected_asins):
                run_collection(selected_asins, "Обновление")
            if a3.button("✕ Удалить выбранные", use_container_width=True, disabled=not selected_asins):
                for a in selected_asins:
                    delete_asin_completely(a)
                st.success(f"Удалено: {len(selected_asins)}")
                st.rerun()
            csv = filtered_df.drop(columns=["Выбор", "raw_created_at", "Фото", "bad_pct", "five_pct", "margin"]) \
                .to_csv(index=False).encode("utf-8-sig")
            a4.download_button("⬇ CSV", csv, "rating_radar.csv", "text/csv", use_container_width=True)

        else:
            records = filtered_df.to_dict(orient="records")
            grid = st.columns(3)
            for i, item in enumerate(records):
                with grid[i % 3]:
                    with st.container(border=True):
                        st.markdown(
                            f"<span class='card-asin'><a href='{item['ASIN']}' target='_blank'>{item['raw_asin']}</a></span>"
                            f"&nbsp;&nbsp;{badge(item['Статус'])}", unsafe_allow_html=True)
                        ic, tc = st.columns([1, 2])
                        if item["Фото"]:
                            try:
                                ic.image(item["Фото"])
                            except Exception:
                                ic.caption("Нет фото")
                        else:
                            ic.caption("Нет фото")
                        r_val = f"{item['Рейтинг']:.2f}" if pd.notnull(item["Рейтинг"]) else "—"
                        d_r = f" ({item['Δ Рейтинг']:+.2f})" if pd.notnull(item["Δ Рейтинг"]) and abs(item["Δ Рейтинг"]) > 0.005 else ""
                        cnt = str(int(item["Отзывы"])) if pd.notnull(item["Отзывы"]) else "—"
                        d_c = f" (+{int(item['Δ Отзывы'])})" if pd.notnull(item["Δ Отзывы"]) and item["Δ Отзывы"] > 0 else ""
                        tc.markdown(f"**{r_val}★**{d_r} · {cnt} отз.{d_c}")
                        tc.markdown(f"Источник `{item['Источник']}` · BSR `{item['BSR']}`")
                        tc.markdown(f"Негатив 1–2★: **{item['1–2★ %']}** {item['Тренд']}")
                        tc.markdown(f"Запас до 4.0: **{item['Запас (до 4.0)']}**")
                        st.caption(f"Обновлено: {item['Время сбора']}")

# ---------- АНАЛИТИКА ----------
with tab_an:
    if filtered_df.empty:
        st.info("Нет данных")
    else:
        # --- ряд 1: распределение + динамика портфеля ---
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown("**Распределение по статусу**")
            dist = filtered_df["Статус"].value_counts().reset_index()
            dist.columns = ["Статус", "Количество"]
            fig = px.pie(dist, values="Количество", names="Статус", hole=0.55, color="Статус",
                         color_discrete_map=STATUS_COLOR)
            fig.update_traces(textinfo="value+percent", textfont_size=12)
            style_fig(fig, 300, showlegend=True, legend=dict(orientation="h", y=-0.12))
            st.plotly_chart(fig, use_container_width=True)

        with c2:
            st.markdown("**Динамика портфеля** — средний рейтинг и доля позиций в риске")
            if hist_df.empty:
                st.caption("Нет истории за выбранный период")
            else:
                daily = hist_df.copy()
                daily["day"] = daily["created_local"].dt.floor("D")
                daily = daily.sort_values("created_at").groupby(["day", "asin"]).last().reset_index()
                agg = daily.groupby("day").agg(
                    avg=("rating", "mean"),
                    risk=("rating", lambda s: (s <= 4.2).mean() * 100),
                    n=("asin", "count"),
                ).reset_index()
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=agg["day"], y=agg["avg"], name="Средний рейтинг", mode="lines+markers",
                                         line=dict(color=PALETTE["accent"], width=3)))
                fig.add_trace(go.Bar(x=agg["day"], y=agg["risk"], name="% в риске", yaxis="y2",
                                     marker_color=PALETTE["risk"], opacity=0.35))
                style_fig(fig, 300, legend=dict(orientation="h", y=-0.2),
                          yaxis=dict(title="Рейтинг", range=[3.5, 5.05]),
                          yaxis2=dict(title="% риск", overlaying="y", side="right", range=[0, 100], showgrid=False))
                st.plotly_chart(fig, use_container_width=True)

        # --- ряд 2: scatter + топ негатива ---
        c3, c4 = st.columns([3, 2])
        with c3:
            st.markdown("**Рейтинг × отзывы** — размер точки = доля 1–2★")
            sc = filtered_df.dropna(subset=["Рейтинг", "Отзывы"]).copy()
            if sc.empty:
                st.caption("Нет данных")
            else:
                sc["size"] = sc["bad_pct"].clip(lower=2) + 4
                fig = px.scatter(sc, x="Отзывы", y="Рейтинг", color="Статус", size="size", size_max=26,
                                 hover_name="raw_asin", hover_data={"size": False, "1–2★ %": True, "Источник": True},
                                 color_discrete_map=STATUS_COLOR, log_x=True)
                fig.add_hline(y=4.2, line_dash="dot", line_color=PALETTE["risk"], annotation_text="4.2 риск")
                fig.add_hline(y=4.5, line_dash="dot", line_color=PALETTE["ok"], annotation_text="4.5")
                style_fig(fig, 340, legend=dict(orientation="h", y=-0.2),
                          yaxis=dict(range=[min(3.0, sc["Рейтинг"].min() - 0.1), 5.05]))
                st.plotly_chart(fig, use_container_width=True)

        with c4:
            st.markdown("**Топ-15 по доле негатива (1–2★)**")
            top = filtered_df[filtered_df["bad_pct"] > 0].nlargest(15, "bad_pct").sort_values("bad_pct")
            if top.empty:
                st.caption("Нет гистограмм")
            else:
                fig = px.bar(top, x="bad_pct", y="raw_asin", orientation="h", color="Статус",
                             color_discrete_map=STATUS_COLOR, text="bad_pct",
                             labels={"bad_pct": "% 1–2★", "raw_asin": ""})
                fig.update_traces(texttemplate="%{text}%", textposition="outside")
                style_fig(fig, 340, showlegend=False, xaxis=dict(range=[0, max(top["bad_pct"].max() * 1.2, 10)]))
                st.plotly_chart(fig, use_container_width=True)

        # --- ряд 3: по источникам + запас ---
        c5, c6 = st.columns(2)
        with c5:
            st.markdown("**Рейтинги по источнику**")
            bx = filtered_df.dropna(subset=["Рейтинг"])
            if bx.empty:
                st.caption("Нет данных")
            else:
                fig = px.box(bx, x="Источник", y="Рейтинг", points="all", color="Источник",
                             hover_name="raw_asin", color_discrete_sequence=px.colors.qualitative.Set2)
                style_fig(fig, 320, showlegend=False, yaxis=dict(range=[min(3.0, bx["Рейтинг"].min() - 0.1), 5.05]))
                st.plotly_chart(fig, use_container_width=True)

        with c6:
            st.markdown("**Самый тонкий запас до 4.0** — сколько единичных отзывов выдержит")
            mg = filtered_df.dropna(subset=["margin"]).nsmallest(15, "margin").sort_values("margin", ascending=False)
            if mg.empty:
                st.caption("Нет данных")
            else:
                fig = px.bar(mg, x="margin", y="raw_asin", orientation="h", color="Статус",
                             color_discrete_map=STATUS_COLOR, text="margin", labels={"margin": "ед.", "raw_asin": ""})
                fig.update_traces(textposition="outside")
                style_fig(fig, 320, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

        # --- ряд 4: тепловая карта изменений + прирост отзывов ---
        st.markdown("---")
        c7, c8 = st.columns([3, 2])
        with c7:
            st.markdown("**Тепловая карта изменения рейтинга** (Δ к предыдущему замеру)")
            if hist_df.empty:
                st.caption("Нет истории")
            else:
                hm = hist_df.copy()
                hm["day"] = hm["created_local"].dt.strftime("%d.%m")
                hm = hm.sort_values("created_at").groupby(["asin", "day"]).last().reset_index()
                hm = hm.sort_values("created_at")
                hm["delta"] = hm.groupby("asin")["rating"].diff()
                piv = hm.pivot_table(index="asin", columns="day", values="delta", aggfunc="last")
                order = hm.drop_duplicates("day").sort_values("created_at")["day"].tolist()
                piv = piv.reindex(columns=order)
                piv = piv.loc[piv.abs().sum(axis=1).sort_values(ascending=False).index[:40]]
                if piv.dropna(how="all").empty:
                    st.caption("Изменений пока нет (нужно ≥2 замера на ASIN)")
                else:
                    fig = go.Figure(go.Heatmap(
                        z=piv.values, x=piv.columns, y=piv.index, colorscale="RdYlGn", zmid=0,
                        zmin=-0.3, zmax=0.3, colorbar=dict(title="Δ★", thickness=12),
                        hovertemplate="%{y}<br>%{x}: %{z:+.2f}<extra></extra>"))
                    style_fig(fig, max(300, 18 * len(piv) + 60))
                    fig.update_yaxes(showgrid=False, autorange="reversed")
                    st.plotly_chart(fig, use_container_width=True)

        with c8:
            st.markdown("**Прирост отзывов за период**")
            if hist_df.empty:
                st.caption("Нет истории")
            else:
                g = hist_df.sort_values("created_at").groupby("asin")["review_count"].agg(["first", "last"])
                g["growth"] = (g["last"] - g["first"]).fillna(0)
                g = g[g["growth"] > 0].nlargest(15, "growth").sort_values("growth").reset_index()
                if g.empty:
                    st.caption("Нет прироста")
                else:
                    g = g.merge(filtered_df[["raw_asin", "Статус"]], left_on="asin", right_on="raw_asin", how="left")
                    fig = px.bar(g, x="growth", y="asin", orientation="h", color="Статус",
                                 color_discrete_map=STATUS_COLOR, text="growth", labels={"growth": "новых отз.", "asin": ""})
                    fig.update_traces(textposition="outside")
                    style_fig(fig, 340, showlegend=False)
                    st.plotly_chart(fig, use_container_width=True)

        # --- ряд 5: все линии ---
        st.markdown("**Динамика рейтинга по ASIN**")
        if hist_df.empty:
            st.caption("Нет истории")
        else:
            fig = px.line(hist_df, x="created_local", y="rating", color="asin", markers=True,
                          labels={"created_local": "Дата", "rating": "Рейтинг", "asin": "ASIN"})
            fig.update_traces(line=dict(width=1.5), marker=dict(size=5), opacity=0.85)
            fig.add_hrect(y0=1.0, y1=4.2, fillcolor=PALETTE["risk"], opacity=0.05, line_width=0)
            fig.add_hrect(y0=4.2, y1=4.5, fillcolor=PALETTE["warn"], opacity=0.05, line_width=0)
            style_fig(fig, 380, yaxis=dict(range=[min(3.0, hist_df["rating"].min() - 0.1), 5.05]),
                      legend=dict(orientation="h", y=-0.2), showlegend=len(f_asins) <= 25)
            st.plotly_chart(fig, use_container_width=True)

# ---------- ПРОГНОЗ ----------
with tab_fc:
    if not all_asins:
        st.info("Нет данных")
    else:
        pc1, pc2 = st.columns([2, 1])
        target = pc1.selectbox("ASIN для детального прогноза", options=f_asins or all_asins)
        horizon = pc2.slider("Горизонт, дней", 7, 90, 30, step=1)

        ah = full_df[full_df["asin"] == target].sort_values("created_at").copy()
        if ah.empty:
            st.info("Нет данных по выбранному ASIN")
        else:
            ah["ts"] = ah["created_at"].astype(np.int64) // 10**9
            X = ah[["ts"]].values
            y_r = ah["rating"].ffill().fillna(0.0).values
            y_c = ah["review_count"].ffill().fillna(0).values
            cur_r, cur_c = float(y_r[-1]), int(y_c[-1])
            last_ts = X[-1][0]
            fdays = np.arange(1, horizon + 1)
            fts = np.array([[last_ts + d * 86400] for d in fdays])

            if len(ah) >= 2:
                mr = LinearRegression().fit(X, y_r)
                mc = LinearRegression().fit(X, y_c)
                pr = np.clip(mr.predict(fts), 1.0, 5.0)
                pc = np.maximum(cur_c, mc.predict(fts))
                r_slope_month = mr.coef_[0] * 86400 * 30
                resid = y_r - mr.predict(X)
                sigma = float(np.std(resid)) if len(resid) > 2 else 0.05
                r_diff, c_diff = cur_r - y_r[0], cur_c - int(y_c[0])
                days_to_42 = None
                if mr.coef_[0] < 0 and cur_r > 4.2:
                    days_to_42 = int((cur_r - 4.2) / (-mr.coef_[0] * 86400))
            else:
                pr = np.array([cur_r] * horizon)
                pc = np.array([cur_c] * horizon)
                r_slope_month, sigma, r_diff, c_diff, days_to_42 = 0.0, 0.05, 0.0, 0, None

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Текущий рейтинг", f"{cur_r:.2f}", delta=f"{r_diff:+.2f}" if abs(r_diff) > 0.005 else None)
            m2.metric("Отзывов", f"{cur_c}", delta=f"{c_diff:+d}" if c_diff else None)
            m3.metric(f"Прогноз (+{horizon} дн.)", f"{pr[-1]:.2f}", delta=f"{pr[-1] - cur_r:+.2f}")
            m4.metric(f"Отзывов (+{horizon} дн.)", f"{int(pc[-1])}", delta=f"+{int(pc[-1]) - cur_c}")
            m5.metric("Дней до 4.2", f"{days_to_42}" if days_to_42 is not None else "—",
                      delta=f"тренд {r_slope_month:+.3f}★/мес" if len(ah) >= 2 else None,
                      delta_color="inverse" if r_slope_month < 0 else "normal")

            last_date = ah["created_at"].iloc[-1].tz_convert(ZoneInfo(selected_tz))
            hist_x = ah["created_at"].dt.tz_convert(ZoneInfo(selected_tz))
            fx = [last_date + pd.Timedelta(days=int(d)) for d in fdays]

            g1, g2 = st.columns([3, 2])
            with g1:
                fig = go.Figure()
                # доверительная полоса
                fig.add_trace(go.Scatter(x=fx + fx[::-1],
                                         y=list(np.clip(pr + 1.96 * sigma, 1, 5)) + list(np.clip(pr - 1.96 * sigma, 1, 5))[::-1],
                                         fill="toself", fillcolor="rgba(245,127,23,0.12)", line=dict(width=0),
                                         name="95% интервал", hoverinfo="skip"))
                fig.add_trace(go.Scatter(x=hist_x, y=ah["rating"], mode="lines+markers", name="История",
                                         line=dict(color=PALETTE["accent"], width=3)))
                fig.add_trace(go.Scatter(x=[last_date] + fx, y=[cur_r] + list(pr), mode="lines", name="Прогноз",
                                         line=dict(color="#f57f17", width=2, dash="dash")))
                fig.add_hline(y=4.2, line_dash="dot", line_color=PALETTE["risk"], annotation_text="4.2")
                style_fig(fig, 360, title=dict(text=f"Рейтинг · {target}", font=dict(size=14)),
                          yaxis=dict(range=[max(1.0, min(cur_r, pr.min()) - 0.4), 5.05]),
                          legend=dict(orientation="h", y=-0.2))
                st.plotly_chart(fig, use_container_width=True)

            with g2:
                h = parse_hist(ah["histogram_json"].iloc[-1])
                st.markdown("**Распределение звёзд (последний замер)**")
                if not h:
                    st.caption("Гистограммы нет")
                else:
                    stars = ["5", "4", "3", "2", "1"]
                    vals = [h.get(s, 0) for s in stars]
                    colors = [PALETTE["ok"], "#7cb342", PALETTE["warn"], "#e65100", PALETTE["risk"]]
                    fig = go.Figure(go.Bar(x=vals, y=[f"{s}★" for s in stars], orientation="h",
                                           marker_color=colors, text=[f"{v}%" for v in vals], textposition="outside"))
                    style_fig(fig, 360, showlegend=False, xaxis=dict(range=[0, max(vals + [10]) * 1.25]))
                    fig.update_yaxes(showgrid=False, autorange="reversed")
                    st.plotly_chart(fig, use_container_width=True)

            # отзывы + скорость
            g3, g4 = st.columns(2)
            with g3:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=hist_x, y=ah["review_count"], mode="lines+markers", name="Отзывы",
                                         line=dict(color="#5e5ce6", width=3)))
                fig.add_trace(go.Scatter(x=[last_date] + fx, y=[cur_c] + list(pc), mode="lines", name="Прогноз",
                                         line=dict(color="#5e5ce6", width=2, dash="dash")))
                style_fig(fig, 300, title=dict(text="Число отзывов", font=dict(size=14)), legend=dict(orientation="h", y=-0.25))
                st.plotly_chart(fig, use_container_width=True)
            with g4:
                vel = ah[["created_at", "review_count", "rating"]].copy()
                vel["d_reviews"] = vel["review_count"].diff()
                vel["d_days"] = vel["created_at"].diff().dt.total_seconds() / 86400
                vel["per_day"] = vel["d_reviews"] / vel["d_days"].replace(0, np.nan)
                vel["d_rating"] = vel["rating"].diff()
                vel = vel.dropna(subset=["per_day"])
                if vel.empty:
                    st.caption("Скорость отзывов: нужно ≥2 замера")
                else:
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=vel["created_at"].dt.tz_convert(ZoneInfo(selected_tz)), y=vel["per_day"],
                                         name="отз./день",
                                         marker_color=[PALETTE["risk"] if d < 0 else PALETTE["ok"] for d in vel["d_rating"].fillna(0)]))
                    style_fig(fig, 300, title=dict(text="Скорость отзывов (цвет = знак Δ рейтинга)", font=dict(size=14)),
                              showlegend=False)
                    st.plotly_chart(fig, use_container_width=True)

        # --- сводный прогноз по портфелю ---
        st.markdown("---")
        st.markdown("**Прогноз по всему портфелю (+30 дн.)** — кто пробьёт 4.2 вниз")
        rows = []
        for a, grp in full_df[full_df["asin"].isin(f_asins)].groupby("asin"):
            grp = grp.sort_values("created_at").dropna(subset=["rating"])
            if len(grp) < 2:
                continue
            xs = (grp["created_at"].astype(np.int64) // 10**9).values.reshape(-1, 1)
            m = LinearRegression().fit(xs, grp["rating"].values)
            cur = float(grp["rating"].iloc[-1])
            p30 = float(np.clip(m.predict([[xs[-1][0] + 30 * 86400]])[0], 1, 5))
            rows.append({"ASIN": a, "Сейчас": cur, "Прогноз +30": p30, "Δ": p30 - cur,
                         "Тренд ★/мес": m.coef_[0] * 86400 * 30, "Замеров": len(grp)})
        if not rows:
            st.caption("Нужно ≥2 замера хотя бы на одном ASIN")
        else:
            pf = pd.DataFrame(rows).sort_values("Прогноз +30")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=pf["ASIN"], y=pf["Сейчас"], name="Сейчас", marker_color="#c7c7cc"))
            fig.add_trace(go.Bar(x=pf["ASIN"], y=pf["Прогноз +30"], name="Прогноз +30",
                                 marker_color=[PALETTE["risk"] if v <= 4.2 else (PALETTE["warn"] if v < 4.5 else PALETTE["ok"])
                                               for v in pf["Прогноз +30"]]))
            fig.add_hline(y=4.2, line_dash="dot", line_color=PALETTE["risk"])
            style_fig(fig, 340, barmode="group", yaxis=dict(range=[3.0, 5.05]), legend=dict(orientation="h", y=-0.3))
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(pf.style.format({"Сейчас": "{:.2f}", "Прогноз +30": "{:.2f}", "Δ": "{:+.2f}", "Тренд ★/мес": "{:+.3f}"}),
                         use_container_width=True, hide_index=True)

# ---------- СБОР И УПРАВЛЕНИЕ ----------
with tab_ops:
    o1, o2 = st.columns(2)
    with o1:
        st.markdown("### Ручной прогон")
        st.caption("Сбор по всему списку прямо сейчас")
        if st.button(f"▶ Запустить прогон ({len(tracked)} ASIN)", type="primary"):
            if not tracked:
                st.warning("Список отслеживания пуст")
            else:
                run_collection(tracked, "Прогон")

        st.markdown("### Автосбор")
        t1, t2 = st.columns(2)
        target_daily_time = t1.time_input("Время ежедневного сбора", value=datetime.time(13, 0), key="daily_run_time")
        t2.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        auto_timer_active = t2.checkbox("Автосбор включён", value=False)
        if auto_timer_active and tracked:
            now_tz = datetime.datetime.now(ZoneInfo(selected_tz))
            sched = datetime.datetime.combine(now_tz.date(), target_daily_time, tzinfo=ZoneInfo(selected_tz))
            if now_tz > sched:
                sched += datetime.timedelta(days=1)
            until = (sched - now_tz).total_seconds()
            st.caption(f"Следующий запуск: {sched:%d.%m %H:%M} ({tz_short}) · через {until / 3600:.1f} ч")
            if 0 <= until <= 60:
                st.warning(f"⏱️ Запуск автосбора в {target_daily_time:%H:%M} ({tz_short})...")
                run_collection(tracked, "Автосбор")

    with o2:
        st.markdown("### Точечная проверка")
        st.caption("Разовая проверка конкретных позиций")
        with st.form("ad_hoc_form"):
            adhoc_input = st.text_area("ASIN, комбинации B0XXXXXX:US или ссылки", height=100,
                                       placeholder="B0H6YBDKXJ:US, https://www.amazon.de/dp/B09NWGDK3S...")
            adhoc_submit = st.form_submit_button("Выполнить проверку")
        if adhoc_submit:
            raw_list = [a.strip() for a in re.split(r"[\s,]+", adhoc_input) if a.strip()]
            if raw_list:
                run_collection(raw_list, "Проверка")

    st.markdown("---")
    with st.expander("Управление списком ASIN", expanded=False):
        h1, h2 = st.columns([3, 1])
        h1.markdown(
            "**Форматы:** `B09NWGDK3S` · `B0H6YBDKXJ:US` · `B09NWGDK3S:DE` · `B09NWGDK3S:BE` · "
            "`https://www.amazon.com/dp/B0H6YBDKXJ`")
        market_filter = h2.selectbox("Фильтр по стране", options=["Все страны"] + list(MARKET_DOMAINS.keys()),
                                     index=0, key="market_filter_select")
        display_tracked = tracked if market_filter == "Все страны" else [
            a for a in tracked if asin_market_map.get(a, "BE") == market_filter or a.endswith(f":{market_filter}")]

        st.markdown(f"**Текущие ASIN (`{market_filter}`)** — {len(display_tracked)} из {len(tracked)}")
        edited = st.text_area("Список", value=", ".join(display_tracked), height=140,
                              key=f"edit_tracked_list_{market_filter}", label_visibility="collapsed")
        b1, b2, b3 = st.columns([1, 1, 2])
        if b1.button("💾 Пересохранить список"):
            raw_list = [a.strip() for a in re.split(r"[\s,]+", edited) if a.strip()]
            clean = [extract_asin(a) for a in raw_list if extract_asin(a)]
            ensure_schema()
            try:
                conn = _conn()
                with conn.cursor() as cur:
                    if market_filter == "Все страны":
                        cur.execute("DELETE FROM tracked_asins;")
                    for code in clean:
                        if len(code) == 10:
                            cur.execute("INSERT INTO tracked_asins (asin) VALUES (%s) ON CONFLICT (asin) DO NOTHING;", (code,))
                conn.commit()
                conn.close()
                st.success("Список сохранён")
                st.rerun()
            except Exception as e:
                st.error(f"Ошибка сохранения: {e}")
        if b2.button("🗑️ Очистить весь список"):
            try:
                conn = _conn()
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tracked_asins;")
                conn.commit()
                conn.close()
                st.success("Список очищен")
                st.rerun()
            except Exception as e:
                st.error(f"Ошибка очистки: {e}")

    st.markdown("### История прогонов")
    runs = get_runs_history()
    if runs.empty:
        st.caption("Пусто")
    else:
        runs["started_at"] = pd.to_datetime(runs["started_at"], utc=True).dt.tz_convert(ZoneInfo(selected_tz))
        runs["finished_at"] = pd.to_datetime(runs["finished_at"], utc=True, errors="coerce").dt.tz_convert(ZoneInfo(selected_tz))
        runs["Длит., мин"] = ((runs["finished_at"] - runs["started_at"]).dt.total_seconds() / 60).round(1)
        runs["Успех, %"] = (runs["ok_count"].fillna(0) / runs["asin_count"].replace(0, np.nan) * 100).round(0)
        r1, r2 = st.columns([2, 3])
        with r1:
            fig = go.Figure()
            fig.add_trace(go.Bar(x=runs["started_at"], y=runs["asin_count"], name="ASIN", marker_color="#c7c7cc"))
            fig.add_trace(go.Bar(x=runs["started_at"], y=runs["ok_count"], name="Валидных", marker_color=PALETTE["ok"]))
            style_fig(fig, 260, barmode="overlay", legend=dict(orientation="h", y=-0.3))
            st.plotly_chart(fig, use_container_width=True)
        with r2:
            show = runs[["started_at", "status", "asin_count", "ok_count", "Успех, %", "Длит., мин"]].copy()
            show["started_at"] = show["started_at"].dt.strftime("%d.%m.%Y %H:%M")
            show.columns = [f"Старт ({tz_short})", "Статус", "ASIN", "Валидных", "Успех, %", "Длит., мин"]
            st.dataframe(show, use_container_width=True, hide_index=True, height=260)
