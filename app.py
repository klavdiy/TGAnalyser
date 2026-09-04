"""Streamlit GUI: рейтинг каналов, графики, импорт CSV."""

from __future__ import annotations

import tempfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from rank_export import catalog_dataframe, err7_dataframe
from rank_metrics import catalog_rows, err7_rows
from rank_queries import channel_posts, snapshot_meta, subscriber_history
from rank_store import DEFAULT_DB, connect, load_catalog, latest_snapshot_id

st.set_page_config(page_title='TGAnalyser', layout='wide', page_icon='✎', menu_items={
    'Get Help': None,
    'Report a bug': None,
    'About': None,
})

WB = {
    'ink': '#1A1A1A',
    'paper': '#FFFEFA',
    'grid': '#F5F3EB',
    'blue': '#1E88E5',
    'red': '#E53935',
    'green': '#43A047',
    'yellow': '#FFF59D',
    'pink': '#F8BBD0',
    'cyan': '#B3E5FC',
}

DISPLAY_COLS = [
    'Место', 'Канал', 'Название', 'Тематика', 'Подписчики', 'Охват',
    'Читаемость, %', 'Перцентиль среди похожих', 'Постов за 1–7 дней',
    'Последний пост', 'Активный', 'Тип',
]

HIDDEN_COLS = {
    'A+_РКН', 'РКН_ссылка', 'Описание', 'Охват_подпись', 'Читаемость_подпись',
    'Перцентиль_подпись', 'Панель_ERR7', 'Ошибка',
}

RANK_RENAME = {
    'Читаемость': 'Читаемость, %',
    'Перцентиль': 'Перцентиль среди похожих',
    'Постов_в_окне': 'Постов за 1–7 дней',
    'Последний_пост': 'Последний пост',
}

RANK_HELP = {
    'Место': 'Место среди активных каналов: выше перцентиль — выше в списке.',
    'Канал': 'Адрес канала в Telegram.',
    'Название': 'Как канал подписан в Telegram.',
    'Тематика': 'Рубрика из списка наблюдения.',
    'Подписчики': 'Сколько человек подписано на момент снимка.',
    'Охват': 'Типичные просмотры одного поста: медиана постов возрастом от суток до недели.',
    'Читаемость, %': 'Какая доля подписчиков в среднем видит пост. Охват ÷ подписчики.',
    'Перцентиль среди похожих': (
        'Сравнение с каналами похожего размера. '
        '80 значит: у 80% таких каналов читаемость ниже. '
        '100 — лучший среди похожих. Прочерк — канал не попал в рейтинг '
        '(мало свежих постов). В списке из семи каналов часто 100: '
        'похожих по числу подписчиков почти нет.'
    ),
    'Постов за 1–7 дней': (
        'Сколько публикаций возрастом от 1 до 7 суток попало в расчёт охвата. '
        'Не все посты канала, а только это окно. Нужно минимум 3, иначе места нет.'
    ),
    'Последний пост': 'Дата самой свежей публикации в снимке.',
    'Активный': 'да — канал публикует и его можно сравнивать. нет — слишком мало постов в окне.',
    'Тип': 'канал (витрина) или чат (группа с обсуждением).',
}


