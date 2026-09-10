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
import re
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
        SELECT asin, rating, review_count, bsr_num, price,
               COALESCE(coupon, FALSE) AS coupon, COALESCE(prime_excl, FALSE) AS prime_excl,
               created_at
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
    hist["price_num"] = hist["price"].map(price_to_num)
    return comp, hist


def price_to_num(val):
    """«86.99 C$», «1.234,56 €», «$1,299.00» → число. Последний разделитель — десятичный."""
    txt = re.sub(r"[^\d,.]", "", str(val or ""))
    if not txt or not any(ch.isdigit() for ch in txt):
        return None
    last_dot, last_com = txt.rfind("."), txt.rfind(",")
    if last_dot == -1 and last_com == -1:
        num = txt
    else:
        sep = "." if last_dot > last_com else ","
        head, _, tail = txt.rpartition(sep)
        if len(tail) == 3 and (last_dot == -1 or last_com == -1) and head.count(sep) == 0 and len(head) <= 3:
            num = (head + tail).replace(".", "").replace(",", "")
        else:
            num = head.replace(".", "").replace(",", "") + "." + tail
    try:
        return float(num)
    except ValueError:
        return None


def esc(v):
    """& < > ломают HTML Telegram — «Men SS & Socks» без этого не уходит."""
    return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _n(fmt, v):
    """Тысячи — неразрывным пробелом: 40 575, а не 40,575."""
    return fmt.format(v).replace(",", "\u00a0")


def make_takeaways(facts):
    """Короткий вывод в конце отчёта: где горит, где выигрываем, что с ценой.
    Считается по фактам, без моделей — правила прозрачные и повторяемые."""
    if not facts:
        return ""
    lines = []

    # 1) провал по всем трём метрикам сразу — самое болезненное
    worst = [f for f in facts if f["lag_rating"] and f["lag_bsr"] and f["lag_reviews"]]
    if worst:
        names = ", ".join(f["group"] for f in worst[:4])
        lines.append(f"🔥 Отстаём по всему: {names}"
                     + (f" и ещё {len(worst) - 4}" if len(worst) > 4 else ""))

    # 2) самый большой разрыв по BSR — там, где нас реально не видно
    bsr_gaps = [f for f in facts if f["lag_bsr"] and f["our_bsr"] and f["best_bsr"]]
    if bsr_gaps:
        top = max(bsr_gaps, key=lambda f: f["our_bsr"] / max(1, f["best_bsr"]))
        ratio = top["our_bsr"] / max(1, top["best_bsr"])
        if ratio >= 3:
            n = int(round(ratio))
            word = "раз" if (n % 10 == 0 or n % 10 >= 5 or 11 <= n % 100 <= 14) else (
                "раза" if n % 10 in (2, 3, 4) else "раз")
            lines.append(f"📉 Дальше всех по BSR: {top['group']} — наш {top['our_bsr']:,.0f} "
                         f"против {top['best_bsr']:,.0f} у {top['best_bsr_brand']} "
                         f"(в {n} {word})".replace(",", " "))

    # 3) дороже конкурентов
    pricey = [f for f in facts if f["pricier"]]
    if pricey:
        both = [f["group"] for f in pricey if f["lag_bsr"]]
        lines.append(f"💸 Дороже рынка: {', '.join(f['group'] for f in pricey[:4])}"
                     + (f" · из них ещё и просели по BSR: {', '.join(both)}" if both else ""))
    else:
        lines.append("💸 По цене мы ниже рынка во всех группах")

    # 4) где держим позицию
    lead = [f for f in facts if not f["lag_rating"] and not f["lag_bsr"]]
    if lead:
        lines.append(f"✅ Уверенно идём: {', '.join(f['group'] for f in lead[:5])}")

    # 5) рейтинг ниже конкурентов почти везде — системный сигнал
    lag_r = [f for f in facts if f["lag_rating"]]
    if len(lag_r) >= max(3, int(len(facts) * 0.6)):
        lines.append(f"⭐ Рейтинг ниже конкурентов в {len(lag_r)} из {len(facts)} групп — "
                     "смотри причины негатива, это не разовое")

    return "\n<b>Итого</b>\n" + "\n".join(lines) + "\n" if lines else ""


