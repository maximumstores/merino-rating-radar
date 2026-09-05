#!/usr/bin/env python3
"""Rating Radar — AI-слой: недельный дайджест и разбор причин негатива.

Использует Anthropic API напрямую через requests (без доп. зависимостей).
Ключ — ANTHROPIC_API_KEY (env / .env / st.secrets).

Принцип: модель НЕ считает цифры. Все агрегаты считаются на нашей стороне
и подаются готовыми; задача модели — интерпретация и приоритеты.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()


def _cfg(name, default=""):
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st
        val = st.secrets.get(name)
        if val:
            os.environ[name] = str(val)
            return str(val)
    except Exception:
        pass
    return default


API_KEY = _cfg("ANTHROPIC_API_KEY")
DATABASE_URL = _cfg("DATABASE_URL")
MODEL = _cfg("ANTHROPIC_MODEL", "claude-sonnet-5")
API_URL = "https://api.anthropic.com/v1/messages"

RISK_LEVEL = 4.24
WARN_LEVEL = 4.45


def conn():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задан")
    return psycopg2.connect(DATABASE_URL)


def ask_claude(system, user, max_tokens=1600, temperature=0.2):
    if not API_KEY:
        raise ValueError("ANTHROPIC_API_KEY не задан")
    r = requests.post(
        API_URL,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "temperature": temperature,
              "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:400]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


# ================================================================= 1. ДАЙДЖЕСТ
def _hist_bad(raw):
    if not raw:
        return None
    try:
        h = json.loads(raw) if isinstance(raw, str) else raw
        return int(h.get("1", 0)) + int(h.get("2", 0))
    except Exception:
        return None


def collect_aggregates(days=7, rollout_date="2026-08-20"):
    """Считает агрегаты за период. Ничего не отдаёт модели напрямую — сначала цифры."""
    with conn() as c:
        df = pd.read_sql(
            """
            SELECT asin, source, rating, review_count, histogram_json, created_at
            FROM asin_metrics
            WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
            ORDER BY created_at ASC;
            """, c)
        try:
            dic = pd.read_sql("SELECT asin, parent_asin, category, product_type, market FROM asin_dictionary", c)
        except Exception:
            dic = pd.DataFrame(columns=["asin", "parent_asin", "category", "product_type", "market"])
        try:
            kinds = pd.read_sql("SELECT asin, COALESCE(kind,'child') AS kind FROM tracked_asins", c)
        except Exception:
            kinds = pd.DataFrame(columns=["asin", "kind"])

    if df.empty:
        return {"empty": True}

    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df["review_count"] = pd.to_numeric(df["review_count"], errors="coerce")
    df["bad_pct"] = df["histogram_json"].apply(_hist_bad)

    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    period = df[df["created_at"] >= cutoff].copy()
    if period.empty:
        period = df.copy()

    cat = dict(zip(dic["asin"], dic["category"])) if not dic.empty else {}
    par = dict(zip(dic["asin"], dic["parent_asin"])) if not dic.empty else {}
    kmap = dict(zip(kinds["asin"], kinds["kind"])) if not kinds.empty else {}

    # последний и первый замер в периоде на ASIN
    first = period.groupby("asin").first()
    last = period.groupby("asin").last()
    j = last.join(first, rsuffix="_first")
    j["d_rating"] = j["rating"] - j["rating_first"]
    j["new_ratings"] = (j["review_count"] - j["review_count_first"]).clip(lower=0)
    j["neg_now"] = j["bad_pct"] / 100 * j["review_count"]
    j["neg_before"] = j["bad_pct_first"] / 100 * j["review_count_first"]
    j["new_neg"] = (j["neg_now"] - j["neg_before"]).clip(lower=0)
    j["category"] = [cat.get(a, "") or "без категории" for a in j.index]
    j["parent"] = [par.get(a, "") or "" for a in j.index]
    j["kind"] = [kmap.get(a, "child") for a in j.index]

    def status(r):
        if pd.isna(r):
            return "нет данных"
        return "риск" if r <= RISK_LEVEL else ("внимание" if r < WARN_LEVEL else "ок")

    j["status"] = j["rating"].apply(status)

    # входящий рейтинг новых оценок
    with pd.option_context("mode.use_inf_as_na", True):
        j["in_rating"] = ((j["rating"] * j["review_count"] - j["rating_first"] * j["review_count_first"])
                          / j["new_ratings"].replace(0, pd.NA)).clip(1, 5)

    total_new = float(j["new_ratings"].sum())
    total_new_neg = float(j["new_neg"].sum())

    by_cat = j.groupby("category").agg(
        позиций=("rating", "count"),
        средний_рейтинг=("rating", "mean"),
        дельта_рейтинга=("d_rating", "mean"),
        новых_оценок=("new_ratings", "sum"),
        новых_негативных=("new_neg", "sum"),
        в_риске=("status", lambda x: int((x == "риск").sum())),
    ).round(2)
    by_cat["доля_негатива_во_входящих_%"] = (by_cat["новых_негативных"] /
                                             by_cat["новых_оценок"].replace(0, pd.NA) * 100).round(0)
    by_cat = by_cat.sort_values("средний_рейтинг").head(20)

    # до/после внедрения бота
    rl = pd.Timestamp(rollout_date, tz="UTC")
    before_after = {}
    for label, part in (("до", df[df["created_at"] < rl]), ("после", df[df["created_at"] >= rl])):
        if part.empty or part["asin"].nunique() == 0:
            before_after[label] = None
            continue
        f = part.groupby("asin").first()
        l = part.groupby("asin").last()
        nn = (l["review_count"] - f["review_count"]).clip(lower=0).sum()
        neg = ((l["bad_pct"] / 100 * l["review_count"]) - (f["bad_pct"] / 100 * f["review_count"])).clip(lower=0).sum()
        before_after[label] = {
            "новых_оценок": int(nn or 0),
            "новых_негативных": int(neg or 0),
            "доля_негатива_%": round(float(neg) / float(nn) * 100, 1) if nn else None,
            "замеров": int(len(part)),
            "дней": int((part["created_at"].max() - part["created_at"].min()).days) + 1,
        }

    worst = j.nsmallest(12, "rating")[["rating", "d_rating", "review_count", "new_ratings",
                                       "new_neg", "category", "parent", "status"]].round(2)
    droppers = j[j["d_rating"] < 0].nsmallest(12, "d_rating")[
        ["rating", "d_rating", "review_count", "new_ratings", "new_neg", "category"]].round(2)

    return {
        "empty": False,
        "период_дней": days,
        "дата_внедрения_бота": rollout_date,
        "всего_позиций": int(j.shape[0]),
        "средний_рейтинг": round(float(j["rating"].mean()), 2),
        "в_риске": int((j["status"] == "риск").sum()),
        "внимание": int((j["status"] == "внимание").sum()),
        "ок": int((j["status"] == "ок").sum()),
        "новых_оценок_за_период": int(total_new),
        "новых_негативных_за_период": int(total_new_neg),
        "доля_негатива_во_входящих_%": round(total_new_neg / total_new * 100, 1) if total_new else None,
        "по_категориям": by_cat.reset_index().to_dict("records"),
        "до_после_бота": before_after,
        "худшие_позиции": worst.reset_index().rename(columns={"index": "asin"}).to_dict("records"),
        "сильнее_всего_упали": droppers.reset_index().rename(columns={"index": "asin"}).to_dict("records"),
    }


DIGEST_SYSTEM = """Ты аналитик e-commerce, работаешь с брендом мериносовой одежды на Amazon.
Тебе дают ГОТОВЫЕ агрегаты мониторинга рейтингов — сам ничего не пересчитывай и не выдумывай цифры,
используй только то, что дано. Если данных для вывода мало, так и скажи.

