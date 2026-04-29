# Инструкция для агента: безопасное обновление продового бота

Этот документ описывает, как обновлять прод на `ssh polite_root` так, чтобы не потерять сеть, доступ к Telegram и рабочее состояние базы.

## Цель

Обновить `remnawave_bot` до свежего `origin/main` с контролируемыми миграциями и возможностью быстрого отката.

## Текущий продовый контур

- Хост: `polite_root`
- Папка проекта: `/opt/remnawave/remnawave-bedolaga-telegram-bot`
- Сервисы:
  - `remnawave_bot`
  - `remnawave_bot_db`
  - `remnawave_bot_redis`
  - `remnawave-miniapp-static`
- Критичные сети бота:
  - `remnawave-network`
  - `remnawave-bedolaga-telegram-bot_bot_network`

## Жесткие правила

1. Не трогать Traefik и внешнюю сетевую схему.
2. Не делать `docker compose up -d` для всего стека; для выката использовать только `docker compose up -d --no-deps bot`.
3. Всегда делать backup БД и snapshot текущего образа до обновления.
4. Если разрыв версий большой или есть legacy-следы Alembic, сначала делать dry-run миграций на копии БД.
5. Не удалять локальные override-файлы (минимум `docker-compose.yml`, `vpn_logo.png`, `miniapp/*` если используются).
6. Если в `.env` есть `TELEGRAM_BOT_PROXY`, а новый код ожидает `PROXY_URL`, перенести значение в `PROXY_URL`.

## Шаг 0. Быстрая диагностика

```bash
ssh polite_root 'hostname && date'
ssh polite_root 'docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"'
ssh polite_root 'cd /opt/remnawave/remnawave-bedolaga-telegram-bot && git branch --show-current && git rev-parse --short HEAD && git status --short'
```

## Шаг 1. Backup перед любыми изменениями

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /opt/remnawave/backups/remnawave-bedolaga-telegram-bot
POSTGRES_DB=$(grep -m1 "^POSTGRES_DB=" .env | cut -d= -f2-)
POSTGRES_USER=$(grep -m1 "^POSTGRES_USER=" .env | cut -d= -f2-)
BACKUP_FILE="/opt/remnawave/backups/remnawave-bedolaga-telegram-bot/prod-${POSTGRES_DB}-${TS}.dump"
docker exec -i remnawave_bot_db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$BACKUP_FILE"
CURRENT_IMAGE=$(docker inspect --format "{{.Config.Image}}" remnawave_bot)
docker image tag "$CURRENT_IMAGE" "remnawave-bedolaga-telegram-bot-bot:preupdate-${TS}"
echo "DB backup: $BACKUP_FILE"
echo "Image snapshot: remnawave-bedolaga-telegram-bot-bot:preupdate-${TS}"
'
```

## Шаг 2. Сохранить локальные override-файлы

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
TS=$(date -u +%Y%m%dT%H%M%SZ)
SAFE_DIR="/opt/remnawave/backups/remnawave-bedolaga-telegram-bot/local-overrides-${TS}"
mkdir -p "$SAFE_DIR/miniapp"
cp -a docker-compose.yml "$SAFE_DIR/docker-compose.yml"
cp -a app/bot.py "$SAFE_DIR/bot.py" || true
cp -a app/config.py "$SAFE_DIR/config.py" || true
cp -a requirements.txt "$SAFE_DIR/requirements.txt" || true
[ -f vpn_logo.png ] && cp -a vpn_logo.png "$SAFE_DIR/vpn_logo.png" || true
[ -f miniapp/index.html ] && cp -a miniapp/index.html "$SAFE_DIR/miniapp/index.html" || true
[ -f miniapp/app-config.json ] && cp -a miniapp/app-config.json "$SAFE_DIR/miniapp/app-config.json" || true
echo "$SAFE_DIR"
'
```

Сохранить путь `SAFE_DIR`, он нужен после `git reset --hard`.

## Шаг 3. Оценить масштаб обновления

```bash
ssh polite_root '
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
git fetch origin
echo "LOCAL=$(git rev-parse HEAD)"
echo "REMOTE=$(git rev-parse origin/main)"
echo "COMMITS_BEHIND=$(git rev-list --count HEAD..origin/main)"
'
```

Если `COMMITS_BEHIND` большой (например десятки/сотни), обязательно dry-run миграций на копии БД.

## Шаг 4. Dry-run миграций на копии БД

### 4.1 Создать тестовую БД и восстановить backup

```bash
ssh polite_root '
set -euo pipefail
TEST_DB=remnawave_bot_upgrade_test_$(date +%Y%m%d)
docker exec -i remnawave_bot_db psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS ${TEST_DB};"
docker exec -i remnawave_bot_db psql -U postgres -d postgres -c "CREATE DATABASE ${TEST_DB};"
echo "$TEST_DB"
'
```

```bash
ssh polite_root 'docker exec -i remnawave_bot_db pg_restore -U postgres -d <TEST_DB> < /opt/remnawave/backups/remnawave-bedolaga-telegram-bot/<LATEST_DUMP>.dump'
```

### 4.2 Собрать образ целевой версии

```bash
ssh polite_root '
set -euo pipefail
rm -rf /opt/remnawave/remnawave-bedolaga-telegram-bot-upgrade-lab
git clone https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot.git /opt/remnawave/remnawave-bedolaga-telegram-bot-upgrade-lab
cd /opt/remnawave/remnawave-bedolaga-telegram-bot-upgrade-lab
git checkout origin/main
git rev-parse --short HEAD
docker build -t remnawave-bot-upgrade-lab:upgrade-test .
'
```

### 4.3 Прогнать Alembic на test DB

