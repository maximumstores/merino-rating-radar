#!/usr/bin/env python3
"""Сводка по конкурентам для Telegram: считается по всем странам и группам,
отправляется в канал «comp». Запускается из GitHub Actions после сбора.

    python comp_report.py            # все страны
    python comp_report.py --country DE
    python comp_report.py --dry      # только напечатать, не отправлять
"""

import argparse
import datetime
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TZ = os.environ.get("RADAR_TZ", "Europe/Kyiv")
OWN_BRANDS_DEFAULT = "Merino.tech"
MIN_REVIEWS = int(os.environ.get("COMP_MIN_REVIEWS", "20"))   # порог, чтобы новичок с 2 оценками не был «лучшим»
DASHBOARD_URL = "https://rating-radar.streamlit.app"


def _conn():
    return psycopg2.connect(DATABASE_URL)


def get_setting(key, default=None):
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT value FROM radar_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception:
        return default


def own_brands():
    raw = get_setting("own_brands", OWN_BRANDS_DEFAULT)
    return [b.strip().lower() for b in str(raw).split(",") if b.strip()]


def is_own(brand, owns):
    b = str(brand or "").lower()
    return any(o and o in b for o in owns)


def load_data(days=3):
    """Конкуренты + их последние замеры."""
    conn = _conn()
    comp = pd.read_sql(
        """
        SELECT t.asin, COALESCE(d.comp_group,'') AS grp, COALESCE(d.market,'') AS market,
               COALESCE(d.brand,'') AS brand
        FROM tracked_asins t
        LEFT JOIN asin_dictionary d ON d.asin = t.asin
        WHERE t.kind = 'competitor';
        """, conn)
    if comp.empty:
        conn.close()
        return comp, pd.DataFrame()
    hist = pd.read_sql(
        """
        SELECT asin, rating, review_count, bsr_num, price, created_at
        FROM asin_metrics
        WHERE asin = ANY(%s) AND created_at >= NOW() - (%s || ' days')::interval
        ORDER BY created_at ASC;
        """, conn, params=(comp["asin"].tolist(), str(int(days))))
    conn.close()
    if hist.empty:
        return comp, hist
    hist["created_at"] = pd.to_datetime(hist["created_at"], utc=True)
    for c in ("rating", "review_count", "bsr_num"):
        hist[c] = pd.to_numeric(hist[c], errors="coerce")
    hist["price_num"] = pd.to_numeric(
        hist["price"].astype(str).str.replace(r"[^\d,.]", "", regex=True)
        .str.replace(r"\.(?=\d{3}\b)", "", regex=True).str.replace(",", "."), errors="coerce")
    return comp, hist


def build_report(country=None, days=3):
    comp, hist = load_data(days)
    if comp.empty or hist.empty:
        return None
    owns = own_brands()
    latest = hist.sort_values("created_at").groupby("asin").last().reset_index()
    meta = comp.set_index("asin")
    latest["brand"] = [str(meta.loc[a, "brand"]) if a in meta.index else "" for a in latest["asin"]]
    latest["grp"] = [str(meta.loc[a, "grp"]) if a in meta.index else "" for a in latest["asin"]]
    latest["market"] = [str(meta.loc[a, "market"]) if a in meta.index else "" for a in latest["asin"]]
    latest["own"] = latest["brand"].apply(lambda b: is_own(b, owns))

    markets = [country] if country else sorted({m for m in latest["market"] if m})
    now = datetime.datetime.now(ZoneInfo(TZ))
    reports = {}

    for mkt in markets:
        part_m = latest[latest["market"] == mkt]
        if part_m.empty:
            continue
        lines = [f"📊 <b>Конкуренты — {mkt}</b>", f"<i>{now:%d.%m.%Y %H:%M}</i>", ""]
        printed = 0
        for g in sorted({x for x in part_m["grp"] if x}):
            part = part_m[part_m["grp"] == g]
            ours, comps = part[part["own"]], part[~part["own"]]
            # «лучший конкурент» ищем только среди тех, у кого база отзывов не игрушечная
            solid = comps[comps["review_count"].fillna(0) >= MIN_REVIEWS]
            lines.append(f"📌 <b>{g}</b> — {len(part)} (наших {len(ours)}, конкурентов {len(comps)})")

            def cmp_line(label, col, better="max", fmt="{:.1f}"):
                o = ours[col].dropna()
                c = solid[col].dropna()
                if o.empty:
                    return
                ov = o.max() if better == "max" else o.min()
                if c.empty:
                    lines.append(f"  • {label}: наш {fmt.format(ov)} · сопоставимых конкурентов нет")
                    return
                idx = c.idxmax() if better == "max" else c.idxmin()
                cv, cb = c.loc[idx], solid.loc[idx, "brand"]
                win = ov >= cv if better == "max" else ov <= cv
                lines.append(f"  • {label}: наш {fmt.format(ov)} / лучший {fmt.format(cv)}"
                             f"{'' if win else ' (' + str(cb)[:22] + ')'} {'🟢' if win else '🔴'}")

            cmp_line("Рейтинг", "rating", "max", "{:.1f}")
            cmp_line("BSR", "bsr_num", "min", "{:,.0f}")
            cmp_line("Отзывы", "review_count", "max", "{:,.0f}")
            po, pc = ours["price_num"].dropna(), solid["price_num"].dropna()
            if not po.empty and not pc.empty:
                avg = pc.mean()
                lines.append(f"  • Цена: наша {po.mean():.2f} / средняя {avg:.2f} "
                             f"{'🟢' if po.mean() <= avg else '🔴'}")
            lines.append("")
            printed += 1
        if printed:
            tail = (f"<i>«Лучший» — среди конкурентов с {MIN_REVIEWS}+ отзывами, "
                    f"новички с парой оценок не в счёт.</i>\n"
                    f"<a href=\"{DASHBOARD_URL}\">Открыть дашборд →</a>")
            reports[mkt] = "\n".join(lines) + tail

    return reports or None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default=None, help="только одна страна, например DE")
    ap.add_argument("--days", type=int, default=3, help="за сколько дней брать последний замер")
    ap.add_argument("--dry", action="store_true", help="напечатать, не отправлять")
    args = ap.parse_args()

    text = build_report(args.country, args.days)
    if not text:
        print("нечего отправлять: нет конкурентов или свежих замеров")
        sys.exit(0)

    if args.dry:
        for mkt, body in text.items():
            print(f"───── {mkt} ─────\n{body}\n")
        sys.exit(0)

    try:
        import notifier
    except Exception as e:
        print("notifier не импортируется:", e)
        sys.exit(1)

    total_sent = 0
    for mkt, body in text.items():
        ch = notifier.channel_for_country(mkt)
        try:
            ok, total = notifier.broadcast(body, channel=ch)
            total_sent += ok
            print(f"{mkt} → канал {ch}: отправлено {ok} из {total}")
        except Exception as e:
            print(f"{mkt} → канал {ch}: ошибка {e}")
    print("итого отправлено:", total_sent)
