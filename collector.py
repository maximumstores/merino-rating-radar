 #!/usr/bin/env python3
"""Rating Radar: каскад BE->NL->reviews + авто-определение страны из ссылок (US, DE, UK, BE, NL и др.)."""

import json
import os
import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
import psycopg2
import requests

load_dotenv()

API_KEY = os.environ.get("SCRAPINGDOG_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SCRAPE_URL = "https://api.scrapingdog.com/scrape"
RETRIES = 5
TIMEOUT = 90

# Маппинг доменов Amazon к кодам стран
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
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def extract_asin(text: str) -> str:
    """Извлекает 10-значный чистый ASIN из текста или ссылки."""
    text = str(text).strip().upper()
    match = re.search(r"(B[0-9A-Z]{9})", text)
    return match.group(1) if match else text


def extract_asin_and_market(text: str) -> tuple[str, str | None, str | None]:
    """
    Возвращает (clean_asin, target_market_code, custom_url_template)
    Если введена ссылка (например https://www.amazon.com/dp/B0H6YBDKXJ),
    определит маркет 'US' и соберет данные именно с amazon.com.
    """
    clean_asin = extract_asin(text)
    if not clean_asin:
        return "", None, None

    if text.startswith("HTTP://") or text.startswith("HTTPS://"):
        try:
            parsed = urlparse(text.lower())
            host = parsed.netloc.replace("www.", "")
            for domain, (market_code, url_tmpl) in DOMAIN_MARKETS.items():
                if domain in host:
                    return clean_asin, market_code, url_tmpl
        except Exception:
            pass

    return clean_asin, None, None


def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не найден в .env")
    return psycopg2.connect(DATABASE_URL)


def ensure_schema():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(
                "ALTER TABLE asin_metrics ADD COLUMN IF NOT EXISTS image_url TEXT;"
            )
            cur.execute(
                "ALTER TABLE asin_metrics ADD COLUMN IF NOT EXISTS bsr TEXT;"
            )
        conn.commit()


def clean_db_trash():
    try:
        ensure_schema()
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM asin_metrics WHERE asin LIKE 'HTTP%' OR LENGTH(asin) > 10;"
                )
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


def get_tracked_asins() -> list[str]:
    ensure_schema()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT asin FROM tracked_asins ORDER BY asin")
            return [row[0] for row in cur.fetchall()]


