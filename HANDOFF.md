# Передача работы — InTrAvia (ветка `SlavasTest`)

Документ для продолжения в новой сессии. Актуален на 15.11.2026.

## Где что лежит

- Репозиторий: `C:\Users\Вячеслав\PycharmProjects\Dorabotka_InTrAvia`
- Ветка: `SlavasTest` (запушена в `origin`, upstream настроен)
- Окружение: `.venv` (Python 3.13), ставится `pip install -r requirements-dev.txt`
- Разработка: `.venv\Scripts\python.exe app.py` → http://127.0.0.1:5000/login.html
- Продакшн: `.venv\Scripts\python.exe serve.py` (waitress) → http://127.0.0.1:8080
- HTTPS: `INTRAVIA_HTTPS=1` + сертификат (см. README, раздел «HTTPS»)
- Тесты: `.venv\Scripts\python.exe -m pytest tests -q` (137 проходит)

## Важное про состояние репозитория

**`main` в GitHub сломан**: в коммите `a142c68` закоммичены неразрешённые
конфликт-маркеры (71 в `app.py`, 20 в dispatcher, 30 в mcc, 6 в pilot).
Из `main` приложение не запускается. В `SlavasTest` конфликты разрешены
в пользу новой стороны — эта ветка рабочая. Команде об этом стоит сообщить.

При пуле из `main` использовать `git fetch --unshallow`, если клон был shallow:
иначе git путает базу слияния и выдаёт перевёрнутые конфликты.

## Учётные записи (демо)

| Роль | Логин | Пароль | Дашборд |
|---|---|---|---|
| admin | admin@aeroflot.ru | admin123 | /admin/dashboard.html |
| dispatcher | dispatcher@aeroflot.ru | disp123 | /dispatcher/dashboard.html |
| mcc | mcc@aeroflot.ru | mcc123 | /mcc/dashboard.html |
| cpu | cpu@aeroflot.ru | cpu123 | /cpu/dashboard.html |
| shift_supervisor | shift@aeroflot.ru | shift123 | /shift/dashboard.html |
| foreman | foreman@aeroflot.ru | foreman123 | /foreman/dashboard.html |
| pilot | pilot1@aeroflot.ru | pilot123 | /pilot/dashboard.html |
| engineer | kuznetsov@aeroflot.ru | eng123 | /engineer/dashboard.html |

## Что уже сделано

- Сквозная цепочка **MCC → ЦПУ → нач. смены → бригадир → техник → MCC**,
  хранение в БД: `requests.chain_stage`, журнал `chain_log`, справочник `staff`.
- У каждого звена свой профиль и дашборд (`templates/supervisor/dashboard.html`).
- Вердикт техника из интерфейса инженера (REPAIR / AOG / MEL), освобождение
  инженера после вердикта.
- ATLB пилота и подпись CRS на сервере (`/api/pilot/atlb`, `.../sign_crs`).
- Отчёты диспетчера на сервере (`reports`, `/api/reports`).
- Выгрузка в Excel: `/api/export/xlsx` (листы «Цепочка» и «Журнал»).
- Серверная авторизация: cookie-сессия, все `/api/*` и страницы закрыты,
  `CORS` не открыт наружу, debug выключен, bind на `127.0.0.1`.
- **Хэширование паролей** (`werkzeug.security`, scrypt): в `users` и `staff`
  хранятся только хэши. Хелперы `hash_password`, `is_password_hash`,
  `verify_password` — в начале `app.py`. Старые записи с открытым паролем
  принимаются один раз и при успешном входе переписываются хэшем (миграция
  без отдельного скрипта). `create_user`, изменение пароля, создание
  инженера/пилота и все сиды пишут хэш.
- `/api/staff` больше не отдаёт колонку `password` (было `SELECT *`).
- **Роли на маршрутах**: декоратор `@roles(...)` в `app.py`; `admin` проходит
  всюду, остальные — только свои действия. Дашборды закрыты матрицей
  `PAGE_ACCESS` — чужой дашборд перекидывает на свой (`ROLE_HOME`).
  Пилот/инженер правят только свой профиль. MCC может назначать любое звено
  цепочки (его дашборд это умеет), остальные — только свой шаг.
- **`/api/chain` фильтруется на сервере**: MCC и admin видят всю цепочку,
  ЦПУ — всё, что вышло из MCC, нач. смены и бригадир — только назначенное им.
- **`/api/requests` и `/api/pilot/atlb` тоже ограничены**: инженер получает
  только свои заявки, пилот — только свой ATLB (имя берётся из базы, `?pilot=`
  не подделать). Пилоту `/api/requests` больше не нужен и закрыт.
- **Права на конкретную запись** (`owns_request`): инженер действует только
  по своей заявке (вердикт, отказ, отмена, аудио), пилот — только по своей
  (правка, отмена, подтверждение, CRS), нач. смены и бригадир — только по
  назначенной им. Диспетчер, MCC и admin работают со всеми — это их зона.