def inject_whiteboard_css() -> None:
    st.markdown(
        """
<style>
@import url('https://fonts.googleapis.com/css2?family=Caveat:wght@600;700&family=Nunito:wght@400;600;700&display=swap');

html, body, [class*="stApp"] {
  background: #FFFEFA !important;
  color: #1A1A1A;
  font-family: 'Nunito', 'Trebuchet MS', sans-serif;
}
.stApp {
  background:
    radial-gradient(circle at 12% 8%, rgba(30, 136, 229, 0.06), transparent 28%),
    radial-gradient(circle at 88% 12%, rgba(229, 57, 53, 0.05), transparent 24%),
    #FFFEFA !important;
}
h1, h2, h3, .stMarkdown h1, .stMarkdown h2 {
  font-family: 'Caveat', 'Segoe Print', cursive !important;
  color: #1A1A1A !important;
  letter-spacing: 0.02em;
  font-weight: 700 !important;
}
h1 { font-size: 3.1rem !important; }
h2, h3 { font-size: 1.9rem !important; }

[data-testid="stSidebar"] {
  background: #F5F3EB !important;
  border-right: 2px solid #1A1A1A;
}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
  font-family: 'Caveat', cursive !important;
}

[data-testid="stMetric"] {
  background: #FFF59D;
  border: 2px solid #1A1A1A;
  border-radius: 3px 14px 4px 10px;
  padding: 0.6rem 0.8rem;
  box-shadow: 4px 4px 0 #1A1A1A;
}
[data-testid="stMetricLabel"] {
  font-family: 'Caveat', cursive !important;
  font-size: 1.35rem !important;
  color: #1A1A1A !important;
}
[data-testid="stMetricValue"] {
  font-family: 'Nunito', sans-serif !important;
  color: #1A1A1A !important;
}

.stTabs [data-baseweb="tab-list"] {
  gap: 0.4rem;
  border-bottom: 2px solid #1A1A1A;
}
.stTabs [data-baseweb="tab"] {
  font-family: 'Caveat', cursive !important;
  font-size: 1.45rem !important;
  background: #FFFEFA;
  border: 2px solid #1A1A1A;
  border-bottom: none;
  border-radius: 8px 12px 0 0;
  padding: 0.2rem 1rem;
}
.stTabs [aria-selected="true"] {
  background: #B3E5FC !important;
}

div[data-testid="stDataFrame"] {
  border: 2px solid #1A1A1A;
  box-shadow: 5px 5px 0 #1A1A1A;
  background: #fff;
}

.wb-subtitle {
  font-family: 'Nunito', sans-serif;
  font-size: 1.05rem;
  color: #333;
  margin: -0.6rem 0 1.2rem 0;
}
.wb-notes {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
  margin: 0.4rem 0 1.4rem 0;
}
.wb-sticky {
  border: 2px solid #1A1A1A;
  padding: 0.85rem 1rem 1rem 1rem;
  box-shadow: 3px 3px 0 rgba(26, 26, 26, 0.85);
  min-height: 8.5rem;
  transform: rotate(-0.4deg);
}
.wb-sticky h4 {
  font-family: 'Caveat', cursive;
  font-size: 1.55rem;
  margin: 0 0 0.35rem 0;
  font-weight: 700;
}
.wb-sticky p {
  font-family: 'Nunito', sans-serif;
  font-size: 0.95rem;
  line-height: 1.35;
  margin: 0;
}
.wb-blue { background: #B3E5FC; transform: rotate(-0.6deg); }
.wb-yellow { background: #FFF59D; transform: rotate(0.5deg); }
.wb-pink { background: #F8BBD0; transform: rotate(-0.2deg); }
.wb-green { background: #C8E6C9; transform: rotate(0.4deg); }
.wb-legend {
  background: #FFFEFA;
  border: 2px dashed #1A1A1A;
  padding: 0.9rem 1.1rem;
  margin: 0 0 1.1rem 0;
}
.wb-legend h4 {
  font-family: 'Caveat', cursive;
  font-size: 1.6rem;
  margin: 0 0 0.5rem 0;
}
.wb-legend dl { margin: 0; }
.wb-legend dt {
  font-weight: 700;
  color: #1E88E5;
  margin-top: 0.45rem;
}
.wb-legend dd {
  margin: 0.1rem 0 0 0;
  color: #1A1A1A;
}
.wb-how {
  background: #FFF8E1;
  border: 2px solid #1A1A1A;
  box-shadow: 4px 4px 0 #1A1A1A;
  padding: 0.9rem 1.1rem;
  margin: 0 0 1rem 0;
}
.wb-how h4 {
  font-family: 'Caveat', cursive;
  font-size: 1.6rem;
  margin: 0 0 0.35rem 0;
}

#MainMenu, header, footer, [data-testid="stDecoration"],
[data-testid="stToolbar"], [data-testid="stStatusWidget"],
[data-testid="stHeader"], [data-testid="stAppFooter"],
[data-testid="stBottom"], section[data-testid="stBottom"],
.stAppToolbar, .stDeployButton, .stAppDeployButton,
div[data-testid="stBottomBlockContainer"],
.viewerBadge_container__r5tak, .viewerBadge_link__qRIco,
a[href*="streamlit.io"], a[href*="streamlit.app"] {
  display: none !important;
  visibility: hidden !important;
  height: 0 !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )


def plotly_whiteboard(fig: go.Figure) -> go.Figure:
    fig.update_layout(
        paper_bgcolor=WB['paper'],
        plot_bgcolor=WB['grid'],
        font=dict(family='Nunito, sans-serif', color=WB['ink'], size=14),
        title_font=dict(family='Caveat, cursive', size=26, color=WB['ink']),
        legend=dict(bgcolor='rgba(255,254,250,0.9)', bordercolor=WB['ink'], borderwidth=1),
        margin=dict(t=60, l=40, r=20, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor='#E0DCC8', linecolor=WB['ink'], zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor='#E0DCC8', linecolor=WB['ink'], zeroline=False)
    return fig


def sticky_notes(*cards: tuple[str, str, str]) -> None:
    cells = []
    for title, text, tone in cards:
        cells.append(
            f'<div class="wb-sticky {tone}"><h4>{title}</h4><p>{text}</p></div>'
        )
    st.markdown(f'<div class="wb-notes">{"".join(cells)}</div>', unsafe_allow_html=True)


def _default_db() -> Path:
    return DEFAULT_DB if DEFAULT_DB.exists() else Path('data/rank.sqlite')


WATCHLIST = {c['username'].lower() for c in load_catalog()}


def _snapshot_usernames(conn, snapshot_id: int | None = None) -> list[str]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT c.username
        FROM channel_stats s
        JOIN channels c ON c.id = s.channel_id
        WHERE s.snapshot_id = ?
        ORDER BY c.username COLLATE NOCASE
        """,
        (snapshot_id,),
    ).fetchall()
    return [r['username'] for r in rows]


