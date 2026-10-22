"""
Прогноз рейтинга по всем ASIN для Rating Radar.

Считается по расписанию (GitHub Actions, после сбора), результат пишется в таблицу
asin_forecast. Дашборд читает готовую таблицу — страница не пересчитывает 1200 ASIN
при каждом открытии.

Логика:
  1. Замеры сводятся к одному значению в день (последний за сутки), чтобы дни с несколькими
     замерами не перевешивали остальные.
  2. По каждому ASIN — линейный тренд за окно (до MAX_WINDOW_DAYS дней).
  3. Горизонт ограничен половиной длины истории ASIN (но не короче 7 дней): прогноз не уходит
     дальше, чем позволяют данные.
  4. Считается прогноз, 95% интервал, дни до порога 4.2 и статус.
  5. ASIN с < MIN_DAYS днями данных помечается «мало данных» и не прогнозируется.

Запуск:
    python forecast.py                      # посчитать и показать сводку (в базу не пишет)
    python forecast.py --save               # посчитать и записать в asin_forecast
    python forecast.py --save --if-new-data # то же, но только если после прошлого расчёта появились замеры
    python forecast.py --horizon 21         # другой горизонт (по умолчанию FORECAST_HORIZON или 14)

Из кода:
    from forecast import compute_portfolio_forecast, load_forecast
"""
import argparse
import os

import numpy as np
import pandas as pd

RISK = 4.2          # красная зона по логике Amazon
WARN = 4.5
MIN_DAYS = 5        # минимум дней с данными для прогноза
MIN_SPAN_DAYS = 14  # минимум календарного охвата: на 7–8 днях наклон — случайность
MAX_WINDOW_DAYS = 60
MAX_BAND = 1.0      # если 95% интервал шире 1★ — прогноз ненадёжен, число не показываем
# рейтинг по горстке оценок — шум, а не тренд; константа та же, что в старом forecast.py (25)
MIN_REVIEWS = int(os.environ.get("FORECAST_MIN_REVIEWS", "25"))
DEFAULT_HORIZON = int(os.environ.get("FORECAST_HORIZON", "14"))

STATUS_ORDER = {"уже в риске": 0, "пробьёт 4.2": 1, "возможен риск": 2, "падает": 3,
                "стабильно": 4, "растёт": 5, "мало данных": 6, "мало отзывов": 7, "ряд шумный": 8}


# ---------------------------------------------------------------- расчёт
def to_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Один замер на ASIN в день (последний за сутки), только валидный рейтинг."""
    d = df.copy()
    d["created_at"] = pd.to_datetime(d["created_at"], utc=True)
    d["rating"] = pd.to_numeric(d["rating"], errors="coerce")
    d = d.dropna(subset=["rating"])
    d["day"] = d["created_at"].dt.floor("D")
    return d.sort_values("created_at").groupby(["asin", "day"], as_index=False).last()


def _fit_one(g: pd.DataFrame, horizon: int, now_day: pd.Timestamp) -> dict:
    g = g.sort_values("day")
    g = g[g["day"] >= g["day"].max() - pd.Timedelta(days=MAX_WINDOW_DAYS)]
    n = len(g)
    span = int((g["day"].max() - g["day"].min()).days)
    cur = float(g["rating"].iloc[-1])
    rev = None
    if "review_count" in g.columns:
        rc = pd.to_numeric(g["review_count"], errors="coerce").dropna()
        rev = int(rc.iloc[-1]) if len(rc) else None
    out = {
        "asin": g["asin"].iloc[0],
        "source": g["source"].iloc[-1] if "source" in g.columns else None,
        "days_with_data": n,
        "span_days": span,
        "rating_now": cur,
        "reviews": rev,
        "last_day": g["day"].max().date(),
        "stale_days": int((now_day - g["day"].max()).days),
        "slope_30d": np.nan,
        "horizon_used": np.nan,
        "forecast": np.nan,
        "forecast_lo": np.nan,
        "forecast_hi": np.nan,
        "days_to_risk": np.nan,
        "status": "",
    }
    if n < MIN_DAYS or span < MIN_SPAN_DAYS:
        out["status"] = "мало данных"
        return out
    if rev is not None and rev < MIN_REVIEWS:
        # 1–3 оценки дают рейтинги 1.0 / 3.5 и скачки на ±1★ от одной оценки — прогнозировать нечего
        out["status"] = "мало отзывов"
        return out

    x = (g["day"] - g["day"].min()).dt.total_seconds().values / 86400.0
    y = g["rating"].values.astype(float)
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    slope = ((x - xm) * (y - ym)).sum() / sxx if sxx > 0 else 0.0
    icpt = ym - slope * xm
    resid = y - (icpt + slope * x)
    s = float(np.sqrt((resid ** 2).sum() / max(n - 2, 1)))

    h = int(min(horizon, max(7, span // 2)))
    x_new = x.max() + h
    pred = icpt + slope * x_new
    se = s * np.sqrt(1 + 1 / n + (x_new - xm) ** 2 / sxx) if sxx > 0 else s
    lo, hi = pred - 1.96 * se, pred + 1.96 * se
    clip = lambda v: float(np.clip(v, 1.0, 5.0))

    out.update(slope_30d=float(slope * 30), horizon_used=h)
    reliable = (hi - lo) <= MAX_BAND
    if reliable:
        out.update(forecast=clip(pred), forecast_lo=clip(lo), forecast_hi=clip(hi))
    if reliable and slope < 0 and cur > RISK:
        d_risk = float((cur - RISK) / -slope)
        if d_risk <= 120:  # дальше 4 месяцев — не информативно
            out["days_to_risk"] = d_risk

    if cur <= RISK:
        out["status"] = "уже в риске"
    elif not reliable:
        out["status"] = "ряд шумный"   # рейтинг скачет, интервал шире 1★ — прогноза нет
    elif pred <= RISK:
        out["status"] = "пробьёт 4.2"
    elif lo <= RISK and slope < 0:
        out["status"] = "возможен риск"
    elif cur < WARN and slope * 30 < -0.05:  # падение > 0.05★ в месяц
        out["status"] = "падает"
    elif slope * 30 > 0.05:
        out["status"] = "растёт"
    else:
        out["status"] = "стабильно"
    return out


def compute_portfolio_forecast(df: pd.DataFrame, horizon: int = DEFAULT_HORIZON) -> pd.DataFrame:
    """df: строки asin_metrics (asin, source, rating, created_at)."""
    daily = to_daily(df)
    if daily.empty:
        return pd.DataFrame()
    now_day = daily["day"].max()
    res = pd.DataFrame([_fit_one(g, horizon, now_day) for _, g in daily.groupby("asin")])
    res["_o"] = res["status"].map(STATUS_ORDER)
    return (res.sort_values(["_o", "days_to_risk", "forecast"])
               .drop(columns="_o").reset_index(drop=True))


# ---------------------------------------------------------------- база
def _connect():
    import psycopg2
    from dotenv import load_dotenv

    load_dotenv()
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS asin_forecast (
                asin           TEXT PRIMARY KEY,
                source         TEXT,
                computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                rating_now     REAL,
                reviews        INTEGER,
                forecast       REAL,
                forecast_lo    REAL,
                forecast_hi    REAL,
                slope_30d      REAL,
                days_to_risk   REAL,
                horizon_used   INTEGER,
                days_with_data INTEGER,
                span_days      INTEGER,
                status         TEXT
            )""")
        cur.execute("ALTER TABLE asin_forecast ADD COLUMN IF NOT EXISTS reviews INTEGER")
    conn.commit()