def build_report(country=None, days=10):
    comp, hist = load_data(days)
    if comp.empty or hist.empty:
        return None
    owns = own_brands()
    hist = hist.sort_values("created_at")
    latest = hist.groupby("asin").last().reset_index()

    # у метрики свой «последний удачный замер»: BSR приходит не каждый раз,
    # и без даты непонятно, свежая цифра или недельной давности
    for col in ("rating", "review_count", "bsr_num", "price_num"):
        ok = hist.dropna(subset=[col]).groupby("asin").last()
        latest[col] = latest["asin"].map(ok[col])
        latest[f"{col}_at"] = latest["asin"].map(ok["created_at"])
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
        lines = [f"📊 <b>Конкуренты — {esc(mkt)}</b>", f"<i>{now:%d.%m.%Y %H:%M}</i>", ""]
        printed = 0
        facts = []
        for g in sorted({x for x in part_m["grp"] if x}):
            part = part_m[part_m["grp"] == g]
            ours, comps = part[part["own"]], part[~part["own"]]
            # «лучший конкурент» ищем только среди тех, у кого база отзывов не игрушечная
            solid = comps[comps["review_count"].fillna(0) >= MIN_REVIEWS]
            lines.append(f"📌 <b>{esc(g)}</b> — {len(part)} (наших {len(ours)}, конкурентов {len(comps)})")

            def _age(idx_row, col):
                """Помечаем цифру датой, если она не из последнего прогона."""
                ts = idx_row.get(f"{col}_at")
                if pd.isna(ts):
                    return ""
                ts = pd.to_datetime(ts)
                hours = (pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 3600
                if hours <= 30:
                    return ""
                return f" ⏳ от {ts.tz_convert(ZoneInfo(TZ)):%d.%m}"

            def cmp_line(label, col, better="max", fmt="{:.1f}"):
                """Бренд сильнейшего конкурента показываем всегда. Если цифра
                не из свежего прогона — рядом дата, чтобы не принять её за сегодняшнюю."""
                o = ours[col].dropna()
                c = solid[col].dropna()
                if o.empty:
                    lines.append(f"  • {label}: у нас данных нет")
                    return
                oidx = o.idxmax() if better == "max" else o.idxmin()
                ov = o.loc[oidx]
                o_age = _age(ours.loc[oidx], col)
                if c.empty:
                    lines.append(f"  • {label}: у нас {_n(fmt, ov)}{o_age} · "
                                 "сопоставимых конкурентов нет")
                    return
                idx = c.idxmax() if better == "max" else c.idxmin()
                cv, cb = c.loc[idx], (esc(str(solid.loc[idx, "brand"])[:22]) or "конкурент")
                c_age = _age(solid.loc[idx], col)
                win = ov >= cv if better == "max" else ov <= cv
                lines.append(f"  • {label}: у нас {_n(fmt, ov)}{o_age} · сильнейший "
                             f"{_n(fmt, cv)}{c_age} ({cb}) — "
                             + ("мы впереди 🟢" if win else "отстаём 🔴"))

            cmp_line("Рейтинг", "rating", "max", "{:.1f}")
            cmp_line("BSR", "bsr_num", "min", "{:,.0f}")
            cmp_line("Отзывы", "review_count", "max", "{:,.0f}")
            po, pc = ours["price_num"].dropna(), solid["price_num"].dropna()
            pricier = False
            if not po.empty and not pc.empty:
                avg = pc.mean()
                pricier = po.mean() > avg
                lines.append(f"  • Цена: у нас {po.mean():.2f} · средняя у конкурентов {avg:.2f} — "
                             + ("мы дороже 🔴" if pricier else "мы дешевле 🟢"))

            for pcol, plabel in (("coupon", "Купоны"), ("prime_excl", "Prime")):
                if pcol not in part.columns:
                    continue
                o_on = int(ours[pcol].fillna(False).astype(bool).sum())
                c_on = int(comps[pcol].fillna(False).astype(bool).sum())
                if o_on == 0 and c_on == 0:
                    lines.append(f"  • {plabel}: нет ни у кого 🟢")
                elif c_on and not o_on:
                    lines.append(f"  • {plabel}: у нас нет · у конкурентов {c_on} из {len(comps)} 🔴")
                elif o_on and not c_on:
                    lines.append(f"  • {plabel}: у нас {o_on} · у конкурентов нет 🟢")
                else:
                    lines.append(f"  • {plabel}: у нас {o_on} из {len(ours)} · "
                                 f"у конкурентов {c_on} из {len(comps)}")

            def _lag(col, better):
                o, c = ours[col].dropna(), solid[col].dropna()
                if o.empty or c.empty:
                    return False, None, None, ""
                ov = o.max() if better == "max" else o.min()
                idx = c.idxmax() if better == "max" else c.idxmin()
                cv = c.loc[idx]
                lag = ov < cv if better == "max" else ov > cv
                return bool(lag), float(ov), float(cv), str(solid.loc[idx, "brand"])[:22]

            lr, _, _, _ = _lag("rating", "max")
            lb, ob, cb_, bb = _lag("bsr_num", "min")
            lv, _, _, _ = _lag("review_count", "max")
            facts.append({"group": g, "lag_rating": lr, "lag_bsr": lb, "lag_reviews": lv,
                          "pricier": pricier, "our_bsr": ob, "best_bsr": cb_, "best_bsr_brand": bb})
            lines.append("")
            printed += 1
        if printed:
            tail = (f"<i>«Сильнейший» — среди конкурентов с {MIN_REVIEWS}+ отзывами, "
                    f"новички с парой оценок не в счёт. ⏳ — цифра не из последнего "
                    f"прогона: Amazon отдал не все данные, показано последнее известное.</i>\n"
                    f"<a href=\"{DASHBOARD_URL}\">Открыть дашборд →</a>")
            reports[mkt] = "\n".join(lines) + make_takeaways(facts) + "\n" + tail

    return reports or None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default=None, help="только одна страна, например DE")
    ap.add_argument("--days", type=int, default=10, help="за сколько дней брать последний замер")
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