Контекст: рейтинг на витрине округлён до 0.1, гистограмма звёзд — до 1%, поэтому на больших базах
производные величины (входящий рейтинг новых оценок, число новых 1-2★) — оценка, а не точный счёт.
Учитывай это: не строй выводов на разнице в пределах округления.

Отдельно отслеживается гипотеза: ИИ-бот возвратов Amazon (внедрён ~20.08.2026) заставляет клиентов
ставить оценки без текста, преимущественно 1-2★. Признак — рейтинг падает, а число оценок почти не растёт.

Пиши по-русски, сжато, без воды и без markdown-заголовков. Структура ответа:
1. Что изменилось за период — 2-4 предложения.
2. Где системно (категории/паренты, а не отдельные ASIN) — список с цифрами.
3. Гипотеза про бота — подтверждается, не подтверждается или данных не хватает, и почему.
4. Что проверить руками — 2-4 конкретных действия с указанием ASIN/категорий.
Не используй канцелярит и не пересказывай входные данные целиком."""


def weekly_digest(days=7, rollout_date="2026-08-20"):
    agg = collect_aggregates(days=days, rollout_date=rollout_date)
    if agg.get("empty"):
        return "Данных пока нет.", agg
    user = ("Агрегаты мониторинга Rating Radar:\n\n"
            + json.dumps(agg, ensure_ascii=False, indent=2, default=str))
    return ask_claude(DIGEST_SYSTEM, user, max_tokens=1600), agg


# ================================================================= 2. ПРИЧИНЫ НЕГАТИВА
CAUSES_SYSTEM = """Ты аналитик отзывов на Amazon для бренда мериносовой одежды.
Тебе дают тексты негативных отзывов (1-2★). Задача — разложить их на причины.

