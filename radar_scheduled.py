#!/usr/bin/env python3
"""radar_scheduled.py — сбор по расписанию без браузера.

Запуск:
    python radar_scheduled.py                    # весь список
    python radar_scheduled.py --shard 0 --shards 4    # только своя четверть
    python radar_scheduled.py --force            # игнорировать проверку времени

Параллельность задаётся переменной RADAR_WORKERS (по умолчанию 6).
Шардирование нужно, когда список большой: N джобов GitHub Actions идут
одновременно, каждый берёт свою часть по остатку от деления хеша ASIN.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

from collector import (check_asin_api, ensure_schema, finish_run,
                       get_tracked_asins, save_batch, start_run)

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TZ = os.environ.get("RADAR_TZ", "Europe/Kyiv")
WORKERS = int(os.environ.get("RADAR_WORKERS", "6"))
VALID = ("US", "BE", "NL", "DE", "UK", "FR", "IT", "ES")


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


def already_ran_today():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT started_at FROM collection_runs "
                        "WHERE status = 'done' ORDER BY started_at DESC LIMIT 1")
            row = cur.fetchone()
        if not row or not row[0]:
            return False
        return row[0].astimezone(ZoneInfo(TZ)).date() == datetime.now(ZoneInfo(TZ)).date()
    except Exception:
        return False


def markets_map():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT asin, market FROM asin_dictionary WHERE market <> ''")
            return dict(cur.fetchall())
    except Exception:
        return {}


def pick_shard(asins, shard, shards):
    """Делит список стабильно: один и тот же ASIN всегда попадает в тот же шард."""
    if shards <= 1:
        return asins
    return [a for a in asins if sum(ord(ch) for ch in a) % shards == shard]


BATCH_SIZE = int(os.environ.get("RADAR_BATCH", "50"))


def collect_one(asin, market):
    """Один ASIN. Только сбор — запись идёт пачками в run(), чтобы не жечь коннекты."""
    try:
        return check_asin_api(asin, market=market, log=lambda m: None)
    except Exception as e:
        return {"asin": asin, "source": "none", "rating": None, "count": None,
                "hist": {}, "image_url": None, "bsr": None, "note": f"ошибка: {e}"[:200]}


def run(shard=0, shards=1):
    ensure_schema()
    tracked = pick_shard(get_tracked_asins(), shard, shards)
    if not tracked:
        print("нечего собирать в этом шарде")
        return

    mk = markets_map()
    label = f"шард {shard + 1}/{shards}" if shards > 1 else "полный список"
    print(f"старт: {len(tracked)} ASIN ({label}), потоков {WORKERS}", flush=True)

    run_id = start_run(len(tracked))
    started = time.time()
    ok = done = 0

    buffer = []

    def flush():
        nonlocal buffer
        if not buffer:
            return
        try:
            save_batch(buffer)
        except Exception as e:
            print(f"  пачка не сохранилась ({len(buffer)}): {e}", flush=True)
        buffer = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(collect_one, a, mk.get(a)) for a in tracked]
        for fut in as_completed(futures):
            done += 1
            try:
                res = fut.result()
                buffer.append(res)
                ok += int(res.get("source") in VALID)
            except Exception as e:
                print(f"  сбой: {e}", flush=True)
            if len(buffer) >= BATCH_SIZE:
                flush()
            if done % 100 == 0 or done == len(tracked):
                el = time.time() - started
                rate = done / el if el else 0
                left = (len(tracked) - done) / rate if rate else 0
                print(f"  {done}/{len(tracked)} · ок {ok} · {el / 60:.1f} мин · "
                      f"осталось ~{left / 60:.1f} мин", flush=True)
    flush()

    finish_run(run_id, ok, "done")
    print(f"готово: {ok}/{len(tracked)} за {(time.time() - started) / 60:.1f} мин", flush=True)


def notify():
    try:
        import notifier
        sent, total = notifier.notify_all(header="Rating Radar — ежедневный сбор")
        print(f"telegram: {sent}/{total}")
    except Exception as e:
        print("telegram error:", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="не проверять время и факт сбора")
    ap.add_argument("--notify", action="store_true", help="разослать алерты после сбора")
    args = ap.parse_args()

    if not args.force:
        if str(get_setting("auto_enabled", "0")) != "1":
            print("автосбор выключен в настройках")
            sys.exit(0)
        target = str(get_setting("auto_time", "13:00"))
        now = datetime.now(ZoneInfo(TZ))
        if now.strftime("%H:%M") < target:
            sys.exit(0)
        # проверку «уже собирали» делает только первый шард,
        # остальные ориентируются на него через свой же запуск в матрице
        if args.shard == 0 and already_ran_today():
            print("сегодня уже собирали")
            sys.exit(0)

    run(shard=args.shard, shards=max(1, args.shards))
    if args.notify:
        notify()