- **Продакшн-сервер и HTTPS**: `serve.py` на waitress (порт 8080). Waitress
  не умеет TLS, поэтому при `INTRAVIA_HTTPS=1` перед ним поднимается
  встроенный TLS-терминатор на loopback, а cookie получает `Secure`.
  Заголовки `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`
  и `SameSite=Lax` + `HttpOnly` для cookie — на всех ответах.
- **База вынесена из проекта и копируется**: путь по умолчанию
  `%LOCALAPPDATA%\InTrAvia\database.db`; старая `database.db` из проекта
  переносится при первом запуске (копия, старый файл остаётся). Перед каждым
  стартом снимается копия в `backups/` (хранятся 14 последних).
- **Смена паролей и чистый старт**: `manage.py` (`list`, `passwd`,
  `create-admin`, `check-defaults`); `INTRAVIA_DEMO_SEED=0` не создаёт демо-учётки.
  Пароль инженера/пилота можно задать при создании через API.
- 137 автотестов в `tests/` (`test_passwords.py`, `test_roles.py`,
  `test_backup.py`, `test_scoping.py`, `test_ownership.py`, `test_manage.py`).

## Что осталось до реальной эксплуатации

1. **Перед показом/эксплуатацией** прогнать `python manage.py check-defaults`
   и сменить демо-пароли (`passwd`), либо поднять чистую базу с
   `INTRAVIA_DEMO_SEED=0` и `create-admin`. В текущей базе демо-пароли не
   тронуты специально — они нужны для входа на демо.
2. Боевой TLS: встроенный терминатор — для стенда. Для эксплуатации поставить
   Caddy/IIS/nginx с настоящим сертификатом и указать `INTRAVIA_TRUSTED_PROXY`.

## Хвосты, не связанные с безопасностью

- **Проверить в браузере отчёты диспетчера** после перевода на `/api/reports`
  (тесты покрывают только сервер).
- **Голосовой ввод ни разу не запускался**: ffmpeg установлен и находится,
  но модель Whisper (`small`, ~460 МБ) не скачана. Перед финалом прогнать
  с реальным микрофоном; на площадке модели может не быть — прогреть кэш.
- В `localStorage` остались зеркала: `dispatcherState_v19`,
  `dispatcherPilotRequests_v4`, `pilotRecords_v4`, `taskHistory`.
  На данные не влияют, но это дубли — можно вычищать постепенно.

## Полезные мелочи

- Переменные окружения: `INTRAVIA_SECRET`, `INTRAVIA_DB`, `INTRAVIA_HOST`,
  `INTRAVIA_PORT`, `INTRAVIA_DEBUG=1`, `INTRAVIA_FFMPEG`, `WHISPER_MODEL`.
  Для `serve.py`: `INTRAVIA_HTTPS=1`, `INTRAVIA_TLS_CERT`, `INTRAVIA_TLS_KEY`,
  `INTRAVIA_TRUSTED_PROXY`. База и бэкапы: `INTRAVIA_DATA_DIR`, `INTRAVIA_DB`,
  `INTRAVIA_BACKUP=0`, `INTRAVIA_BACKUP_DIR`, `INTRAVIA_BACKUP_KEEP`.
  Учётные записи: `INTRAVIA_DEMO_SEED=0` (без демо-учёток).
- База по умолчанию: `%LOCALAPPDATA%\InTrAvia\database.db`, бэкапы в
  `...\InTrAvia\backups`. Старая `database.db` в проекте больше не используется,
  но и не удалена — можно оставить как точку отката.
- `serve.py` при HTTPS держит waitress только на 127.0.0.1 и слушает снаружи
  TLS-терминатором; `server.effective_port` выбирается динамически, чтобы не
  занимать порт 8080 дважды.
- В `app.py` есть блоки, помеченные `[AI]` / `[MAN]` / `[MIX]` — так команда
  отмечала, что писал ассистент, а что человек. Ориентируйтесь на `[MAN]`.
- `database.db` вне git (`.gitignore`), создаётся и наполняется при старте.
- Пароли: формат werkzeug `<метод>$<соль>$<хэш>`. `verify_password` вторым
  значением возвращает «запись была открытым текстом» — `auth_login` в этом
  случае сразу переписывает её хэшем. Если встретится баг «пароль не
  подходит» после смены, проверять `is_password_hash` у строки в базе.
- Хэш scrypt занимает ~0,17 с; вход поэтому чуть медленнее — это нормально.
- Роли: `@roles('mcc')` ставится под `@app.route` и всегда пропускает
  `admin`. Добавляя новый маршрут, сразу решите, какие роли его зовут:
  без декоратора он открыт любому вошедшему.
