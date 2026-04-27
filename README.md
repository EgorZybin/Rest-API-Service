# Sherlock API

REST API сервис поверх Telegram-бота Sherlock.

Что делает сервис:
- принимает поисковые запросы,
- кладет задачи в очередь,
- диспетчер выполняет задачи через пул Telegram-аккаунтов,
- отдает результат через `/v1/tasks/*`, SSE или webhook.

## Быстрый старт

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

sherlock-api db upgrade
sherlock-api serve --reload
# в отдельном процессе:
sherlock-api dispatcher run
```

Swagger: `http://localhost:8000/docs`  
Health: `http://localhost:8000/v1/health`

## Конфиг аккаунтов

- `ACCOUNTS_DIR` — каталог с `.session + .json` (по умолчанию `./accounts`).
- Управление аккаунтами выполняется через admin API (`/v1/admin/accounts/*`).

## Основные endpoint-ы

### Клиентские
- `GET /v1/health`
- `POST /v1/search/phone`
- `POST /v1/search/nick`
- `POST /v1/search/photo`
- `POST /v1/search/email`
- `POST /v1/search/car-plate`
- `POST /v1/search/vin`
- `POST /v1/search/address`
- `POST /v1/search/cadastre`
- `POST /v1/search/docs/inn`
- `POST /v1/search/docs/snils`
- `POST /v1/search/docs/passport`
- `POST /v1/search/legal`
- `POST /v1/search/domain-ip`
- `POST /v1/search/tag`
- `GET /v1/tasks`
- `GET /v1/tasks/{id}`
- `GET /v1/tasks/{id}/interactions`
- `GET /v1/tasks/{id}/stream`

`POST /v1/search/*` всегда работает в async-режиме: ручка возвращает `task_id`,
дальше результат читается через `/v1/tasks/*`, SSE или webhook.

Webhook (`webhook_url`) отправляет `POST` с JSON-envelope:
- `event` — тип события (`task.terminal`)
- `event_version` — версия формата события (сейчас `1`)
- `attempt` — номер попытки доставки
- `sent_at` — UTC timestamp отправки
- `task` — полное состояние задачи (`TaskOut`)

### Admin: аккаунты
- `GET /v1/admin/accounts`
- `GET /v1/admin/accounts/summary`
- `PATCH /v1/admin/accounts/{id}`
- `POST /v1/admin/accounts/{id}/deactivate`
- `DELETE /v1/admin/accounts/{id}/delete`
- `POST /v1/admin/accounts/load`
- `POST /v1/admin/accounts/upload`
- `POST /v1/admin/accounts/{id}/health`
- `POST /v1/admin/accounts/health/all`
- `POST /v1/admin/accounts/profile/all`

## Авторизация

Все защищенные ручки используют один ключ из `.env`:

- `CLIENT_API_KEY`
- заголовок: `X-API-Key: <CLIENT_API_KEY>`

## CLI

```bash
sherlock-api serve [--reload]
sherlock-api dispatcher run

sherlock-api db upgrade [REVISION]
sherlock-api db downgrade [REVISION]
sherlock-api db current

sherlock-api tasks list [--status STATUS] [--limit N]
sherlock-api tasks show ID
```

## Makefile

```bash
make run
make dispatcher
make migrate-up
make lint
make fmt
make check
```
