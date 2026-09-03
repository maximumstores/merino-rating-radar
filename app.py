import json
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

from collector import (
    add_tracked_asins,
    check_asin,
    ensure_schema,
    finish_run,
    get_tracked_asins,
    remove_tracked_asin,
    save_to_db,
    start_run,
)

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(page_title="Rating Radar Dashboard", layout="wide", page_icon="📊")

st.title("📊 Rating Radar Dashboard")

# --- Блок управления сбором ASIN ---

KYIV = ZoneInfo("Europe/Kyiv")


def get_last_run():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        row = pd.read_sql(
            "SELECT started_at, finished_at, asin_count, ok_count, status "
            "FROM collection_runs ORDER BY started_at DESC LIMIT 1",
            conn,
        )
        conn.close()
        return row.iloc[0] if not row.empty else None
    except Exception:
        return None


last_run = get_last_run()
if last_run is not None:
    started_kyiv = pd.to_datetime(last_run["started_at"]).tz_convert(KYIV)
    status_label = "✅ завершён" if last_run["status"] == "done" else "⏳ идёт"
    st.info(
        f"Последний сбор: {started_kyiv:%d.%m.%Y %H:%M} по Киеву — {status_label} · "
        f"{int(last_run['ok_count'] or 0)}/{int(last_run['asin_count'] or 0)} чистых (BE/NL)"
    )
else:
    st.warning("Прогонов ещё не было")

st.caption("Плановый сбор идёт ежедневно в 09:00 по Киеву (Планировщик на сервере)")

st.markdown("### 📋 Общий список ASIN (для планового сбора)")
tracked = get_tracked_asins()
st.caption(f"Сейчас в списке: {len(tracked)} ASIN")

with st.expander("Добавить ASIN в общий список"):
    new_asins_input = st.text_area(
        "По одного на строку или через запятую",
        height=100,
        key="add_tracked",
    )
    if st.button("Добавить в список"):
        new_asins = [
            a.strip().upper()
            for a in re.split(r"[\s,]+", new_asins_input)
            if a.strip()
        ]
        if new_asins:
            ensure_schema()
            add_tracked_asins(new_asins)
            st.success(f"Добавлено: {len(new_asins)}. Обнови страницу.")

st.markdown("### 🚀 Собрать сейчас")
col_a, col_b = st.columns(2)

with col_a:
    st.markdown("**Весь общий список** (как в плановом сборе)")
    if st.button(f"Запустить сбор по всем {len(tracked)} ASIN"):
        if not tracked:
            st.warning("Список пуст — добавь ASIN выше")
        else:
            ensure_schema()
            run_id = start_run(len(tracked))
            progress = st.progress(0.0, text=f"0/{len(tracked)}")
            log_box = st.empty()
            log_lines = []
            ok = 0
            for i, asin in enumerate(tracked, 1):

                def _log(msg, _lines=log_lines):
                    _lines.append(msg)
                    log_box.code("\n".join(_lines[-15:]))

                try:
                    res = check_asin(asin, log=_log)
                    save_to_db(res)
                    if res.get("source") in ("BE", "NL"):
                        ok += 1
                except Exception as e:
                    _log(f"❌ {asin}: ошибка {e}")
                progress.progress(i / len(tracked), text=f"{i}/{len(tracked)}")
            finish_run(run_id, ok, "done")
            st.success(f"Готово: {ok}/{len(tracked)} чистых. Обнови страницу.")

