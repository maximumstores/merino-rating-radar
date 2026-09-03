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

# Пользовательский CSS для стилизации под Apple / SaaS Clean UI
st.markdown(
    """
<style>
    /* Общий фон и字体 */
    .stApp {
        background-color: #fbfbfd;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
    }
    
    /* Стилизация заголовков */
    h1 {
        font-weight: 600 !important;
        letter-spacing: -0.02em !important;
        color: #1d1d1f !important;
    }
    
    h3 {
        font-weight: 500 !important;
        letter-spacing: -0.01em !important;
        color: #1d1d1f !important;
    }

    /* Стилизация бейджей статуса */
    .badge {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        text-align: center;
    }
    .badge-success { background-color: #e6f4ea; color: #137333; }
    .badge-warning { background-color: #fef7e0; color: #b06000; }
    .badge-danger { background-color: #fce8e6; color: #c5221f; }
    .badge-neutral { background-color: #f1f3f4; color: #5f6368; }

    /* Табличные разделители и рамки */
    div[data-testid="stForm"] {
        border: 1px solid #e5e5ea !important;
        border-radius: 12px !important;
        background-color: #ffffff !important;
    }

    /* Кнопки */
    .stButton>button {
        border-radius: 8px !important;
        border: 1px solid #d2d2d7 !important;
        background-color: #ffffff !important;
        color: #1d1d1f !important;
        font-weight: 400 !important;
        transition: all 0.2s ease !important;
    }
    .stButton>button:hover {
        border-color: #0071e3 !important;
        color: #0071e3 !important;
        background-color: #f5f5f7 !important;
    }
</style>
""",
    unsafe_allow_html=True,
)

clean_db_trash()

st.title("Rating Radar")
st.caption("Мониторинг качества листингов и прогнозирование рейтинга")

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
    status_label = "завершён" if last_run["status"] == "done" else "в процессе"
    st.info(
        f"Последний сбор данных: {started_kyiv:%d.%m.%Y в %H:%M} (Киев) — статус: {status_label} · "
        f"Валидных данных: {int(last_run['ok_count'] or 0)} из {int(last_run['asin_count'] or 0)}"
    )
else:
    st.warning("История сборов пуста")

