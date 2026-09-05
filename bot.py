#!/usr/bin/env python3
"""Rating Radar Telegram bot (@RatingRadar_bot) — long polling.

Нужен ТОЛЬКО если хочется мгновенных ответов на команды. Без него дашборд
сам разбирает очередь команд при загрузке страницы и после каждого прогона
(notifier.process_updates), просто с задержкой.

Запуск отдельным процессом:
    python bot.py
"""

import time
import traceback

from notifier import BOT_TOKEN, ensure_subs_schema, get_state, handle_command, set_state, tg_call

POLL_TIMEOUT = 50


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    ensure_subs_schema()
    offset = int(get_state("update_offset", 0) or 0)
    print("bot started, offset =", offset)
    while True:
        try:
            res = tg_call("getUpdates", offset=offset + 1, timeout=POLL_TIMEOUT,
                          allowed_updates=["message"])
            if not res.get("ok"):
                time.sleep(5)
                continue
            for upd in res.get("result", []):
                offset = max(offset, upd["update_id"])
                if "message" in upd:
                    try:
                        handle_command(upd["message"])
                    except Exception:
                        traceback.print_exc()
            if res.get("result"):
                set_state("update_offset", offset)
        except KeyboardInterrupt:
            break
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
