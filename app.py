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

# Секреты: .env локально, st.secrets в Streamlit Cloud. Прокидываем в os.environ
# ДО импорта collector/notifier — они читают переменные на уровне модуля.
load_dotenv()
for _k in ("DATABASE_URL", "SCRAPINGDOG_API_KEY", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"):
    if not os.environ.get(_k):
        try:
            _v = st.secrets.get(_k)
            if _v:
                os.environ[_k] = str(_v)
        except Exception:
            pass

from collector import (
    check_asin,
    check_asin_api,
    clean_db_trash,
    delete_asin_completely,
    ensure_reviews_schema,
    ensure_schema,
    extract_asin,
    extract_children,
    fetch_product_json,
    fetch_reviews,
    fetch_reviews_api,
    finish_run,
    get_tracked_asins,
    save_reviews,
    save_to_db,
    start_run,
)

try:
    import notifier
    NOTIFIER_OK = True
except Exception:
    notifier = None
    NOTIFIER_OK = False

try:
    import ai_insights
    AI_OK = True
except Exception:
    ai_insights = None
    AI_OK = False

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
        return {a: "—" for a in all_tracked}
    try:
        conn = _conn()
        df_src = pd.read_sql(
            "SELECT DISTINCT ON (asin) asin, source FROM asin_metrics ORDER BY asin, created_at DESC", conn)
        conn.close()
        db_map = dict(zip(df_src["asin"], df_src["source"]))
        # фактическая страна последнего сбора; "—" если ещё не собирали или не нашли
        return {a: (db_map.get(a) if db_map.get(a) in MARKET_DOMAINS else "—") for a in all_tracked}
    except Exception:
        return {a: "—" for a in all_tracked}


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


def ensure_settings_table():
    if not DATABASE_URL:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS radar_settings ("
                        "key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ DEFAULT NOW());")
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_setting(key, default=None):
    ensure_settings_table()
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM radar_settings WHERE key = %s", (key,))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def set_setting(key, value):
    ensure_settings_table()
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO radar_settings (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
                        (key, str(value)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def enrich_dictionary_from_api(res: dict):
    """Пишет parent / категорию / страну из structured API в справочник.
    Значения из загруженного spr не затираются — они приоритетнее."""
    asin = res.get("asin")
    if not asin:
        return
    path = [x.strip() for x in (res.get("category_path") or "").split("›") if x.strip()]
    category = path[-1] if path else ""
    subcat = path[-2] if len(path) > 1 else ""
    ensure_dict_table()
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO asin_dictionary (asin, parent_asin, category, subcategory, market, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (asin) DO UPDATE SET
                parent_asin = COALESCE(NULLIF(asin_dictionary.parent_asin, ''), EXCLUDED.parent_asin),
                category    = COALESCE(NULLIF(asin_dictionary.category, ''), EXCLUDED.category),
                subcategory = COALESCE(NULLIF(asin_dictionary.subcategory, ''), EXCLUDED.subcategory),
                market      = COALESCE(NULLIF(asin_dictionary.market, ''), EXCLUDED.market),
                updated_at  = NOW();
            """,
            (asin, res.get("parent_asin") or "", category, subcat, res.get("source") or ""))
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
    use_api = st.session_state.get("use_api_mode", True)
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
            res = check_asin_api(item, log=_log) if use_api else check_asin(item, log=_log)
        except Exception as e:
            _log(f"Ошибка {item}: {e}")
            res = {"asin": extract_asin(item), "source": "none", "rating": None, "count": None,
                   "hist": {}, "image_url": None, "bsr": None, "note": f"ошибка сбора: {e}"[:200]}
        try:
            save_to_db(res)   # пишем даже неудачный замер — иначе дыра в истории
        except Exception as e:
            _log(f"Не сохранился {item}: {e}")
        if res.get("parent_asin") or res.get("category_path"):
            try:
                enrich_dictionary_from_api(res)   # API отдал parent/категорию — в справочник
            except Exception:
                pass
        if res.get("source") in VALID_SOURCES:
            ok += 1
        progress.progress(i / len(items), text=f"{i}/{len(items)}")
    finish_run(run_id, ok, "done")
    st.session_state["last_auto_run"] = time.time()
    msg = f"{label} завершён: {ok}/{len(items)} успешно."
    if NOTIFIER_OK and st.session_state.get("tg_notify_on", True):
        try:
            notifier.process_updates()
            sent, total = notifier.notify_all(header=f"Rating Radar — {label.lower()} завершён")
            msg += f" Telegram: отправлено {sent} из {total}."
        except Exception as e:
            msg += f" Telegram: ошибка отправки ({e})."
    st.success(msg)
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
    st.markdown(
        "<div style='margin-top:6px'>"
        "<a href='https://t.me/RatingRadar_bot' target='_blank' "
        "style='display:inline-block;background:#229ED9;color:#fff;padding:6px 14px;border-radius:999px;"
        "font-size:13px;font-weight:600;text-decoration:none'>✈️ Алерты в Telegram — @RatingRadar_bot</a>"
        "<span class='muted' style='margin-left:10px'>подписка в один клик: /start</span></div>",
        unsafe_allow_html=True)

def ensure_kind_column():
    if not DATABASE_URL:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE tracked_asins ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'child';")
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_tracked_with_kind():
    """{asin: 'child'|'parent'}"""
    ensure_schema()
    ensure_kind_column()
    try:
        conn = _conn()
        df = pd.read_sql("SELECT asin, COALESCE(kind, 'child') AS kind FROM tracked_asins ORDER BY asin", conn)
        conn.close()
        return dict(zip(df["asin"], df["kind"]))
    except Exception:
        return {a: "child" for a in get_tracked_asins()}


KIND_LABEL = {"child": "Чайлд", "parent": "Парент"}
# разбираем накопившиеся команды бота (/start и др.) — работает без отдельного воркера
if NOTIFIER_OK and notifier.BOT_TOKEN:
    try:
        notifier.process_updates()
    except Exception:
        pass

tracked_kind = get_tracked_with_kind()
tracked = list(tracked_kind.keys())
tracked_by_kind = {k: [a for a, kk in tracked_kind.items() if kk == k] for k in KIND_LABEL}
asin_market_map = get_asin_markets_map(tracked)
full_df = get_full_history()
ensure_dict_table()
try:
    ensure_reviews_schema()   # таблицы отзывов — до первого сбора
except Exception:
    pass
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
            "kind": tracked_kind.get(str(r["asin"]), "child"),
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

    # разбивка по странам: справочник → иначе откуда реально собрали
    cty = {}
    for a in tracked:
        cty[asin_market_map.get(a, "—")] = cty.get(asin_market_map.get(a, "—"), 0) + 1
    cty_sorted = sorted(cty.items(), key=lambda kv: (kv[0] == "—", -kv[1]))
    cty_str = " · ".join(f"{k} {v}" for k, v in cty_sorted)
    n_cty = sum(1 for k in cty if k != "—")

    k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
    kpi(k1, "Позиций", n_total, f"отслеживается {len(tracked)}")
    kpi(k7, "Стран", n_cty, cty_str)
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
(tab_port, tab_port_p, tab_ai, tab_dyn, tab_dyn_p, tab_an, tab_bot, tab_fc, tab_ops,
 tab_help) = st.tabs(
    ["📋 Портфель (Чайлд)", "📋 Портфель (Парент)", "🧠 AI-анализ",
     "📅 Динамика по дням (Чайлд)", "📅 Динамика по дням (Парент)",
     "📊 Аналитика", "🤖 Бот возвратов", "📈 Прогноз", "⚙️ Сбор и управление", "ℹ️ Как это работает"])

# ---------- ПОРТФЕЛЬ ----------
def render_portfolio(filtered_df, kind):
    kind_asins = tracked_by_kind.get(kind, [])
    filtered_df = filtered_df[filtered_df["kind"] == kind].copy() if not filtered_df.empty else filtered_df
    if not kind_asins and filtered_df.empty:
        st.info(f"Список «{KIND_LABEL[kind]}» пуст — добавь ASIN во вкладке «Сбор и управление» → "
                f"«Загрузка ASIN — Портфель ({KIND_LABEL[kind]})»")
    elif filtered_df.empty:
        st.warning("В базе нет сохранённых метрик под текущие фильтры")
    else:
        hc, vc = st.columns([3, 1])
        hc.markdown(f"### Сводный отчёт — {KIND_LABEL[kind]} <span class='muted'>· {len(filtered_df)} из {len(kind_asins)} позиций</span>",
                    unsafe_allow_html=True)
        view_mode = vc.radio("Вид", options=["Таблица", "Карточки"], horizontal=True, label_visibility="collapsed",
                             key=f"view_mode_{kind}")

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
                    "Источник": st.column_config.TextColumn("Страна", width="small", disabled=True),
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
                use_container_width=True, hide_index=True, key=f"table_editor_{kind}",
            )

            selected_asins = [extract_asin(u) for u in edited_df.loc[edited_df["Выбор"] == True, "ASIN"] if extract_asin(u)]

            a1, a2, a3, a4 = st.columns([3, 1, 1, 1])
            if selected_asins:
                a1.markdown(f"**Выбрано: {len(selected_asins)}** · `{', '.join(selected_asins)}`")
            else:
                a1.caption("Отметьте строки галочкой для массовых действий")
            if a2.button("↻ Обновить выбранные", use_container_width=True, disabled=not selected_asins, key=f"upd_{kind}"):
                run_collection(selected_asins, "Обновление")
            if a3.button("✕ Удалить выбранные", use_container_width=True, disabled=not selected_asins, key=f"del_{kind}"):
                for a in selected_asins:
                    delete_asin_completely(a)
                st.success(f"Удалено: {len(selected_asins)}")
                st.rerun()
            csv = filtered_df.drop(columns=["Выбор", "raw_created_at", "Фото", "bad_pct", "five_pct", "margin", "Рейтинг ★"],
                                   errors="ignore") \
                .to_csv(index=False).encode("utf-8-sig")
            a4.download_button("⬇ CSV", csv, f"rating_radar_{kind}.csv", "text/csv", use_container_width=True, key=f"csv_{kind}")

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
                        tc.markdown(f"Страна `{item['Источник']}` · BSR `{item['BSR']}`")
                        if item.get("Категория", "—") != "—":
                            tc.markdown(f"<span class='muted'>{item['Категория']}"
                                        f"{' · ' + item['Parent'] if item['Parent'] else ''}</span>", unsafe_allow_html=True)
                        tc.markdown(f"Негатив 1–2★: **{item['1–2★ %']}** {item['Тренд']}")
                        tc.markdown(f"Запас до 4.0: **{item['Запас (до 4.0)']}**")
                        st.caption(f"Обновлено: {item['Время сбора']}")



with tab_port:
    render_portfolio(filtered_df, "child")

with tab_port_p:
    render_portfolio(filtered_df, "parent")


# ---------- AI-АНАЛИЗ ----------
with tab_ai:
    if not AI_OK:
        st.error("Модуль ai_insights.py не найден")
    elif not ai_insights.API_KEY:
        st.warning("Не задан ANTHROPIC_API_KEY — добавь в Secrets приложения")
    else:
        st.markdown("### AI-анализ")
        st.markdown("<div class='muted'>Цифры считаются на нашей стороне, модель их только интерпретирует — "
                    "она ничего не пересчитывает и не выдумывает.</div>", unsafe_allow_html=True)

        sub_digest, sub_reviews, sub_ro = st.tabs(
            ["📰 Дайджест", "💬 Причины негатива", "🔇 Оценки без текста"])

        # ---------- дайджест ----------
        with sub_digest:
            g1, g2, g3 = st.columns([1, 1, 2])
            dg_days = g1.selectbox("Период", [7, 14, 30], index=0, format_func=lambda d: f"{d} дн.",
                                   key="ai_days")
            dg_rollout = g2.date_input("Дата внедрения бота", value=datetime.date(2026, 8, 20), key="ai_rollout")
            g3.markdown("<div class='muted' style='margin-top:28px'>Один запрос к модели. "
                        "Подаются агрегаты: статусы, дельты, входящий рейтинг, разрез по категориям, до/после.</div>",
                        unsafe_allow_html=True)

            c1, c2 = st.columns([1, 1])
            if c1.button("🧠 Собрать дайджест", type="primary", key="ai_digest_btn"):
                with st.spinner("Считаю агрегаты и спрашиваю модель…"):
                    try:
                        text, agg = ai_insights.weekly_digest(days=dg_days, rollout_date=str(dg_rollout))
                        st.session_state["ai_digest_text"] = text
                        st.session_state["ai_digest_agg"] = agg
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
            if c2.button("👁 Показать только агрегаты", key="ai_agg_btn"):
                try:
                    st.session_state["ai_digest_agg"] = ai_insights.collect_aggregates(
                        days=dg_days, rollout_date=str(dg_rollout))
                    st.session_state.pop("ai_digest_text", None)
                except Exception as e:
                    st.error(f"Ошибка: {e}")

            if st.session_state.get("ai_digest_text"):
                st.markdown("---")
                st.markdown(st.session_state["ai_digest_text"])
                s1, s2 = st.columns([1, 3])
                if NOTIFIER_OK and notifier.BOT_TOKEN and s1.button("📤 Отправить в Telegram", key="ai_send_tg"):
                    try:
                        body = st.session_state["ai_digest_text"].replace("<", "&lt;").replace(">", "&gt;")
                        ok_n, total = notifier.broadcast(
                            f"<b>Rating Radar — дайджест за {dg_days} дн.</b>\n\n{body}"
                            f"\n\n<a href=\"{notifier.DASHBOARD_URL}\">Открыть дашборд →</a>")
                        st.success(f"Отправлено {ok_n} из {total}")
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
                s2.download_button("⬇ Скачать текст", st.session_state["ai_digest_text"].encode("utf-8"),
                                   f"digest_{dg_days}d.md", "text/markdown", key="ai_dl")

            if st.session_state.get("ai_digest_agg"):
                with st.expander("Агрегаты, которые ушли в модель", expanded=False):
                    st.json(st.session_state["ai_digest_agg"], expanded=False)

        # ---------- причины негатива ----------
        with sub_reviews:
            st.markdown("**Шаг 1. Собрать тексты отзывов 1–2★**")
            st.markdown("<div class='muted'>Отдельный проход по страницам отзывов — это дополнительные запросы "
                        "к скрейперу, поэтому делается точечно, а не по всему портфелю.</div>",
                        unsafe_allow_html=True)
            f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
            pool = filtered_df["raw_asin"].tolist() if not filtered_df.empty else all_asins
            default_pick = filtered_df.nsmallest(5, "Рейтинг")["raw_asin"].tolist() if not filtered_df.empty else []
            rv_asins = f1.multiselect("ASIN для сбора отзывов", options=pool, default=default_pick,
                                      key="ai_rv_asins")
            rv_market = f2.selectbox("Страна", options=list(MARKET_DOMAINS.keys()), index=1, key="ai_rv_market")
            rv_pages = f3.number_input("Страниц", 1, 5, 2, key="ai_rv_pages")
            f4.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
            if f4.button("📥 Собрать", disabled=not rv_asins, key="ai_rv_fetch", use_container_width=True):
                prog = st.progress(0.0)
                log_box = st.empty()
                lines, total_saved = [], 0
                for i, a in enumerate(rv_asins, 1):
                    def _log(m, _l=lines):
                        _l.append(m)
                        log_box.code("\n".join(_l[-10:]))
                    try:
                        if st.session_state.get("use_api_mode", True):
                            data = fetch_reviews_api(a, market=rv_market, star_filter="critical",
                                                     pages=int(rv_pages), log=_log)
                            if not data["reviews"]:
                                _log(f"  {a}: API пусто, пробую HTML")
                                data = fetch_reviews(a, market=rv_market, star_filter="critical",
                                                     pages=int(rv_pages), log=_log)
                        else:
                            data = fetch_reviews(a, market=rv_market, star_filter="critical",
                                                 pages=int(rv_pages), log=_log)
                        total_saved += save_reviews(data)
                    except Exception as e:
                        _log(f"{a}: ошибка {e}")
                    prog.progress(i / len(rv_asins), text=f"{i}/{len(rv_asins)}")
                st.success(f"Сохранено новых отзывов: {total_saved}")

            st.markdown("---")
            st.markdown("**Шаг 2. Разбор причин**")
            a1, a2, a3 = st.columns([2, 1, 1])
            cats = sorted(set(calc_df["Категория"].tolist())) if not calc_df.empty else []
            an_cat = a1.selectbox("Категория (или все)", options=["Все"] + [c for c in cats if c != "—"],
                                  key="ai_an_cat")
            an_limit = a2.number_input("Сколько отзывов", 10, 200, 60, step=10, key="ai_an_limit")
            a3.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
            run_an = a3.button("🧠 Разобрать", type="primary", key="ai_an_btn", use_container_width=True)

            try:
                preview = ai_insights.get_reviews_for_analysis(
                    asins=rv_asins or None,
                    category=None if an_cat == "Все" else an_cat,
                    limit=int(an_limit))
            except Exception as e:
                preview = pd.DataFrame()
                st.error(f"Не читаются отзывы: {e}")
            st.caption(f"В базе под эти условия: {len(preview)} отзывов 1–2★")
            if not preview.empty:
                with st.expander("Показать тексты", expanded=False):
                    st.dataframe(preview[["asin", "stars", "title", "body", "review_date"]],
                                 use_container_width=True, hide_index=True, height=260)

            if run_an:
                with st.spinner("Читаю отзывы…"):
                    try:
                        ctx = f"Категория: {an_cat}. Бренд: мериносовая одежда, Amazon."
                        st.session_state["ai_causes"] = ai_insights.analyze_negative_reviews(preview, ctx)
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
            if st.session_state.get("ai_causes"):
                st.markdown("---")
                st.markdown(st.session_state["ai_causes"])

        # ---------- rating-only ----------
        with sub_ro:
            st.markdown("**Прямая оценка бота: оценки без текста**")
            st.markdown("<div class='muted'>Всего оценок (с витрины) минус отзывы с текстом (со страницы отзывов). "
                        "Разница — оценки без текста, включая те, что оставляет бот возвратов. "
                        "Это точный счёт, в отличие от косвенной аномалии по округлённому рейтингу.</div>",
                        unsafe_allow_html=True)
            try:
                ro = ai_insights.rating_only_estimate()
            except Exception as e:
                ro = pd.DataFrame()
                st.error(f"Ошибка: {e}")
            if ro.empty:
                st.info("Нужны собранные отзывы — сделай «Собрать» на вкладке «Причины негатива». "
                        "Счётчик отзывов с текстом пишется автоматически при сборе.")
            else:
                m1, m2, m3 = st.columns(3)
                m1.metric("Позиций с данными", len(ro))
                m2.metric("Оценок всего", int(ro["review_count"].sum()))
                m3.metric("Без текста", int(ro["rating_only"].sum()),
                          delta=f"{ro['rating_only'].sum() / max(1, ro['review_count'].sum()) * 100:.0f}% от всех",
                          delta_color="off")
                show = ro[["asin", "source", "rating", "review_count", "reviews_with_text",
                           "rating_only", "rating_only_%"]].copy()
                show.columns = ["ASIN", "Страна", "Рейтинг", "Оценок всего", "С текстом",
                                "Без текста", "% без текста"]
                st.dataframe(show, use_container_width=True, hide_index=True, height=380)
                fig = px.bar(ro.head(20), x="asin", y="rating_only_%", color="rating_only_%",
                             color_continuous_scale=["#1f8a4c", "#c77800", "#d13438"],
                             labels={"asin": "", "rating_only_%": "% оценок без текста"})
                style_fig(fig, 320, showlegend=False, coloraxis_showscale=False)
                st.plotly_chart(fig, use_container_width=True)

# ---------- ДИНАМИКА ПО ДНЯМ (широкая таблица) ----------
def render_dynamics(filtered_df, hist_df, kind):
    st.markdown(f"### Динамика по дням — {KIND_LABEL[kind]}")
    kind_asins = set(tracked_by_kind.get(kind, []))
    filtered_df = filtered_df[filtered_df["kind"] == kind].copy() if not filtered_df.empty else filtered_df
    hist_df = hist_df[hist_df["asin"].isin(kind_asins)].copy() if not hist_df.empty else hist_df
    st.markdown("<div class='muted'>Каждый ASIN — блок строк (Rating / BSR / Reviews / 1–2★), колонки — даты замеров. "
                "Цвет рейтинга по логике Amazon. Период задаётся фильтром сверху.</div>", unsafe_allow_html=True)

    if not kind_asins:
        st.info(f"Список «{KIND_LABEL[kind]}» пуст — добавь ASIN во вкладке «Сбор и управление»")
    elif hist_df.empty:
        st.info("Нет истории под текущие фильтры")
    else:
        d1, d2, d3 = st.columns([2, 1.5, 1.5])
        params_sel = d1.multiselect("Параметры", ["Rating", "BSR", "Reviews", "1–2★ %"],
                                    default=["Rating", "BSR", "Reviews"], key=f"dyn_params_{kind}")
        sort_by = d2.selectbox("Сортировка ASIN", ["По последнему рейтингу ↑", "По последнему рейтингу ↓",
                                                   "По падению за период", "По алфавиту"], key=f"dyn_sort_{kind}")
        gran_d = d3.radio("Шаг", ["День", "Неделя"], horizontal=True, key=f"dyn_gran_{kind}")
        fill_gaps = d3.checkbox("Заполнять пропуски", value=True, key=f"dyn_fill_{kind}",
                                help="В дни без сбора показывать последнее известное значение "
                                     "(курсивом и бледнее). В базу ничего не пишется.")

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

        # где реально был замер, а где значение будет перенесено
        measured = {"Rating": piv_r.notna(), "BSR": piv_b.notna(),
                    "Reviews": piv_c.notna(), "1–2★ %": piv_n.notna()}
        if fill_gaps:
            piv_r = piv_r.ffill(axis=1)
            piv_b = piv_b.ffill(axis=1)
            piv_c = piv_c.ffill(axis=1)
            piv_n = piv_n.ffill(axis=1)

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
        wide = pd.DataFrame(blocks, columns=["ASIN", "Страна", "Parameter"] + day_labels)

        carried_rows = []
        for _, r in wide.iterrows():
            m = measured.get(r["Parameter"])
            if m is None or r["ASIN"] not in m.index:
                carried_rows.append([True] * len(day_labels))
            else:
                carried_rows.append([bool(x) for x in m.loc[r["ASIN"]].reindex(days).fillna(False).values])
        carried = pd.DataFrame(carried_rows, columns=day_labels)

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
            for a, src, f, g in zip(wide["ASIN"], wide["Страна"], first_row, is_group)
        ]
        disp["Страна"] = disp["Страна"].where(first_row, "")

        # ---- рендер: нативная таблица с выбором строк ----
        def fmt_cell(v, p):
            if pd.isna(v):
                return ""
            if p == "Группа (ср. ★)":
                return f"{v:.2f}"
            if p == "Rating":
                return f"{v:.1f}"
            if p == "1–2★ %":
                return f"{int(v)}%"
            return f"{int(v)}"

        row_asin = ["" if p == "Группа (ср. ★)" else a
                    for a, p in zip(wide["ASIN"], wide["Parameter"])]

        disp = wide.copy()
        for col in day_labels:
            disp[col] = [fmt_cell(v, p) for v, p in zip(wide[col], wide["Parameter"])]
        first_row = disp["Parameter"] == (params_sel[0] if params_sel else "")
        disp = disp.rename(columns={"Parameter": "Параметр"})

        def style_rows(row):
            p = wide.loc[row.name, "Parameter"]
            vals = wide.loc[row.name, day_labels]
            out = [""] * len(row)
            is_measured = (list(carried.loc[row.name]) if row.name in carried.index
                           else [True] * len(day_labels))
            if p == "Группа (ср. ★)":
                out = ["background-color:#e8e8ed;font-weight:700"] * len(row)
                out[3:] = [rating_color(v) for v in vals]
                return out
            if p == "Rating":
                out[3:] = [rating_color(v) for v in vals]
            elif p == "1–2★ %":
                out[3:] = ["background-color:#ffcdd2" if pd.notnull(v) and v > 15 else
                           ("background-color:#fff9c4" if pd.notnull(v) and v > 8 else "") for v in vals]
            elif p in ("Reviews", "BSR"):   # noqa: подсветка динамики ниже
                prev, styles = None, []
                for v in vals:
                    st_ = ""
                    if pd.notnull(v) and prev is not None and pd.notnull(prev):
                        if p == "Reviews" and v > prev:
                            st_ = "color:#1f8a4c;font-weight:600"
                        elif p == "BSR":
                            st_ = ("color:#d13438" if v > prev * 1.15
                                   else ("color:#1f8a4c" if v < prev * 0.85 else ""))
                    styles.append(st_)
                    if pd.notnull(v):
                        prev = v
                out[3:] = styles
            for i, ok in enumerate(is_measured):   # перенесённые — курсив и бледнее
                if not ok:
                    out[3 + i] = ((out[3 + i] + ";") if out[3 + i] else "") + "font-style:italic;opacity:.45"
            return out

        def style_asin_col(col):
            # ASIN виден в каждой строке, но у не-первых строк блока — приглушённый
            return ["font-weight:600" if f else "color:#b0b0b8" for f in first_row]

        styled = (disp.style.apply(style_rows, axis=1)
                  .apply(style_asin_col, subset=["ASIN"]))

        st.caption(f"{len(order)} ASIN × {len(days)} {'недель' if gran_d == 'Неделя' else 'дней'} · {len(wide)} строк")

        # ASIN дублируем в каждую строку блока — иначе при скролле непонятно, чья строка
        disp["ASIN"] = [a if a else "" for a in wide["ASIN"]]
        disp["Страна"] = list(wide["Страна"])

        def _col(label, width, pin=False):
            try:
                return st.column_config.TextColumn(label, width=width, pinned=pin)
            except TypeError:      # старые версии Streamlit без pinned
                return st.column_config.TextColumn(label, width=width)

        cfg = {
            "ASIN": _col("ASIN", "medium", pin=True),
            "Страна": _col("Страна", "small", pin=True),
            "Параметр": _col("Параметр", "small", pin=True),
        }
        cfg.update({d: st.column_config.TextColumn(d, width="small") for d in day_labels})

        sel = st.dataframe(
            styled, use_container_width=True, hide_index=True,
            height=min(760, 40 + 35 * len(disp)),
            on_select="rerun", selection_mode="multi-row", key=f"dyn_table_{kind}",
            column_config=cfg,
        )

        picked_rows = []
        try:
            picked_rows = sel.selection["rows"]
        except Exception:
            pass
        picked = sorted({row_asin[i] for i in picked_rows if i < len(row_asin) and row_asin[i]})

        u1, u2, u3 = st.columns([3, 1, 1])
        if picked:
            u1.markdown(f"**Отмечено: {len(picked)}** · `{', '.join(picked[:12])}`"
                        f"{' …' if len(picked) > 12 else ''}")
        else:
            u1.caption("Отметь галочками строки нужных ASIN — годится любая строка блока")
        if u2.button(f"↻ Обновить ({len(picked)})", disabled=not picked, type="primary",
                     key=f"dyn_upd_btn_{kind}", use_container_width=True):
            run_collection(picked, "Обновление")
        if u3.button(f"↻ Все ({len(order)})", key=f"dyn_upd_all_{kind}", use_container_width=True,
                     help="Пересобрать все ASIN из таблицы"):
            run_collection(list(order), "Обновление")

        st.markdown("<div class='muted' style='margin-top:4px'>"
                    "🟢 ≥4.5 · 🟡 4.3–4.4 · 🔴 ≤4.2 · зелёные Reviews — прибавились · "
                    "красный BSR — просел более чем на 15%"
                    + (" · <i>курсивом и бледнее</i> — сбора в этот день не было, "
                       "показано последнее известное значение" if fill_gaps else "")
                    + "</div>", unsafe_allow_html=True)

        e1, e2 = st.columns([1, 5])
        e1.download_button("⬇ CSV", wide.to_csv(index=False).encode("utf-8-sig"), f"rating_dynamics_{kind}.csv", "text/csv",
                           use_container_width=True, key=f"dyn_csv_{kind}")
        try:
            import io
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                styled.to_excel(xw, sheet_name="Dynamics", index=False)
            e2.download_button("⬇ Excel с цветами", buf.getvalue(), f"rating_dynamics_{kind}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"dyn_xlsx_{kind}")
        except Exception:
            pass


with tab_dyn:
    render_dynamics(filtered_df, hist_df, "child")

with tab_dyn_p:
    render_dynamics(filtered_df, hist_df, "parent")

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
            show.columns = ["ASIN", "Страна", "Период", "Было ★", "Стало ★", "Δ★", "Было оценок", "Стало оценок",
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

def render_asin_manager(kind):
    tracked_k = tracked_by_kind.get(kind, [])
    # --- одна страна на весь блок: и фильтр списка, и страна для новых ---
    by_country = {}
    for a in tracked_k:
        # страна = из справочника, иначе — откуда реально собрали в последний раз
        by_country.setdefault(asin_market_map.get(a, "—"), []).append(a)
    counts = " · ".join(f"**{k}**: {len(v)}" for k, v in sorted(by_country.items(), key=lambda kv: (kv[0] == "—", kv[0])))
    st.markdown(f"{KIND_LABEL[kind]} — отслеживается **{len(tracked_k)}** — {counts} &nbsp; "
                f"<span class='muted'>(страна — из справочника, иначе откуда реально собрали; «—» = ещё не собирали или не нашли)</span>",
                unsafe_allow_html=True)

    c_sel, c_hint = st.columns([1, 3])
    country = c_sel.selectbox("Страна", options=["Все страны"] + list(MARKET_DOMAINS.keys()) + ["— без страны"],
                              index=1, key=f"asin_country_sel_{kind}")
    c_hint.markdown("<div class='muted' style='margin-top:30px'>Выбранная страна применяется ко всему блоку: "
                    "показывает ASIN этой страны, новые ASIN из пачки получают её, пересохранение меняет только её список. "
                    "Форматы: <code>B09NWGDK3S</code> · <code>B09NWGDK3S:DE</code> · ссылка на листинг "
                    "(страна из суффикса/ссылки имеет приоритет).</div>", unsafe_allow_html=True)

    sel_market = country if country in MARKET_DOMAINS else None
    if country == "Все страны":
        display_tracked = tracked_k
    elif country == "— без страны":
        display_tracked = by_country.get("—", [])
    else:
        display_tracked = by_country.get(country, [])

    # ---- добавить пачку ----
    st.markdown("**➕ Добавить ASIN** <span class='muted'>— дубли не запишутся, только новые</span>",
                unsafe_allow_html=True)
    add_text = st.text_area("Пачка ASIN / ссылок", height=90, key=f"add_asins_text_{kind}",
                            placeholder="B09NWGDK3S, B0H6YBDKXJ:US, https://www.amazon.com/dp/…",
                            label_visibility="collapsed")
    if add_text.strip():
        new_c, dup_c, inv_c, mk_map, bd = parse_asin_batch(add_text, tracked, sel_market)
        p1, p2, p3 = st.columns(3)
        p1.markdown(f"🟢 **Новых: {len(new_c)}**" + (f"<br><span class='muted'>{', '.join(new_c[:30])}"
                    f"{' …' if len(new_c) > 30 else ''}</span>" if new_c else ""), unsafe_allow_html=True)
        p2.markdown(f"🟡 **Уже в базе: {len(dup_c)}**" + (f"<br><span style='color:#b06000'>{', '.join(dup_c[:30])}"
                    f"{' …' if len(dup_c) > 30 else ''}</span>" if dup_c else ""), unsafe_allow_html=True)
        p3.markdown(f"🔴 **Нераспознано: {len(inv_c)}**" + (f"<br><span style='color:#c5221f'>{', '.join(inv_c[:15])}"
                    f"{' …' if len(inv_c) > 15 else ''}</span>" if inv_c else "")
                    + (f"<br><span class='muted'>повторы внутри пачки: {len(bd)}</span>" if bd else ""),
                    unsafe_allow_html=True)
        lbl = f"➕ Добавить {len(new_c)} новых" + (f" → {sel_market}" if sel_market else " (каскад)")
        if st.button(lbl, type="primary", disabled=not new_c, key=f"add_asins_btn_{kind}"):
            ensure_schema()
            try:
                conn = _conn()
                with conn.cursor() as cur:
                    for code in new_c:
                        cur.execute("INSERT INTO tracked_asins (asin, kind) VALUES (%s, %s) ON CONFLICT (asin) DO UPDATE SET kind = EXCLUDED.kind;", (code, kind))
                conn.commit()
                conn.close()
                save_markets({c: m for c, m in mk_map.items() if c in new_c})
                st.success(f"Добавлено {len(new_c)} · пропущено как дубли {len(dup_c)}")
                st.rerun()
            except Exception as e:
                st.error(f"Ошибка добавления: {e}")

    # ---- заменить ASIN ----
    st.markdown("---")
    st.markdown("**🔁 Заменить ASIN** <span class='muted'>— старый уходит из отслеживания, новый встаёт на его место "
                "и наследует категорию/parent/страну из справочника</span>", unsafe_allow_html=True)
    r1, r2, r3, r4 = st.columns([2, 2, 1.2, 1])
    old_asin = r1.selectbox("Старый ASIN", options=[""] + tracked_k, key=f"repl_old_{kind}")
    new_asin_raw = r2.text_input("Новый ASIN / ссылка", key=f"repl_new_{kind}", placeholder="B0XXXXXXXX или ссылка")
    keep_hist = r3.checkbox("Сохранить историю старого", value=True, key=f"repl_keep_{kind}")
    new_code = extract_asin(new_asin_raw) if new_asin_raw.strip() else ""
    new_valid = bool(new_code) and len(new_code) == 10 and new_code != old_asin
    if new_asin_raw.strip() and not new_valid:
        r2.caption("⚠️ не похоже на ASIN")
    elif new_valid and new_code in tracked:
        r2.caption("⚠️ уже отслеживается — будет просто снят старый")
    r4.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
    if r4.button("Заменить", disabled=not (old_asin and new_valid), key=f"repl_btn_{kind}", use_container_width=True):
        try:
            ensure_schema()
            ensure_dict_table()
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tracked_asins (asin, kind) VALUES (%s, %s) ON CONFLICT (asin) DO UPDATE SET kind = EXCLUDED.kind;", (new_code, kind))
                cur.execute("DELETE FROM tracked_asins WHERE asin = %s;", (old_asin,))
                if not keep_hist:
                    cur.execute("DELETE FROM asin_metrics WHERE asin = %s;", (old_asin,))
                d = dict_map.get(old_asin)
                mk_new = None
                tail = new_asin_raw.split(":")[-1].upper()
                if ":" in new_asin_raw and tail in MARKET_DOMAINS:
                    mk_new = tail
                else:
                    for k, dom in MARKET_DOMAINS.items():
                        if dom in new_asin_raw.lower():
                            mk_new = k
                if d or mk_new:
                    cur.execute(
                        """
                        INSERT INTO asin_dictionary (asin, parent_asin, category, subcategory, product_type, brand, market, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (asin) DO UPDATE SET
                            parent_asin = COALESCE(NULLIF(EXCLUDED.parent_asin, ''), asin_dictionary.parent_asin),
                            category = COALESCE(NULLIF(EXCLUDED.category, ''), asin_dictionary.category),
                            subcategory = COALESCE(NULLIF(EXCLUDED.subcategory, ''), asin_dictionary.subcategory),
                            product_type = COALESCE(NULLIF(EXCLUDED.product_type, ''), asin_dictionary.product_type),
                            brand = COALESCE(NULLIF(EXCLUDED.brand, ''), asin_dictionary.brand),
                            market = COALESCE(NULLIF(EXCLUDED.market, ''), asin_dictionary.market),
                            updated_at = NOW();
                        """,
                        (new_code, (d or {}).get("parent_asin", ""), (d or {}).get("category", ""),
                         (d or {}).get("subcategory", ""), (d or {}).get("product_type", ""),
                         (d or {}).get("brand", ""), mk_new or (d or {}).get("market", "") or ""))
                cur.execute("INSERT INTO asin_metrics (asin, source, note) VALUES (%s, %s, %s);",
                            (new_code, "none", f"заменил {old_asin}"))
            conn.commit()
            conn.close()
            st.success(f"{old_asin} → {new_code}. Запусти точечную проверку нового ASIN или дождись автосбора.")
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка замены: {e}")

    # ---- редактор списка выбранной страны ----
    st.markdown("---")
    st.markdown(f"**Список ({country})** — {len(display_tracked)} из {len(tracked_k)} "
                "<span class='muted'>· удалить — сотри из текста и пересохрани</span>", unsafe_allow_html=True)
    edited = st.text_area("Список", value=", ".join(display_tracked), height=140,
                          key=f"edit_tracked_list_{kind}_{country}_{len(display_tracked)}", label_visibility="collapsed")
    b1, b2, b3 = st.columns([1.2, 1.2, 3])
    if b1.button("💾 Пересохранить список", key=f"resave_btn_{kind}"):
        clean, dup_c, inv_c, markets, bd = parse_asin_batch(edited, [], None)
        if sel_market:
            for code in clean:
                markets.setdefault(code, sel_market)
        ensure_schema()
        try:
            conn = _conn()
            with conn.cursor() as cur:
                # удаляем только из текущего среза; в «Все страны» — полная замена
                to_remove = [a for a in display_tracked if a not in clean]
                for a in to_remove:
                    cur.execute("DELETE FROM tracked_asins WHERE asin = %s;", (a,))
                for code in clean:
                    cur.execute("INSERT INTO tracked_asins (asin, kind) VALUES (%s, %s) ON CONFLICT (asin) DO UPDATE SET kind = EXCLUDED.kind;", (code, kind))
            conn.commit()
            conn.close()
            save_markets(markets)
            msg = f"Сохранено {len(clean)} ASIN"
            if to_remove:
                msg += f" · снято с отслеживания: {len(to_remove)}"
            if bd:
                msg += f" · повторов в тексте убрано: {len(bd)}"
            if inv_c:
                msg += f" · нераспознано: {len(inv_c)} ({', '.join(inv_c[:5])}{' …' if len(inv_c) > 5 else ''})"
            st.success(msg)
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка сохранения: {e}")
    if b2.button(f"🗑️ Очистить список ({KIND_LABEL[kind]})", key=f"clear_btn_{kind}"):
        try:
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tracked_asins WHERE kind = %s;", (kind,))
            conn.commit()
            conn.close()
            st.success(f"Список «{KIND_LABEL[kind]}» очищен")
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка очистки: {e}")


# ---------- СБОР И УПРАВЛЕНИЕ ----------
with tab_ops:
    o1, o2 = st.columns(2)
    st.session_state.setdefault("use_api_mode", True)
    mode_c1, mode_c2 = st.columns([1.2, 3])
    st.session_state["use_api_mode"] = mode_c1.toggle(
        "Structured API", value=st.session_state["use_api_mode"],
        help="Amazon Product API Scrapingdog (JSON) вместо парсинга HTML")
    mode_c2.markdown(
        "<div class='muted' style='margin-top:22px'><b>Structured API</b> отдаёт рейтинг, число отзывов, "
        "<b>parent ASIN</b>, категорию и цену готовым JSON — надёжнее HTML и не ломается от вёрстки. "
        "Parent и категория при этом сами пишутся в справочник. Если API не ответил — откат на HTML-парсер.</div>",
        unsafe_allow_html=True)
    st.markdown("---")

    with o1:
        st.markdown("### Ручной прогон")
        st.caption("Сбор по всему списку прямо сейчас")
        if st.button(f"▶ Запустить прогон ({len(tracked)} ASIN)", type="primary"):
            if not tracked:
                st.warning("Список отслеживания пуст")
            else:
                run_collection(tracked, "Прогон")

        st.markdown("### Автосбор")
        saved_time = get_setting("auto_time", "13:00")
        try:
            _h, _m = (int(x) for x in str(saved_time).split(":")[:2])
        except Exception:
            _h, _m = 13, 0
        saved_on = str(get_setting("auto_enabled", "0")) == "1"

        t1, t2 = st.columns(2)
        target_daily_time = t1.time_input("Время ежедневного сбора", value=datetime.time(_h, _m),
                                          key="daily_run_time")
        t2.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        auto_timer_active = t2.checkbox("Автосбор включён", value=saved_on, key="auto_enabled_cb")

        # сохраняем только при реальном изменении
        if target_daily_time.strftime("%H:%M") != str(saved_time):
            set_setting("auto_time", target_daily_time.strftime("%H:%M"))
        if auto_timer_active != saved_on:
            set_setting("auto_enabled", "1" if auto_timer_active else "0")

        st.caption(f"Сохранено: {target_daily_time:%H:%M} {tz_short}, "
                   f"автосбор {'включён' if auto_timer_active else 'выключен'} — настройка хранится в базе "
                   "и переживает перезапуск.")
        st.warning("Автосбор в браузере срабатывает, только пока вкладка открыта. Для сбора без участия "
                   "человека нужен внешний запуск radar_scheduled.py по расписанию (cron / Railway / VPS) — "
                   "он читает то же время из базы.", icon="⚠️")

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
    with st.expander("👨‍👦 Подтянуть чайлдов из паренты", expanded=False):
        st.markdown("<div class='muted'>Structured API возвращает все варианты (цвета и размеры) одним запросом. "
                    "Вводишь парент или любой чайлд — получаешь весь список и добавляешь нужные в отслеживание.</div>",
                    unsafe_allow_html=True)
        k1, k2, k3 = st.columns([2, 1, 1])
        parent_in = k1.text_input("ASIN паренты или чайлда", key="kids_asin", placeholder="B0H8SFPK44 или ссылка")
        kids_market = k2.selectbox("Страна", options=list(MARKET_DOMAINS.keys()), index=1, key="kids_market")
        k3.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
        if k3.button("🔍 Найти", disabled=not parent_in.strip(), key="kids_find", use_container_width=True):
            with st.spinner("Запрашиваю API…"):
                try:
                    obj = fetch_product_json(parent_in, kids_market, log=lambda m: None)
                    st.session_state["kids_found"] = extract_children(obj) if obj else []
                    st.session_state["kids_parent"] = (obj or {}).get("parent_asin") or extract_asin(parent_in)
                except Exception as e:
                    st.error(f"Ошибка: {e}")

        kids = st.session_state.get("kids_found")
        if kids is not None:
            if not kids:
                st.warning("Вариантов не найдено — у товара нет вариаций или API не ответил")
            else:
                pa = st.session_state.get("kids_parent", "")
                st.success(f"Найдено вариантов: {len(kids)} · parent: {pa or '—'}")
                kdf = pd.DataFrame(kids)
                kdf["уже в базе"] = kdf["asin"].isin(tracked)
                st.dataframe(kdf.rename(columns={"asin": "ASIN", "value": "Вариант", "dim": "Измерение"}),
                             use_container_width=True, hide_index=True, height=260)
                fresh = [k["asin"] for k in kids if k["asin"] not in tracked]
                kk1, kk2 = st.columns([1, 1])
                add_kind = kk1.radio("Добавить как", ["Чайлд", "Парент"], horizontal=True, key="kids_kind")
                if kk2.button(f"➕ Добавить {len(fresh)} новых", type="primary", disabled=not fresh,
                              key="kids_add", use_container_width=True):
                    kind_val = "child" if add_kind == "Чайлд" else "parent"
                    try:
                        ensure_schema()
                        conn = _conn()
                        with conn.cursor() as cur:
                            for a in fresh:
                                cur.execute("INSERT INTO tracked_asins (asin, kind) VALUES (%s, %s) "
                                            "ON CONFLICT (asin) DO UPDATE SET kind = EXCLUDED.kind;", (a, kind_val))
                        conn.commit()
                        conn.close()
                        save_markets({a: kids_market for a in fresh})
                        if pa:
                            ensure_dict_table()
                            conn = _conn()
                            with conn.cursor() as cur:
                                for a in fresh:
                                    cur.execute(
                                        "INSERT INTO asin_dictionary (asin, parent_asin) VALUES (%s, %s) "
                                        "ON CONFLICT (asin) DO UPDATE SET parent_asin = "
                                        "COALESCE(NULLIF(asin_dictionary.parent_asin,''), EXCLUDED.parent_asin), "
                                        "updated_at = NOW();", (a, pa))
                            conn.commit()
                            conn.close()
                        st.success(f"Добавлено {len(fresh)} позиций")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ошибка: {e}")

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

    with st.expander("📥 Загрузка ASIN — 📋 Портфель (Чайлд)", expanded=False):
        render_asin_manager("child")
    with st.expander("📥 Загрузка ASIN — 📋 Портфель (Парент)", expanded=False):
        render_asin_manager("parent")

    st.markdown("---")
    with st.expander("🔔 Telegram-уведомления (@RatingRadar_bot)", expanded=False):
        if not NOTIFIER_OK:
            st.error("Модуль notifier.py не найден или не импортируется")
        elif not notifier.BOT_TOKEN:
            st.warning("Не задан TELEGRAM_BOT_TOKEN — добавь его в Secrets приложения (или в .env локально)")
        else:
            st.session_state.setdefault("tg_notify_on", True)
            t1, t2, t3 = st.columns([1.4, 1.4, 2])
            st.session_state["tg_notify_on"] = t1.toggle("Слать после каждого прогона",
                                                         value=st.session_state["tg_notify_on"])
            try:
                subs = notifier.get_subscribers(active_only=False)
            except Exception as e:
                subs = pd.DataFrame()
                st.error(f"Не читаются подписчики: {e}")
            active_n = int(subs["active"].sum()) if not subs.empty else 0
            t2.metric("Подписчиков", f"{active_n}", delta=f"всего {len(subs)}", delta_color="off")
            t3.markdown(
                "<div style='margin-top:16px'>"
                "<a href='https://t.me/RatingRadar_bot' target='_blank' "
                "style='display:inline-block;background:#229ED9;color:#fff;padding:7px 16px;border-radius:999px;"
                "font-size:14px;font-weight:600;text-decoration:none'>✈️ Открыть @RatingRadar_bot</a>"
                "<div class='muted' style='margin-top:6px'>Подписка — <code>/start</code> в боте. "
                "Фильтры: <code>/kind</code>, <code>/country</code>, <code>/drop</code>, <code>/only_status</code>.</div>"
                "</div>", unsafe_allow_html=True)

            b1, b2, b3 = st.columns([1.2, 1.2, 2])
            if b1.button("📤 Отправить отчёт сейчас", key="tg_send_now"):
                try:
                    sent, total = notifier.notify_all(header="Rating Radar — отчёт по запросу",
                                                      silent_if_empty=False)
                    st.success(f"Отправлено {sent} из {total}")
                except Exception as e:
                    st.error(f"Ошибка: {e}")
            with st.expander("🩺 Диагностика бота", expanded=False):
                if st.button("Проверить связь", key="tg_diag"):
                    d = notifier.diagnose()
                    ok_token = d.get("bot_username")
                    if ok_token:
                        st.success(f"Бот на связи: @{ok_token} · токен {d['token_tail']}")
                    else:
                        st.error(f"Бот не отвечает. {d.get('error') or d.get('getMe')}")
                    if d.get("webhook_url"):
                        st.error(f"Установлен вебхук: {d['webhook_url']} — из-за него getUpdates не работает. "
                                 "Нажми «Снять вебхук» ниже.")
                    if d.get("getUpdates_error"):
                        st.error(f"getUpdates: {d['getUpdates_error']}")
                    st.write({
                        "токен виден": d.get("token_present"),
                        "DATABASE_URL виден": d.get("db_present"),
                        "offset": d.get("offset"),
                        "необработанных сообщений": d.get("pending"),
                        "они": d.get("pending_texts"),
                        "подписчиков в базе": d.get("subscribers"),
                    })
                d1, d2 = st.columns(2)
                if d1.button("Снять вебхук", key="tg_drop_wh"):
                    st.write(notifier.drop_webhook())
                if d2.button("Сбросить offset на 0", key="tg_reset_off",
                             help="Перечитать всю очередь Telegram с начала (последние 24ч)"):
                    notifier.reset_offset(0)
                    st.success("offset = 0, теперь нажми «Проверить новые команды»")

            if b1.button("🔄 Проверить новые команды", key="tg_poll"):
                try:
                    n = notifier.process_updates()
                    if n:
                        st.success(f"Обработано команд: {n}")
                        st.rerun()
                    else:
                        st.info("Новых команд нет. Если бот молчит — открой «Диагностика бота».")
                except Exception as e:
                    st.error(f"Ошибка: {e}")
            if b2.button("👁 Показать текст отчёта", key="tg_preview"):
                try:
                    al = notifier.build_alerts()
                    st.code(notifier.format_report(al, header="Rating Radar"), language="html")
                    st.caption(f"Алертов: {len(al)}")
                except Exception as e:
                    st.error(f"Ошибка: {e}")
            bc = b3.text_input("Разослать своё сообщение", key="tg_broadcast",
                               placeholder="текст всем подписчикам…")
            if bc.strip() and b3.button("Разослать", key="tg_bc_btn"):
                try:
                    ok_n, total = notifier.broadcast(bc.strip())
                    st.success(f"Разослано {ok_n} из {total}")
                except Exception as e:
                    st.error(f"Ошибка: {e}")

            if not subs.empty:
                show = subs.copy()
                for col, fmt_ in (("created_at", "%d.%m.%Y"), ("last_sent_at", "%d.%m %H:%M")):
                    if col in show.columns:
                        show[col] = pd.to_datetime(show[col], utc=True, errors="coerce").dt.strftime(fmt_)
                    else:
                        show[col] = "—"
                cols_map = {"username": "Юзернейм", "first_name": "Имя", "kinds": "Тип", "countries": "Страны",
                            "min_drop": "Порог ★", "only_status_change": "Только смена", "active": "Активен",
                            "created_at": "Подписан", "last_sent_at": "Последняя отправка"}
                have = [c for c in cols_map if c in show.columns]
                show = show[have].rename(columns=cols_map)
                st.dataframe(show, use_container_width=True, hide_index=True, height=220)
            else:
                st.caption("Подписчиков пока нет — открой бота и отправь /start")

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


# ---------- КАК ЭТО РАБОТАЕТ ----------
with tab_help:
    st.markdown(
        """
<style>
.hw h2 { font-size:22px; font-weight:700; margin:22px 0 8px; letter-spacing:-.02em; }
.hw h3 { font-size:15px; font-weight:650; margin:14px 0 6px; }
.hw p, .hw li { font-size:14px; line-height:1.5; color:#1d1d1f; }
.hw .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; margin:8px 0 6px; }
.hw .card { background:#fff; border:1px solid #e5e5ea; border-radius:12px; padding:14px 16px; }
.hw .card .t { font-weight:650; font-size:14px; margin-bottom:6px; }
.hw .card .d { font-size:13px; color:#3a3a3c; line-height:1.45; }
.hw code { background:#f2f2f7; padding:1px 6px; border-radius:4px; font-size:12.5px; }
.hw .flow { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0 14px; }
.hw .step { background:#fff; border:1px solid #e5e5ea; border-radius:999px; padding:6px 14px; font-size:13px; font-weight:600; }
.hw .arr { color:#8e8e93; }
.hw table { border-collapse:collapse; font-size:13px; margin:6px 0 10px; }
.hw th, .hw td { border-bottom:1px solid #e5e5ea; padding:6px 12px; text-align:left; vertical-align:top; }
.hw th { color:#6e6e73; font-weight:600; background:#f5f5f7; }
.hw .b { display:inline-block; padding:1px 9px; border-radius:999px; color:#fff; font-size:12px; font-weight:600; }
.hw .note { background:#fff8e1; border:1px solid #ffe082; border-radius:10px; padding:10px 14px; font-size:13px; margin:10px 0; }
.hw .ok { background:#e8f5e9; border:1px solid #a5d6a7; border-radius:10px; padding:10px 14px; font-size:13px; margin:10px 0; }
</style>
<div class="hw">

<h2>Что это</h2>
<p><b>Rating Radar</b> — мониторинг индивидуальных рейтингов чайлд-ASIN. На основных маркетплейсах рейтинг чайлда скрыт под парентом, поэтому радар снимает его с витрины напрямую и копит историю по дням: рейтинг, число оценок, распределение звёзд, BSR. На этой истории строятся статусы, динамика, детектор бота возвратов и прогноз.</p>

<h2>Как идёт сбор</h2>
<div class="flow">
  <span class="step">1. Список ASIN</span><span class="arr">→</span>
  <span class="step">2. Страна из справочника</span><span class="arr">→</span>
  <span class="step">3. Витрина Amazon</span><span class="arr">→</span>
  <span class="step">4. Парсинг метрик</span><span class="arr">→</span>
  <span class="step">5. База (история)</span><span class="arr">→</span>
  <span class="step">6. Дашборд</span>
</div>
<div class="grid">
  <div class="card"><div class="t">Когда</div><div class="d">Автосбор раз в сутки в заданное время (по умолчанию 13:00 Киев, вкладка «Сбор и управление») — либо вручную кнопкой «Запустить прогон». Отдельно — «Точечная проверка» для нескольких позиций и «Обновить выбранные» из таблицы портфеля.</div></div>
  <div class="card"><div class="t">Откуда</div><div class="d">Если у ASIN в справочнике стоит страна (например <code>US</code>) — коллектор идёт на <code>amazon.com</code>. Если страны нет — каскад <code>amazon.com.be → amazon.nl</code> (там чайлды показываются отдельно). Если и там пусто — фолбэк по письменным ревью, помечается как «неполные данные».</div></div>
  <div class="card"><div class="t">Что снимается</div><div class="d">Рейтинг (0.1), число оценок, гистограмма звёзд в %, BSR, фото. Каждый замер пишется отдельной строкой — ничего не перезаписывается, история сохраняется полностью.</div></div>
</div>

<h2>Статусы и цвета</h2>
<p>Пороги — по логике Amazon, одинаковые во всех вкладках:</p>
<table>
<tr><th>Статус</th><th>Рейтинг</th><th>Что значит</th></tr>
<tr><td><span class="b" style="background:#1f8a4c">ОК</span></td><td>≥ 4.5</td><td>Норма, ничего не делаем</td></tr>
<tr><td><span class="b" style="background:#c77800">Внимание</span></td><td>4.3 – 4.4</td><td>Рейтинг начал проседать, смотрим на негатив и запас</td></tr>
<tr><td><span class="b" style="background:#d13438">Риск</span></td><td>≤ 4.2</td><td>Красная зона, нужны меры</td></tr>
<tr><td><span class="b" style="background:#8e8e93">Нет данных</span></td><td>—</td><td>Витрина не отдала рейтинг (нет оценок, вариация, капча)</td></tr>
</table>
<h3>Дополнительные показатели</h3>
<ul>
  <li><b>1–2★ %</b> — доля единиц и двоек из гистограммы. Подсветка: &gt; 8% жёлтая, &gt; 15% красная.</li>
  <li><b>Запас (до 4.0)</b> — сколько единичных оценок выдержит рейтинг, прежде чем упадёт к 4.0. Формула: <code>оценок × (рейтинг − 4.0) / 3</code>. Чем меньше — тем уязвимее позиция.</li>
  <li><b>Δ★ / Δ отз.</b> — изменение к предыдущему замеру.</li>
  <li><b>Тренд</b> — ↓ если негатив &gt; 20% или рейтинг &lt; 4.2; ↑ если негатив &lt; 8% и рейтинг ≥ 4.5.</li>
</ul>

<h2>Фильтры и группировка</h2>
<p>Панель сверху действует на все вкладки сразу: ASIN, категория, parent, страна, статус, тумблер «Только США», часовой пояс, период истории. <b>«Группировать по»</b> включает разрез по категории / подкатегории / виду / parent / бренду / стране — появляется сводка по группам в портфеле, заголовки групп в таблице по дням, разрез по группам в аналитике и в боте.</p>

<h2>Справочник</h2>
<p>Группы и страны берутся из spr (Google Sheet или CSV/XLSX), загрузка во вкладке «Сбор и управление» → «Справочник». Колонки распознаются по названию:</p>
<table>
<tr><th>В радаре</th><th>Колонка в spr</th></tr>
<tr><td>Категория</td><td><code>Category+parent</code> (или <code>категория</code>, <code>category</code>)</td></tr>
<tr><td>Подкатегория</td><td><code>Parent group</code> (или <code>subcategory</code>)</td></tr>
<tr><td>Вид</td><td><code>тип</code> / <code>type</code> / <code>вид</code></td></tr>
<tr><td>Parent</td><td><code>Parent ASIN</code></td></tr>
<tr><td>Страна</td><td><code>market</code> / <code>country</code> / <code>страна</code> — US, BE, NL, DE, UK, FR, IT, ES</td></tr>
</table>
<p>Строки с пометкой в «Архив» пропускаются. Режим «дополнить» обновляет существующие записи и добавляет новые, «заменить» — стирает справочник и грузит заново.</p>

<h2>Вкладки</h2>
<div class="grid">
  <div class="card"><div class="t">📋 Портфель</div><div class="d">Текущее состояние каждой позиции: статус, рейтинг, оценки, дельты, негатив, запас, BSR. Галочки → массовое обновление или удаление. Вид «Карточки» — то же с фото. Выгрузка CSV.</div></div>
  <div class="card"><div class="t">📅 Динамика по дням</div><div class="d">Таблица ASIN × даты, как в гугл-шите: Rating / BSR / Reviews / 1–2★. Рейтинг закрашен по статусу, зелёные Reviews — выросли, красный BSR — просел на 15%+. Шаг день или неделя, сортировка по падению за период. Выгрузка в Excel с цветами.</div></div>
  <div class="card"><div class="t">📊 Аналитика</div><div class="d">Портфель целиком: распределение по статусам, средний рейтинг и % риска по дням, рейтинг × оценки (кто крупный и падает), топ по негативу, самый тонкий запас, разрез по странам и группам, тепловая карта Δ рейтинга, прирост оценок.</div></div>
  <div class="card"><div class="t">🤖 Бот возвратов</div><div class="d">Детектор ИИ-бота возвратов Amazon — см. ниже.</div></div>
  <div class="card"><div class="t">📈 Прогноз</div><div class="d">Линейный тренд по истории ASIN: рейтинг и оценки на 7–90 дней вперёд, 95% интервал, «дней до 4.2», скорость оценок в день, гистограмма звёзд. Сводный прогноз по портфелю на +30 дней — кто пробьёт красную зону.</div></div>
  <div class="card"><div class="t">⚙️ Сбор и управление</div><div class="d">Ручной прогон, автосбор, точечная проверка, справочник, список ASIN (добавление с проверкой дублей, редактирование, очистка), история прогонов.</div></div>
</div>

<h2>Детектор бота возвратов</h2>
<p><b>Гипотеза.</b> Новый ИИ-бот Amazon при возврате просит покупателя поставить оценку без текста. Такие оценки — в основном 1–2★. На витрине это выглядит так: <b>рейтинг падает, а число оценок почти не растёт</b>. Обычный негативный отзыв двигает обе цифры; бот двигает только рейтинг.</p>
<h3>Что считаем между двумя замерами</h3>
<ul>
  <li><b>Новых оценок</b> = оценок сейчас − оценок в прошлый раз.</li>
  <li><b>Входящий рейтинг</b> = средний балл именно новых оценок: <code>(R₂·N₂ − R₁·N₁) / (N₂ − N₁)</code>. Если по портфелю он ниже ~3 — заходит негатив.</li>
  <li><b>Новых 1–2★</b> ≈ Δ(% негатива × оценок) — сколько единиц/двоек добавилось.</li>
  <li><b>Аномалия</b> = рейтинг упал на ≥ порог (по умолчанию 0.1) <u>и</u> оценок прибавилось меньше порога % от базы (по умолчанию 0.5%). Оба порога настраиваются.</li>
</ul>
<h3>Что показываем</h3>
<ul>
  <li>KPI «до / после» даты внедрения (по умолчанию 20.08.2026): новых оценок, из них негатив, доля негатива во входящих, входящий рейтинг, число аномалий.</li>
  <li>Прирост оценок и негатива по дням/неделям с линией внедрения; входящий рейтинг и аномалии по периодам.</li>
  <li>Карта аномалий: каждая точка — замер ASIN, ось X прирост оценок %, ось Y Δ рейтинга; красная зона — «упал без роста».</li>
  <li>Таблица аномалий с CSV; разрез по группам и по ASIN до/после.</li>
</ul>
<div class="note"><b>Оговорка про точность.</b> Витринный рейтинг округлён до 0.1, гистограмма — до 1%. На большой базе (тысячи оценок) падение на 0.1 при +2 оценках математически означает, что реальный рейтинг был у границы округления — поэтому «входящий рейтинг» и «новых 1–2★» это оценка, не точный счёт. Сигнал надёжный на уровне портфеля и категорий, на уровне одного крупного ASIN — смотрим серию замеров, не один. Чем чаще сбор — тем точнее.</div>

<h2>Алерты в Telegram</h2>
<p>Бот <a href="https://t.me/RatingRadar_bot" target="_blank"><b>@RatingRadar_bot</b></a> — подписка командой <code>/start</code>.
После каждого сбора присылает: смену цвета листинга, падение рейтинга и аномалии бота возвратов.
Настройки подписки прямо в чате: <code>/kind child|parent|all</code>, <code>/country US,BE</code>,
<code>/drop 0.1</code> (порог падения), <code>/only_status on</code> (только смена цвета),
<code>/report</code> (отчёт по запросу), <code>/status</code> (мои настройки и сводка), <code>/stop</code>.</p>

<h2>Типовой сценарий</h2>
<ol>
  <li>Загрузить spr → включить «Группировать по: Категория».</li>
  <li>Добавить US-ASIN пачкой (страна US) → запустить прогон.</li>
  <li>Через несколько дней: «Портфель» — кто ушёл в жёлтую/красную зону; «Динамика» — где именно упало; «Бот возвратов» — вырос ли негатив после внедрения и в каких категориях.</li>
  <li>«Прогноз» — кому осталось меньше всего дней до 4.2, туда усилия в первую очередь.</li>
</ol>
<div class="ok"><b>Ограничение источника.</b> Официальный API Amazon отдаёт только текстовые отзывы, не оценки — поэтому всё берётся с витрины через скрейпинг. Это же причина, почему иногда встречается «Нет данных»: капча, вариация, временная недоступность. Такие позиции подхватятся на следующем прогоне.</div>

</div>
""",
        unsafe_allow_html=True,
    )
