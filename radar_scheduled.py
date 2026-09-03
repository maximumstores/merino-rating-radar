#!/usr/bin/env python3
"""Плановый прогон для Планировщика Windows.

Список ASIN берётся из БД (таблица tracked_asins) — тот же список,
что виден и редактируется в дашборде. Не из локального файла.

Планировщик:
    schtasks /create /tn "RatingRadarDaily" ^
        /tr "python C:\\...\\merino-rating-radar\\radar_scheduled.py" ^
        /sc daily /st 09:00 /ru SYSTEM
"""

from datetime import datetime
import os
import sys
import time

# Определение директории скрипта и её добавление в sys.path
# Это решает проблему ModuleNotFoundError: No module named 'collector'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from collector import (  # noqa: E402
    check_asin,
    ensure_schema,
    finish_run,
    get_tracked_asins,
    save_to_db,
    start_run,
)

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")


def main():
    ensure_schema()
    asins = get_tracked_asins()
    if not asins:
        sys.exit(
            "Список tracked_asins пуст — добавь ASIN через дашборд перед первым плановым прогоном"
        )

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(
        LOG_DIR, f"scheduled_{datetime.now():%Y%m%d_%H%M%S}.log"
    )
    run_id = start_run(len(asins))
    ok = 0

    with open(log_path, "w", encoding="utf-8") as fh:

        def log(msg: str):
            print(msg)
            fh.write(msg + "\n")
            fh.flush()

        log(f"Плановый прогон #{run_id}: {len(asins)} ASIN")
        for i, asin in enumerate(asins, 1):
            try:
                res = check_asin(asin, log=log)
                save_to_db(res)
                if res.get("source") in ("BE", "NL"):
                    ok += 1
            except Exception as e:
                log(f"❌ {asin}: ошибка {e}")
            log(f"[{i}/{len(asins)}] готово\n")
            time.sleep(0.5)

        finish_run(run_id, ok, "done")
        log(f"Итог: {ok}/{len(asins)} чистых (BE/NL). Лог: {log_path}")


if __name__ == "__main__":
    main()
