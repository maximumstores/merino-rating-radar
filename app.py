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

        # Флаги и логика цвета
        flag = "🟢"
        if source == "none" or rating is None:
            flag = "⚪"
        elif rating <= 4.2:
            flag = "🔴"
        elif rating == 4.3 or (rating < 4.4) or bad_pct > 15:
            flag = "🟡"
        else:
            flag = "🟢"

        asin = row["asin"]
        url = f"https://www.amazon.com.be/dp/{asin}?language=en_GB"

        img_url = row["image_url"]
        if pd.isna(img_url) or not isinstance(img_url, str) or not img_url.startswith("http"):
            img_url = None

        bsr_val = row["bsr"] if ("bsr" in row and pd.notnull(row["bsr"]) and row["bsr"]) else "—"

        return pd.Series([
            asin,
            flag,
            url,
            img_url,
            source,
            rating,
            cnt,
            bad_pct_str,
            margin_str,
            bsr_val,
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
        "BSR",
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

    records = filtered_df.to_dict(orient="records")

    st.markdown("---")

    def get_colored_rating(rating_val):
        if not isinstance(rating_val, float) or pd.isna(rating_val):
            return "—"
        val_str = f"{rating_val:.2f}"
        if rating_val >= 4.4:
            return f"<span style='color: #2e7d32; font-weight: bold;'>🟢 {val_str}</span>"
        elif 4.25 <= rating_val <= 4.35:
            return f"<span style='color: #f57f17; font-weight: bold;'>🟡 {val_str}</span>"
        else:
            return f"<span style='color: #c62828; font-weight: bold;'>🔴 {val_str}</span>"

    # --- ОТОБРАЖЕНИЕ: ТАБЛИЦА ---
    if view_mode == "📊 Таблица":
        # Шапка таблицы
        th_cols = st.columns([0.6, 1.2, 1.2, 1.2, 1.5, 1.2, 1.0, 1.0, 1.0, 1.5, 1.2, 1.5])
        th_cols[0].markdown("**Флаг**")
        th_cols[1].markdown("**Действия**")
        th_cols[2].markdown("**ASIN**")
        th_cols[3].markdown("**Фото**")
        th_cols[4].markdown("**Маркетплейс**")
        th_cols[5].markdown("**Рейтинг**")
        th_cols[6].markdown("**Отзывы**")
        th_cols[7].markdown("**1–2★ %**")
        th_cols[8].markdown("**Запас**")
        th_cols[9].markdown("**BSR**")
        th_cols[10].markdown("**Комментарий**")
        th_cols[11].markdown("**Дата сбора**")

        st.markdown("<hr style='margin: 5px 0 15px 0;'>", unsafe_allow_html=True)

        for item in records:
            r_cols = st.columns([0.6, 1.2, 1.2, 1.2, 1.5, 1.2, 1.0, 1.0, 1.0, 1.5, 1.2, 1.5])
            
            r_cols[0].markdown(f"### {item['Флаг']}")
            
            act_c1, act_c2 = r_cols[1].columns(2)
            if act_c1.button("🔄", key=f"tbl_run_{item['raw_asin']}", help="Собрать сейчас"):
                ensure_schema()
                run_id = start_run(1)
                res = check_asin(item['raw_asin'])
                save_to_db(res)
                finish_run(run_id, 1 if res.get("source") in ("BE", "NL") else 0, "done")
                st.rerun()
                
            if act_c2.button("🗑️", key=f"tbl_del_{item['raw_asin']}", help="Удалить из базы"):
                delete_asin_completely(item['raw_asin'])
                st.rerun()

            r_cols[2].markdown(f"[{item['raw_asin']}]({item['ASIN']})")
            
            if item["Фото"]:
                try:
                    r_cols[3].image(item["Фото"], width=45)
                except Exception:
                    r_cols[3].caption("—")
            else:
                r_cols[3].caption("—")

            r_cols[4].write(item['Маркетплейс (источник)'])
            r_cols[5].markdown(get_colored_rating(item['Рейтинг']), unsafe_allow_html=True)
            r_cols[6].write(str(item['Кол-во рейтингов']))
            r_cols[7].write(item['1–2★ %'])
            r_cols[8].write(item['Запас до 4.0'])
            r_cols[9].write(str(item['BSR']))
            r_cols[10].write(item['Комментарий'])
            r_cols[11].write(item['Дата сбора'])

    # --- ОТОБРАЖЕНИЕ: КАРТОЧКИ ---
    else:
        grid_cols = st.columns(3)
        for idx, item in enumerate(records):
            col = grid_cols[idx % 3]
            with col:
                with st.container(border=True):
                    head_col1, head_col2, head_col3 = st.columns([3, 1, 1])
                    head_col1.markdown(f"### {item['Флаг']} [{item['raw_asin']}]({item['ASIN']})")
                    
                    if head_col2.button("🔄", key=f"card_run_{item['raw_asin']}", help="Собрать сейчас"):
                        ensure_schema()
                        run_id = start_run(1)
                        res = check_asin(item['raw_asin'])
                        save_to_db(res)
                        finish_run(run_id, 1 if res.get("source") in ("BE", "NL") else 0, "done")
                        st.rerun()

                    if head_col3.button("🗑️", key=f"card_del_{item['raw_asin']}", help="Удалить из базы"):
                        delete_asin_completely(item['raw_asin'])
                        st.rerun()

                    img_c, info_c = st.columns([1, 2])
                    if item["Фото"]:
                        try:
                            img_c.image(item["Фото"])
                        except Exception:
                            img_c.caption("Ошибка фото")
                    else:
                        img_c.caption("Нет фото")

                    info_c.markdown(f"**Источник:** `{item['Маркетплейс (источник)']}`")
                    colored_r = get_colored_rating(item['Рейтинг'])
                    info_c.markdown(f"**Рейтинг:** {colored_r} ({item['Кол-во рейтингов']} отз.)", unsafe_allow_html=True)
                    info_c.markdown(f"**1–2★ плохих:** {item['1–2★ %']}")
                    info_c.markdown(f"**Запас до 4.0:** 🛡️ {item['Запас до 4.0']}")
                    info_c.markdown(f"**BSR:** 🏆 `{item['BSR']}`")

                    st.caption(f"Обновлено: {item['Дата сбора']}")