with col_b:
    st.markdown("**Точечная проверка** (не влияет на общий список)")
    with st.form("ad_hoc_form"):
        adhoc_input = st.text_area("ASIN для разовой проверки", height=100)
        adhoc_submit = st.form_submit_button("Проверить эти ASIN")
    if adhoc_submit:
        adhoc_asins = [
            a.strip().upper()
            for a in re.split(r"[\s,]+", adhoc_input)
            if a.strip()
        ]
        if adhoc_asins:
            ensure_schema()
            run_id = start_run(len(adhoc_asins))
            progress = st.progress(0.0, text=f"0/{len(adhoc_asins)}")
            log_box = st.empty()
            log_lines = []
            ok = 0
            for i, asin in enumerate(adhoc_asins, 1):

                def _log(msg, _lines=log_lines):
                    _lines.append(msg)
                    log_box.code("\n".join(_lines[-15:]))

                try:
                    res = check_asin(asin, log=_log)
                    save_to_db(res)
                    if res.get("source") in ("BE", "NL"):
                        ok += 1
                except Exception as e:
                    _log(f"❌ {asin}: ошибка {e}")
                progress.progress(
                    i / len(adhoc_asins), text=f"{i}/{len(adhoc_asins)}"
                )
            finish_run(run_id, ok, "done")
            st.success(
                f"Готово: {ok}/{len(adhoc_asins)} чистых. Обнови страницу."
            )

# --- Основная логика работы с базой и отображением метрик ---


def get_data():
    if not DATABASE_URL:
        st.error("DATABASE_URL не найден в файле .env")
        return pd.DataFrame()
    try:
        conn = psycopg2.connect(DATABASE_URL)
        query = """
            SELECT 
                id,
                asin,
                source,
                rating,
                review_count,
                histogram_json,
                note,
                created_at
            FROM asin_metrics 
            ORDER BY created_at DESC;
        """
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        st.error(f"Ошибка подключения к базе данных: {e}")
        return pd.DataFrame()


df = get_data()

if df.empty:
    st.warning(
        "В базе данных пока нет записей. Запустите скрипт сборщика `radar_check.py`!"
    )
else:
    st.sidebar.header("Фильтры")
    all_asins = df["asin"].dropna().unique().tolist()
    all_sources = df["source"].dropna().unique().tolist()

    selected_asins = st.sidebar.multiselect(
        "Выберите ASIN", options=all_asins, default=all_asins
    )
    selected_sources = st.sidebar.multiselect(
        "Источник данных", options=all_sources, default=all_sources
    )

    filtered_df = df[
        (df["asin"].isin(selected_asins))
        & (df["source"].isin(selected_sources))
    ]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Всего проверок", len(filtered_df))
    col2.metric("Уникальных ASIN", filtered_df["asin"].nunique())

    avg_rating = filtered_df["rating"].dropna().mean()
    col3.metric(
        "Средний рейтинг",
        f"{avg_rating:.2f}" if not pd.isna(avg_rating) else "—",
    )

    total_reviews = filtered_df["review_count"].dropna().sum()
    col4.metric("Сумма отзывов", f"{int(total_reviews):,}")

    st.markdown("---")

    st.subheader("📋 История сбора данных")
    display_df = filtered_df.drop(columns=["histogram_json"]).copy()
    st.dataframe(display_df, use_container_width=True)

    st.markdown("---")
    st.subheader("⭐ Распределение звёзд (Гистограмма)")

    if not filtered_df.empty:
        selected_asin = st.selectbox(
            "Выберите ASIN для детализации",
            options=filtered_df["asin"].unique(),
        )
        row = filtered_df[filtered_df["asin"] == selected_asin].iloc[0]
        hist_raw = row["histogram_json"]

        if hist_raw:
            hist_dict = (
                json.loads(hist_raw)
                if isinstance(hist_raw, str)
                else hist_raw
            )
            if hist_dict:
                hist_df = (
                    pd.DataFrame(
                        list(hist_dict.items()), columns=["Звёзды", "Процент"]
                    )
                    .astype({"Звёзды": int, "Процент": int})
                    .sort_values("Звёзды", ascending=False)
                )
                hist_df["Звёзды"] = hist_df["Звёзды"].astype(str) + " ★"
                st.bar_chart(hist_df.set_index("Звёзды"))
            else:
                st.info("Гистограмма для этого ASIN пуста.")
        else:
            st.info("Данные о гистограмме отсутствуют.") 
