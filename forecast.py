"""
Расчёт тренда и прогноза рейтинга по накопленной истории asin_metrics.
Ничего не пишет в базу — чистые функции над DataFrame из get_data().

Использование в app.py:
    from forecast import build_forecast_table
    forecast_df = build_forecast_table(df)
    st.dataframe(forecast_df)
"""

import numpy as np
import pandas as pd

FORECAST_HORIZONS_DAYS = {
    "0.5 мес": 15,
    "1 мес": 30,
    "2 мес": 60,
    "3 мес": 90,
    "6 мес": 180,
}

RED_THRESHOLD = 4.2
LOW_STAR_YELLOW_PCT = 15
MIN_RATINGS_FOR_CONFIDENCE = 25


def _linreg_slope(days: np.ndarray, ratings: np.ndarray) -> tuple[float, float]:
    """Наименьшие квадраты: возвращает (наклон в рейтинге/день, intercept)."""
    if len(days) < 2:
        return 0.0, ratings[-1] if len(ratings) else np.nan
    A = np.vstack([days, np.ones_like(days)]).T
    slope, intercept = np.linalg.lstsq(A, ratings, rcond=None)[0]
    return slope, intercept


def _low_star_pct(hist: dict) -> float | None:
    if not hist:
        return None
    total = sum(hist.values())
    if total == 0:
        return None
    low = hist.get(1, 0) + hist.get(2, 0)
    return round(100 * low / total, 1)


def _margin_to_threshold(rating: float, count: int, threshold: float = 4.0) -> int | None:
    """Сколько единиц (1★) нужно добавить, чтобы средний упал ниже threshold."""
    if rating is None or count is None or count <= 0 or rating <= threshold:
        return 0
    # текущая сумма баллов
    total_score = rating * count
    n = 0
    cur_rating = rating
    cur_count = count
    cur_score = total_score
    while cur_rating > threshold and n < 100000:
        cur_score += 1  # добавляем единицу
        cur_count += 1
        cur_rating = cur_score / cur_count
        n += 1
    return n


def _flag(rating: float, count: int, low_pct: float | None, slope_per_day: float) -> str:
    if rating is None:
        return "⚪"
    if rating < RED_THRESHOLD:
        return "🔴"
    trend_down = slope_per_day < -0.001  # заметное падение
    thin_data = count is not None and count < MIN_RATINGS_FOR_CONFIDENCE
    high_low_pct = low_pct is not None and low_pct >= LOW_STAR_YELLOW_PCT
    if trend_down or thin_data or high_low_pct:
        return "🟡"
    return "🟢"


def build_forecast_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    df — результат get_data(): все строки asin_metrics, включая историю по каждому ASIN.
    Возвращает по одной строке на ASIN с трендом, прогнозом на горизонты и флагом.
    """
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["created_at"] = pd.to_datetime(df["created_at"])
    rows = []

    for asin, g in df.groupby("asin"):
        g = g.sort_values("created_at")
        g = g.dropna(subset=["rating"])
        if g.empty:
            continue

        latest = g.iloc[-1]
        t0 = g["created_at"].min()
        days = (g["created_at"] - t0).dt.total_seconds() / 86400
        slope, intercept = _linreg_slope(days.values, g["rating"].values)

        last_day = days.iloc[-1]
        current_rating = float(latest["rating"])
        count = int(latest["review_count"]) if pd.notna(latest["review_count"]) else None

        hist = latest.get("histogram_json")
        if isinstance(hist, str):
            import json
            hist = json.loads(hist) if hist else {}
        low_pct = _low_star_pct(hist or {})

        margin = _margin_to_threshold(current_rating, count)
        flag = _flag(current_rating, count, low_pct, slope)

        row = {
            "asin": asin,
            "источник": latest.get("source"),
            "рейтинг_текущий": round(current_rating, 2),
            "кол-во": count,
            "1-2★_%": low_pct,
            "тренд_в_день": round(slope, 5),
            "запас_до_4.0": margin,
            "флаг": flag,
            "точек_истории": len(g),
        }
        # прогноз на горизонты — только если есть хотя бы 2 точки, иначе плоская линия
        for label, horizon_days in FORECAST_HORIZONS_DAYS.items():
            forecast_day = last_day + horizon_days
            predicted = intercept + slope * forecast_day if len(g) >= 2 else current_rating
            predicted = max(1.0, min(5.0, predicted))  # клип в разумные границы
            row[label] = round(predicted, 2)

        rows.append(row)

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    flag_order = {"🔴": 0, "🟡": 1, "🟢": 2, "⚪": 3}
    result["_sort"] = result["флаг"].map(flag_order)
    result = result.sort_values("_sort").drop(columns="_sort").reset_index(drop=True)
    return result
