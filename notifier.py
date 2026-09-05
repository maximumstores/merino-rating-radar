#!/usr/bin/env python3
"""Rating Radar — Telegram-уведомления.

Две части:
  * подписчики (таблица telegram_subscribers) — кто получает алерты и с какими фильтрами;
  * расчёт алертов по последнему прогону + отправка.

Токен берётся из переменной окружения TELEGRAM_BOT_TOKEN (Streamlit Secrets / .env).
Никогда не хардкодить токен в файле — репозиторий приватный, но токен утекает в историю git.
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()


def _cfg(name, default=""):
    """env → .env → st.secrets (Streamlit Cloud кладёт секреты только туда)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st  # noqa: WPS433 — опционально, вне Streamlit не нужен
        val = st.secrets.get(name)
        if val:
            os.environ[name] = str(val)   # чтобы collector.py и остальные тоже увидели
            return str(val)
    except Exception:
        pass
    return default


BOT_TOKEN = _cfg("TELEGRAM_BOT_TOKEN")
DATABASE_URL = _cfg("DATABASE_URL")
API = "https://api.telegram.org/bot{token}/{method}"

# пороги алертов (можно переопределить через env)
DROP_THRESHOLD = float(_cfg("ALERT_DROP", "0.1"))       # падение рейтинга, ★
GROWTH_THRESHOLD = float(_cfg("ALERT_GROWTH", "0.5"))   # прирост оценок, % от базы
RISK_LEVEL = 4.24
WARN_LEVEL = 4.45

SUBS_SQL = """
CREATE TABLE IF NOT EXISTS telegram_subscribers (
    chat_id      BIGINT PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    kinds        TEXT NOT NULL DEFAULT 'child,parent',
    countries    TEXT NOT NULL DEFAULT 'all',
    min_drop     NUMERIC(3,2) NOT NULL DEFAULT 0.10,
    only_status_change BOOLEAN NOT NULL DEFAULT FALSE,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS telegram_bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------- инфраструктура
def conn():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задан")
    return psycopg2.connect(DATABASE_URL)


def ensure_subs_schema():
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(SUBS_SQL)
        c.commit()


def get_state(key, default=None):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM telegram_bot_state WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
    except Exception:
        return default


def set_state(key, value):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_bot_state (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, str(value)),
            )
        c.commit()


def get_subscribers(active_only=True):
    ensure_subs_schema()
    q = "SELECT chat_id, username, first_name, kinds, countries, min_drop, only_status_change, active, created_at " \
        "FROM telegram_subscribers"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY created_at"
    with conn() as c:
        return pd.read_sql(q, c)


def upsert_subscriber(chat_id, username=None, first_name=None, active=True):
    ensure_subs_schema()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telegram_subscribers (chat_id, username, first_name, active)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, telegram_subscribers.username),
                    first_name = COALESCE(EXCLUDED.first_name, telegram_subscribers.first_name),
                    active = EXCLUDED.active;
                """,
                (chat_id, username, first_name, active),
            )
        c.commit()


def set_subscriber_field(chat_id, field, value):
    allowed = {"kinds", "countries", "min_drop", "only_status_change", "active"}
    if field not in allowed:
        raise ValueError(f"нельзя менять поле {field}")
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(f"UPDATE telegram_subscribers SET {field} = %s WHERE chat_id = %s", (value, chat_id))
        c.commit()


# ---------------------------------------------------------------- отправка
def tg_call(method, **payload):
    if not BOT_TOKEN:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN не задан"}
    try:
        r = requests.post(API.format(token=BOT_TOKEN, method=method), json=payload, timeout=30)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


def send_message(chat_id, text, disable_preview=True):
    return tg_call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                   disable_web_page_preview=disable_preview)


# ---------------------------------------------------------------- расчёт алертов
MARKET_DOMAINS = {
    "US": "amazon.com", "BE": "amazon.com.be", "NL": "amazon.nl", "DE": "amazon.de",
    "UK": "amazon.co.uk", "FR": "amazon.fr", "IT": "amazon.it", "ES": "amazon.es",
}


def _status(rating):
    if rating is None or pd.isna(rating):
        return "нет данных", "⚪"
    if rating <= RISK_LEVEL:
        return "риск", "🔴"
    if rating < WARN_LEVEL:
        return "внимание", "🟡"
    return "ок", "🟢"


def _bad_pct(raw):
    if not raw:
        return None
    try:
        h = json.loads(raw) if isinstance(raw, str) else raw
        return int(h.get("1", 0)) + int(h.get("2", 0))
    except Exception:
        return None


