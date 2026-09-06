#!/usr/bin/env python3
"""Rating Radar: каскад BE->NL->reviews + авто-определение страны из ссылок (US, DE, UK, BE, NL и др.)."""

import json
import os
import re
import time
from urllib.parse import urlparse

import threading
from contextlib import contextmanager

import psycopg2
import requests
from psycopg2 import extras, pool
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("SCRAPINGDOG_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SCRAPE_URL = "https://api.scrapingdog.com/scrape"
RETRIES = 5
TIMEOUT = 90
BSR_RETRIES = int(os.environ.get("BSR_RETRIES", "5"))   # доборы BSR/гистограммы, если не распарсились

DOMAIN_MARKETS = {
    "amazon.com": ("US", "https://www.amazon.com/dp/{asin}"),
    "amazon.com.be": ("BE", "https://www.amazon.com.be/dp/{asin}?language=en_GB"),
    "amazon.nl": ("NL", "https://www.amazon.nl/dp/{asin}?language=en_GB"),
    "amazon.de": ("DE", "https://www.amazon.de/dp/{asin}?language=en_GB"),
    "amazon.co.uk": ("UK", "https://www.amazon.co.uk/dp/{asin}"),
    "amazon.fr": ("FR", "https://www.amazon.fr/dp/{asin}"),
    "amazon.it": ("IT", "https://www.amazon.it/dp/{asin}"),
    "amazon.es": ("ES", "https://www.amazon.es/dp/{asin}"),
}
# порядок важен: сначала самые длинные домены, чтобы amazon.com не съел amazon.com.be
DOMAIN_ORDER = sorted(DOMAIN_MARKETS, key=len, reverse=True)
MARKET_DOMAIN_BY_CODE = {code: domain for domain, (code, _t) in DOMAIN_MARKETS.items()}

DEFAULT_MARKETS = [
    ("BE", "https://www.amazon.com.be/dp/{asin}?language=en_GB"),
    ("NL", "https://www.amazon.nl/dp/{asin}?language=en_GB"),
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asin_metrics (
    id SERIAL PRIMARY KEY,
    asin TEXT NOT NULL,
    source TEXT,
    rating NUMERIC(3,2),
    review_count INTEGER,
    histogram_json JSONB,
    image_url TEXT,
    bsr TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    asin_count INTEGER,
    ok_count INTEGER,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS tracked_asins (
    asin TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'child',
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def extract_asin(text: str) -> str:
    """Извлекает 10-значный чистый ASIN из текста или ссылки. Пустая строка, если не нашёл."""
    text = str(text).strip().upper()
    match = re.search(r"(B[0-9A-Z]{9})", text)
    return match.group(1) if match else ""


def extract_asin_and_market(text: str):
    """(clean_asin, market_code | None, url_template | None). Регистр ссылки не важен."""
    raw = str(text).strip()
    clean_asin = extract_asin(raw)
    if not clean_asin:
        return "", None, None

    low = raw.lower()

    # 1) прямая ссылка
    if low.startswith("http://") or low.startswith("https://"):
        try:
            host = urlparse(low).netloc.replace("www.", "")
            for domain in DOMAIN_ORDER:
                if host.endswith(domain):
                    code, tmpl = DOMAIN_MARKETS[domain]
                    return clean_asin, code, tmpl
        except Exception:
            pass

    # 2) суффикс вида B0XXXXXXXX:US
    if ":" in raw:
        tail = raw.rsplit(":", 1)[-1].strip().upper()
        for domain, (code, tmpl) in DOMAIN_MARKETS.items():
            if tail == code:
                return clean_asin, code, tmpl

    return clean_asin, None, None


# ---- пул соединений: при десятках потоков открывать коннект на каждую запись нельзя ----
_POOL = None
_POOL_LOCK = threading.Lock()
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))


def _get_pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                if not DATABASE_URL:
                    raise ValueError("DATABASE_URL не найден в .env")
                _POOL = pool.ThreadedConnectionPool(1, POOL_MAX, DATABASE_URL)
    return _POOL


@contextmanager
def db():
    """Соединение из пула. Возвращается обратно даже при ошибке."""
    p = _get_pool()
    conn = p.getconn()
    try:
        yield conn
    finally:
        try:
            p.putconn(conn)
        except Exception:
            pass


def get_db_connection():
    """Совместимость со старым кодом: отдельное соединение вне пула."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в .env")
    return psycopg2.connect(DATABASE_URL)


def ensure_schema():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute("ALTER TABLE asin_metrics ADD COLUMN IF NOT EXISTS image_url TEXT;")
            cur.execute("ALTER TABLE asin_metrics ADD COLUMN IF NOT EXISTS bsr TEXT;")
            cur.execute("ALTER TABLE tracked_asins ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'child';")
        conn.commit()


def clean_db_trash():
    try:
        ensure_schema()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM asin_metrics WHERE asin LIKE 'HTTP%' OR LENGTH(asin) > 10;")
            conn.commit()
    except Exception:
        pass


def delete_asin_completely(asin: str):
    clean = extract_asin(asin)
    if not clean:
        return
    ensure_schema()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tracked_asins WHERE asin = %s", (clean,))
            cur.execute("DELETE FROM asin_metrics WHERE asin = %s", (clean,))
        conn.commit()


def get_tracked_asins() -> list:
    ensure_schema()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT asin FROM tracked_asins ORDER BY asin")
            return [row[0] for row in cur.fetchall()]


def add_tracked_asins(asins: list, kind: str = "child"):
    ensure_schema()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for raw in asins:
                clean_asin = extract_asin(raw)
                if clean_asin and len(clean_asin) == 10:
                    cur.execute(
                        "INSERT INTO tracked_asins (asin, kind) VALUES (%s, %s) "
                        "ON CONFLICT (asin) DO UPDATE SET kind = EXCLUDED.kind",
                        (clean_asin, kind),
                    )
        conn.commit()


def save_to_db(data: dict):
    clean_asin = extract_asin(data.get("asin", ""))
    if not clean_asin or len(clean_asin) != 10:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO asin_metrics (asin, source, rating, review_count, histogram_json, image_url, bsr, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    clean_asin,
                    data.get("source"),
                    data.get("rating"),
                    data.get("count"),
                    json.dumps(data.get("hist", {})),
                    data.get("image_url"),
                    data.get("bsr"),
                    data.get("note", ""),
                ),
            )
        conn.commit()


def save_batch(rows: list) -> int:
    """Пишет пачку замеров одним запросом. rows — список dict как из check_asin."""
    clean = []
    for d in rows:
        a = extract_asin(d.get("asin", ""))
        if not a or len(a) != 10:
            continue
        clean.append((a, d.get("source"), d.get("rating"), d.get("count"),
                      json.dumps(d.get("hist", {})), d.get("image_url"),
                      d.get("bsr"), d.get("note", "")))
    if not clean:
        return 0
    with db() as conn:
        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                "INSERT INTO asin_metrics "
                "(asin, source, rating, review_count, histogram_json, image_url, bsr, note) VALUES %s",
                clean, page_size=200)
        conn.commit()
    return len(clean)


def start_run(asin_count: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO collection_runs (asin_count, status) VALUES (%s, 'running') RETURNING id",
                (asin_count,),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def finish_run(run_id: int, ok_count: int, status: str = "done"):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE collection_runs SET finished_at = now(), ok_count = %s, status = %s WHERE id = %s",
                (ok_count, status, run_id),
            )
        conn.commit()


def fetch(url: str) -> str:
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(
                SCRAPE_URL,
                params={"api_key": API_KEY, "url": url, "dynamic": "false"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(2 * attempt)
    return None


def sanity_check(html: str) -> bool:
    low = html.lower()
    return not ("robot check" in low or "captcha" in low or "api-services-support@amazon" in low)


def in_variation(soup: BeautifulSoup) -> bool:
    """True, если на странице показан рейтинг паренты, а не конкретного чайлда.

    Признак: есть блок выбора вариаций (twister) и при этом рейтинг отдан на уровне
    родительской карточки. Тогда цифру брать нельзя — уходим на следующий маркет каскада.
    """
    try:
        twister = soup.select_one("#twister, #twisterContainer, #variation_size_name, #variation_color_name")
        if not twister:
            return False
        # На BE/NL у чайлда с собственными отзывами есть свой acrCustomerReviewText.
        own_reviews = soup.select_one("#acrCustomerReviewText")
        if own_reviews:
            digits = re.sub(r"[^\d]", "", own_reviews.get_text())
            if digits and int(digits) > 0:
                return False
        return True
    except Exception:
        return False


def parse_bsr(soup: BeautifulSoup, html: str):
    for sel in ("#SalesRank", "#detailBullets_feature_div", "#productDetails_detailBullets_sections1",
                "#prodDetails", "#detailBulletsWrapper_feature_div"):
        node = soup.select_one(sel)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        m = re.search(r"(?:#|nr\.?\s*)([0-9][0-9,.\s]*)\s*(?:in|en|dans|im|nella)\s+([^#(\n]{2,40})", text, re.I)
        if m:
            return f"#{m.group(1).strip()} {m.group(2).strip()[:30]}"
    m = re.search(r"(?:#|nr\.?\s*)([0-9][0-9,.]{2,})\s*(?:in|en|dans|im|nella)\s+([^<#(\n]{2,40})", html, re.I)
    if m:
        return f"#{m.group(1).strip()} {m.group(2).strip()[:30]}"
    return None


def parse_review_count(soup: BeautifulSoup, html: str):
    node = soup.select_one("#acrCustomerReviewText, [data-hook='total-review-count']")
    if node:
        digits = re.sub(r"[^\d]", "", node.get_text())
        if digits:
            return int(digits)
    # запасной путь: "1 234 beoordelingen / ratings / évaluations / Bewertungen / valutazioni"
    m = re.search(r"([0-9][0-9.,\s]{0,12})\s*(?:global\s+)?"
                  r"(?:ratings?|reviews?|beoordelingen|évaluations|Bewertungen|valoraciones|valutazioni)",
                  html, re.I)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            return int(digits)
    return None


def parse_rating(soup: BeautifulSoup, html: str) -> dict:
    out = {"rating": None, "count": None, "hist": {}, "image_url": None, "bsr": None}

    img = soup.select_one("#landingImage, #imgBlkFront, #main-image") or soup.select_one("img[data-old-hires]")
    if img:
        out["image_url"] = img.get("data-old-hires") or img.get("src")

    out["bsr"] = parse_bsr(soup, html)

    m = re.search(r"([0-9][.,][0-9])\s*(?:out of|van|sur|von|di|de)\s*5", html, re.I)
    if m:
        out["rating"] = float(m.group(1).replace(",", "."))
    else:
        pop = soup.select_one("#acrPopover")
        title = pop.get("title") if pop else None
        if title:
            m2 = re.search(r"([0-9][.,][0-9])", title)
            if m2:
                out["rating"] = float(m2.group(1).replace(",", "."))

    out["count"] = parse_review_count(soup, html)

    for row in soup.select("#histogramTable tr, li[id*='star'], a[href*='filterByStar'], [data-hook='histogram-row']"):
        text = row.get_text(" ", strip=True)
        m3 = re.search(r"([1-5])\s*(?:star|ster|sterren|étoile|Stern|stelle|estrella)[^%]{0,40}?(\d{1,3})\s*%", text, re.I)
        if m3:
            out["hist"][int(m3.group(1))] = int(m3.group(2))

    if not out["hist"]:
        for m4 in re.finditer(r"([1-5])\s*st\w+[^%]{0,60}?(\d{1,3})\s*%", html, re.I):
            star, pct = int(m4.group(1)), int(m4.group(2))
            if star not in out["hist"]:
                out["hist"][star] = pct

    return out


def parse_reviews_page(asin: str) -> dict:
    url = f"https://www.amazon.com.be/product-reviews/{asin}?language=en_GB&reviewerType=all_reviews"
    html = fetch(url)
    if not html:
        return {"rating": None, "count": None, "image_url": None, "bsr": None}
    soup = BeautifulSoup(html, "html.parser")
    img = soup.select_one("img[data-hook='product-image'], #cm_cr-product_preview img")
    image_url = img.get("src") if img else None

    stars = [float(m.group(1).replace(",", "."))
             for m in re.finditer(r"([0-9][.,][0-9])\s*(?:out of|van|sur|von|di|de)\s*5", html, re.I)]
    stars = stars[1:] if len(stars) > 1 else []
    if not stars:
        return {"rating": None, "count": None, "image_url": image_url, "bsr": None}
    return {"rating": round(sum(stars) / len(stars), 2), "count": len(stars),
            "image_url": image_url, "bsr": None}


def check_asin(raw_input: str, log=print) -> dict:
    clean_asin, target_market, custom_tmpl = extract_asin_and_market(raw_input)
    if not clean_asin:
        log(f"  пропуск: не распознан ASIN в «{raw_input}»")
        return {"asin": "", "source": "none", "rating": None, "count": None,
                "hist": {}, "image_url": None, "bsr": None, "note": "не распознан ASIN"}

    log(f"=== {clean_asin} ===")

    # 1) явно указанная страна (ссылка или суффикс :US)
    if target_market and custom_tmpl:
        url = custom_tmpl.format(asin=clean_asin)
        log(f"  [{target_market}] прямой запрос…")
        try:
            html = fetch(url)
            if html and sanity_check(html):
                soup = BeautifulSoup(html, "html.parser")
                data = parse_rating(soup, html)
                if data["rating"] is not None:
                    log(f"  [{target_market}] OK: rating={data['rating']} count={data['count']}")
                    return {"asin": clean_asin, "source": target_market, **data}
                log(f"  [{target_market}] рейтинг не найден на странице")
            else:
                log(f"  [{target_market}] пусто или капча")
        except Exception as e:
            log(f"  [{target_market}] ошибка: {e}")

    # 2) каскад BE -> NL
    for market, tmpl in DEFAULT_MARKETS:
        url = tmpl.format(asin=clean_asin)
        try:
            html = fetch(url)
            if not html or not sanity_check(html):
                log(f"  [{market}] пусто или капча")
                continue
            soup = BeautifulSoup(html, "html.parser")
            if in_variation(soup):
                log(f"  [{market}] рейтинг только у паренты — пропуск")
                continue
            data = parse_rating(soup, html)
            if data["rating"] is None:
                log(f"  [{market}] рейтинг не найден")
                continue
            if not data.get("bsr") or not data.get("hist"):
                extra = enrich_bsr_hist(clean_asin, market,
                                        need_bsr=not data.get("bsr"),
                                        need_hist=not data.get("hist"),
                                        tries=max(1, BSR_RETRIES - 1), log=log)
                data["bsr"] = data.get("bsr") or extra["bsr"]
                if not data.get("hist") and extra["hist"]:
                    data["hist"] = extra["hist"]
            log(f"  [{market}] OK: rating={data['rating']} count={data['count']} "
                f"bsr={data.get('bsr') or '—'}")
            return {"asin": clean_asin, "source": market, **data}
        except Exception as e:
            log(f"  [{market}] ошибка: {e}")

    # 3) фолбэк по письменным ревью
    log("  [reviews-only] фолбэк по письменным ревью (BE)")
    try:
        rv = parse_reviews_page(clean_asin)
    except Exception as e:
        log(f"  [reviews-only] ошибка: {e}")
        rv = {"rating": None, "count": None, "image_url": None, "bsr": None}

    if rv["rating"] is not None:
        return {"asin": clean_asin, "source": "reviews-only", "rating": rv["rating"], "count": rv["count"],
                "hist": {}, "image_url": rv["image_url"], "bsr": None,
                "note": "только письменные ревью, данные неполные"}

    log("  результат: данных нет")
    return {"asin": clean_asin, "source": "none", "rating": None, "count": None,
            "hist": {}, "image_url": None, "bsr": None, "note": "не найден ни на одном маркете"}


# ---------------------------------------------------------------- отзывы (тексты)
REVIEWS_SQL = """
CREATE TABLE IF NOT EXISTS asin_reviews (
    id SERIAL PRIMARY KEY,
    asin TEXT NOT NULL,
    market TEXT,
    review_id TEXT,
    stars NUMERIC(2,1),
    title TEXT,
    body TEXT,
    review_date TEXT,
    verified BOOLEAN,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (asin, review_id)
);

CREATE TABLE IF NOT EXISTS review_counts (
    id SERIAL PRIMARY KEY,
    asin TEXT NOT NULL,
    market TEXT,
    ratings_total INTEGER,      -- всего оценок (с витрины)
    reviews_with_text INTEGER,  -- из них с текстом (со страницы отзывов)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

REVIEW_DOMAINS = {
    "US": "https://www.amazon.com/product-reviews/{asin}",
    "BE": "https://www.amazon.com.be/product-reviews/{asin}?language=en_GB",
    "NL": "https://www.amazon.nl/product-reviews/{asin}?language=en_GB",
    "DE": "https://www.amazon.de/product-reviews/{asin}?language=en_GB",
    "UK": "https://www.amazon.co.uk/product-reviews/{asin}",
    "FR": "https://www.amazon.fr/product-reviews/{asin}",
    "IT": "https://www.amazon.it/product-reviews/{asin}",
    "ES": "https://www.amazon.es/product-reviews/{asin}",
}


def ensure_reviews_schema():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(REVIEWS_SQL)
        conn.commit()


def _reviews_url(asin, market, star_filter=None, page=1):
    base = REVIEW_DOMAINS.get(market, REVIEW_DOMAINS["BE"]).format(asin=asin)
    sep = "&" if "?" in base else "?"
    parts = ["reviewerType=all_reviews", f"pageNumber={page}", "sortBy=recent"]
    if star_filter:
        parts.append(f"filterByStar={star_filter}")   # critical | one_star | two_star | positive
    return base + sep + "&".join(parts)


def parse_reviews_html(html: str) -> dict:
    """Возвращает {'total_with_text': int|None, 'reviews': [ {...}, ... ]}"""
    soup = BeautifulSoup(html, "html.parser")
    out = {"total_with_text": None, "reviews": []}

    node = soup.select_one("[data-hook='cr-filter-info-review-rating-count']")
    if node:
        nums = re.findall(r"([0-9][0-9.,\s]*)", node.get_text(" ", strip=True))
        digits = [int(re.sub(r"[^\d]", "", n)) for n in nums if re.sub(r"[^\d]", "", n)]
        if len(digits) >= 2:
            out["total_with_text"] = digits[1]      # «X ratings, Y with reviews»
        elif digits:
            out["total_with_text"] = digits[0]

    for card in soup.select("[data-hook='review']"):
        try:
            rid = card.get("id")
            st_node = card.select_one("[data-hook='review-star-rating'], [data-hook='cmps-review-star-rating']")
            stars = None
            if st_node:
                m = re.search(r"([0-9][.,][0-9]|[1-5])", st_node.get_text(" ", strip=True))
                if m:
                    stars = float(m.group(1).replace(",", "."))
            title_node = card.select_one("[data-hook='review-title']")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            title = re.sub(r"^[0-9][.,][0-9]\s*(out of|van|sur|von)\s*5\s*", "", title, flags=re.I).strip()
            body_node = card.select_one("[data-hook='review-body']")
            body = body_node.get_text(" ", strip=True) if body_node else ""
            date_node = card.select_one("[data-hook='review-date']")
            date = date_node.get_text(" ", strip=True) if date_node else ""
            verified = bool(card.select_one("[data-hook='avp-badge']"))
            if body or title:
                out["reviews"].append({"review_id": rid, "stars": stars, "title": title[:300],
                                       "body": body[:4000], "review_date": date[:100], "verified": verified})
        except Exception:
            continue
    return out


def fetch_reviews(asin: str, market: str = "BE", star_filter: str = "critical",
                  pages: int = 2, log=print) -> dict:
    """Тянет тексты отзывов. star_filter: critical (1-2★) | positive | None (все)."""
    clean = extract_asin(asin)
    if not clean:
        return {"asin": "", "market": market, "total_with_text": None, "reviews": []}

    all_reviews, total = [], None
    for page in range(1, max(1, pages) + 1):
        url = _reviews_url(clean, market, star_filter, page)
        html = fetch(url)
        if not html or not sanity_check(html):
            log(f"  [{market}] отзывы стр.{page}: пусто или капча")
            break
        parsed = parse_reviews_html(html)
        if total is None:
            total = parsed["total_with_text"]
        if not parsed["reviews"]:
            break
        all_reviews.extend(parsed["reviews"])
        if len(parsed["reviews"]) < 8:
            break
    log(f"  [{market}] отзывов собрано: {len(all_reviews)}")
    return {"asin": clean, "market": market, "total_with_text": total, "reviews": all_reviews}


def save_reviews(data: dict):
    if not data.get("asin"):
        return 0
    ensure_reviews_schema()
    saved = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for r in data.get("reviews", []):
                cur.execute(
                    """
                    INSERT INTO asin_reviews (asin, market, review_id, stars, title, body, review_date, verified)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (asin, review_id) DO NOTHING;
                    """,
                    (data["asin"], data.get("market"), r.get("review_id"), r.get("stars"),
                     r.get("title"), r.get("body"), r.get("review_date"), r.get("verified")))
                saved += cur.rowcount
            if data.get("total_with_text") is not None:
                cur.execute(
                    "INSERT INTO review_counts (asin, market, reviews_with_text) VALUES (%s, %s, %s);",
                    (data["asin"], data.get("market"), int(data["total_with_text"])))
        conn.commit()
    return saved


# ================================================================= Scrapingdog structured API
SD_PRODUCT_URL = "https://api.scrapingdog.com/amazon/product"
SD_REVIEWS_URL = "https://api.scrapingdog.com/amazon/reviews"

# код рынка -> (domain, country) для Scrapingdog. Нужны ОБА параметра.
SD_MARKET = {
    "US": ("com", "us"),
    "BE": ("com.be", "be"),
    "NL": ("nl", "nl"),
    "DE": ("de", "de"),
    "UK": ("co.uk", "gb"),
    "FR": ("fr", "fr"),
    "IT": ("it", "it"),
    "ES": ("es", "es"),
}


def _sd_params(market):
    domain, country = SD_MARKET.get(market, SD_MARKET["BE"])
    return {"api_key": API_KEY, "domain": domain, "country": country}


def _sd_get(url, params, tries=3):
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    return None
        except requests.RequestException:
            pass
        time.sleep(2 * attempt)
    return None


def _as_obj(payload):
    """Scrapingdog иногда отдаёт список из одного объекта."""
    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload if isinstance(payload, dict) else None


def _num(val):
    if val is None:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None


def _rating(val):
    if val is None:
        return None
    m = re.search(r"([0-9][.,][0-9])|([1-5])", str(val))
    if not m:
        return None
    return float((m.group(0)).replace(",", "."))


def fetch_product_json(asin: str, market: str = "BE", log=print):
    """Товар через structured API Scrapingdog. Возвращает сырой JSON или None."""
    clean = extract_asin(asin)
    if not clean:
        return None
    if not API_KEY:
        log("  API: SCRAPINGDOG_API_KEY не задан")
        return None
    params = _sd_params(market)
    params["asin"] = clean
    data = _sd_get(SD_PRODUCT_URL, params)
    obj = _as_obj(data)
    if obj is None:
        log(f"  [{market}] API: пустой ответ")
    return obj


def parse_product_json(obj: dict, asin: str, market: str) -> dict:
    """Приводит JSON Scrapingdog к формату check_asin."""
    out = {"asin": asin, "source": market, "rating": None, "count": None, "hist": {},
           "image_url": None, "bsr": None, "note": ""}
    if not obj:
        return out

    out["rating"] = _rating(obj.get("average_rating") or obj.get("rating"))
    out["count"] = _num(obj.get("total_reviews") or obj.get("ratings_total") or obj.get("reviews_count"))
    out["image_url"] = obj.get("main_image") or (obj.get("images") or [None])[0]

    # BSR: в structured-ответе может лежать по-разному
    bsr = obj.get("bestsellers_rank") or obj.get("best_sellers_rank") or obj.get("bsr")
    if isinstance(bsr, list) and bsr:
        first = bsr[0]
        if isinstance(first, dict):
            out["bsr"] = f"#{_num(first.get('rank'))} {str(first.get('category', ''))[:30]}"
        else:
            out["bsr"] = str(first)[:40]
    elif bsr:
        out["bsr"] = str(bsr)[:40]
    elif obj.get("product_category"):
        out["bsr"] = None

    # распределение звёзд, если API его отдаёт
    hist = obj.get("rating_breakdown") or obj.get("histogram") or obj.get("ratings_breakdown")
    if isinstance(hist, dict):
        for k, v in hist.items():
            m = re.search(r"[1-5]", str(k))
            if not m:
                continue
            pct = _num(v.get("percentage") if isinstance(v, dict) else v)
            if pct is not None:
                out["hist"][int(m.group(0))] = pct

    extra = []
    if obj.get("parent_asin"):
        extra.append(f"parent {obj['parent_asin']}")
    if obj.get("product_category"):
        extra.append(str(obj["product_category"])[:80])
    if obj.get("price"):
        extra.append(str(obj["price"]))
    out["note"] = " · ".join(extra)[:200]
    out["parent_asin"] = obj.get("parent_asin") or ""
    out["category_path"] = obj.get("product_category") or ""
    out["price"] = obj.get("price") or ""
    return out


def extract_children(obj: dict) -> list:
    """Все чайлды паренты из customization_options (цвета + размеры)."""
    kids = []
    opts = (obj or {}).get("customization_options") or {}
    for _dim, items in opts.items():
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            a = extract_asin(it.get("asin") or it.get("url") or "")
            if a and len(a) == 10:
                kids.append({"asin": a, "value": str(it.get("value") or "")[:80], "dim": _dim})
    seen, out = set(), []
    for k in kids:
        if k["asin"] not in seen:
            seen.add(k["asin"])
            out.append(k)
    return out


def fetch_reviews_api(asin: str, market: str = "BE", pages: int = 2, star_filter="critical", log=print) -> dict:
    """Отзывы через structured API. Возвращает тот же формат, что fetch_reviews."""
    clean = extract_asin(asin)
    if not clean or not API_KEY:
        return {"asin": clean, "market": market, "total_with_text": None, "reviews": []}

    collected, total = [], None
    for page in range(1, max(1, pages) + 1):
        params = _sd_params(market)
        params.update({"asin": clean, "page": page})
        if star_filter:
            params["filter_by_star"] = star_filter
        data = _sd_get(SD_REVIEWS_URL, params)
        obj = _as_obj(data) or {}
        items = obj.get("customer_reviews") or obj.get("reviews") or (data if isinstance(data, list) else [])
        if isinstance(items, dict):
            items = [items]
        if total is None:
            total = _num(obj.get("total_reviews_with_text") or obj.get("reviews_count") or obj.get("total_reviews"))
        if not items:
            log(f"  [{market}] API отзывы стр.{page}: пусто")
            break
        for it in items:
            if not isinstance(it, dict):
                continue
            body = it.get("review") or it.get("body") or it.get("text") or ""
            title = it.get("title") or it.get("review_title") or ""
            if not (body or title):
                continue
            collected.append({
                "review_id": str(it.get("id") or it.get("review_id") or f"{clean}-{page}-{len(collected)}")[:120],
                "stars": _rating(it.get("rating") or it.get("stars")),
                "title": str(title)[:300],
                "body": str(body)[:4000],
                "review_date": str(it.get("date") or it.get("review_date") or "")[:100],
                "verified": bool(it.get("verified_purchase") or it.get("verified")),
            })
        if len(items) < 5:
            break
    log(f"  [{market}] API отзывов собрано: {len(collected)}")
    return {"asin": clean, "market": market, "total_with_text": total, "reviews": collected}


def enrich_bsr_hist(asin: str, market: str, need_bsr=True, need_hist=True,
                    tries: int = None, log=print) -> dict:
    """Добирает BSR и гистограмму звёзд со страницы товара.

    Amazon отдаёт блок с BSR не на каждой выдаче (разные шаблоны страницы,
    ленивая подгрузка), поэтому пробуем несколько раз, чередуя варианты URL,
    пока не получим нужное. Возвращает {'bsr': ..., 'hist': {...}}.
    """
    tries = BSR_RETRIES if tries is None else tries
    out = {"bsr": None, "hist": {}}
    clean = extract_asin(asin)
    if not clean:
        return out

    domain = MARKET_DOMAIN_BY_CODE.get(market, "amazon.com.be")
    variants = [
        f"https://www.{domain}/dp/{clean}",
        f"https://www.{domain}/dp/{clean}?language=en_GB",
        f"https://www.{domain}/gp/product/{clean}",
        f"https://www.{domain}/dp/{clean}?th=1&psc=1",
    ]

    for attempt in range(1, max(1, tries) + 1):
        url = variants[(attempt - 1) % len(variants)]
        html = fetch(url)
        if not html or not sanity_check(html):
            log(f"  [{market}] добор BSR попытка {attempt}/{tries}: пусто или капча")
            time.sleep(1)
            continue

        soup = BeautifulSoup(html, "html.parser")
        if need_bsr and not out["bsr"]:
            out["bsr"] = parse_bsr(soup, html)
        if need_hist and not out["hist"]:
            data = parse_rating(soup, html)
            if data.get("hist"):
                out["hist"] = data["hist"]

        got_bsr = out["bsr"] or not need_bsr
        got_hist = out["hist"] or not need_hist
        if got_bsr and got_hist:
            log(f"  [{market}] добор ок с попытки {attempt}"
                f"{': BSR ' + str(out['bsr']) if out['bsr'] else ''}")
            return out
        log(f"  [{market}] добор попытка {attempt}/{tries}: "
            f"BSR {'есть' if out['bsr'] else 'нет'}, гистограмма {'есть' if out['hist'] else 'нет'}")
        time.sleep(1)

    return out


def check_asin_api(raw_input: str, market: str = None, log=print, fallback_html=True) -> dict:
    """Сбор через structured API с откатом на HTML-парсер."""
    clean, mkt_from_input, _ = extract_asin_and_market(raw_input)
    if not clean:
        return {"asin": "", "source": "none", "rating": None, "count": None,
                "hist": {}, "image_url": None, "bsr": None, "note": "не распознан ASIN"}

    for mkt in [m for m in (market, mkt_from_input, "BE", "NL") if m]:
        obj = fetch_product_json(clean, mkt, log=log)
        if not obj:
            continue
        parsed = parse_product_json(obj, clean, mkt)
        if parsed["rating"] is not None:
            log(f"  [{mkt}] API OK: rating={parsed['rating']} count={parsed['count']}")
            # structured API не отдаёт BSR и распределение звёзд — добираем со страницы
            if not parsed.get("bsr") or not parsed.get("hist"):
                extra = enrich_bsr_hist(clean, mkt,
                                        need_bsr=not parsed.get("bsr"),
                                        need_hist=not parsed.get("hist"), log=log)
                parsed["bsr"] = parsed.get("bsr") or extra["bsr"]
                if not parsed.get("hist") and extra["hist"]:
                    parsed["hist"] = extra["hist"]
            parsed["_raw"] = obj
            return parsed
        log(f"  [{mkt}] API: рейтинга нет в ответе")

    if fallback_html:
        log("  откат на HTML-парсер")
        return check_asin(raw_input, log=log)
    return {"asin": clean, "source": "none", "rating": None, "count": None,
            "hist": {}, "image_url": None, "bsr": None, "note": "API не отдал рейтинг"}
