# Аккаунт одним passkey и recovery-коды

Человек без Telegram, Google и почты создаёт аккаунт одним passkey, получает 8 recovery-кодов
и позже привязывает Telegram или Google. Работает поверх обычного `PASSKEY_*` (см. `.env.example`).

## Включение

| Настройка | По умолчанию | Смысл |
|---|---|---|
| `PASSKEY_SIGNUP_ENABLED` | `false` | Открыть регистрацию ключом. Требует рабочий `PASSKEY_ENABLED` + `PASSKEY_RP_ID` + `PASSKEY_ORIGINS` |
| `PASSKEY_SIGNUP_TRIAL_ENABLED` | `false` | Триал для `auth_type='passkey'` (поверх `TRIAL_DISABLED_FOR`) |
| `PASSKEY_SIGNUP_BONUSES_ENABLED` | `false` | Рекламные кампании и реферальные бонусы при регистрации |
| `PASSKEY_SIGNUP_LIMIT_PER_MINUTE/HOUR/DAY` | `3/10/30` | Попыток регистрации с одного IP; `0` выключает окно |

Анонимный аккаунт создаётся за секунды, поэтому триал, кампании и рефералы ему по умолчанию
не выдаются: человек платит первым (баланс, покупка) или привязывает Telegram и попадает под
обычные правила. Проверка триала сидит в `Settings.is_trial_disabled_for_user` и действует во всех
местах, где триал проверяется (кабинет, бот, miniapp, rich menu).

При `INVITE_ONLY_ENABLED=true` регистрация ключом закрыта (403 `registration_invite_required`):
приглашение привязано к личности, а здесь её нет.

## Ограничение: уведомления

У такого аккаунта нет ни почты, ни Telegram. Уведомления (окончание подписки, низкий баланс,
ответы поддержки) идут только в кабинет, пока человек не привяжет канал. Об этом стоит сказать
в интерфейсе при показе recovery-кодов.

## Эндпоинты

Публичные:

- `GET  /cabinet/auth/passkey/status` — `{enabled, signup_enabled}`.
- `POST /cabinet/auth/passkey/signup/options` — тело пустое или `{display_name?}` → `{challenge_id, options}`.
  `user.id` в options случайный (32 байта): у аккаунта ещё нет id.
- `POST /cabinet/auth/passkey/signup/verify` — `{challenge_id, credential, name?, accepted_legal_documents?,
  campaign_slug?, referral_code?}` → `AuthResponse` + `recovery_codes` (один раз, `Cache-Control: no-store`).
  User, ключ и коды создаются одной транзакцией. `409 passkey_already_registered` на занятый
  `credential_id`, `428` без обязательных галочек согласия (челлендж при этом не тратится).
  `campaign_slug` и `referral_code` игнорируются, пока `PASSKEY_SIGNUP_BONUSES_ENABLED` выключен.
- `POST /cabinet/auth/recovery/login` — `{code}` → `AuthResponse` + `recovery_login: true`.
  Фронт обязан сразу предложить добавить новый passkey.

Под JWT:

- `GET  /cabinet/passkey/recovery-codes` — `{remaining, total}`.
- `POST /cabinet/passkey/recovery-codes/regenerate` — заменяет весь набор, новые коды показывает один раз;
  3 раза в час на пользователя.

## Как устроены коды

- 80 бит из `secrets`, 16 символов Crockford base32, показ `XXXX-XXXX-XXXX-XXXX`. При вводе регистр,
  дефисы и пробелы не важны, `I`/`L` читаются как `1`, `O` как `0`.
- В `user_recovery_codes` лежит только `HMAC-SHA256(перец, код)`; перец выводится из `CABINET_JWT_SECRET`.
  Открытый код нигде не хранится и не логируется. **Смена `CABINET_JWT_SECRET` обнуляет все коды** —
  владельцам придётся перевыпустить набор.
- Вход по коду: один `UPDATE ... SET used_at=now() WHERE code_hash=:h AND used_at IS NULL RETURNING user_id`
  (из двух параллельных входов выигрывает один), коммит сразу, и только потом проверка статуса аккаунта.
  Любой отказ — один и тот же 401 `Recovery code is invalid`.
- Лимиты входа по коду: 5 в минуту и 20 в час с IP, `fail_closed` (без Redis — 429).
- Recovery-коды не считаются способом входа для гарда «нельзя удалить последний passkey».

## userHandle

Ключи, добавленные из профиля, используют handle, выведенный из `user_id`. Аккаунт, созданный
самим passkey, получает случайный handle до того, как у него появился id; он хранится в
`user_passkeys.user_handle`. Вход принимает оба варианта.