```bash
ssh polite_root '
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
POSTGRES_DB=$(grep -m1 "^POSTGRES_DB=" .env | cut -d= -f2-)
POSTGRES_USER=$(grep -m1 "^POSTGRES_USER=" .env | cut -d= -f2-)
docker run --rm \
  --network remnawave-bedolaga-telegram-bot_bot_network \
  --env-file /opt/remnawave/remnawave-bedolaga-telegram-bot/.env \
  -e POSTGRES_HOST=remnawave_bot_db \
  -e POSTGRES_PORT=5432 \
  -e POSTGRES_DB=<TEST_DB> \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e SKIP_MIGRATION=true \
  remnawave-bot-upgrade-lab:upgrade-test \
  python -c "import asyncio; from app.database.migrations import run_alembic_upgrade; asyncio.run(run_alembic_upgrade())"
'
```

Если dry-run упал, прод не обновлять. Исправить причину и повторить dry-run.

## Шаг 5. Выкатка в прод

### 5.1 Переключить checkout на target commit

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
git fetch origin
git stash push --include-untracked -m "pre-upgrade-$(date +%Y%m%d)" || true
git checkout main
git reset --hard origin/main
'
```

### 5.2 Вернуть локальные override-файлы

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
SAFE_DIR=<SAFE_DIR_FROM_STEP_2>
cp -a "$SAFE_DIR/docker-compose.yml" ./docker-compose.yml
[ -f "$SAFE_DIR/vpn_logo.png" ] && cp -a "$SAFE_DIR/vpn_logo.png" ./vpn_logo.png || true
mkdir -p miniapp
[ -f "$SAFE_DIR/miniapp/index.html" ] && cp -a "$SAFE_DIR/miniapp/index.html" ./miniapp/index.html || true
[ -f "$SAFE_DIR/miniapp/app-config.json" ] && cp -a "$SAFE_DIR/miniapp/app-config.json" ./miniapp/app-config.json || true
'
```

### 5.3 Гарантировать прокси-переменную в новом формате

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
if ! grep -q "^PROXY_URL=" .env; then
  TELEGRAM_PROXY=$(grep -m1 "^TELEGRAM_BOT_PROXY=" .env | cut -d= -f2- || true)
  [ -n "$TELEGRAM_PROXY" ] && printf "\nPROXY_URL=%s\n" "$TELEGRAM_PROXY" >> .env
fi
'
```

### 5.4 Legacy-safe patch (если нужен)

Если в проде исторически был legacy drift, перед миграцией добавить отсутствующую колонку:

```bash
ssh polite_root 'docker exec -i remnawave_bot_db psql -U postgres -d remnawave_bot -c "ALTER TABLE IF EXISTS partner_applications ADD COLUMN IF NOT EXISTS desired_commission_percent INTEGER;"'
```

### 5.5 Сборка и миграции на продовой БД

```bash
ssh polite_root '
set -euo pipefail
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
POSTGRES_USER=$(grep -m1 "^POSTGRES_USER=" .env | cut -d= -f2-)
POSTGRES_DB=$(grep -m1 "^POSTGRES_DB=" .env | cut -d= -f2-)
docker compose build --pull bot
docker run --rm \
  --network remnawave-bedolaga-telegram-bot_bot_network \
  --env-file /opt/remnawave/remnawave-bedolaga-telegram-bot/.env \
  -e POSTGRES_HOST=remnawave_bot_db \
  -e POSTGRES_PORT=5432 \
  -e POSTGRES_DB="$POSTGRES_DB" \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e SKIP_MIGRATION=true \
  remnawave-bedolaga-telegram-bot-bot:latest \
  python -c "import asyncio; from app.database.migrations import run_alembic_upgrade; asyncio.run(run_alembic_upgrade())"
'
```

### 5.6 Перезапустить только бот

```bash
ssh polite_root 'cd /opt/remnawave/remnawave-bedolaga-telegram-bot && docker compose up -d --no-deps bot'
```

## Шаг 6. Пост-проверки

```bash
ssh polite_root 'docker inspect --format "bot={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" remnawave_bot'
ssh polite_root 'docker inspect --format "db={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" remnawave_bot_db'
ssh polite_root 'docker inspect --format "redis={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" remnawave_bot_redis'
ssh polite_root 'docker inspect --format "{{json .NetworkSettings.Networks}}" remnawave_bot'
ssh polite_root 'cd /opt/remnawave/remnawave-bedolaga-telegram-bot && TOKEN=$(grep -m1 "^WEB_API_DEFAULT_TOKEN=" .env | cut -d= -f2-) && curl -fsS -H "X-API-Key: $TOKEN" http://127.0.0.1:8080/health'
ssh polite_root 'docker logs remnawave_bot 2>&1 | grep -E "Proxy configured|Nalogo proxy configured" | tail -n 20'
```

Ожидаемо:

- `remnawave_bot` = `running/healthy`
- DB/Redis = `running/healthy` и без рестарта
- `/health` отдаёт `status=ok`
- бот остаётся в двух сетях (`remnawave-network` и bot_network)
- в логах есть прокси-конфигурация (если используется)

## Быстрый откат (без восстановления БД)

```bash
ssh polite_root '
cd /opt/remnawave/remnawave-bedolaga-telegram-bot
ROLLBACK_TAG=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep "^remnawave-bedolaga-telegram-bot-bot:preupdate-" | head -n1)
docker image tag "$ROLLBACK_TAG" remnawave-bedolaga-telegram-bot-bot:latest
docker compose up -d --no-deps bot
'
```

## Полный откат с БД (только при необходимости)

1. Остановить только `bot`.
2. Восстановить БД из dump в `remnawave_bot` (через `dropdb/createdb/pg_restore`).
3. Перезапустить `bot`.

Эта операция рискованнее и требует окна обслуживания.