# Блок управления списком
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
            "ASIN или ссылки на Amazon", height=80, placeholder="Введите ссылки..."
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

    def process_row(row):
        rating = row["rating"]
        cnt = row["review_count"]
        source = row["source"]
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
            margin = int((cnt * (rating - 4.0)) / 3.0)
            margin_str = f"{max(0, margin)} ед."

        # Определение статуса (Бейджа)
        badge_html = '<span class="badge badge-success">ОК</span>'
        if source == "none" or rating is None:
            badge_html = '<span class="badge badge-neutral">Нет данных</span>'
        elif rating <= 4.2:
            badge_html = '<span class="badge badge-danger">Риск</span>'
        elif rating == 4.3 or bad_pct > 15:
            badge_html = '<span class="badge badge-warning">Внимание</span>'

        # Тренд
        trend_symbol = "—"
        if bad_pct > 20 or (rating is not None and rating < 4.2):
            trend_symbol = "↓"
        elif bad_pct < 8 and rating is not None and rating >= 4.5:
            trend_symbol = "↑"

        asin = row["asin"]
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

        return pd.Series([
            asin,
            badge_html,
            url,
            img_url,
            source,
            rating,
            cnt,
            bad_pct_str,
            trend_symbol,
            margin_str,
            bsr_val,
            row["note"] if row["note"] else "",
            (
                row["created_at"].strftime("%d.%m.%Y %H:%M")
                if pd.notnull(row["created_at"])
                else "—"
            ),
        ])

    calc_df = df.apply(process_row, axis=1)
    calc_df.columns = [
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

    st.markdown("### Сводный отчет")

    top_col1, top_col2, top_col3 = st.columns([2, 2, 1])

    all_asins = df["asin"].tolist()
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
        view_mode = st.radio(
            "Формат", options=["Таблица", "Карточки"], horizontal=True
        )

    filtered_df = calc_df[
        calc_df["raw_asin"].isin(sel_asins)
        & calc_df["Источник"].isin(sel_sources)
    ]

    records = filtered_df.to_dict(orient="records")

    st.markdown("<br>", unsafe_allow_html=True)

    def get_rating_html(val):
        if not isinstance(val, float) or pd.isna(val):
            return "—"
        val_str = f"{val:.2f}"
        if val >= 4.4:
            color = "#137333"
        elif 4.25 <= val <= 4.35:
            color = "#b06000"
        else:
            color = "#c5221f"
        return f"<span style='color: {color}; font-weight: 600;'>{val_str}</span>"

    # --- ТАБЛИЧНЫЙ ВИД ---
    if view_mode == "Таблица":
        th_cols = st.columns(
            [0.8, 0.8, 1.2, 0.8, 1.0, 1.0, 0.9, 0.8, 0.6, 1.1, 1.3, 1.2]
        )
        th_cols[0].markdown("**Статус**")
        th_cols[1].markdown("**Действия**")
        th_cols[2].markdown("**ASIN**")
        th_cols[3].markdown("**Фото**")
        th_cols[4].markdown("**Источник**")
        th_cols[5].markdown("**Рейтинг**")
        th_cols[6].markdown("**Отзывы**")
        th_cols[7].markdown("**1–2★ %**")
        th_cols[8].markdown("**Тренд**")
        th_cols[9].markdown("**Запас**")
        th_cols[10].markdown("**BSR**")
        th_cols[11].markdown("**Обновлено**")

        st.markdown(
            "<hr style='margin: 8px 0 16px 0; border: none; border-top: 1px solid #e5e5ea;'>",
            unsafe_allow_html=True,
        )

        for item in records:
            r_cols = st.columns(
                [0.8, 0.8, 1.2, 0.8, 1.0, 1.0, 0.9, 0.8, 0.6, 1.1, 1.3, 1.2]
            )

            r_cols[0].markdown(item["Статус"], unsafe_allow_html=True)

            act_c1, act_c2 = r_cols[1].columns(2)
            if act_c1.button(
                "↻", key=f"tbl_run_{item['raw_asin']}", help="Обновить"
            ):
                ensure_schema()
                run_id = start_run(1)
                res = check_asin(item["raw_asin"])
                save_to_db(res)
                finish_run(
                    run_id, 1 if res.get("source") in ("BE", "NL") else 0, "done"
                )
                st.rerun()

            if act_c2.button(
                "✕", key=f"tbl_del_{item['raw_asin']}", help="Удалить"
            ):
                delete_asin_completely(item["raw_asin"])
                st.rerun()

            r_cols[2].markdown(f"[{item['raw_asin']}]({item['ASIN']})")

            if item["Фото"]:
                try:
                    r_cols[3].image(item["Фото"], width=36)
                except Exception:
                    r_cols[3].caption("—")
            else:
                r_cols[3].caption("—")

            r_cols[4].write(item["Источник"])
            r_cols[5].markdown(
                get_rating_html(item["Рейтинг"]), unsafe_allow_html=True
            )
            r_cols[6].write(str(item["Отзывы"]))
            r_cols[7].write(item["1–2★ %"])
            r_cols[8].write(item["Тренд"])
            r_cols[9].write(item["Запас (до 4.0)"])
            r_cols[10].write(str(item["BSR"]))
            r_cols[11].write(item["Обновлено"])

    # --- ВИД КАРТОЧЕК ---
    else:
        grid_cols = st.columns(3)
        for idx, item in enumerate(records):
            col = grid_cols[idx % 3]
            with col:
                with st.container(border=True):
                    head_col1, head_col2, head_col3 = st.columns([3, 1, 1])
                    head_col1.markdown(
                        f"{item['Статус']} &nbsp; **[{item['raw_asin']}]({item['ASIN']})**",
                        unsafe_allow_html=True,
                    )

                    if head_col2.button(
                        "↻", key=f"card_run_{item['raw_asin']}", help="Обновить"
                    ):
                        ensure_schema()
                        run_id = start_run(1)
                        res = check_asin(item["raw_asin"])
                        save_to_db(res)
                        finish_run(
                            run_id,
                            1 if res.get("source") in ("BE", "NL") else 0,
                            "done",
                        )
                        st.rerun()

                    if head_col3.button(
                        "✕", key=f"card_del_{item['raw_asin']}", help="Удалить"
                    ):
                        delete_asin_completely(item["raw_asin"])
                        st.rerun()

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
                    rating_fmt = get_rating_html(item["Рейтинг"])
                    info_c.markdown(
                        f"**Рейтинг:** {rating_fmt} &nbsp;({item['Отзывы']} отз.)",
                        unsafe_allow_html=True,
                    )
                    info_c.markdown(
                        f"**Негативные (1–2★):** {item['1–2★ %']} (Тренд: {item['Тренд']})"
                    )
                    info_c.markdown(
                        f"**Запас до 4.0:** {item['Запас (до 4.0)']}"
                    )
                    info_c.markdown(f"**BSR:** `{item['BSR']}`")

                    st.caption(f"Обновлено: {item['Обновлено']}")