def add_tracked_asins(asins: list[str]):
    ensure_schema()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for raw in asins:
                clean_asin = extract_asin(raw)
                if clean_asin and len(clean_asin) == 10:
                    cur.execute(
                        "INSERT INTO tracked_asins (asin) VALUES (%s) ON CONFLICT (asin) DO NOTHING",
                        (clean_asin,),
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


def fetch(url: str) -> str | None:
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
    if (
        "robot check" in low
        or "captcha" in low
        or "api-services-support@amazon" in low
    ):
        return False
    return True


def parse_bsr(soup: BeautifulSoup, html: str) -> str | None:
    m = re.search(r"#([0-9,.]+)\s*(?:in|в)\s*([^<\n(\n]+)", html, re.I)
    if m:
        return f"#{m.group(1)} {m.group(2).strip()[:30]}"

    bsr_li = soup.select_one("#SalesRank, #detailBullets_feature_div")
    if bsr_li:
        text = bsr_li.get_text(" ", strip=True)
        m2 = re.search(r"#([0-9,.]+)\s*(?:in|в)\s*([^<\n(]+)", text, re.I)
        if m2:
            return f"#{m2.group(1)} {m2.group(2).strip()[:30]}"
    return None


def parse_rating(soup: BeautifulSoup, html: str) -> dict:
    out = {
        "rating": None,
        "count": None,
        "hist": {},
        "image_url": None,
        "bsr": None,
    }

    img = soup.select_one(
        "#landingImage, #imgBlkFront, #main-image"
    ) or soup.select_one("img[data-old-hires]")
    if img:
        out["image_url"] = img.get("data-old-hires") or img.get("src")

    out["bsr"] = parse_bsr(soup, html)

    m = re.search(r"([0-9][.,][0-9])\s*(?:out of|van|sur|von)\s*5", html)
    if m:
        out["rating"] = float(m.group(1).replace(",", "."))
    else:
        pop = soup.select_one("#acrPopover")
        if pop and pop.get("title"):
            m2 = re.search(r"([0-9][.,][0-9])", pop["title"])
            if m2:
                out["rating"] = float(m2.group(1).replace(",", "."))

    cnt = soup.select_one("#acrCustomerReviewText")
    if cnt:
        digits = re.sub(r"[^\d]", "", cnt.get_text())
        if digits:
            out["count"] = int(digits)

    for row in soup.select(
        "#histogramTable tr, li[id*='star'], a[href*='filterByStar']"
    ):
        text = row.get_text(" ", strip=True)
        m3 = re.search(
            r"([1-5])\s*(?:star|ster|étoile|Stern).*?(\d{1,3})\s*%",
            text,
            re.I,
        )
        if m3:
            out["hist"][int(m3.group(1))] = int(m3.group(2))

    if not out["hist"]:
        for m4 in re.finditer(
            r"([1-5])\s*st\w+[^%]{0,60}?(\d{1,3})\s*%", html, re.I
        ):
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

    stars = [
        float(m.group(1).replace(",", "."))
        for m in re.finditer(
            r"([0-9][.,][0-9])\s*(?:out of|van|sur|von)\s*5", html
        )
    ]
    stars = stars[1:] if len(stars) > 1 else []
    if not stars:
        return {
            "rating": None,
            "count": None,
            "image_url": image_url,
            "bsr": None,
        }
    return {
        "rating": round(sum(stars) / len(stars), 2),
        "count": len(stars),
        "image_url": image_url,
        "bsr": None,
    }


def check_asin(raw_input: str, log=print) -> dict:
    clean_asin, target_market, custom_tmpl = extract_asin_and_market(raw_input)
    log(f"=== {clean_asin} ===")

    # Если была передана прямая ссылка на конкретный маркетплейс (например Amazon US)
    if target_market and custom_tmpl:
        url = custom_tmpl.format(asin=clean_asin)
        log(f"  [{target_market}] Запрос по прямой ссылке...")
        html = fetch(url)
        if html and sanity_check(html):
            soup = BeautifulSoup(html, "html.parser")
            data = parse_rating(soup, html)
            if data["rating"] is not None:
                log(f"  [{target_market}] OK: rating={data['rating']} count={data['count']}")
                return {"asin": clean_asin, "source": target_market, **data}

    # Иначе используем дефолтный каскадный поиск BE -> NL
    for market, tmpl in DEFAULT_MARKETS:
        url = tmpl.format(asin=clean_asin)
        html = fetch(url)
        if not html or not sanity_check(html):
            continue

        soup = BeautifulSoup(html, "html.parser")
        if in_variation(soup):
            continue
        data = parse_rating(soup, html)
        if data["rating"] is None:
            continue
        log(f"  [{market}] OK: rating={data['rating']} count={data['count']}")
        return {"asin": clean_asin, "source": market, **data}

    log("  [reviews-only] фолбэк по письменным ревью (BE)")
    rv = parse_reviews_page(clean_asin)
    if rv["rating"] is not None:
        return {
            "asin": clean_asin,
            "source": "reviews-only",
            "rating": rv["rating"],
            "count": rv["count"],
            "hist": {},
            "image_url": rv["image_url"],
            "bsr": None,
            "note": "только письменные ревью, данные неполные",
        }
    return {
        "asin": clean_asin,
        "source": "none",
        "rating": None,
        "count": None,
        "hist": {},
        "image_url": None,
        "bsr": None,
    }