def _keep_watchlist(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or 'Канал' not in df.columns:
        return df
    return df[df['Канал'].str.lstrip('@').str.lower().isin(WATCHLIST)].copy()


@st.cache_data(show_spinner=False)
def load_from_sqlite(db_path: str, mtime: float) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    conn = connect(Path(db_path))
    meta = snapshot_meta(conn)
    if not meta:
        conn.close()
        return pd.DataFrame(), pd.DataFrame(), {}
    cat = _keep_watchlist(catalog_dataframe(catalog_rows(conn, meta['id'])))
    err = _keep_watchlist(err7_dataframe(err7_rows(conn, panel_only=True)))
    conn.close()
    return cat, err, meta


def guess_column(df: pd.DataFrame, names: list[str]) -> str | None:
    lower = {c.lower().strip(): c for c in df.columns}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    for key, col in lower.items():
        for name in names:
            if name.lower() in key:
                return col
    return None


def load_uploaded(file) -> pd.DataFrame:
    name = file.name.lower()
    if name.endswith('.csv'):
        return pd.read_csv(file)
    if name.endswith(('.xlsx', '.xls')):
        xl = pd.ExcelFile(file)
        sheet = 'Catalog' if 'Catalog' in xl.sheet_names else xl.sheet_names[0]
        return pd.read_excel(xl, sheet_name=sheet)
    raise ValueError('Нужен CSV или Excel')


def to_rank_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Приводит чужой CSV к колонкам рейтинга, если получится."""
    out = df.copy()
    rename = {}
    mapping = {
        'Канал': ['канал', 'channel', 'username', 'user'],
        'Подписчики': ['подписчик', 'subscribers', 'participants', 'subs'],
        'Охват': ['охват', 'reach', 'просмотр', 'views'],
        'Читаемость': ['читаемость', 'err', 'readability', 'engagement'],
        'Тематика': ['тематик', 'topic', 'category'],
        'Название': ['название', 'title', 'name'],
        'Место': ['место', 'place', 'rank'],
        'Перцентиль': ['перцентил', 'percentile'],
    }
    for target, aliases in mapping.items():
        if target in out.columns:
            continue
        found = guess_column(out, aliases)
        if found:
            rename[found] = target
    if rename:
        out = out.rename(columns=rename)
    if 'Канал' in out.columns:
        out['Канал'] = out['Канал'].astype(str).map(
            lambda x: x if x.startswith('@') else f'@{x.lstrip("@")}'
        )
    return out


def friendly_rank(df: pd.DataFrame) -> pd.DataFrame:
    view = df.rename(columns=RANK_RENAME).copy()
    if 'Место' in view.columns:
        view['Место'] = view['Место'].astype(str).replace({'nan': '—', 'None': '—'})
    if 'Перцентиль среди похожих' in view.columns:
        view['Перцентиль среди похожих'] = view['Перцентиль среди похожих'].apply(
            lambda v: '—' if pd.isna(v) else str(int(v))
        )
    return view


def scatter_readability(df: pd.DataFrame):
    plot_df = df.dropna(subset=['Подписчики', 'Читаемость']).copy()
    if plot_df.empty:
        st.info('Нет пар «подписчики × читаемость».')
        return
    color = 'Тематика' if 'Тематика' in plot_df.columns else None
    hover = [c for c in ('Канал', 'Название', 'Охват', 'Перцентиль') if c in plot_df.columns]
    fig = px.scatter(
        plot_df,
        x='Подписчики',
        y='Читаемость',
        color=color,
        hover_data=hover,
        log_x=True,
        title='Читаемость и размер аудитории',
        color_discrete_sequence=['#1E88E5', '#E53935', '#43A047', '#7E57C2', '#EC407A'],
    )
    fig.update_traces(marker=dict(size=14, line=dict(width=1.5, color=WB['ink'])))
    fig.update_layout(yaxis_title='Читаемость, %', xaxis_title='Подписчики')
    st.plotly_chart(plotly_whiteboard(fig), width='stretch')


def bar_top(df: pd.DataFrame, metric: str, n: int = 20):
    if metric not in df.columns:
        return
    plot_df = df.dropna(subset=[metric]).copy()
    if 'Активный' in plot_df.columns:
        plot_df = plot_df[plot_df['Активный'] == 'да']
    plot_df = plot_df.nlargest(n, metric)
    if plot_df.empty:
        return
    y = 'Канал' if 'Канал' in plot_df.columns else plot_df.columns[0]
    fig = px.bar(
        plot_df.sort_values(metric),
        x=metric,
        y=y,
        orientation='h',
        title=f'Кто впереди по «{metric.lower()}»',
        hover_data=[c for c in ('Подписчики', 'Охват', 'Тематика') if c in plot_df.columns],
        color_discrete_sequence=[WB['blue']],
    )
    fig.update_traces(marker_line_color=WB['ink'], marker_line_width=1.5)
    fig.update_layout(yaxis={'categoryorder': 'total ascending'}, height=max(420, len(plot_df) * 36))
    st.plotly_chart(plotly_whiteboard(fig), width='stretch')


def show_rank_table(df: pd.DataFrame):
    view = friendly_rank(df)
    cols = [c for c in DISPLAY_COLS if c in view.columns]
    extra = [c for c in view.columns if c not in cols and c not in HIDDEN_COLS and c not in RANK_RENAME]
    shown = view[cols + extra]
    config = {
        name: st.column_config.Column(name, help=RANK_HELP[name])
        for name in shown.columns if name in RANK_HELP
    }
    st.dataframe(shown, width='stretch', hide_index=True, column_config=config)


def rank_legend_html() -> str:
    items = ''.join(
        f'<dt>{name}</dt><dd>{help_text}</dd>'
        for name, help_text in RANK_HELP.items()
    )
    return (
        '<div class="wb-legend">'
        '<h4>Как читать таблицу</h4>'
        '<p>Одна строка — один Telegram-канал. Сортировка уже стоит: сверху те, кого лучше читают среди похожих по размеру.</p>'
        f'<dl>{items}</dl>'
        '</div>'
    )


def posts_frame(posts: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    pdf = pd.DataFrame(posts)
    if pdf.empty:
        return pdf
    pdf['posted_at'] = pd.to_datetime(pdf['posted_at'], utc=True)
    pdf['reactions'] = pd.to_numeric(pdf['reactions'], errors='coerce').fillna(0)
    pdf['views'] = pd.to_numeric(pdf['views'], errors='coerce')
    pdf['forwards'] = pd.to_numeric(pdf['forwards'], errors='coerce')
    display = pd.DataFrame({
        'Когда': pdf['posted_at'].dt.tz_convert('Europe/Minsk').dt.strftime('%d.%m.%Y %H:%M'),
        'Просмотры': pdf['views'].astype('Int64'),
        'Пересылки': pdf['forwards'].astype('Int64'),
        'Реакции': pdf['reactions'].clip(lower=0).astype(int),
        'Номер поста': pdf['msg_id'],
    })
    return display, pdf


def main() -> None:
    inject_whiteboard_css()
    st.title('TGAnalyser')
    st.markdown(
        '<p class="wb-subtitle">Рейтинг Telegram-каналов по тому, какую долю подписчиков дотягивает пост.</p>',
        unsafe_allow_html=True,
    )

    st.sidebar.header('Данные')
    source = st.sidebar.radio(
        'Источник',
        ['SQLite снимка', 'Загрузить CSV / Excel'],
        index=0 if _default_db().exists() else 1,
    )

    rank_df = pd.DataFrame()
    err_df = pd.DataFrame()
    meta: dict = {}
    uploaded_raw = pd.DataFrame()
    db_path = str(_default_db())

    if source == 'SQLite снимка':
        db_path = st.sidebar.text_input('Путь к rank.sqlite', value=str(_default_db()))
        sqlite_file = st.sidebar.file_uploader('Или загрузить .sqlite', type=['sqlite', 'db'])
        if sqlite_file is not None:
            tmp = Path(tempfile.gettempdir()) / 'rank_uploaded.sqlite'
            tmp.write_bytes(sqlite_file.getvalue())
            db_path = str(tmp)
        if Path(db_path).exists():
            try:
                rank_df, err_df, meta = load_from_sqlite(db_path, Path(db_path).stat().st_mtime)
            except Exception as exc:
                st.sidebar.error(f'Не удалось прочитать базу: {exc}')
        else:
            st.sidebar.warning('Файла нет — соберите снимок (`collect_rank.py`) или загрузите CSV.')
    else:
        up = st.sidebar.file_uploader('CSV или Excel (лист Catalog)', type=['csv', 'xlsx', 'xls'])
        if up is not None:
            try:
                uploaded_raw = load_uploaded(up)
                rank_df = to_rank_frame(uploaded_raw)
            except Exception as exc:
                st.sidebar.error(str(exc))

    if not rank_df.empty and 'Активный' in rank_df.columns:
        only_active = st.sidebar.checkbox('Только активные', value=False)
        if only_active:
            rank_df = rank_df[rank_df['Активный'] == 'да']

    topics = sorted(rank_df['Тематика'].dropna().unique()) if 'Тематика' in rank_df.columns else []
    if topics:
        picked = st.sidebar.multiselect('Тематика', topics, default=topics)
        rank_df = rank_df[rank_df['Тематика'].isin(picked)]

    if meta:
        st.sidebar.markdown(
            f"Снимок **#{meta.get('id')}** · {meta.get('collected_at', '')} · "
            f"{meta.get('channels', 0)} каналов"
        )

    tab_overview, tab_rank, tab_channel, tab_csv = st.tabs(
        ['Обзор', 'Рейтинг', 'Каналы', 'CSV-анализ']
    )

    with tab_overview:
        if rank_df.empty:
            st.info('Нет данных. Слева укажите sqlite или загрузите CSV/Excel.')
        else:
            sticky_notes(
                (
                    'Каналов',
                    'Сколько Telegram-каналов сейчас в списке наблюдения.',
                    'wb-yellow',
                ),
                (
                    'Читаемость',
                    'Доля подписчиков, которая в среднем видит пост. Чем выше — тем живее канал.',
                    'wb-blue',
                ),
                (
                    'Охват',
                    'Типичные просмотры одного поста за последние дни, не сумма за месяц.',
                    'wb-pink',
                ),
            )
            c1, c2, c3, c4 = st.columns(4)
            c1.metric('Каналов', len(rank_df))
            if 'Подписчики' in rank_df.columns:
                c2.metric('Медиана подписчиков', f"{int(rank_df['Подписчики'].median()):,}".replace(',', ' '))
            if 'Читаемость' in rank_df.columns:
                med = rank_df['Читаемость'].median()
                c3.metric('Медиана читаемости', f'{med:.1f}%' if pd.notna(med) else '—')
            if 'Охват' in rank_df.columns:
                med_r = rank_df['Охват'].median()
                c4.metric('Медиана охвата', f'{int(med_r):,}'.replace(',', ' ') if pd.notna(med_r) else '—')
            scatter_readability(rank_df)
            left, right = st.columns(2)
            with left:
                bar_top(rank_df, 'Читаемость')
            with right:
                bar_top(rank_df, 'Охват')
            if not err_df.empty:
                st.subheader('ERR₇')
                st.caption('Недельная читаемость поста, когда накопились зрелые публикации.')
                st.dataframe(err_df, width='stretch', hide_index=True)

    with tab_rank:
        if rank_df.empty:
            st.info('Нет данных рейтинга.')
        else:
            sticky_notes(
                (
                    'Перцентиль',
                    'Это не место и не процент просмотров. Это «лучше скольких похожих по числу подписчиков». '
                    '80 значит: у восьми из десяти каналов того же размера читаемость ниже. '
                    'В коротком списке из семи каналов почти все разного размера — тогда стоит 100: сравнивать не с кем.',
                    'wb-blue',
                ),
                (
                    'Постов за 1–7 дней',
                    'Сколько публикаций возрастом от суток до недели участвует в охвате. '
                    'Свежие (младше суток) и старые (старше недели) сюда не входят.',
                    'wb-yellow',
                ),
                (
                    'Читаемость',
                    'Охват делим на подписчиков. 30% значит: типичный пост видит примерно треть аудитории.',
                    'wb-green',
                ),
            )
            st.markdown(rank_legend_html(), unsafe_allow_html=True)
            show_rank_table(rank_df)
            buf = BytesIO()
            export_cols = [c for c in rank_df.columns if c not in HIDDEN_COLS]
            rank_df[export_cols].to_csv(buf, index=False, encoding='utf-8-sig')
            st.download_button('Скачать CSV', buf.getvalue(), 'rank_catalog.csv', 'text/csv')

    with tab_channel:
        st.markdown(
            '<div class="wb-how"><h4>Как читать эту вкладку</h4>'
            '<p>Слева сверху выберите канал. График — лента публикаций: ось X это дата, '
            'ось Y — сколько раз открыли пост. Размер точки — реакции. '
            'Таблица ниже — те же посты по строкам: одна строка = один пост.</p></div>',
            unsafe_allow_html=True,
        )
        if source != 'SQLite снимка' or not Path(db_path).exists():
            st.info('Посты канала доступны из sqlite-снимка (`collect_rank.py`).')
        else:
            conn = connect(Path(db_path))
            users = [
                u for u in _snapshot_usernames(conn, meta.get('id') if meta else None)
                if u.lower() in WATCHLIST
            ]
            if not users:
                st.info('В последнем снимке нет каналов из списка наблюдения.')
            else:
                choice = st.selectbox('Telegram-канал', users, format_func=lambda u: f'@{u}')
                posts = channel_posts(conn, choice)
                hist = subscriber_history(conn, choice)
                if not posts:
                    st.warning('В снимке нет постов этого канала.')
                else:
                    display, pdf = posts_frame(posts)
                    plot_df = pdf.copy()
                    plot_df['размер'] = plot_df['reactions'].clip(lower=0) + 1
                    fig = px.scatter(
                        plot_df, x='posted_at', y='views',
                        size='размер',
                        hover_data={'msg_id': True, 'forwards': True, 'reactions': True, 'размер': False},
                        title=f'Просмотры постов @{choice}',
                        color_discrete_sequence=[WB['blue']],
                    )
                    fig.update_traces(marker=dict(line=dict(width=1, color=WB['ink'])))
                    fig.update_layout(xaxis_title='Когда опубликован', yaxis_title='Просмотры')
                    st.plotly_chart(plotly_whiteboard(fig), width='stretch')
                    st.markdown(
                        '<div class="wb-legend"><h4>Колонки таблицы постов</h4><dl>'
                        '<dt>Когда</dt><dd>Дата и время публикации (Минск).</dd>'
                        '<dt>Просмотры</dt><dd>Сколько раз пост открыли к моменту снимка.</dd>'
                        '<dt>Пересылки</dt><dd>Сколько раз пост переслали.</dd>'
                        '<dt>Реакции</dt><dd>Сумма эмодзи-реакций на пост.</dd>'
                        '<dt>Номер поста</dt><dd>Номер сообщения в канале, как в ссылке t.me/канал/номер.</dd>'
                        '</dl></div>',
                        unsafe_allow_html=True,
                    )
                    st.dataframe(
                        display,
                        width='stretch',
                        hide_index=True,
                        column_config={
                            'Когда': st.column_config.TextColumn('Когда', help='Дата публикации по Минску.'),
                            'Просмотры': st.column_config.NumberColumn('Просмотры', help='Сколько раз открыли пост.'),
                            'Пересылки': st.column_config.NumberColumn('Пересылки', help='Сколько раз переслали.'),
                            'Реакции': st.column_config.NumberColumn('Реакции', help='Сумма реакций.'),
                            'Номер поста': st.column_config.NumberColumn(
                                'Номер поста', help='ID сообщения в канале.'
                            ),
                        },
                    )
                if len(hist) > 1:
                    hdf = pd.DataFrame(hist)
                    hdf['collected_at'] = pd.to_datetime(hdf['collected_at'], utc=True)
                    fig2 = px.line(
                        hdf, x='collected_at', y='subscribers', markers=True,
                        title='Подписчики от снимка к снимку',
                        color_discrete_sequence=[WB['red']],
                    )
                    fig2.update_layout(xaxis_title='Снимок', yaxis_title='Подписчики')
                    st.plotly_chart(plotly_whiteboard(fig2), width='stretch')
            conn.close()

    with tab_csv:
        st.markdown(
            'Любой CSV: выгрузка `parser.py`, Excel рейтинга или свой файл. '
            'Выберите оси — получите график.'
        )
        csv_file = st.file_uploader('Файл для произвольного анализа', type=['csv', 'xlsx', 'xls'], key='anycsv')
        work = uploaded_raw if not uploaded_raw.empty and csv_file is None else pd.DataFrame()
        if csv_file is not None:
            work = load_uploaded(csv_file)
        if work.empty:
            st.caption('Загрузите файл здесь или на вкладке слева.')
        else:
            st.dataframe(work.head(50), width='stretch', hide_index=True)
            numeric = work.select_dtypes(include='number').columns.tolist()
            all_cols = work.columns.tolist()
            if len(numeric) >= 1:
                x = st.selectbox('Ось X', all_cols, index=0)
                y = st.selectbox('Ось Y', numeric, index=min(1, len(numeric) - 1) if numeric else 0)
                color = st.selectbox('Цвет', ['—'] + all_cols, index=0)
                fig = px.scatter(
                    work, x=x, y=y,
                    color=None if color == '—' else color,
                    title=f'{y} от {x}',
                )
                st.plotly_chart(plotly_whiteboard(fig), width='stretch')
            if len(numeric) >= 2:
                st.subheader('Корреляции')
                st.dataframe(work[numeric].corr().round(3), width='stretch')


main()
