# Лёгкий триал для аккаунтов без Telegram

Short English summary: accounts without a Telegram id (email, Google, other OAuth, passkey) can get a
short, capped "lite" trial instead of the regular one. It is off by default (`TRIAL_LITE_ENABLED=false`);
with the flag off nothing changes. Everything is configurable live from the admin settings
(category "trial") and the cost ceiling is `TRIAL_LITE_DAILY_LIMIT x TRIAL_LITE_TRAFFIC_LIMIT_GB` per day.

## Зачем

Аккаунт кабинета заводится за секунды, поэтому полный триал (например 14 дней и 1000 ГБ) без привязки к
Telegram можно накручивать, сколько ни есть почтовых адресов. Полный триал остаётся за аккаунтом с
Telegram («якорь»). Остальным даётся короткая проверка с потолком расходов: 30 триалов × 2 ГБ ≈ 60 ГБ в сутки,
сколько бы аккаунтов ни завели.

## Режимы триала

Режим решает одна функция, `app.services.trial_eligibility.get_trial_mode(user)`; её зовут кабинет, бот,
мини-приложение и rich-меню, поэтому они не расходятся.

| Условие | Режим |
|---|---|
| `TRIAL_DISABLED_FOR=all` | нет триала (закрыты оба режима) |
| у аккаунта есть `telegram_id` | `full`, по прежним правилам (`TRIAL_DISABLED_FOR`, `PASSKEY_SIGNUP_TRIAL_ENABLED`) |
| нет `telegram_id`, `TRIAL_LITE_ENABLED=true` | `lite` |
| нет `telegram_id`, `TRIAL_LITE_ENABLED=false` | как раньше: `full` или нет, по `TRIAL_DISABLED_FOR` |

`TRIAL_DISABLED_FOR=email` закрывает полный триал аккаунтам без Telegram, но не лёгкий. Использован ли триал
уже, по-прежнему решает `User.is_trial_already_used()` (один триал на пользователя, лёгкий тоже).

Лёгкий триал выдаёт только кабинет (`POST /cabinet/subscription/trial`). Бот и мини-приложение работают с
аккаунтами, у которых есть Telegram, и открывают только полный триал.

## Настройки

Все параметры лежат в `Settings`, видны в админке кабинета и бота в категории «Пробный период» и
применяются сразу, без перезапуска. Границы проверяются при вводе из админки; значение из `.env` вне границ
ограничивается при чтении.

| Переменная | По умолчанию | Границы | Смысл |
|---|---|---|---|
| `TRIAL_LITE_ENABLED` | `false` | да/нет | включает лёгкий триал |
| `TRIAL_LITE_DURATION_HOURS` | `24` | 1–720 | срок в часах |
| `TRIAL_LITE_TRAFFIC_LIMIT_GB` | `2` | 1–1000 | трафик, ГБ (безлимита нет) |
| `TRIAL_LITE_DEVICE_LIMIT` | `1` | 1–20 | устройства |
| `TRIAL_LITE_DAILY_LIMIT` | `30` | 0–100000 | лёгких триалов в сутки на всю систему; `0` закрывает триал |
| `TRIAL_LITE_PER_IP_DAILY_LIMIT` | `1` | 0–1000 | лёгких триалов в сутки с одного IP; `0` выключает проверку |

Чтобы включить на проде, достаточно `TRIAL_LITE_ENABLED=true` (остальное по умолчанию) или переключателя
в админке. Нужен Redis.

## Что проверяется при выдаче

1. **Журнал личностей.** При выдаче триала в `trial_identity_ledger` записываются ключи личности:
   `tg:<id>`, `email:<нормализованный адрес>` (только подтверждённый), `google:<sub>` (так же `yandex:`,
   `discord:`, `vk:`). Если любой ключ уже в журнале, триал не выдаётся, даже если аккаунт создан заново.
   У gmail.com и googlemail.com точки и `+tag` в адресе срезаются. В таблице только `HMAC-SHA256` ключа под
   перцем из `CABINET_JWT_SECRET`, связи с пользователем нет; ротация секрета обнуляет память журнала. Сброс
   триала администратором (`trial_reset_at`) журнал обходит. Журнал работает только при включённом флаге.
2. **Лимит на IP.** Не больше `TRIAL_LITE_PER_IP_DAILY_LIMIT` лёгких триалов в сутки с адреса
   (реальный IP через `get_client_ip`, то есть учитывается `CABINET_TRUSTED_PROXIES`; IPv6 считается по сети /64).
3. **Общий дневной бюджет.** Не больше `TRIAL_LITE_DAILY_LIMIT` лёгких триалов в сутки на всю систему.

Сутки — календарный день Europe/Moscow. Счётчики лежат в Redis (`lite_trial:<YYYY-MM-DD>:daily` и
`lite_trial:<YYYY-MM-DD>:ip:<адрес>`) и сами истекают к концу суток. Оба лимита занимаются одним Lua-скриптом,
то есть без гонки «проверил, потом записал»; отказ по одному лимиту не тратит другой. Слот занимается до
создания подписки и возвращается, если создать её не удалось. **Redis недоступен — лёгкий триал не
выдаётся** (безопасный отказ: без счётчика потолок расходов не гарантирован).

Посмотреть расход за сегодня: `redis-cli GET lite_trial:$(TZ=Europe/Moscow date +%F):daily`.

## API

`GET /cabinet/subscription/trial` (`TrialInfoResponse`) получил поля:

* `kind`: `full`, `lite` или `null` (триала нет);
* `duration_hours`: точный срок (у лёгкого триала `duration_days` округляется вниз и может быть `0`);
* `reason_unavailable` теперь может быть кодом: `daily_limit_reached`, `ip_limit_reached`,
  `identity_already_used`, `not_available`; прежние тексты («You already have an active subscription»,
  «Trial already used», «Trial is not available for your account type») сохранены.

`POST /cabinet/subscription/trial` при отказе по лёгкому триалу отвечает `429` (дневной лимит, лимит IP) или
`400` (остальное) с `detail: {code, message}`; `code` совпадает с `reason_unavailable`.

Подписка лёгкого триала: `is_trial=true`, срок `TRIAL_LITE_DURATION_HOURS`, `TRIAL_LITE_TRAFFIC_LIMIT_GB`,
`TRIAL_LITE_DEVICE_LIMIT`, без привязки к тарифу; сквады те же, что у обычного триала. Срок хранится датой
окончания, поэтому истечение, уведомление «триал скоро закончится» (за 2 часа) и покупка платной подписки
работают как у обычного триала. Пользователю без Telegram уведомление уходит по почте.

## Миграция 0139

* снимает NOT NULL с `users.telegram_id` и `support_audit_logs.actor_telegram_id` (на базах, прошедших старые
  миграции, колонки остались обязательными, хотя модели объявляют их необязательными); шаг пропускается там,
  где колонка уже необязательная;
* создаёт `trial_identity_ledger`. Миграция идемпотентна; downgrade удаляет только журнал.

## Что не входит

Апгрейд лёгкого триала до полного при привязке Telegram, подарки, HWID-учёт, фронтенд кабинета (читает `kind`,
`duration_hours`, `reason_unavailable`). Журнал личностей в боте и мини-приложении при выдаче полного триала
не ведётся и не проверяется.