def build_alerts():
    """Сравнивает последний замер каждого ASIN с предыдущим. Возвращает DataFrame алертов."""
    with conn() as c:
        df = pd.read_sql(
            """
            SELECT asin, source, rating, review_count, histogram_json, created_at
            FROM asin_metrics
            WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
            ORDER BY created_at ASC;
            """, c)
        try:
            kinds = pd.read_sql("SELECT asin, COALESCE(kind,'child') AS kind FROM tracked_asins", c)
        except Exception:
            kinds = pd.DataFrame(columns=["asin", "kind"])
        try:
            dic = pd.read_sql("SELECT asin, category, market FROM asin_dictionary", c)
        except Exception:
            dic = pd.DataFrame(columns=["asin", "category", "market"])

    if df.empty:
        return pd.DataFrame()

    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df["review_count"] = pd.to_numeric(df["review_count"], errors="coerce")
    df["bad_pct"] = df["histogram_json"].apply(_bad_pct)

    last = df.groupby("asin").tail(1).set_index("asin")
    prev = df.groupby("asin").nth(-2)
    prev = prev.set_index("asin") if "asin" in prev.columns else prev

    kind_map = dict(zip(kinds["asin"], kinds["kind"])) if not kinds.empty else {}
    cat_map = dict(zip(dic["asin"], dic["category"])) if not dic.empty else {}
    mkt_map = dict(zip(dic["asin"], dic["market"])) if not dic.empty else {}

    rows = []
    for asin, r in last.iterrows():
        if asin not in prev.index:
            continue
        p = prev.loc[asin]
        rating, prev_rating = r["rating"], p["rating"]
        cnt, prev_cnt = r["review_count"], p["review_count"]
        if pd.isna(rating) or pd.isna(prev_rating):
            continue

        d_rating = float(rating) - float(prev_rating)
        d_cnt = (int(cnt) - int(prev_cnt)) if pd.notnull(cnt) and pd.notnull(prev_cnt) else None
        growth_pct = (d_cnt / prev_cnt * 100) if d_cnt is not None and prev_cnt else None

        st_now, ic_now = _status(rating)
        st_prev, _ = _status(prev_rating)
        status_changed = st_now != st_prev

        bot_anomaly = (d_rating <= -DROP_THRESHOLD
                       and growth_pct is not None and growth_pct <= GROWTH_THRESHOLD)

        if not (status_changed or d_rating <= -DROP_THRESHOLD or bot_anomaly):
            continue

        market = mkt_map.get(asin) or (r["source"] if r["source"] in MARKET_DOMAINS else None)
        rows.append({
            "asin": asin,
            "kind": kind_map.get(asin, "child"),
            "country": market or "—",
            "category": cat_map.get(asin) or "",
            "rating": float(rating),
            "prev_rating": float(prev_rating),
            "d_rating": d_rating,
            "reviews": int(cnt) if pd.notnull(cnt) else None,
            "d_reviews": d_cnt,
            "growth_pct": growth_pct,
            "bad_pct": r["bad_pct"],
            "status": st_now,
            "icon": ic_now,
            "status_changed": status_changed,
            "prev_status": st_prev,
            "bot_anomaly": bool(bot_anomaly),
            "url": f"https://www.{MARKET_DOMAINS.get(market or r['source'], 'amazon.com.be')}/dp/{asin}",
            "checked_at": r["created_at"],
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["bot_anomaly", "d_rating"], ascending=[False, True])
    return out


def format_report(alerts, kind_label=None, header="Rating Radar"):
    if alerts.empty:
        return f"<b>{header}</b>\nПрогон завершён — изменений по порогам нет."

    bots = alerts[alerts["bot_anomaly"]]
    drops = alerts[(~alerts["bot_anomaly"]) & (alerts["d_rating"] < 0)]
    ups = alerts[alerts["d_rating"] > 0]

    lines = [f"<b>{header}</b>"]
    if kind_label:
        lines.append(f"<i>{kind_label}</i>")
    lines.append("")

    def block(title, part, limit=12, mark=""):
        if part.empty:
            return
        lines.append(f"<b>{title} — {len(part)}</b>")
        for _, a in part.head(limit).iterrows():
            cat = f" · {a['category']}" if a["category"] else ""
            dr = f"{a['prev_rating']:.1f} → {a['rating']:.1f}"
            dc = f", оценок {a['d_reviews']:+d}" if a["d_reviews"] is not None else ""
            st_txt = f" [{a['prev_status']} → {a['status']}]" if a["status_changed"] else ""
            lines.append(f"{a['icon']} <a href=\"{a['url']}\">{a['asin']}</a> {a['country']}{cat}\n"
                         f"    {dr}{dc}{st_txt} {mark}")
        if len(part) > limit:
            lines.append(f"    …ещё {len(part) - limit}")
        lines.append("")

    block("🤖 Аномалии бота возвратов", bots, mark="— рейтинг упал без роста оценок")
    block("📉 Падение рейтинга", drops)
    block("📈 Рост", ups, limit=6)

    lines.append(f"<i>{datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC</i>")
    return "\n".join(lines)


def _filter_for_subscriber(alerts, sub):
    if alerts.empty:
        return alerts
    out = alerts.copy()
    kinds = [k.strip() for k in str(sub["kinds"]).split(",") if k.strip()]
    if kinds and set(kinds) != {"child", "parent"}:
        out = out[out["kind"].isin(kinds)]
    countries = str(sub["countries"]).strip()
    if countries and countries != "all":
        allowed = [c.strip().upper() for c in countries.split(",") if c.strip()]
        out = out[out["country"].isin(allowed)]
    min_drop = float(sub["min_drop"] or 0)
    if min_drop > 0:
        out = out[(out["d_rating"] <= -min_drop) | out["bot_anomaly"] | out["status_changed"]]
    if bool(sub["only_status_change"]):
        out = out[out["status_changed"] | out["bot_anomaly"]]
    return out


def notify_all(header="Rating Radar — прогон завершён", silent_if_empty=True):
    """Считает алерты и рассылает подписчикам с учётом их фильтров. Возвращает (отправлено, всего подписчиков)."""
    ensure_subs_schema()
    alerts = build_alerts()
    subs = get_subscribers(active_only=True)
    sent = 0
    for _, sub in subs.iterrows():
        part = _filter_for_subscriber(alerts, sub)
        if part.empty and silent_if_empty:
            continue
        kinds = str(sub["kinds"])
        label = None
        if kinds == "child":
            label = "Чайлд"
        elif kinds == "parent":
            label = "Парент"
        res = send_message(int(sub["chat_id"]), format_report(part, label, header))
        if res.get("ok"):
            sent += 1
            with conn() as c:
                with c.cursor() as cur:
                    cur.execute("UPDATE telegram_subscribers SET last_sent_at = now() WHERE chat_id = %s",
                                (int(sub["chat_id"]),))
                c.commit()
    return sent, len(subs)


def broadcast(text):
    """Ручная рассылка произвольного текста всем активным подписчикам."""
    subs = get_subscribers(active_only=True)
    ok = 0
    for _, sub in subs.iterrows():
        if send_message(int(sub["chat_id"]), text).get("ok"):
            ok += 1
    return ok, len(subs)


# ---------------------------------------------------------------- команды бота
HELP = """<b>Rating Radar</b> — алерты по рейтингам Amazon.

Присылаю после каждого прогона: смену цвета листинга (\U0001F7E2\U0001F7E1\U0001F534), падение рейтинга и аномалии бота возвратов (рейтинг упал, а оценок почти не прибавилось).

<b>Команды</b>
/start — подписаться
/stop — отписаться
/status — мои настройки и сводка портфеля
/report — отчёт по последнему прогону
/kind child|parent|all — чайлды, паренты или всё
/country US,BE или all — по каким странам
/drop 0.1 — порог падения рейтинга
/only_status on|off — только смена цвета и аномалии
/help — эта справка"""


def get_subscriber(chat_id):
    try:
        with conn() as c:
            df = pd.read_sql("SELECT * FROM telegram_subscribers WHERE chat_id = %s", c, params=(chat_id,))
        return df.iloc[0] if not df.empty else None
    except Exception:
        return None


def portfolio_summary():
    try:
        with conn() as c:
            df = pd.read_sql(
                """
                SELECT DISTINCT ON (asin) asin, rating FROM asin_metrics
                WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
                ORDER BY asin, created_at DESC;
                """, c)
            run = pd.read_sql(
                "SELECT started_at, ok_count, asin_count FROM collection_runs ORDER BY started_at DESC LIMIT 1", c)
    except Exception:
        return "не удалось прочитать базу"
    if df.empty:
        return "данных пока нет"
    r = pd.to_numeric(df["rating"], errors="coerce")
    line = (f"\U0001F7E2 {int((r >= WARN_LEVEL).sum())} · \U0001F7E1 {int(((r > RISK_LEVEL) & (r < WARN_LEVEL)).sum())} · "
            f"\U0001F534 {int((r <= RISK_LEVEL).sum())} · \u26AA {int(r.isna().sum())} · средний {r.mean():.2f}")
    if not run.empty:
        t = pd.to_datetime(run.iloc[0]["started_at"])
        line += (f"\nПоследний сбор: {t:%d.%m %H:%M} UTC "
                 f"({int(run.iloc[0]['ok_count'] or 0)}/{int(run.iloc[0]['asin_count'] or 0)})")
    return line


def handle_command(msg):
    """Обрабатывает одно входящее сообщение Telegram."""
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    user = msg.get("from", {}) or {}
    if not chat_id:
        return
    if not text.startswith("/"):
        send_message(chat_id, HELP)
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start":
        upsert_subscriber(chat_id, user.get("username"), user.get("first_name"), active=True)
        send_message(chat_id, "\u2705 Подписка оформлена. Буду присылать отчёт после каждого сбора.\n\n" + HELP)

    elif cmd == "/stop":
        upsert_subscriber(chat_id, user.get("username"), user.get("first_name"), active=False)
        send_message(chat_id, "Отписал. /start — включить снова.")

    elif cmd == "/status":
        sub = get_subscriber(chat_id)
        if sub is None:
            send_message(chat_id, "Ты не подписан. /start — подписаться.")
            return
        send_message(chat_id,
                     f"<b>Подписка</b>: {'активна' if sub['active'] else 'выключена'}\n"
                     f"Тип: <code>{sub['kinds']}</code>\n"
                     f"Страны: <code>{sub['countries']}</code>\n"
                     f"Порог падения: <code>{float(sub['min_drop']):.2f}\u2605</code>\n"
                     f"Только смена цвета: <code>{'да' if sub['only_status_change'] else 'нет'}</code>\n\n"
                     f"<b>Портфель</b>\n{portfolio_summary()}")

    elif cmd == "/report":
        send_message(chat_id, format_report(build_alerts(), header="Rating Radar — по запросу"))

    elif cmd == "/kind":
        val = {"child": "child", "чайлд": "child", "parent": "parent", "парент": "parent",
               "all": "child,parent", "все": "child,parent"}.get(arg.lower())
        if not val:
            send_message(chat_id, "Формат: /kind child | parent | all")
            return
        set_subscriber_field(chat_id, "kinds", val)
        send_message(chat_id, f"Тип обновлён: <code>{val}</code>")

    elif cmd == "/country":
        val = "all" if arg.lower() in ("all", "все", "") else ",".join(
            c.strip().upper() for c in arg.split(",") if c.strip())
        set_subscriber_field(chat_id, "countries", val)
        send_message(chat_id, f"Страны обновлены: <code>{val}</code>")

    elif cmd == "/drop":
        try:
            v = float(arg.replace(",", "."))
            assert 0 <= v <= 5
        except Exception:
            send_message(chat_id, "Формат: /drop 0.1")
            return
        set_subscriber_field(chat_id, "min_drop", v)
        send_message(chat_id, f"Порог падения: <code>{v:.2f}\u2605</code>")

    elif cmd == "/only_status":
        v = arg.lower() in ("on", "да", "true", "1", "yes")
        set_subscriber_field(chat_id, "only_status_change", v)
        send_message(chat_id, f"Только смена цвета и аномалии: <code>{'да' if v else 'нет'}</code>")

    else:
        send_message(chat_id, HELP)


def process_updates(timeout=0, max_updates=50):
    """Разбирает накопившиеся команды одним запросом (без отдельного воркера).

    Вызывается из дашборда при загрузке страницы и после прогона. Возвращает
    число обработанных сообщений. Безопасно вызывать часто — при пустой очереди
    это один быстрый HTTP-запрос.
    """
    if not BOT_TOKEN:
        return 0
    ensure_subs_schema()
    offset = int(get_state("update_offset", 0) or 0)
    res = tg_call("getUpdates", offset=offset + 1, timeout=timeout,
                  limit=max_updates, allowed_updates=["message"])
    if not res.get("ok"):
        return 0
    handled = 0
    for upd in res.get("result", []):
        offset = max(offset, upd["update_id"])
        if "message" in upd:
            try:
                handle_command(upd["message"])
                handled += 1
            except Exception:
                pass
    if res.get("result"):
        set_state("update_offset", offset)
    return handled


if __name__ == "__main__":
    ensure_subs_schema()
    print("команд обработано:", process_updates())
    n, total = notify_all(silent_if_empty=False)
    print(f"отправлено {n} из {total}")
