# LolzOfftopik

Личный Telegram-клиент для раздела **«Оффтоп»** на lolz.live (`forum_id=8`). Бот:

- следит за новыми темами в разделе через официальный API `prod-api.lolz.live` (Bearer JWT, scope `read post`);
- присылает их тебе в личку с фото/видео из первого поста и кнопками **❤ Лайк** / **✍ Ответить**;
- после ответа карточка темы превращается в «✅ Ответил…» с кнопкой **✏ Изменить ответ**;
- управление поллингом — кнопками **▶ Начать оффтопить** / **⏹ Окончить оффтоп**;
- доступен только владельцу (`TELEGRAM_OWNER_ID`); любой другой получает «⛔ Вы не создатель.»;
- режим **«👀 Просмотр»** — заходит на HTML-страницы тем под твоей сессией, чтобы XenForo показывал тебя в виджете «members currently viewing this thread» (см. ниже).

## Локальный запуск

```bash
cp .env.example .env
# заполни .env
pip install -r requirements.txt
python -m app.main
```

## Деплой на Render (free Web Service)

1. В Render → New → Web Service → подключить этот репозиторий.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python -m app.main`
4. Добавить env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `LOLZ_API_TOKEN` (остальные есть в `render.yaml`).
5. Health check path: `/health`.
6. (Опционально) `LOLZ_XF_USER_COOKIE`, `LOLZ_XF_SESSION_COOKIE`, `LOLZ_XF_CSRF_COOKIE` — для режима «Просмотр».

Free-план Render усыпляет сервис после ~15 минут без HTTP-трафика. Чтобы держать его живым:

- **cron-job.org** → создать job, который дергает `https://<имя-сервиса>.onrender.com/health` каждые 10 минут.

## Конфиг

См. `.env.example`.

## Режим «👀 Просмотр»

Когда заданы все три куки сессии (`xf_user`, `xf_session`, `xf_csrf` — взять из DevTools браузера, где залогинен на lolz.live), в нижней клавиатуре появляется кнопка **«👀 Просмотр: вкл/выкл»**. После включения бот в фоне ходит по темам форума `LOLZ_OFFTOP_FORUM_ID` под твоей сессией. Другие пользователи видят тебя в списке «смотрят тему».

Поведение целенаправленно «человечное»:

- Несколько параллельных «вкладок» (`CONCURRENCY` в `app/viewer.py`).
- Длительность «чтения» — экспоненциальное распределение (короткие просмотры частые, длинные редкие).
- ~10% тем «закрыл сразу», ~25% тем — догрузка `?page=2`.
- Никаких фиксированных интервалов между темами.
- Браузерный fingerprint: реалистичный Chrome User-Agent, `Sec-Fetch-*`, `Accept-Language: ru-RU`, `Referer`.
- При 3 ответах подряд `401`/`403` — бот сам выключает просмотр и шлёт в ТГ «❗ Куки протухли».

## Структура

```
app/
  config.py        # env vars
  main.py          # entry point
  health.py        # /health HTTP endpoint
  poller.py        # background poll loop
  notif_poller.py  # /notifications -> Telegram
  viewer.py        # «Просмотр» — HTML hits with browser cookies
  db/store.py      # SQLite state
  lolz/
    client.py      # Bearer-auth lolz API client (rate-limited)
    parser.py      # post -> thread, media extraction
  bot/
    handlers.py    # aiogram handlers (start/stop, like/reply/edit, view)
    cards.py       # render thread cards / replied state in TG
    keyboards.py   # inline keyboards
```

## Rate limit lolz API

20 req/min (3 сек между запросами). Клиент в `app/lolz/client.py` соблюдает это автоматически.