Правила:
- Работай только с тем, что написано в отзывах. Ничего не додумывай.
- Категории причин выбирай сам из содержания, но держись бытового языка:
  размер/посадка, качество ткани, катышки/износ, цвет не тот, запах, колется,
  усадка после стирки, доставка/упаковка, прислали не то, цена/ожидания, брак.
- Для каждой причины: сколько отзывов, доля в %, 1-2 характерные формулировки
  своими словами (НЕ цитируй дословно длиннее нескольких слов).
- Отдельно отметь отзывы без содержательной претензии (одна фраза, эмоция без деталей) —
  это косвенный признак rating-only/возвратных оценок.

Ответ по-русски, компактно, без markdown-заголовков:
1. Разбивка причин — список «причина — N отзывов (X%) — суть».
2. Что чинить в первую очередь — 2-3 пункта: что именно и в продукте или в листинге.
3. Если видна разница между ASIN или категориями — отметь одной строкой."""


def ensure_reviews_schema():
    """Дублирует создание таблиц из collector — чтобы чтение не падало до первого сбора."""
    try:
        from collector import ensure_reviews_schema as _ensure
        _ensure()
        return True
    except Exception:
        return False


def get_reviews_for_analysis(asins=None, category=None, stars_max=2, limit=60, days=None):
    ensure_reviews_schema()
    with conn() as c:
        q = ["SELECT r.asin, r.stars, r.title, r.body, r.review_date, r.market FROM asin_reviews r"]
        where, params = ["r.stars <= %s"], [stars_max]
        if category:
            try:
                pd.read_sql("SELECT 1 FROM asin_dictionary LIMIT 1", c)
                q.append("JOIN asin_dictionary d ON d.asin = r.asin")
                where.append("d.category = %s")
                params.append(category)
            except Exception:
                pass   # справочник ещё не загружен — фильтр по категории пропускаем
        if asins:
            where.append("r.asin = ANY(%s)")
            params.append(list(asins))
        if days:
            where.append("r.fetched_at >= now() - interval '%s days'" % int(days))
        q.append("WHERE " + " AND ".join(where))
        q.append("ORDER BY r.fetched_at DESC LIMIT %s")
        params.append(int(limit))
        return pd.read_sql(" ".join(q), c, params=params)


def analyze_negative_reviews(reviews_df, context=""):
    if reviews_df.empty:
        return "Нет собранных негативных отзывов. Сначала запусти сбор отзывов во вкладке AI-анализ."
    blocks = []
    for _, r in reviews_df.iterrows():
        stars = f"{r['stars']:.0f}★" if pd.notnull(r["stars"]) else "?★"
        blocks.append(f"[{r['asin']} · {stars} · {r.get('review_date') or ''}]\n"
                      f"{(r.get('title') or '').strip()}\n{(r.get('body') or '').strip()}")
    user = (f"{context}\n\nВсего отзывов: {len(blocks)}\n\n" + "\n\n---\n\n".join(blocks))[:120000]
    return ask_claude(CAUSES_SYSTEM, user, max_tokens=2000)


# ================================================================= 3. rating-only
def rating_only_estimate():
    """Прямая оценка бота: всего оценок минус отзывы с текстом."""
    ensure_reviews_schema()
    with conn() as c:
        m = pd.read_sql(
            """
            SELECT DISTINCT ON (asin) asin, source, rating, review_count, created_at
            FROM asin_metrics WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
            ORDER BY asin, created_at DESC;
            """, c)
        try:
            rc = pd.read_sql(
                """
                SELECT DISTINCT ON (asin) asin, reviews_with_text, created_at AS counted_at
                FROM review_counts ORDER BY asin, created_at DESC;
                """, c)
        except Exception:
            rc = pd.DataFrame(columns=["asin", "reviews_with_text", "counted_at"])
    if m.empty or rc.empty:
        return pd.DataFrame()
    out = m.merge(rc, on="asin", how="inner")
    out["review_count"] = pd.to_numeric(out["review_count"], errors="coerce")
    out["reviews_with_text"] = pd.to_numeric(out["reviews_with_text"], errors="coerce")
    out["rating_only"] = (out["review_count"] - out["reviews_with_text"]).clip(lower=0)
    out["rating_only_%"] = (out["rating_only"] / out["review_count"].replace(0, pd.NA) * 100).round(1)
    return out.sort_values("rating_only_%", ascending=False)