def load_history(conn) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT asin, source, rating, review_count, created_at
        FROM asin_metrics
        WHERE asin NOT LIKE 'HTTP%%' AND LENGTH(asin) <= 10
          AND created_at >= now() - interval '%d days'
        ORDER BY created_at ASC
        """ % (MAX_WINDOW_DAYS + 5),
        conn,
    )


def has_new_data(conn) -> bool:
    """True, если замеры появились после последнего расчёта (или расчёта ещё не было)."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(computed_at) FROM asin_forecast")
        last_calc = cur.fetchone()[0]
        if last_calc is None:
            return True
        cur.execute("SELECT MAX(created_at) FROM asin_metrics")
        last_meas = cur.fetchone()[0]
    return bool(last_meas and last_meas > last_calc)


def save_forecast(conn, pf: pd.DataFrame) -> int:
    from psycopg2.extras import execute_values

    ensure_table(conn)
    cols = ["asin", "source", "rating_now", "reviews", "forecast", "forecast_lo", "forecast_hi",
            "slope_30d", "days_to_risk", "horizon_used", "days_with_data", "span_days", "status"]
    rows = []
    for r in pf[cols].itertuples(index=False):
        rows.append(tuple(None if (isinstance(v, float) and np.isnan(v)) else
                          (v.item() if hasattr(v, "item") else v) for v in r))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE asin_forecast")  # всегда актуальный срез: ушедшие ASIN не висят
        execute_values(cur, f"""
            INSERT INTO asin_forecast ({', '.join(cols)}) VALUES %s""", rows)
    conn.commit()
    return len(rows)


def load_forecast(conn=None) -> pd.DataFrame:
    """Готовый прогноз для дашборда. Пустой DataFrame, если таблицы/данных ещё нет."""
    own = conn is None
    try:
        conn = conn or _connect()
        df = pd.read_sql("SELECT * FROM asin_forecast", conn)
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    ap.add_argument("--save", action="store_true", help="записать результат в asin_forecast")
    ap.add_argument("--if-new-data", action="store_true",
                    help="считать только если после прошлого расчёта появились замеры")
    ap.add_argument("--csv", default="", help="дополнительно сохранить в CSV")
    args = ap.parse_args()

    conn = _connect()
    try:
        ensure_table(conn)
        if args.if_new_data and not has_new_data(conn):
            print("новых замеров после прошлого расчёта нет — пропуск")
            return
        pf = compute_portfolio_forecast(load_history(conn), args.horizon)
        if pf.empty:
            print("нет данных для прогноза")
            return
        print(f"ASIN: {len(pf)}, горизонт запрошен: {args.horizon} дн.")
        print(pf["status"].value_counts().to_string())
        if args.save:
            print(f"записано в asin_forecast: {save_forecast(conn, pf)}")
        if args.csv:
            pf.to_csv(args.csv, index=False, encoding="utf-8-sig")
            print("CSV:", args.csv)
        soon = pf[pf["status"].isin(["пробьёт 4.2", "возможен риск", "падает"])]
        cols0 = ["asin", "source", "rating_now", "reviews", "forecast", "forecast_lo", "forecast_hi",
                 "slope_30d", "days_to_risk", "days_with_data", "status"]
        print("\nЕщё НЕ красные, но идут к 4.2 (ради этого прогноз):")
        print(soon[cols0].head(30).round(3).to_string(index=False) if not soon.empty else "  нет")
        risky = pf[pf["status"].isin(["уже в риске"])]
        cols = ["asin", "source", "rating_now", "reviews", "forecast", "forecast_lo", "forecast_hi",
                "slope_30d", "days_to_risk", "days_with_data", "status"]
        print("\nУже красные (рейтинг ≤ 4.2 сейчас), топ-20:")
        print(risky[cols].head(20).round(3).to_string(index=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
