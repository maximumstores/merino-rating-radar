import os
import json
import psycopg2
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(page_title="Rating Radar Dashboard", layout="wide", page_icon="📊")

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

st.title("📊 Rating Radar Dashboard")

df = get_data()

if df.empty:
    st.warning("В базе данных пока нет записей. Запустите скрипт сборщика `radar_check.py`!")
else:
    st.sidebar.header("Фильтры")
    all_asins = df["asin"].dropna().unique().tolist()
    all_sources = df["source"].dropna().unique().tolist()
    
    selected_asins = st.sidebar.multiselect("Выберите ASIN", options=all_asins, default=all_asins)
    selected_sources = st.sidebar.multiselect("Источник данных", options=all_sources, default=all_sources)

    filtered_df = df[
        (df["asin"].isin(selected_asins)) & 
        (df["source"].isin(selected_sources))
    ]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Всего проверок", len(filtered_df))
    col2.metric("Уникальных ASIN", filtered_df["asin"].nunique())
    
    avg_rating = filtered_df["rating"].dropna().mean()
    col3.metric("Средний рейтинг", f"{avg_rating:.2f}" if not pd.isna(avg_rating) else "—")
    
    total_reviews = filtered_df["review_count"].dropna().sum()
    col4.metric("Сумма отзывов", f"{int(total_reviews):,}")

    st.markdown("---")

    st.subheader("📋 История сбора данных")
    display_df = filtered_df.drop(columns=["histogram_json"]).copy()
    st.dataframe(display_df, use_container_width=True)

    st.markdown("---")
    st.subheader("⭐ Распределение звёзд (Гистограмма)")
    
    if not filtered_df.empty:
        selected_asin = st.selectbox("Выберите ASIN для детализации", options=filtered_df["asin"].unique())
        row = filtered_df[filtered_df["asin"] == selected_asin].iloc[0]
        hist_raw = row["histogram_json"]

        if hist_raw:
            hist_dict = json.loads(hist_raw) if isinstance(hist_raw, str) else hist_raw
            if hist_dict:
                hist_df = (
                    pd.DataFrame(list(hist_dict.items()), columns=["Звёзды", "Процент"])
                    .astype({"Звёзды": int, "Процент": int})
                    .sort_values("Звёзды", ascending=False)
                )
                hist_df["Звёзды"] = hist_df["Звёзды"].astype(str) + " ★"
                st.bar_chart(hist_df.set_index("Звёзды"))
            else:
                st.info("Гистограмма для этого ASIN пуста.")
        else:
            st.info("Данные о гистограмме отсутствуют.")
