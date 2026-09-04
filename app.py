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
                "Риск": PALETTE["risk"], "Нет данных": PALETTE["none"],
                "🟢 ОК": PALETTE["ok"], "🟡 Внимание": PALETTE["warn"],
                "🔴 Риск": PALETTE["risk"], "⚪ Нет данных": PALETTE["none"]}

st.markdown(
    """
<style>
    /* --- прячем служебный хром Streamlit --- */
    header[data-testid="stHeader"] { display: none !important; }
    [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
    .stAppDeployButton, #MainMenu, footer { display: none !important; visibility: hidden !important; }
    .stApp > header { height: 0 !important; }
    .block-container { padding-top: 1rem !important; }

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


def status_key(text):
    return str(text).split(" ", 1)[-1] if str(text)[:1] in "🟢🟡🔴⚪" else str(text)


def badge(text):
    return f"<span class='badge' style='background:{STATUS_COLOR.get(status_key(text), PALETTE['none'])}'>{status_key(text)}</span>"


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


DICT_COLS = ["asin", "parent_asin", "category", "subcategory", "product_type", "brand", "market"]
# порядок алиасов = приоритет (первый найденный побеждает). Заточено под SPR Алёны:
# ASIN | категория | категории (Mens LS 165) | плотность | тип | Parent group | Category+parent | Архив …
DICT_ALIASES = {
    "asin": ["asin", "child", "child asin", "child_asin", "sku asin", "asin child"],
    "category": ["category+parent", "category + parent", "категория", "category", "cat"],
    "subcategory": ["parent group", "parent_group", "subcategory", "sub category", "sub_category", "подкатегория", "subcat",
                    "категории"],
    "product_type": ["тип", "product type", "product_type", "type", "вид", "вид товара", "kind", "плотность"],
    "brand": ["brand", "бренд"],
    "market": ["market", "country", "страна", "marketplace", "рынок"],
    "archive": ["архив", "archive", "archived"],
    # parent_asin последним: иначе «parent» перехватит «Parent group»
    "parent_asin": ["parent asin", "parent_asin", "парент", "родитель", "parent"],
}
DICT_COLS_ALL = DICT_COLS + ["archive"]


def ensure_dict_table():
    if not DATABASE_URL:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS asin_dictionary (
                    asin TEXT PRIMARY KEY,
                    parent_asin TEXT,
                    category TEXT,
                    subcategory TEXT,
                    product_type TEXT,
                    brand TEXT,
                    market TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """)
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_dictionary():
    if not DATABASE_URL:
        return pd.DataFrame(columns=DICT_COLS)
    try:
        conn = _conn()
        df = pd.read_sql("SELECT " + ", ".join(DICT_COLS) + " FROM asin_dictionary", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame(columns=DICT_COLS)


def normalize_dict_df(raw: pd.DataFrame) -> pd.DataFrame:
    """Приводит любой справочник (CSV/XLSX/Google Sheet) к DICT_COLS по алиасам колонок."""
    cols = {c: str(c).strip().lower() for c in raw.columns}
    mapping = {}
    for target, aliases in DICT_ALIASES.items():
        for a in aliases:                       # приоритет по порядку алиасов
            for c, lc in cols.items():
                if c in mapping.values():
                    continue
                if lc == a or (len(a) > 3 and lc.startswith(a)):
                    mapping[target] = c
                    break
            if target in mapping:
                break
    out = pd.DataFrame()
    for t in DICT_COLS_ALL:
        out[t] = raw[mapping[t]].astype(str).str.strip() if t in mapping else ""
    # архивные позиции выкидываем (в SPR колонка «Архив» с пометкой)
    if "archive" in mapping:
        arch = out["archive"].str.lower().isin(["1", "true", "да", "yes", "архив", "x", "+"])
        out = out[~arch]
    out = out.drop(columns=["archive"])
    out["asin"] = out["asin"].apply(lambda v: extract_asin(v) or "")
    out = out[out["asin"].str.len() == 10].copy()
    out["parent_asin"] = out["parent_asin"].apply(lambda v: extract_asin(v) or "")
    out["market"] = out["market"].str.upper().str.strip()
    out.loc[~out["market"].isin(MARKET_DOMAINS), "market"] = ""
    out = out.replace({"nan": "", "None": ""}).drop_duplicates("asin", keep="last")
    return out.reset_index(drop=True)


def save_dictionary(df: pd.DataFrame, replace: bool):
    ensure_dict_table()
    conn = _conn()
    with conn.cursor() as cur:
        if replace:
            cur.execute("DELETE FROM asin_dictionary;")
        for r in df.itertuples(index=False):
            cur.execute(
                """
                INSERT INTO asin_dictionary (asin, parent_asin, category, subcategory, product_type, brand, market, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (asin) DO UPDATE SET
                    parent_asin = EXCLUDED.parent_asin, category = EXCLUDED.category,
                    subcategory = EXCLUDED.subcategory, product_type = EXCLUDED.product_type,
                    brand = EXCLUDED.brand,
                    market = CASE WHEN EXCLUDED.market <> '' THEN EXCLUDED.market ELSE asin_dictionary.market END,
                    updated_at = NOW();
                """,
                (r.asin, r.parent_asin, r.category, r.subcategory, r.product_type, r.brand, r.market))
    conn.commit()
    conn.close()


def parse_asin_batch(text, existing, default_market=None):
    """Разбирает пачку ASIN/ссылок. Возвращает (new_codes, dup_codes, invalid_tokens, markets, dup_in_batch)."""
    raw_list = [a.strip() for a in re.split(r"[\s,;]+", text or "") if a.strip()]
    seen, new, dups, invalid, markets, batch_dups = set(), [], [], [], {}, []
    existing = set(existing)
    for tok in raw_list:
        code = extract_asin(tok)
        if not code or len(code) != 10:
            invalid.append(tok)
            continue
        if code in seen:
            batch_dups.append(code)
            continue
        seen.add(code)
        mk = None
        tail = tok.split(":")[-1].upper()
        if ":" in tok and tail in MARKET_DOMAINS:
            mk = tail
        else:
            for k, dom in MARKET_DOMAINS.items():
                if dom in tok.lower():
                    mk = k
                    break
        if not mk and default_market in MARKET_DOMAINS:
            mk = default_market
        if mk:
            markets[code] = mk
        (dups if code in existing else new).append(code)
    return new, dups, invalid, markets, batch_dups


def save_markets(markets):
    if not markets:
        return
    ensure_dict_table()
    conn = _conn()
    with conn.cursor() as cur:
        for code, mk in markets.items():
            cur.execute("INSERT INTO asin_dictionary (asin, market) VALUES (%s, %s) "
                        "ON CONFLICT (asin) DO UPDATE SET market = EXCLUDED.market, updated_at = NOW();", (code, mk))
    conn.commit()
    conn.close()


def gsheet_to_csv_url(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


GROUP_LABELS = {"category": "Категория", "subcategory": "Подкатегория", "product_type": "Вид",
                "parent_asin": "Parent", "brand": "Бренд", "market": "Страна"}


def parse_hist(raw):
    if not raw:
        return None
    try:
        h = json.loads(raw) if isinstance(raw, str) else raw
        return {str(k): int(v) for k, v in h.items()}
    except Exception:
        return None


def run_collection(items, label="Прогон"):
    items = with_market(items)
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
ensure_dict_table()
dict_df = get_dictionary()
dict_map = dict_df.set_index("asin").to_dict("index") if not dict_df.empty else {}
# страна из справочника имеет приоритет над каскадом
for a, row in dict_map.items():
    if row.get("market") in MARKET_DOMAINS and a in asin_market_map:
        asin_market_map[a] = row["market"]


def with_market(asin_list):
    """Коллектор понимает страну только из ссылки (extract_asin_and_market смотрит на домен),
    поэтому ASIN со страной из справочника или суффиксом :XX превращаем в полный URL."""
    out = []
    for a in asin_list:
        a = str(a).strip()
        if a.lower().startswith("http"):
            out.append(a)
            continue
        code = extract_asin(a)
        mk = None
        tail = a.split(":")[-1].upper()
        if ":" in a and tail in MARKET_DOMAINS:
            mk = tail
        elif code in dict_map and dict_map[code].get("market") in MARKET_DOMAINS:
            mk = dict_map[code]["market"]
        out.append(f"https://www.{MARKET_DOMAINS[mk]}/dp/{code}" if mk else code)
    return out

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

        # Логика Amazon: ≥4.5 зелёный · 4.3–4.4 жёлтый · ≤4.2 красный
        if source == "none" or rating is None:
            status = "Нет данных"
        elif rating <= 4.24:
            status = "Риск"
        elif rating < 4.45:
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

        d = dict_map.get(str(r["asin"]), {})
        rows.append({
            "Выбор": False,
            "raw_asin": str(r["asin"]),
            "Parent": d.get("parent_asin") or "",
            "Категория": d.get("category") or "—",
            "Подкатегория": d.get("subcategory") or "—",
            "Вид": d.get("product_type") or "—",
            "Бренд": d.get("brand") or "—",
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


def rating_emoji(r):
    if r is None or pd.isna(r):
        return "⚪"
    if r >= 4.45:
        return "🟢"
    if r >= 4.25:
        return "🟡"
    return "🔴"


calc_df = build_calc_df(full_df)
if not calc_df.empty:
    for c in ["Рейтинг", "Δ Рейтинг", "Отзывы", "Δ Отзывы", "margin"]:
        calc_df[c] = pd.to_numeric(calc_df[c], errors="coerce")
    calc_df["Рейтинг ★"] = [f"{rating_emoji(r)} {r:.2f}" if pd.notnull(r) else "⚪ —" for r in calc_df["Рейтинг"]]
    calc_df["Статус"] = [f"{rating_emoji(r)} {s_}" for r, s_ in zip(calc_df["Рейтинг"], calc_df["Статус"])]

# ==================== KPI ====================
if not calc_df.empty:
    n_total = len(calc_df)
    n_risk = int(calc_df["Статус"].str.endswith("Риск").sum())
    n_warn = int(calc_df["Статус"].str.endswith("Внимание").sum())
    n_ok = int(calc_df["Статус"].str.endswith("ОК").sum())
    n_none = int(calc_df["Статус"].str.endswith("Нет данных").sum())
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
    kpi(k4, "Внимание", n_warn, "4.3–4.4★", PALETTE["warn"])
    kpi(k5, "Риск", n_risk, "≤ 4.2★", PALETTE["risk"])
    kpi(k6, "Отзывов всего", f"{tot_reviews:,}".replace(",", " "),
        f"+{new_reviews} с прошлого замера" if new_reviews else f"нет данных: {n_none}")
    st.markdown(
        "<div class='muted' style='margin:8px 0 4px'>Цвета по логике Amazon: "
        f"{badge('ОК')} 4.5–5.0★ &nbsp; {badge('Внимание')} 4.3–4.4★ &nbsp; "
        f"{badge('Риск')} ≤ 4.2★ &nbsp; {badge('Нет данных')} данные не собраны</div>",
        unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

# ==================== ГЛОБАЛЬНЫЕ ФИЛЬТРЫ ====================
all_asins = calc_df["raw_asin"].tolist() if not calc_df.empty else []
all_sources = sorted(calc_df["Источник"].dropna().unique().tolist()) if not calc_df.empty else []
all_cats = sorted(calc_df["Категория"].unique().tolist()) if not calc_df.empty else []
all_parents = sorted(p for p in calc_df["Parent"].unique().tolist() if p) if not calc_df.empty else []

fc1, fc2, fc3, fc4, fc5, fc6 = st.columns([1.8, 1.5, 1.5, 1.3, 1.4, 1.1])
with fc1:
    sel_asins = st.multiselect("Фильтр ASIN", options=all_asins, default=[], placeholder="Все ASIN")
with fc2:
    sel_cats = st.multiselect("Категория", options=all_cats, default=[], placeholder="Все")
with fc3:
    sel_parents = st.multiselect("Parent", options=all_parents, default=[], placeholder="Все")
with fc4:
    sel_sources = st.multiselect("Страна", options=all_sources, default=[], placeholder="Все")
with fc5:
    sel_status = st.multiselect("Статус", options=["🟢 ОК", "🟡 Внимание", "🔴 Риск", "⚪ Нет данных"], default=[],
                                placeholder="Все")
with fc6:
    st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
    only_us = st.toggle("🇺🇸 Только США", value=False)

fc7, fc8, fc9 = st.columns([1.6, 1.2, 3])
with fc7:
    selected_tz_label = st.selectbox("Часовой пояс", options=list(TIMEZONES.keys()), index=0, key="sel_tz_val")
    selected_tz = TIMEZONES[selected_tz_label]
    tz_short = selected_tz_label.split(" ")[0]
with fc8:
    period_days = st.selectbox("Период истории", options=[7, 14, 30, 60, 90, 365], index=2,
                               format_func=lambda d: f"{d} дн.")
with fc9:
    group_field = st.selectbox("Группировать по", options=["Нет"] + list(GROUP_LABELS.values()), index=0,
                               help="Группы берутся из справочника (вкладка «Сбор и управление» → Справочник)")
    group_col = {v: k for k, v in GROUP_LABELS.items()}.get(group_field)
    GROUP_DF_COL = {"category": "Категория", "subcategory": "Подкатегория", "product_type": "Вид",
                    "parent_asin": "Parent", "brand": "Бренд", "market": "Источник"}.get(group_col)

if not calc_df.empty:
    src_ok = ["US"] if only_us else (sel_sources if sel_sources else all_sources)
    filtered_df = calc_df[
        calc_df["raw_asin"].isin(sel_asins if sel_asins else all_asins)
        & calc_df["Категория"].isin(sel_cats if sel_cats else all_cats)
        & (calc_df["Parent"].isin(sel_parents) if sel_parents else True)
        & calc_df["Источник"].isin(src_ok)
        & (calc_df["Статус"].isin(sel_status) if sel_status else True)
    ].copy()
    if GROUP_DF_COL:
        filtered_df["_group"] = filtered_df[GROUP_DF_COL].replace("", "—")
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
tab_port, tab_dyn, tab_an, tab_bot, tab_fc, tab_ops = st.tabs(
    ["📋 Портфель", "📅 Динамика по дням", "📊 Аналитика", "🤖 Бот возвратов", "📈 Прогноз", "⚙️ Сбор и управление"])

# ---------- ПОРТФЕЛЬ ----------
with tab_port:
    if filtered_df.empty:
        st.warning("В базе нет сохранённых метрик под текущие фильтры")
    else:
        hc, vc = st.columns([3, 1])
        hc.markdown(f"### Сводный отчёт <span class='muted'>· {len(filtered_df)} из {len(calc_df)} позиций</span>",
                    unsafe_allow_html=True)
        view_mode = vc.radio("Вид", options=["Таблица", "Карточки"], horizontal=True, label_visibility="collapsed")

        if GROUP_DF_COL:
            st.markdown(f"**Сводка по группам: {group_field}**")
            gsum = filtered_df.groupby("_group").agg(
                Позиций=("raw_asin", "count"), Рейтинг=("Рейтинг", "mean"),
                Отзывов=("Отзывы", "sum"), Риск=("Статус", lambda x: int(x.str.endswith("Риск").sum())),
                Внимание=("Статус", lambda x: int(x.str.endswith("Внимание").sum())),
                ОК=("Статус", lambda x: int(x.str.endswith("ОК").sum())),
                Негатив=("bad_pct", "mean")).reset_index().rename(columns={"_group": group_field})
            gsum = gsum.sort_values("Рейтинг")
            st.dataframe(gsum.style.format({"Рейтинг": "{:.2f}", "Отзывов": "{:,.0f}", "Негатив": "{:.0f}%"}),
                         use_container_width=True, hide_index=True, height=min(400, 40 + 35 * len(gsum)))
            st.markdown("<br>", unsafe_allow_html=True)

        if view_mode == "Таблица":
            cols_order = ["Выбор", "Статус", "Время сбора", "ASIN", "Фото", "Источник", "Категория", "Parent",
                          "Рейтинг ★", "Δ Рейтинг", "Отзывы", "Δ Отзывы", "1–2★ %", "Тренд", "Запас (до 4.0)", "BSR",
                          "Комментарий"]
            if GROUP_DF_COL:
                cols_order = [GROUP_DF_COL] + [c for c in cols_order if c != GROUP_DF_COL]
                filtered_df = filtered_df.sort_values(["_group", "Рейтинг"], na_position="last")
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
                    "Категория": st.column_config.TextColumn("Категория", width="medium", disabled=True),
                    "Parent": st.column_config.TextColumn("Parent", width="small", disabled=True),
                    "Рейтинг ★": st.column_config.TextColumn("Рейтинг", width="small", disabled=True),
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
            csv = filtered_df.drop(columns=["Выбор", "raw_created_at", "Фото", "bad_pct", "five_pct", "margin", "Рейтинг ★"],
                                   errors="ignore") \
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
                        if item.get("Категория", "—") != "—":
                            tc.markdown(f"<span class='muted'>{item['Категория']}"
                                        f"{' · ' + item['Parent'] if item['Parent'] else ''}</span>", unsafe_allow_html=True)
                        tc.markdown(f"Негатив 1–2★: **{item['1–2★ %']}** {item['Тренд']}")
                        tc.markdown(f"Запас до 4.0: **{item['Запас (до 4.0)']}**")
                        st.caption(f"Обновлено: {item['Время сбора']}")


# ---------- ДИНАМИКА ПО ДНЯМ (широкая таблица) ----------
with tab_dyn:
    st.markdown("### Динамика по дням")
    st.markdown("<div class='muted'>Каждый ASIN — блок строк (Rating / BSR / Reviews / 1–2★), колонки — даты замеров. "
                "Цвет рейтинга по логике Amazon. Период задаётся фильтром сверху.</div>", unsafe_allow_html=True)

    if hist_df.empty:
        st.info("Нет истории под текущие фильтры")
    else:
        d1, d2, d3 = st.columns([2, 1.5, 1.5])
        params_sel = d1.multiselect("Параметры", ["Rating", "BSR", "Reviews", "1–2★ %"],
                                    default=["Rating", "BSR", "Reviews"])
        sort_by = d2.selectbox("Сортировка ASIN", ["По последнему рейтингу ↑", "По последнему рейтингу ↓",
                                                   "По падению за период", "По алфавиту"])
        gran_d = d3.radio("Шаг", ["День", "Неделя"], horizontal=True)

        h = hist_df.copy()
        h["day"] = (h["created_local"].dt.to_period("W").dt.start_time if gran_d == "Неделя"
                    else h["created_local"].dt.floor("D"))
        h["bad"] = h["histogram_json"].apply(lambda x: (lambda d: d.get("1", 0) + d.get("2", 0) if d else np.nan)(parse_hist(x)))
        h["bsr_num"] = pd.to_numeric(h["bsr"].astype(str).str.replace(r"[^\d]", "", regex=True), errors="coerce")
        snap_d = h.sort_values("created_at").groupby(["asin", "day"]).last().reset_index()
        days = sorted(snap_d["day"].unique())
        day_labels = [d.strftime("%d.%m.%Y") for d in days]

        piv_r = snap_d.pivot(index="asin", columns="day", values="rating").reindex(columns=days)
        piv_b = snap_d.pivot(index="asin", columns="day", values="bsr_num").reindex(columns=days)
        piv_c = snap_d.pivot(index="asin", columns="day", values="review_count").reindex(columns=days)
        piv_n = snap_d.pivot(index="asin", columns="day", values="bad").reindex(columns=days)

        # порядок ASIN
        last_r = piv_r.ffill(axis=1).iloc[:, -1]
        first_r = piv_r.bfill(axis=1).iloc[:, 0]
        if sort_by == "По последнему рейтингу ↑":
            order = last_r.sort_values().index
        elif sort_by == "По последнему рейтингу ↓":
            order = last_r.sort_values(ascending=False).index
        elif sort_by == "По падению за период":
            order = (last_r - first_r).sort_values().index
        else:
            order = sorted(piv_r.index)

        src_map = filtered_df.set_index("raw_asin")["Источник"].to_dict()
        grp_map = filtered_df.set_index("raw_asin")["_group"].to_dict() if GROUP_DF_COL else {}
        blocks = []
        param_map = {"Rating": piv_r, "BSR": piv_b, "Reviews": piv_c, "1–2★ %": piv_n}
        if GROUP_DF_COL:
            order = sorted(order, key=lambda a: (str(grp_map.get(a, "—")), list(order).index(a)))
        cur_group = object()
        for a in order:
            g_val = grp_map.get(a, "—") if GROUP_DF_COL else None
            if GROUP_DF_COL and g_val != cur_group:
                cur_group = g_val
                # строка-заголовок группы: средний рейтинг по дням
                members = [x for x in order if grp_map.get(x, "—") == g_val]
                blocks.append([f"▶ {g_val}", "", "Группа (ср. ★)"] + piv_r.loc[members].mean().round(2).tolist())
            for pname in params_sel:
                row = param_map[pname].loc[a].tolist()
                blocks.append([a, src_map.get(a, ""), pname] + row)
        wide = pd.DataFrame(blocks, columns=["ASIN", "Ист.", "Parameter"] + day_labels)

        # --- стилизация ---
        def rating_color(v):
            if pd.isna(v):
                return ""
            if v >= 4.45:
                return "background-color:#7ee36b;color:#1d1d1f;font-weight:600"
            if v >= 4.25:
                return "background-color:#ffee58;color:#1d1d1f;font-weight:600"
            return "background-color:#e53935;color:#fff;font-weight:600"

        def style_row(row):
            p = row["Parameter"]
            out = [""] * len(row)
            vals = row[day_labels]
            if p == "Группа (ср. ★)":
                out = ["background-color:#e8e8ed;font-weight:700"] * len(row)
                out[3:] = [rating_color(v) for v in vals]
                return out
            if p == "Rating":
                out[3:] = [rating_color(v) for v in vals]
            elif p == "1–2★ %":
                out[3:] = ["background-color:#ffcdd2" if pd.notnull(v) and v > 15 else
                           ("background-color:#fff9c4" if pd.notnull(v) and v > 8 else "") for v in vals]
            elif p == "Reviews":
                prev = None
                styles = []
                for v in vals:
                    if pd.notnull(v) and prev is not None and pd.notnull(prev) and v > prev:
                        styles.append("color:#1f8a4c;font-weight:600")
                    else:
                        styles.append("")
                    if pd.notnull(v):
                        prev = v
                out[3:] = styles
            elif p == "BSR":
                prev = None
                styles = []
                for v in vals:
                    if pd.notnull(v) and prev is not None and pd.notnull(prev):
                        styles.append("color:#d13438" if v > prev * 1.15 else ("color:#1f8a4c" if v < prev * 0.85 else ""))
                    else:
                        styles.append("")
                    if pd.notnull(v):
                        prev = v
                out[3:] = styles
            return out

        def fmt(v, p):
            if pd.isna(v):
                return ""
            if p in ("Rating", "Группа (ср. ★)"):
                return f"{v:.2f}" if p.startswith("Группа") else f"{v:.1f}"
            if p == "1–2★ %":
                return f"{int(v)}%"
            return f"{int(v)}"

        disp = wide.copy()
        for col in day_labels:
            disp[col] = [fmt(v, p) for v, p in zip(wide[col], wide["Parameter"])]
        # прячем повтор ASIN внутри блока; первая строка блока — кликабельная ссылка на листинг
        first_row = disp["Parameter"] == (params_sel[0] if params_sel else "")
        is_group = disp["Parameter"] == "Группа (ср. ★)"
        disp["ASIN"] = [
            (a if g else (f"https://www.{MARKET_DOMAINS.get(src, 'amazon.com.be')}/dp/{a}" if f else ""))
            for a, src, f, g in zip(wide["ASIN"], wide["Ист."], first_row, is_group)
        ]
        disp["Ист."] = disp["Ист."].where(first_row, "")

        # ---- рендер HTML: ссылки на ASIN, цветные ячейки, липкая шапка ----
        def cell_style(p, v, prev):
            if p == "Группа (ср. ★)":
                return rating_color(v) if pd.notnull(v) else "background:#e8e8ed"
            if p == "Rating":
                return rating_color(v)
            if p == "1–2★ %" and pd.notnull(v):
                return "background:#ffcdd2" if v > 15 else ("background:#fff9c4" if v > 8 else "")
            if p == "Reviews" and pd.notnull(v) and prev is not None and pd.notnull(prev) and v > prev:
                return "color:#1f8a4c;font-weight:600"
            if p == "BSR" and pd.notnull(v) and prev is not None and pd.notnull(prev):
                return "color:#d13438" if v > prev * 1.15 else ("color:#1f8a4c" if v < prev * 0.85 else "")
            return ""

        th = "".join(f"<th>{c}</th>" for c in ["ASIN", "Ист.", "Параметр"] + day_labels)
        trs = []
        for _, r in wide.iterrows():
            p = r["Parameter"]
            is_grp = p == "Группа (ср. ★)"
            first = (not is_grp) and p == (params_sel[0] if params_sel else "")
            if is_grp:
                a_cell = f"<b>{r['ASIN']}</b>"
            elif first:
                a_cell = (f"<a href='https://www.{MARKET_DOMAINS.get(r['Ист.'], 'amazon.com.be')}/dp/{r['ASIN']}' "
                          f"target='_blank' style='font-family:ui-monospace,Menlo,monospace;font-weight:600;"
                          f"color:{PALETTE['accent']};text-decoration:none'>{r['ASIN']}</a>")
            else:
                a_cell = ""
            tds = [f"<td class='c-asin'>{a_cell}</td>", f"<td>{r['Ист.'] if first else ''}</td>",
                   f"<td>{'<b>' + p + '</b>' if is_grp else p}</td>"]
            prev = None
            for col in day_labels:
                v = r[col]
                tds.append(f"<td style='{cell_style(p, v, prev)}'>{fmt(v, p)}</td>")
                if pd.notnull(v):
                    prev = v
            row_cls = "grp" if is_grp else ("blk" if first else "")
            trs.append(f"<tr class='{row_cls}'>{''.join(tds)}</tr>")

        html = f"""
<style>
.dyn-wrap {{ max-height: 760px; overflow: auto; border:1px solid #e5e5ea; border-radius:12px; background:#fff; }}
.dyn {{ border-collapse: separate; border-spacing:0; font-size:13px; min-width:100%; }}
.dyn th {{ position: sticky; top:0; background:#f5f5f7; color:#6e6e73; font-weight:600; text-align:right;
           padding:8px 10px; border-bottom:1px solid #e5e5ea; white-space:nowrap; z-index:2; }}
.dyn th:nth-child(-n+3) {{ text-align:left; }}
.dyn td {{ padding:6px 10px; border-bottom:1px solid #f0f0f2; text-align:right; white-space:nowrap; }}
.dyn td:nth-child(-n+3) {{ text-align:left; }}
.dyn td.c-asin {{ position: sticky; left:0; background:#fff; z-index:1; min-width:120px; }}
.dyn th:first-child {{ position: sticky; left:0; z-index:3; }}
.dyn tr.blk td {{ border-top:1px solid #d9d9de; }}
.dyn tr.grp td {{ background:#e8e8ed; border-top:2px solid #c7c7cc; }}
</style>
<div class='dyn-wrap'><table class='dyn'><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table></div>
"""
        st.caption(f"{len(order)} ASIN × {len(days)} {'недель' if gran_d == 'Неделя' else 'дней'} · {len(wide)} строк")
        st.markdown(html, unsafe_allow_html=True)

        # Styler — только для выгрузки в Excel
        def style_row_num(row):
            return style_row(wide.loc[row.name])
        disp = wide.copy()
        for col in day_labels:
            disp[col] = [fmt(v, p) for v, p in zip(wide[col], wide["Parameter"])]
        styled = disp.style.apply(style_row_num, axis=1)

        e1, e2 = st.columns([1, 5])
        e1.download_button("⬇ CSV", wide.to_csv(index=False).encode("utf-8-sig"), "rating_dynamics.csv", "text/csv",
                           use_container_width=True)
        try:
            import io
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                styled.to_excel(xw, sheet_name="Dynamics", index=False)
            e2.download_button("⬇ Excel с цветами", buf.getvalue(), "rating_dynamics.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception:
            pass

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

        if GROUP_DF_COL and filtered_df["_group"].nunique() > 1:
            st.markdown(f"**Разрез по: {group_field}** — средний рейтинг и доля позиций в риске")
            gb = filtered_df.groupby("_group").agg(
                avg=("Рейтинг", "mean"), n=("raw_asin", "count"),
                risk=("Статус", lambda x: x.str.endswith("Риск").mean() * 100),
                neg=("bad_pct", "mean")).reset_index().sort_values("avg")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=gb["_group"], y=gb["avg"], name="Средний ★", text=gb["avg"].round(2),
                                 textposition="outside",
                                 marker_color=[PALETTE["risk"] if v <= 4.24 else (PALETTE["warn"] if v < 4.45 else PALETTE["ok"])
                                               for v in gb["avg"].fillna(0)]))
            fig.add_trace(go.Scatter(x=gb["_group"], y=gb["risk"], name="% в риске", yaxis="y2", mode="lines+markers",
                                     line=dict(color=PALETTE["ink"], width=2, dash="dot")))
            fig.add_hline(y=4.2, line_dash="dot", line_color=PALETTE["risk"])
            style_fig(fig, 340, legend=dict(orientation="h", y=-0.3), yaxis=dict(range=[3.0, 5.15]),
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


# ---------- ДЕТЕКТОР БОТА ВОЗВРАТОВ ----------
with tab_bot:
    st.markdown("### Детектор ИИ-бота возвратов Amazon")
    st.markdown(
        "<div class='muted'>Маркер: рейтинг падает, а число оценок почти не растёт — значит новые оценки "
        "пришли пустыми 1–2★ (rating-only, без текста). Считаем по каждому замеру: сколько оценок добавилось "
        "и какой у них средний балл (входящий рейтинг), плюс оценку прироста негатива через гистограмму 1–2★.</div>",
        unsafe_allow_html=True)

    bc1, bc2, bc3, bc4 = st.columns([1.4, 1, 1, 1.2])
    rollout = bc1.date_input("Дата внедрения бота", value=datetime.date(2026, 8, 20))
    gran = bc2.radio("Гранулярность", ["День", "Неделя"], horizontal=True)
    drop_thr = bc3.number_input("Порог падения ★", value=0.1, step=0.05, min_value=0.05, format="%.2f")
    growth_thr = bc4.number_input("Макс. прирост оценок, %", value=0.5, step=0.1, min_value=0.0,
                                  help="Аномалия = рейтинг упал ≥ порога, а оценок прибавилось меньше этого % от базы")

    src_df = full_df[full_df["asin"].isin(f_asins)].copy() if not full_df.empty else pd.DataFrame()
    if src_df.empty:
        st.info("Нет данных под текущие фильтры")
    else:
        src_df["created_local"] = src_df["created_at"].dt.tz_convert(ZoneInfo(selected_tz))
        src_df["bucket"] = (src_df["created_local"].dt.to_period("W").dt.start_time
                            if gran == "Неделя" else src_df["created_local"].dt.floor("D"))
        src_df["bad_pct"] = src_df["histogram_json"].apply(lambda h: (lambda d: d.get("1", 0) + d.get("2", 0) if d else np.nan)(parse_hist(h)))
        # последний замер в бакете на ASIN
        snap = src_df.sort_values("created_at").groupby(["asin", "bucket"]).last().reset_index()
        snap = snap.sort_values(["asin", "bucket"]).dropna(subset=["rating", "review_count"])
        g = snap.groupby("asin")
        snap["prev_rating"] = g["rating"].shift()
        snap["prev_count"] = g["review_count"].shift()
        snap["prev_bad"] = g["bad_pct"].shift()
        snap = snap.dropna(subset=["prev_rating"])
        snap["new_ratings"] = (snap["review_count"] - snap["prev_count"]).clip(lower=0)
        snap["d_rating"] = snap["rating"] - snap["prev_rating"]
        snap["growth_pct"] = snap["new_ratings"] / snap["prev_count"].replace(0, np.nan) * 100
        # входящий рейтинг новых оценок (рейтинг на витрине округлён до 0.1 → даём диапазон)
        def incoming(r, c, pr, pc, n, shift):
            if n <= 0:
                return np.nan
            return ((r + shift) * c - (pr - shift) * pc) / n
        snap["in_rating"] = [np.clip(incoming(r, c, pr, pc, n, 0.0), 1, 5)
                             for r, c, pr, pc, n in zip(snap["rating"], snap["review_count"], snap["prev_rating"],
                                                        snap["prev_count"], snap["new_ratings"])]
        snap["in_rating_lo"] = [np.clip(incoming(r, c, pr, pc, n, -0.05), 1, 5)
                                for r, c, pr, pc, n in zip(snap["rating"], snap["review_count"], snap["prev_rating"],
                                                           snap["prev_count"], snap["new_ratings"])]
        # оценка негатива через гистограмму
        snap["neg_est"] = (snap["bad_pct"] / 100 * snap["review_count"]).round()
        snap["prev_neg_est"] = (snap["prev_bad"] / 100 * snap["prev_count"]).round()
        snap["new_neg"] = (snap["neg_est"] - snap["prev_neg_est"]).clip(lower=0)
        snap["anomaly"] = (snap["d_rating"] <= -drop_thr) & (snap["growth_pct"].fillna(0) <= growth_thr)
        snap["after"] = snap["bucket"].dt.date >= rollout

        # --- KPI до / после ---
        def period_stats(d):
            n = d["new_ratings"].sum()
            neg = d["new_neg"].sum()
            drops = int((d["d_rating"] < 0).sum())
            anom = int(d["anomaly"].sum())
            w_in = (d["in_rating"] * d["new_ratings"]).sum() / n if n > 0 else np.nan
            return n, neg, drops, anom, w_in

        before, after = snap[~snap["after"]], snap[snap["after"]]
        nb, negb, dropb, anb, inb = period_stats(before)
        na, nega, dropa, ana, ina = period_stats(after)
        share_b = negb / nb * 100 if nb else np.nan
        share_a = nega / na * 100 if na else np.nan

        st.markdown(f"**До {rollout:%d.%m} vs после** (по текущему фильтру, {len(snap['asin'].unique())} ASIN)")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Новых оценок (после)", f"{int(na)}", delta=f"до: {int(nb)}", delta_color="off")
        m2.metric("Из них негатив (оценка)", f"{int(nega)}", delta=f"до: {int(negb)}", delta_color="off")
        m3.metric("Доля негатива во входящих", f"{share_a:.0f}%" if pd.notnull(share_a) else "—",
                  delta=f"{share_a - share_b:+.0f} п.п. к периоду до" if pd.notnull(share_a) and pd.notnull(share_b) else None,
                  delta_color="inverse")
        m4.metric("Входящий рейтинг (после)", f"{ina:.2f}" if pd.notnull(ina) else "—",
                  delta=f"{ina - inb:+.2f} к периоду до" if pd.notnull(ina) and pd.notnull(inb) else None)
        m5.metric("Аномалий (падение без роста)", f"{ana}", delta=f"до: {anb} · падений всего {dropa}", delta_color="off")

        # --- график 1: прирост оценок vs прирост негатива по периодам ---
        agg = snap.groupby("bucket").agg(new_ratings=("new_ratings", "sum"), new_neg=("new_neg", "sum"),
                                         anomalies=("anomaly", "sum"),
                                         drops=("d_rating", lambda x: int((x < 0).sum()))).reset_index()
        agg["neg_share"] = agg["new_neg"] / agg["new_ratings"].replace(0, np.nan) * 100
        snap["_w"] = snap["in_rating"].fillna(0) * snap["new_ratings"]
        w_sum = snap.groupby("bucket")["_w"].sum()
        agg["in_rating"] = (w_sum.reindex(agg["bucket"]).values
                            / agg["new_ratings"].replace(0, np.nan).values)

        rl = pd.Timestamp(rollout)
        gc1, gc2 = st.columns(2)
        with gc1:
            st.markdown(f"**Прирост оценок и негатива по {'неделям' if gran == 'Неделя' else 'дням'}**")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=agg["bucket"], y=agg["new_ratings"], name="Новых оценок", marker_color="#c7c7cc"))
            fig.add_trace(go.Bar(x=agg["bucket"], y=agg["new_neg"], name="Из них 1–2★ (оценка)", marker_color=PALETTE["risk"]))
            fig.add_trace(go.Scatter(x=agg["bucket"], y=agg["neg_share"], name="Доля негатива, %", yaxis="y2",
                                     mode="lines+markers", line=dict(color=PALETTE["warn"], width=2)))
            fig.add_vline(x=rl, line_dash="dash", line_color=PALETTE["ink"], annotation_text="бот", annotation_position="top")
            style_fig(fig, 340, barmode="overlay", legend=dict(orientation="h", y=-0.25),
                      yaxis2=dict(title="%", overlaying="y", side="right", range=[0, 100], showgrid=False))
            st.plotly_chart(fig, use_container_width=True)
        with gc2:
            st.markdown("**Входящий рейтинг новых оценок и число аномалий**")
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=agg["bucket"], y=agg["in_rating"], name="Входящий рейтинг", mode="lines+markers",
                                     line=dict(color=PALETTE["accent"], width=3)))
            fig.add_trace(go.Bar(x=agg["bucket"], y=agg["anomalies"], name="Аномалии (шт.)", yaxis="y2",
                                 marker_color=PALETTE["risk"], opacity=0.45))
            fig.add_trace(go.Bar(x=agg["bucket"], y=agg["drops"], name="Падений ★ (шт.)", yaxis="y2",
                                 marker_color=PALETTE["warn"], opacity=0.3))
            fig.add_vline(x=rl, line_dash="dash", line_color=PALETTE["ink"], annotation_text="бот", annotation_position="top")
            style_fig(fig, 340, barmode="overlay", legend=dict(orientation="h", y=-0.25),
                      yaxis=dict(title="★", range=[1, 5.05]),
                      yaxis2=dict(title="шт.", overlaying="y", side="right", showgrid=False, rangemode="tozero"))
            st.plotly_chart(fig, use_container_width=True)

        # --- график 2: scatter падение vs прирост ---
        st.markdown("**Карта аномалий** — каждая точка = замер ASIN; красная зона = падение рейтинга при почти нулевом приросте оценок")
        sc = snap.dropna(subset=["growth_pct"]).copy()
        sc["период"] = np.where(sc["after"], "после бота", "до бота")
        fig = px.scatter(sc, x="growth_pct", y="d_rating", color="период", symbol="anomaly",
                         hover_name="asin", hover_data={"bucket": True, "new_ratings": True, "in_rating": ":.2f", "anomaly": False},
                         color_discrete_map={"до бота": "#8e8e93", "после бота": PALETTE["accent"]},
                         symbol_map={True: "x", False: "circle"},
                         labels={"growth_pct": "Прирост оценок, % от базы", "d_rating": "Δ рейтинга"})
        fig.add_shape(type="rect", x0=0, x1=growth_thr, y0=-1, y1=-drop_thr, fillcolor=PALETTE["risk"], opacity=0.08, line_width=0)
        fig.add_hline(y=0, line_color="#c7c7cc")
        style_fig(fig, 340, legend=dict(orientation="h", y=-0.25),
                  yaxis=dict(range=[min(-0.3, sc["d_rating"].min() - 0.05), max(0.3, sc["d_rating"].max() + 0.05)]))
        st.plotly_chart(fig, use_container_width=True)

        # --- таблица аномалий ---
        st.markdown("**Замеры с аномалией**")
        an = snap[snap["anomaly"]].sort_values("bucket", ascending=False)
        if an.empty:
            st.caption("Аномалий под текущие пороги нет")
        else:
            show = an[["asin", "source", "bucket", "prev_rating", "rating", "d_rating", "prev_count", "review_count",
                       "new_ratings", "in_rating", "new_neg", "after"]].copy()
            show["bucket"] = show["bucket"].dt.strftime("%d.%m.%Y")
            show["after"] = show["after"].map({True: "после", False: "до"})
            show.columns = ["ASIN", "Ист.", "Период", "Было ★", "Стало ★", "Δ★", "Было оценок", "Стало оценок",
                            "Новых", "Входящий ★", "Новых 1–2★", "Бот"]
            st.dataframe(show.style.format({"Было ★": "{:.1f}", "Стало ★": "{:.1f}", "Δ★": "{:+.2f}",
                                            "Входящий ★": "{:.2f}", "Было оценок": "{:.0f}", "Стало оценок": "{:.0f}",
                                            "Новых": "{:.0f}", "Новых 1–2★": "{:.0f}"}),
                         use_container_width=True, hide_index=True, height=min(420, 40 + 35 * len(show)))
            st.download_button("⬇ CSV аномалий", show.to_csv(index=False).encode("utf-8-sig"), "bot_anomalies.csv", "text/csv")

        # --- по группе: до/после ---
        if GROUP_DF_COL:
            st.markdown(f"**По {group_field.lower()}: новые оценки и доля негатива до/после**")
            gm = filtered_df.set_index("raw_asin")["_group"].to_dict()
            snap["_group"] = snap["asin"].map(gm).fillna("—")
            gg = snap.groupby(["_group", "after"]).agg(n=("new_ratings", "sum"), neg=("new_neg", "sum"),
                                                        anom=("anomaly", "sum")).reset_index()
            gg["share"] = gg["neg"] / gg["n"].replace(0, np.nan) * 100
            gg["Период"] = gg["after"].map({True: "после", False: "до"})
            gt = gg.pivot(index="_group", columns="Период", values=["n", "share", "anom"])
            gt.columns = [f"{a} ({b})" for a, b in gt.columns]
            gt = gt.rename(columns=lambda c: c.replace("n (", "Новых оценок (").replace("share (", "% негатива (")
                           .replace("anom (", "Аномалий ("))
            st.dataframe(gt.reset_index().rename(columns={"_group": group_field}).style.format(
                {c: ("{:.0f}%" if "негатива" in c else "{:.0f}") for c in gt.columns}, na_rep="—"),
                use_container_width=True, hide_index=True)

        # --- по ASIN: до/после ---
        st.markdown("**По ASIN: доля негатива во входящих оценках до и после**")
        per = snap.groupby(["asin", "after"]).agg(n=("new_ratings", "sum"), neg=("new_neg", "sum")).reset_index()
        per["share"] = per["neg"] / per["n"].replace(0, np.nan) * 100
        pv = per.pivot(index="asin", columns="after", values="share").rename(columns={False: "до", True: "после"})
        pv = pv.dropna(how="all")
        if pv.empty or "после" not in pv:
            st.caption("Недостаточно замеров после даты внедрения")
        else:
            pv["Δ"] = pv["после"] - pv.get("до", np.nan)
            pv = pv.sort_values("после", ascending=False).head(30)
            fig = go.Figure()
            if "до" in pv:
                fig.add_trace(go.Bar(x=pv.index, y=pv["до"], name="до", marker_color="#c7c7cc"))
            fig.add_trace(go.Bar(x=pv.index, y=pv["после"], name="после", marker_color=PALETTE["risk"]))
            style_fig(fig, 320, barmode="group", yaxis=dict(title="% негатива", range=[0, 100]),
                      legend=dict(orientation="h", y=-0.3))
            st.plotly_chart(fig, use_container_width=True)

        st.caption("Оговорка: витринный рейтинг округлён до 0.1, а гистограмма — до 1 %, поэтому на больших базах "
                   "(тысячи оценок) входящий рейтинг и число новых 1–2★ — оценка, не точный счёт. "
                   "Чем чаще замеры, тем точнее.")

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
    with st.expander(f"📚 Справочник ASIN (категории / parent / вид / страна) — загружено: {len(dict_df)}", expanded=False):
        st.markdown("<div class='muted'>Колонки распознаются по названию: <code>asin</code>/<code>child</code>, "
                    "<code>parent</code>, <code>category</code>/<code>категория</code>, <code>subcategory</code>, "
                    "<code>type</code>/<code>вид</code>, <code>brand</code>, <code>market</code>/<code>страна</code> (US, BE…). "
                    "Лишние колонки игнорируются.</div>", unsafe_allow_html=True)
        s1, s2 = st.columns(2)
        up = s1.file_uploader("CSV / XLSX справочника", type=["csv", "xlsx"])
        gs_url = s2.text_input("…или ссылка на Google Sheet (доступ по ссылке)", placeholder="https://docs.google.com/spreadsheets/d/…")
        replace_mode = st.radio("Режим", ["Дополнить / обновить", "Заменить полностью"], horizontal=True)
        if st.button("📥 Загрузить справочник", type="primary"):
            try:
                if up is not None:
                    raw = pd.read_excel(up) if up.name.lower().endswith("xlsx") else pd.read_csv(up)
                elif gs_url.strip():
                    raw = pd.read_csv(gsheet_to_csv_url(gs_url.strip()))
                else:
                    raw = None
                if raw is None or raw.empty:
                    st.warning("Файл или ссылка не заданы")
                else:
                    nd = normalize_dict_df(raw)
                    if nd.empty:
                        st.error("Не нашёл колонку с ASIN")
                    else:
                        save_dictionary(nd, replace=(replace_mode == "Заменить полностью"))
                        st.success(f"Сохранено {len(nd)} строк · категорий: {nd['category'].replace('', np.nan).nunique()} · "
                                   f"parent: {nd['parent_asin'].replace('', np.nan).nunique()}")
                        st.rerun()
            except Exception as e:
                st.error(f"Ошибка загрузки: {e}")
        if not dict_df.empty:
            st.dataframe(dict_df.rename(columns={"asin": "ASIN", "parent_asin": "Parent", "category": "Категория",
                                                 "subcategory": "Подкатегория", "product_type": "Вид",
                                                 "brand": "Бренд", "market": "Страна"}),
                         use_container_width=True, hide_index=True, height=260)
            miss = [a for a in tracked if a not in dict_map]
            if miss:
                st.caption(f"Без записи в справочнике: {len(miss)} ASIN из списка отслеживания")

    with st.expander("Управление списком ASIN", expanded=False):
        h1, h2 = st.columns([3, 1])
        h1.markdown(
            "**Форматы:** `B09NWGDK3S` · `B0H6YBDKXJ:US` · `B09NWGDK3S:DE` · `B09NWGDK3S:BE` · "
            "`https://www.amazon.com/dp/B0H6YBDKXJ`")
        market_filter = h2.selectbox("Фильтр по стране", options=["Все страны"] + list(MARKET_DOMAINS.keys()),
                                     index=0, key="market_filter_select")
        display_tracked = tracked if market_filter == "Все страны" else [
            a for a in tracked if asin_market_map.get(a, "BE") == market_filter or a.endswith(f":{market_filter}")]

        # ---- быстрое добавление с проверкой дублей ----
        st.markdown("**➕ Добавить ASIN в список** <span class='muted'>— дубли не запишутся, только новые</span>",
                    unsafe_allow_html=True)
        ad1, ad2 = st.columns([4, 1])
        add_text = ad1.text_area("Пачка ASIN / ссылок", height=90, key="add_asins_text",
                                 placeholder="B09NWGDK3S, B0H6YBDKXJ:US, https://www.amazon.com/dp/…",
                                 label_visibility="collapsed")
        add_market = ad2.selectbox("Страна", options=["— (каскад)"] + list(MARKET_DOMAINS.keys()), index=1,
                                   key="add_market_sel")
        if add_text.strip():
            new_c, dup_c, inv_c, mk_map, bd = parse_asin_batch(add_text, tracked,
                                                              add_market if add_market in MARKET_DOMAINS else None)
            p1, p2, p3 = st.columns(3)
            p1.markdown(f"🟢 **Новых: {len(new_c)}**" + (f"<br><span class='muted'>{', '.join(new_c[:30])}"
                        f"{' …' if len(new_c) > 30 else ''}</span>" if new_c else ""), unsafe_allow_html=True)
            p2.markdown(f"🟡 **Уже в базе: {len(dup_c)}**" + (f"<br><span style='color:#b06000'>{', '.join(dup_c[:30])}"
                        f"{' …' if len(dup_c) > 30 else ''}</span>" if dup_c else ""), unsafe_allow_html=True)
            p3.markdown(f"🔴 **Нераспознано: {len(inv_c)}**" + (f"<br><span style='color:#c5221f'>{', '.join(inv_c[:15])}"
                        f"{' …' if len(inv_c) > 15 else ''}</span>" if inv_c else "")
                        + (f"<br><span class='muted'>повторы внутри пачки: {len(bd)}</span>" if bd else ""),
                        unsafe_allow_html=True)
            if st.button(f"➕ Добавить {len(new_c)} новых", type="primary", disabled=not new_c, key="add_asins_btn"):
                ensure_schema()
                try:
                    conn = _conn()
                    with conn.cursor() as cur:
                        for code in new_c:
                            cur.execute("INSERT INTO tracked_asins (asin) VALUES (%s) ON CONFLICT (asin) DO NOTHING;", (code,))
                    conn.commit()
                    conn.close()
                    save_markets({c: m for c, m in mk_map.items() if c in new_c})
                    st.success(f"Добавлено {len(new_c)} · пропущено как дубли {len(dup_c)}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка добавления: {e}")
        st.markdown("---")

        st.markdown(f"**Текущие ASIN (`{market_filter}`)** — {len(display_tracked)} из {len(tracked)} "
                    "<span class='muted'>(полное редактирование: удалить — сотри из текста и пересохрани)</span>",
                    unsafe_allow_html=True)
        edited = st.text_area("Список", value=", ".join(display_tracked), height=140,
                              key=f"edit_tracked_list_{market_filter}", label_visibility="collapsed")
        b0, b1, b2 = st.columns([1.2, 1, 1])
        default_market = b0.selectbox("Страна для новых ASIN", options=["— (каскад BE/NL)"] + list(MARKET_DOMAINS.keys()),
                                      index=1, help="Записывается в справочник; при прогоне коллектор пойдёт на этот домен")
        if b1.button("💾 Пересохранить список"):
            existing_for_check = [] if market_filter == "Все страны" else tracked
            clean, dup_c, inv_c, markets, bd = parse_asin_batch(edited, existing_for_check, None)
            # явные :XX / ссылки — всегда; страна по умолчанию — только для действительно новых ASIN
            if default_market in MARKET_DOMAINS:
                for code in clean:
                    if code not in markets and code not in dict_map and code not in tracked:
                        markets[code] = default_market
            ensure_schema()
            try:
                conn = _conn()
                with conn.cursor() as cur:
                    if market_filter == "Все страны":
                        cur.execute("DELETE FROM tracked_asins;")
                    for code in clean:
                        cur.execute("INSERT INTO tracked_asins (asin) VALUES (%s) ON CONFLICT (asin) DO NOTHING;", (code,))
                conn.commit()
                conn.close()
                save_markets(markets)
                msg = f"Сохранено {len(clean)} ASIN"
                if dup_c:
                    msg += f" · уже были: {len(dup_c)}"
                if bd:
                    msg += f" · повторов в тексте убрано: {len(bd)}"
                if inv_c:
                    msg += f" · нераспознано: {len(inv_c)} ({', '.join(inv_c[:5])}{' …' if len(inv_c) > 5 else ''})"
                st.success(msg)
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
