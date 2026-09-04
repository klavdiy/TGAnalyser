# TGAnalyser

Рейтинг публичных Telegram-каналов по читаемости: снимок через официальный Telegram API, локальная база, Excel и веб-интерфейс.

Новое относительно старой версии: вместо разбора одного канала «вглубь» инструмент сравнивает набор каналов между собой. Охват — медиана просмотров постов возрастом от суток до недели. Читаемость — охват, делённый на число подписчиков. Перцентиль — насколько канал читают лучше каналов похожего размера.

---

## Что получается на выходе

1. **SQLite-снимок** `data/rank.sqlite` — подписчики, посты за 90 дней, просмотры, пересылки, реакции, тексты.
2. **Excel и Markdown** в `TGSpyder_Output/rank/` — таблица рейтинга.
3. **Публичный JSON** — только метрики, без текстов и about: `python rank_export.py --json latest.json`
4. **Закрытый JSON** — тексты, ссылки, пересылки, about: `python rank_export.py --json-admin private.json` (не коммитить в открытый репозиторий)
5. **Локальный GUI** — `streamlit run app.py`

Сессии Telegram, `.env` и sqlite в git не входят.

---

## Что нужно заранее

- Python 3.10 или новее
- Аккаунт Telegram
- Приложение на [my.telegram.org](https://my.telegram.org) → *API development tools* → тип **Desktop**
  - `api_id` — число
  - `api_hash` — строка
- Каналы должны быть публичными (есть username)

Ключи выдаёт Telegram вам лично. Их нельзя публиковать и нельзя класть в git.

---

## Установка с нуля

```bash
git clone https://github.com/klavdiy/TGAnalyser.git
cd TGAnalyser

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

Состав `requirements.txt`: Telethon, pandas, openpyxl, streamlit, plotly, tqdm, qrcode.

---

## Настройка ключей

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

```
TG_API_ID=12345678
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Подставьте свои значения с my.telegram.org. Файл `.env` уже в `.gitignore`.

Необязательно:

- `TG_API_ID_2` / `TG_API_HASH_2` — второе приложение, если первое получило длинный FloodWait
- `TG_CLOUD_PASSWORD` — только если Telegram при входе просит облачный пароль

---

## Каталог каналов

Список задаётся в `data/catalog.yaml`:

```yaml
channels:
- username: example_channel
  topic: Сообщество ИБ
  panel: true
```

- `username` — без `@`
- `topic` — подпись тематики в таблице
- `panel: true` — канал попадает в снимок `--only-panel`

В репозитории лежит готовый watchlist. Замените его своим.

---

## Первый вход в Telegram

Сессия создаётся один раз, файл `analyser_session.session` остаётся у вас на диске.

```bash
python collect_rank.py --login --only-panel
```

В терминале появится QR. На телефоне: **Настройки → Устройства → Подключить устройство**. После подтверждения вход запоминается.

Если Telegram просит облачный пароль, задайте `TG_CLOUD_PASSWORD` в `.env` и повторите команду.

---

## Снимок и рейтинг

```bash
python collect_rank.py --only-panel
python rank_export.py
streamlit run app.py
```

GUI: http://localhost:8501

Полезные флаги сбора:

| Флаг | Зачем |
|---|---|
| `--only-panel` | Только каналы с `panel: true` |
| `--resume` | Дособрать последний незавершённый снимок |
| `--delay 1.2` | Пауза между каналами, секунды |
| `--account auto` | Все заполненные приложения, с переключением при FloodWait |
| `--account 1` / `--account 2` | Только первое или второе приложение |
| `--days 14` | Глубина постов |
| `--full-history username` | Все посты канала, без окна `--days` |
| `--login` | Только авторизация, без сбора |

Экспорт:

```bash
python rank_export.py
python rank_export.py --json TGSpyder_Output/rank/latest.json
python rank_export.py --json-admin TGSpyder_Output/rank/private.json
```

Публичный JSON можно класть в GitHub Pages. Закрытый — только в приватный репозиторий: исходники открытого сайта его не содержат, страница `/tganalyst/admin/` забирает файл через GitHub API после входа.

---

## Как читать метрики

| Поле | Смысл |
|---|---|
| Охват | Медиана просмотров постов возрастом 24–168 часов |
| Читаемость | Охват ÷ подписчики, в процентах |
| Перцентиль | Доля каналов похожего размера с более низкой читаемостью. 80 = лучше 80% сверстников |
| Постов за 1–7 дней | Сколько публикаций попало в расчёт охвата. Нужно минимум 3, иначе места нет |
| Место | Только у активных каналов |

Похожий размер: подписчики в диапазоне 0,5×–2×. Если таких мало — сравниваются каналы того же порядка величины.

---

## Что лежит в репозитории

| Файл | Роль |
|---|---|
| `collect_rank.py` | Снимок каналов |
| `rank_store.py` | SQLite |
| `rank_metrics.py` | Охват, читаемость, перцентиль |
| `rank_export.py` | Excel, Markdown, JSON |
| `rank_queries.py` | Посты и история подписчиков |
| `tg_config.py` | Чтение ключей из `.env` |
| `tg_auth.py` | QR-вход |
| `app.py` | Streamlit GUI |
| `data/catalog.yaml` | Список каналов |
| `.env.example` | Шаблон ключей без значений |

Локальные сессии, пароли, sqlite и разовые скрипты разбора одного канала в git не публикуются.

---

## Автоматизация

В репозитории есть workflow `.github/workflows/collect-rank.yml` (ручной запуск). Для продакшена снимок раз в 12 часов крутится на сайте [klavdiy.github.io/tganalyst](https://klavdiy.github.io/tganalyst/): Actions забирает этот репозиторий как код сборщика, без копирования скриптов.

Секреты GitHub Actions (если запускаете сбор сами):

- `TG_API_ID`, `TG_API_HASH`
- `TELEGRAM_SESSION_B64` — `base64` от файла `analyser_session.session`

Опционально те же имена с суффиксом `_2`. Значения хранятся только в зашифрованных секретах GitHub, не в файлах репозитория.

---

## Ограничения

- Публичные каналы. Закрытые без username собрать нельзя.
- Telegram ограничивает частоту запросов (FloodWait). Скрипт ждёт или переключается на второе приложение.
- Охват — оценка по окну 1–7 дней, не официальная статистика канала.
- Список участников broadcast-канала API не отдаёт.

---

## Этика

Инструмент для анализа каналов, которыми вы владеете, или публичных каналов в рамках правил Telegram и закона. Автор не несёт ответственности за неправомерное использование.

Уязвимости — через GitHub Security Advisory, не публичным issue. См. [SECURITY.md](SECURITY.md).

---

## Лицензия

MIT — [LICENSE](LICENSE)
