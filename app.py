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

st.set_page_config(
    page_title="Rating Radar — Executive Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    .stApp {
        background-color: #fbfbfd;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    }
    h1 {
        font-weight: 600 !important;
        letter-spacing: -0.02em !important;
        color: #1d1d1f !important;
    }
    div[data-testid="stForm"] {
        border: 1px solid #e5e5ea !important;
        border-radius: 8px !important;
        background-color: #ffffff !important;
    }
</style>
""",
    unsafe_allow_html=True,
)

clean_db_trash()

st.title("Rating Radar")
st.caption("Мониторинг качества листингов и прогнозирование рейтинга")

TIMEZONES = {
    "Киев (EEST / EET)": "Europe/Kyiv",
    "UTC": "UTC",
    "Берлин / Париж (CET)": "Europe/Berlin",
    "Лондон (BST / GMT)": "Europe/London",
    "Нью-Йорк (EDT / EST)": "America/New_York",
}


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
    started_kyiv = pd.to_datetime(last_run["started_at"]).tz_convert(
        ZoneInfo("Europe/Kyiv")
    )
    status_label = "завершён" if last_run["status"] == "done" else "в процессе"
    st.info(
        f"Последний сбор данных: {started_kyiv:%d.%m.%Y в %H:%M} (Киев) — статус: {status_label} · "
        f"Валидных данных: {int(last_run['ok_count'] or 0)} из {int(last_run['asin_count'] or 0)}"
    )
else:
    st.warning("История сборов пуста")

tracked = get_tracked_asins()

with st.expander("Управление списком ASIN"):
    st.caption(f"Отслеживается позиций: {len(tracked)}")
    new_asins_input = st.text_area(
        "Ввод ASIN (по одному на строку или списком)",
        height=80,
        key="add_tracked",
        placeholder="B09NWGDK3S, B0DDV2DBZH...",
    )
    if st.button("Сохранить в базу"):
        raw_list = [
            a.strip() for a in re.split(r"[\s,]+", new_asins_input) if a.strip()
        ]
        new_asins = [extract_asin(a) for a in raw_list if extract_asin(a)]
        if new_asins:
            ensure_schema()
            add_tracked_asins(new_asins)
            st.success(f"Успешно добавлено позиций: {len(new_asins)}")
            st.rerun()

st.markdown("### Сбор данных")
col_a, col_b = st.columns(2)

with col_a:
    st.markdown("**Полный прогон**")
    st.caption("Запуск сбора по всем отслеживаемым позициям")
    if st.button(f"Запустить прогон ({len(tracked)} ASIN)"):
        if not tracked:
            st.warning("Список отслеживания пуст")
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
                    _log(f"Ошибка {asin}: {e}")
                progress.progress(i / len(tracked), text=f"{i}/{len(tracked)}")
            finish_run(run_id, ok, "done")
            st.success(f"Прогон завершен: {ok}/{len(tracked)} успешно.")
            st.rerun()

with col_b:
    st.markdown("**Точечная проверка**")
    st.caption("Разовая проверка конкретных позиций")
    with st.form("ad_hoc_form"):
        adhoc_input = st.text_area(
            "ASIN или ссылки на Amazon",
            height=80,
            placeholder="Введите ссылки...",
        )
        adhoc_submit = st.form_submit_button("Выполнить проверку")
    if adhoc_submit:
        raw_list = [
            a.strip() for a in re.split(r"[\s,]+", adhoc_input) if a.strip()
        ]
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
                    _log(f"Ошибка {asin}: {e}")
                progress.progress(
                    i / len(adhoc_asins), text=f"{i}/{len(adhoc_asins)}"
                )
            finish_run(run_id, ok, "done")
            st.success(f"Проверка завершена: {ok}/{len(adhoc_asins)} успешно.")
            st.rerun()


def get_data():
    if not DATABASE_URL:
        st.error("DATABASE_URL не настроен")
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
                bsr,
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
    st.warning("В базе данных нет сохраненных метрик")
else:
    top_col1, top_col2, top_col3, top_col4 = st.columns([2, 2, 2, 1])

    all_asins = df["asin"].dropna().tolist()
    all_sources = df["source"].dropna().unique().tolist()

    with top_col1:
        sel_asins = st.multiselect(
            "Фильтр ASIN", options=all_asins, default=all_asins
        )
    with top_col2:
        sel_sources = st.multiselect(
            "Фильтр Источника", options=all_sources, default=all_sources
        )
    with top_col3:
        selected_tz_label = st.selectbox(
            "Часовой пояс сбора",
            options=list(TIMEZONES.keys()),
            index=0,
        )
        selected_tz = TIMEZONES[selected_tz_label]
    with top_col4:
        view_mode = st.radio(
            "Формат", options=["Таблица", "Карточки"], horizontal=True
        )

    def process_row(row):
        try:
            rating = float(row["rating"]) if pd.notnull(row["rating"]) else None
        except Exception:
            rating = None

        try:
            cnt = (
                int(row["review_count"])
                if pd.notnull(row["review_count"])
                else None
            )
        except Exception:
            cnt = None

        source = str(row["source"]) if pd.notnull(row["source"]) else "none"
        hist_raw = row["histogram_json"]

        bad_pct = 0
        bad_pct_str = "—"
        if hist_raw:
            try:
                h = (
                    json.loads(hist_raw)
                    if isinstance(hist_raw, str)
                    else hist_raw
                )
                bad_pct = int(h.get("1", 0)) + int(h.get("2", 0))
                bad_pct_str = f"{bad_pct}%"
            except Exception:
                pass

        margin_str = "—"
        if rating is not None and cnt is not None and rating > 4.0:
            try:
                margin = int((cnt * (rating - 4.0)) / 3.0)
                margin_str = f"{max(0, margin)} ед."
            except Exception:
                margin_str = "—"

        status_text = "ОК"
        if source == "none" or rating is None:
            status_text = "Нет данных"
        elif rating <= 4.2:
            status_text = "Риск"
        elif rating == 4.3 or bad_pct > 15:
            status_text = "Внимание"

        trend_symbol = "—"
        if bad_pct > 20 or (rating is not None and rating < 4.2):
            trend_symbol = "↓"
        elif bad_pct < 8 and rating is not None and rating >= 4.5:
            trend_symbol = "↑"

        asin = str(row["asin"])
        url = f"https://www.amazon.com.be/dp/{asin}?language=en_GB"

        img_url = row["image_url"]
        if (
            pd.isna(img_url)
            or not isinstance(img_url, str)
            or not img_url.startswith("http")
        ):
            img_url = None

        bsr_val = (
            row["bsr"]
            if ("bsr" in row and pd.notnull(row["bsr"]) and row["bsr"])
            else "—"
        )

        if pd.notnull(row["created_at"]):
            dt_tz = pd.to_datetime(row["created_at"]).tz_convert(
                ZoneInfo(selected_tz)
            )
            created_fmt = dt_tz.strftime("%d.%m.%Y %H:%M")
        else:
            created_fmt = "—"

        return pd.Series([
            False,
            asin,
            status_text,
            url,
            img_url,
            source,
            rating,
            cnt,
            bad_pct_str,
            trend_symbol,
            margin_str,
            bsr_val,
            row["note"] if pd.notnull(row["note"]) else "",
            created_fmt,
        ])

    calc_df = df.apply(process_row, axis=1)
    calc_df.columns = [
        "Выбор",
        "raw_asin",
        "Статус",
        "ASIN",
        "Фото",
        "Источник",
        "Рейтинг",
        "Отзывы",
        "1–2★ %",
        "Тренд",
        "Запас (до 4.0)",
        "BSR",
        "Комментарий",
        "Обновлено",
    ]

    filtered_df = calc_df[
        calc_df["raw_asin"].isin(sel_asins)
        & calc_df["Источник"].isin(sel_sources)
    ]

    # --- ЗАГОЛОВОК С ПОДСЧЕТОМ КОЛИЧЕСТВА ---
    st.markdown(f"### Сводный отчет &nbsp; <span style='font-size: 16px; color: #6e6e73; font-weight: normal;'>(Показано: **{len(filtered_df)}** из {len(calc_df)} позиций)</span>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    # --- ТАБЛИЧНЫЙ ВИД С ГАЛОЧКАМИ ---
    if view_mode == "Таблица":
        display_tbl = filtered_df.drop(columns=["raw_asin"])

        edited_df = st.data_editor(
            display_tbl,
            column_config={
                "Выбор": st.column_config.CheckboxColumn(
                    "Выбор",
                    default=False,
                    width="small",
                ),
                "Статус": st.column_config.TextColumn(
                    "Статус",
                    width="small",
                    disabled=True,
                ),
                "ASIN": st.column_config.LinkColumn(
                    "ASIN",
                    display_text=r"https://www\.amazon\.com\.be/dp/(B[0-9A-Z]{9})\?language=en_GB",
                    width="medium",
                    disabled=True,
                ),
                "Фото": st.column_config.ImageColumn(
                    "Фото",
                    width="small",
                ),
                "Источник": st.column_config.TextColumn(
                    "Источник", width="small", disabled=True
                ),
                "Рейтинг": st.column_config.NumberColumn(
                    "Рейтинг",
                    format="%.2f",
                    width="small",
                    disabled=True,
                ),
                "Отзывы": st.column_config.NumberColumn(
                    "Отзывы", width="small", disabled=True
                ),
                "1–2★ %": st.column_config.TextColumn(
                    "1–2★ %", width="small", disabled=True
                ),
                "Тренд": st.column_config.TextColumn(
                    "Тренд", width="small", disabled=True
                ),
                "Запас (до 4.0)": st.column_config.TextColumn(
                    "Запас", width="medium", disabled=True
                ),
                "BSR": st.column_config.TextColumn(
                    "BSR", width="medium", disabled=True
                ),
                "Комментарий": st.column_config.TextColumn(
                    "Комментарий", width="large", disabled=True
                ),
                "Обновлено": st.column_config.TextColumn(
                    f"Обновлено ({selected_tz_label.split(' ')[0]})",
                    width="medium",
                    disabled=True,
                ),
            },
            use_container_width=True,
            hide_index=True,
            key="table_editor",
        )

        st.caption(f"Всего отображается в таблице: {len(filtered_df)} строк")

        selected_rows = edited_df[edited_df["Выбор"] == True]
        selected_asins = [
            extract_asin(url)
            for url in selected_rows["ASIN"].tolist()
            if extract_asin(url)
        ]

        st.markdown("<br>", unsafe_allow_html=True)
        act_col1, act_col2, act_col3 = st.columns([3, 1, 1])

        if selected_asins:
            act_col1.markdown(
                f"**Выбрано позиций: {len(selected_asins)}** (`{', '.join(selected_asins)}`)"
            )
        else:
            act_col1.caption(
                "Отметьте галочками нужные строки в первой колонке для массовых действий"
            )

        if act_col2.button(
            "↻ Обновить выбранные",
            use_container_width=True,
            disabled=not selected_asins,
        ):
            ensure_schema()
            run_id = start_run(len(selected_asins))
            ok = 0
            for asin in selected_asins:
                res = check_asin(asin)
                save_to_db(res)
                if res.get("source") in ("BE", "NL"):
                    ok += 1
            finish_run(run_id, ok, "done")
            st.success(f"Обновлено позиций: {len(selected_asins)}")
            st.rerun()

        if act_col3.button(
            "✕ Удалить выбранные",
            use_container_width=True,
            disabled=not selected_asins,
        ):
            for asin in selected_asins:
                delete_asin_completely(asin)
            st.success(f"Удалено позиций: {len(selected_asins)}")
            st.rerun()

    # --- ВИД КАРТОЧЕК ---
    else:
        records = filtered_df.to_dict(orient="records")
        grid_cols = st.columns(3)
        for idx, item in enumerate(records):
            col = grid_cols[idx % 3]
            with col:
                with st.container(border=True):
                    st.markdown(
                        f"**[{item['raw_asin']}]({item['ASIN']})** &nbsp; • &nbsp; `{item['Статус']}`"
                    )

                    st.markdown("<br>", unsafe_allow_html=True)
                    img_c, info_c = st.columns([1, 2])

                    if item["Фото"]:
                        try:
                            img_c.image(item["Фото"])
                        except Exception:
                            img_c.caption("Нет фото")
                    else:
                        img_c.caption("Нет фото")

                    info_c.markdown(f"**Источник:** `{item['Источник']}`")
                    r_val = (
                        f"{item['Рейтинг']:.2f}"
                        if pd.notnull(item["Рейтинг"])
                        else "—"
                    )
                    cnt_fmt = (
                        str(item["Отзывы"])
                        if pd.notnull(item["Отзывы"])
                        else "—"
                    )
                    info_c.markdown(f"**Рейтинг:** {r_val} ({cnt_fmt} отз.)")
                    info_c.markdown(
                        f"**Негативные (1–2★):** {item['1–2★ %']} ({item['Тренд']})"
                    )
                    info_c.markdown(
                        f"**Запас до 4.0:** {item['Запас (до 4.0)']}"
                    )
                    info_c.markdown(f"**BSR:** `{item['BSR']}`")

                    st.caption(f"Обновлено: {item['Обновлено']}")
