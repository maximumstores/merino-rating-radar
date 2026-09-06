#!/usr/bin/env python3
"""radar_scheduled.py — сбор по расписанию без браузера.

Запуск раз в минуту по крону:  * * * * * cd /path && python radar_scheduled.py
Скрипт сам решает, пора ли: сравнивает текущее время с auto_time из базы
и проверяет, что сегодня сбора ещё не было.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

from collector import (check_asin_api, ensure_schema, finish_run,
                       get_tracked_asins, save_to_db, start_run)

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TZ = os.environ.get("RADAR_TZ", "Europe/Kyiv")
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
            cur.execute("SELECT started_at FROM collection_runs ORDER BY started_at DESC LIMIT 1")
            row = cur.fetchone()
        if not row or not row[0]:
            return False
        last = row[0].astimezone(ZoneInfo(TZ)).date()
        return last == datetime.now(ZoneInfo(TZ)).date()
    except Exception:
        return False


def markets_map():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT asin, market FROM asin_dictionary WHERE market <> ''")
            return dict(cur.fetchall())
    except Exception:
        return {}


def run():
    ensure_schema()
    tracked = get_tracked_asins()
    if not tracked:
        print("список пуст")
        return
    mk = markets_map()
    run_id = start_run(len(tracked))
    ok = 0
    for i, asin in enumerate(tracked, 1):
        try:
            res = check_asin_api(asin, market=mk.get(asin), log=lambda m: None)
        except Exception as e:
            res = {"asin": asin, "source": "none", "rating": None, "count": None,
                   "hist": {}, "image_url": None, "bsr": None, "note": f"ошибка: {e}"[:200]}
        try:
            save_to_db(res)
        except Exception as e:
            print(f"не сохранён {asin}: {e}")
        if res.get("source") in VALID:
            ok += 1
        if i % 10 == 0:
            print(f"{i}/{len(tracked)}")
    finish_run(run_id, ok, "done")
    print(f"готово: {ok}/{len(tracked)}")

    try:
        import notifier
        sent, total = notifier.notify_all(header="Rating Radar — ежедневный сбор")
        print(f"telegram: {sent}/{total}")
    except Exception as e:
        print("telegram error:", e)


if __name__ == "__main__":
    if str(get_setting("auto_enabled", "0")) != "1":
        print("автосбор выключен в настройках")
        raise SystemExit(0)
    target = str(get_setting("auto_time", "13:00"))
    now = datetime.now(ZoneInfo(TZ))
    if now.strftime("%H:%M") < target:
        raise SystemExit(0)
    if already_ran_today():
        print("сегодня уже собирали")
        raise SystemExit(0)
    run() 
