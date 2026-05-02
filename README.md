# LolzOfftopik

Личный Telegram-клиент для раздела **«Оффтоп»** на lolz.live (`forum_id=8`). Бот:

- следит за новыми темами в разделе через официальный API `prod-api.lolz.live` (Bearer JWT, scope `read post`);
- присылает их тебе в личку с фото/видео из первого поста и кнопками **❤ Лайк** / **✍ Ответить**;
- после ответа карточка темы превращается в «✅ Ответил…» с кнопкой **✏ Изменить ответ**;
- управление поллингом — кнопками **▶ Начать оффтопить** / **⏹ Окончить оффтоп**;
- защищён паролем (по умолчанию `мега`) — без пароля бот молчит;
- ограничен одним пользователем (`TELEGRAM_OWNER_ID`).

## Локальный запуск

```bash
cp .env.example .env
# заполни .env
pip install -r requirements.txt
python -m app.main
```

После запуска:
- открой бота в Telegram, нажми `/start`,
- введи пароль `мега`,
- нажми `▶ Начать оффтопить`.

## Деплой на Render (free Web Service)

1. В Render → New → Web Service → подключить этот репозиторий.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python -m app.main`
4. Добавить env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `LOLZ_API_TOKEN` (остальные есть в `render.yaml`).
5. Health check path: `/health`.

Free-план Render усыпляет сервис после ~15 минут без HTTP-трафика. Чтобы держать его живым:

- **cron-job.org** → создать job, который дергает `https://<имя-сервиса>.onrender.com/health` каждые 10 минут.

## Конфиг

См. `.env.example`.

## AI-черновики ответов (опционально)

Если задан `GEMINI_API_KEY`, бот под каждой новой темой присылает отдельным
сообщением **черновик ответа** от Gemini с тремя кнопками:

- **✅ Разрешить** — отправить этот текст в тему как ответ;
- **🔄 Другой вариант** — перегенерировать с другим temperature;
- **❌ Закрыть** — просто удалить черновик.

**Пока ты не нажал «Разрешить» — на форум ничего не уходит.** Включение/выключение
рантайм:

- `/ai_on` — включить черновики;
- `/ai_off` — выключить;
- `/ai_status` — состояние, модель, кол-во обученных реплик.

Чтобы AI отвечал в твоём стиле, сначала спарси свои старые ответы из оффтопа:

```
/learn_replies                    # 50 страниц timeline, до 500 реплик
/learn_replies 100                # 100 страниц
/learn_replies 50 1000            # 50 страниц, цель — 1000 реплик
```

Парсер ходит по `/users/{me}/timeline`, фильтрует посты в форуме `LOLZ_OFFTOP_FORUM_ID`,
очищает BBCode и складывает в SQLite. Из-за rate-limit lolz API (3 сек/запрос)
сбор займёт время.

## Структура

```
app/
  config.py        # env vars
  main.py          # entry point
  health.py        # /health HTTP endpoint
  poller.py        # background poll loop
  notif_poller.py  # /notifications -> Telegram
  db/store.py      # SQLite state
  lolz/
    client.py      # Bearer-auth lolz API client (rate-limited)
    parser.py      # post -> thread, media extraction
  ai/
    gemini.py      # async REST client for Gemini generateContent
    suggester.py   # build prompt -> draft message + permission buttons
    learn.py       # walk timeline, store user's old offtop replies
  bot/
    handlers.py    # aiogram handlers (password, start/stop, like/reply/edit, ai)
    cards.py       # render thread cards / replied state in TG
    keyboards.py   # inline keyboards
```

## Rate limit lolz API

20 req/min (3 сек между запросами). Клиент в `app/lolz/client.py` соблюдает это автоматически.
