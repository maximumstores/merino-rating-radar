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
    clean_db_trash,
    delete_asin_completely,
    ensure_schema,
    extract_asin,
    finish_run,
    get_tracked_asins,
    save_to_db,
    start_run,
)

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(page_title="Rating Radar Dashboard", layout="wide", page_icon="📊")

# Авточистка мусорных записей из БД
clean_db_trash()

st.title("📊 Rating Radar Dashboard")

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
        "По одному на строку, ссылкой или через запятую",
        height=100,
        key="add_tracked",
    )
    if st.button("Добавить в список"):
        raw_list = [a.strip() for a in re.split(r"[\s,]+", new_asins_input) if a.strip()]
        new_asins = [extract_asin(a) for a in raw_list if extract_asin(a)]
        if new_asins:
            ensure_schema()
            add_tracked_asins(new_asins)
            st.success(f"Добавлено: {len(new_asins)}. Перезагружаю...")
            st.rerun()

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
            st.success(f"Готово: {ok}/{len(tracked)} чистых.")
            st.rerun()

with col_b:
    st.markdown("**Точечная проверка** (не влияет на общий список)")
    with st.form("ad_hoc_form"):
        adhoc_input = st.text_area("ASIN или ссылка для проверки", height=100)
        adhoc_submit = st.form_submit_button("Проверить эти ASIN")
    if adhoc_submit:
        raw_list = [a.strip() for a in re.split(r"[\s,]+", adhoc_input) if a.strip()]
        adhoc_asins = [extract_asin(a) for a in raw_list if extract_asin(a)]
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
                progress.progress(i / len(adhoc_asins), text=f"{i}/{len(adhoc_asins)}")
            finish_run(run_id, ok, "done")
            st.success(f"Готово: {ok}/{len(adhoc_asins)} чистых.")
            st.rerun()


def get_data():
    if not DATABASE_URL:
        st.error("DATABASE_URL не найден в .env")
        return pd.DataFrame()
    try:
        conn = psycopg2.connect(DATABASE_URL)
        query = """
            SELECT DISTINCT ON (asin)
                id,
                asin,
                source,
                rating,
                review_count,
                histogram_json,
                image_url,
                note,
                created_at
            FROM asin_metrics 
            WHERE asin NOT LIKE 'HTTP%' AND LENGTH(asin) <= 10
            ORDER BY asin, created_at DESC;
        """
        df = pd.read_sql(query, conn)
        conn.close()
        if not df.empty:
            df = df.sort_values("created_at", ascending=False)
        return df
    except Exception as e:
        st.error(f"Ошибка подключения к БД: {e}")
        return pd.DataFrame()


df = get_data()

st.markdown("---")

if df.empty:
    st.warning("В базе данных пока нет корректных записей.")
else:
    # --- РАСЧЕТ ПОЛЕЙ ---
    def process_row(row):
        rating = row["rating"]
        cnt = row["review_count"]
        source = row["source"]
        hist_raw = row["histogram_json"]

        bad_pct = 0
        bad_pct_str = "—"
        if hist_raw:
            try:
                h = json.loads(hist_raw) if isinstance(hist_raw, str) else hist_raw
                bad_pct = int(h.get("1", 0)) + int(h.get("2", 0))
                bad_pct_str = f"{bad_pct}%"
            except Exception:
                pass

        margin_str = "—"
        if rating is not None and cnt is not None and rating > 4.0:
            margin = int((cnt * (rating - 4.0)) / 3.0)
            margin_str = f"{max(0, margin)} ед."

        flag = "🟢"
        if source == "none" or rating is None:
            flag = "⚪"
        elif rating < 4.0:
            flag = "🔴"
        elif rating < 4.3 or bad_pct > 15:
            flag = "🟡"

        asin = row["asin"]
        url = f"https://www.amazon.com.be/dp/{asin}?language=en_GB"

        return pd.Series([
            asin,
            flag,
            url,
            row["image_url"],
            source,
            rating,
            cnt,
            bad_pct_str,
            margin_str,
            row["note"] if row["note"] else "",
            row["created_at"].strftime("%d.%m.%Y %H:%M") if pd.notnull(row["created_at"]) else "—"
        ])

    calc_df = df.apply(process_row, axis=1)
    calc_df.columns = [
        "raw_asin",
        "Флаг",
        "ASIN",
        "Фото",
        "Маркетплейс (источник)",
        "Рейтинг",
        "Кол-во рейтингов",
        "1–2★ %",
        "Запас до 4.0",
        "Комментарий",
        "Дата сбора"
    ]

    # --- ФИЛЬТРЫ И ПЕРЕКЛЮЧЕНИЕ ВИДА ---
    st.subheader("📋 Сводный отчет")
    
    top_col1, top_col2, top_col3 = st.columns([2, 2, 1])

    all_asins = df["asin"].tolist()
    all_sources = df["source"].dropna().unique().tolist()

    with top_col1:
        sel_asins = st.multiselect("Фильтр по ASIN", options=all_asins, default=all_asins)
    with top_col2:
        sel_sources = st.multiselect("Фильтр по Источнику", options=all_sources, default=all_sources)
    with top_col3:
        view_mode = st.radio("Вид отображения", options=["📊 Таблица", "🎴 Карточки"], horizontal=True)

    # Фильтрация
    filtered_df = calc_df[
        calc_df["raw_asin"].isin(sel_asins) &
        calc_df["Маркетплейс (источник)"].isin(sel_sources)
    ]

    # --- УДАЛЕНИЕ ИЗ БАЗЫ ОТДЕЛЬНЫМ БЛОКОМ ---
    with st.expander("🗑️ Удалить ASIN из базы"):
        del_asin_sel = st.selectbox("Выберите ASIN для полного удаления", options=[""] + filtered_df["raw_asin"].tolist())
        if st.button("Удалить выбранный ASIN") and del_asin_sel:
            delete_asin_completely(del_asin_sel)
            st.success(f"ASIN {del_asin_sel} успешно удален из базы!")
            st.rerun()

    st.markdown("---")

    # --- ОТОБРАЖЕНИЕ: ТАБЛИЦА ---
    if view_mode == "📊 Таблица":
        display_tbl = filtered_df.drop(columns=["raw_asin"])
        st.dataframe(
            display_tbl,
            column_config={
                "ASIN": st.column_config.LinkColumn(
                    "ASIN",
                    display_text=r"https://www\.amazon\.com\.be/dp/(B[0-9A-Z]{9})\?language=en_GB"
                ),
                "Фото": st.column_config.ImageColumn("Фото", width="small"),
                "Рейтинг": st.column_config.NumberColumn("Рейтинг", format="%.2f"),
            },
            use_container_width=True,
            hide_index=True
        )

    # --- ОТОБРАЖЕНИЕ: КАРТОЧКИ ---
    else:
        grid_cols = st.columns(3)
        for idx, row in enumerate(filtered_df.itertuples()):
            col = grid_cols[idx % 3]
            with col:
                with st.container(border=True):
                    # Шляпка карточки
                    head_col1, head_col2 = st.columns([3, 1])
                    head_col1.markdown(f"### {row.Флаг} [{row.raw_asin}]({row.ASIN})")
                    if head_col2.button("🗑️", key=f"del_{row.raw_asin}"):
                        delete_asin_completely(row.raw_asin)
                        st.rerun()

                    # Фото и метрики
                    img_c, info_c = st.columns([1, 2])
                    if row.Фото:
                        img_c.image(row.Фото, use_container_width=True)
                    else:
                        img_c.caption("Нет фото")

                    info_c.markdown(f"**Источник:** `{row.getattr('Маркетплейс_(источник)')}`")
                    rating_val = f"{row.Рейтинг:.2f}" if isinstance(row.Рейтинг, float) else "—"
                    info_c.markdown(f"**Рейтинг:** ⭐ **{rating_val}** ({row.getattr('Кол-во_рейтингов')} отзыва)")
                    info_c.markdown(f"**1–2★ плохих:** {row.getattr('_8')}") # 1-2★ %
                    info_c.markdown(f"**Запас до 4.0:** 🛡️ {row.getattr('_9')}")

                    st.caption(f"Обновлено: {row.getattr('Дата_сбора')}") 
