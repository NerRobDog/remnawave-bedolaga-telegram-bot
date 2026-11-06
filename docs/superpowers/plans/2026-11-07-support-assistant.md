# Support Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On every new support ticket, post an analysis card to the admin ticket topic (deterministic checks + Claude card + owner buttons). On a bare first message, send the client a fixed set of clarifying questions once.

**Architecture:** New package `app/services/support_assist/`. Pure functions compute flags from a raw snapshot. The snapshot is collected from the bot DB and the Remnawave API. An allowlist serializer builds the LLM payload. Claude returns structured JSON, our code renders it. A fire-and-forget pipeline is hooked after the existing ticket notifications. One new table stores cards. Owner buttons reuse the manual admin-reply code path.

**Tech Stack:** Python 3.13, aiogram 3, SQLAlchemy async + Alembic, pytest + pytest-asyncio, `anthropic` Python SDK.

**Spec:** `docs/superpowers/specs/2026-11-06-support-assistant-design.md`. Spike source: `~/Library/Application Support/Claude/side-session-notes/local_1a6c31c3-6831-49dd-8103-b9ec88c5ee96/support-spike/` (checks.py, system_prompt.md, kb.json, report.md). Data map: `support-data-map.md` in the same folder.

**Base:** branch `feat/support-assistant-design`, on top of prod HEAD `7922f85e` (`feat/trial-end-offer`). The fork's `main` is stale; do not base on it.

## Global Constraints

- `SUPPORT_ASSIST_ENABLED: bool = False` and `SUPPORT_ASSIST_AUTO_ASK_ENABLED: bool = False` by default. Also `SUPPORT_ASSIST_SEND_BUTTONS_ENABLED: bool = False` for shadow phase 1. Plus `ANTHROPIC_API_KEY: str = ''`, `SUPPORT_ASSIST_MODEL: str = 'claude-opus-5-5'`, `SUPPORT_ASSIST_TIMEOUT_SECONDS: int = 90`, `SUPPORT_ASSIST_RECOMPUTE_MINUTES: int = 10`. Declare them next to the SUPPORT_* block in `app/config.py:105-140`.
- Read-only: the assistant never calls a mutating Remnawave API method or a CRUD writer, except for its own table, the auto-ask message and the send-on-button path.
- Never in the LLM payload: id, telegram_id, username, first/last name, email, IP, subscription URL, short_uuid, uuid, payment amounts or requisites. The client's ticket text goes as is.
- The client receives only (a) fixed question texts from locales, or (b) text the owner explicitly sent with a button.
- Any exception in the assistant is logged and swallowed. The ticket flow, notifications and SLA behave exactly as with the flag off.
- Card tone (in prompt): Russian, formal «вы», no apologies, no emoji in the draft, no promises of refunds, days or deadlines, no internal names (nodes, squads, flags).
- Migration `0142_support_assist`, `revision = '0142'`, `down_revision = '0141'`.
- Fork rule: commit messages **without** AI attribution (no Co-Authored-By trailer).
- Prod rollout is NOT part of this plan. It goes through the bedolaga-prod-update skill after the owner's «да», coordinated by the «Пахан» session.

## Review Focus

1. Two admins, or one admin double-tapping, press [Отправить] → the client gets exactly one message (atomic status transition). Test in Task 8.
2. The client sends a second message while the first card is still generating → at most one card per `SUPPORT_ASSIST_RECOMPUTE_MINUTES` per ticket; auto-ask never twice. Test in Task 7.
3. A user without a subscription, without `remnawave_id`, or with the panel API down → the card still appears, with ❓/no_subscription and no crash. Test in Tasks 3 and 7.
4. The first message is a photo with no text, or text in English from an `en` user → photo-only counts as bare but the screenshot question is not asked; questions come in the user's language. Test in Task 5.
5. A very long client text or many facts → the rendered card fits Telegram's 4096-char limit (draft truncated last). Test in Task 6.

---

### Task 1: Panel data source check (API vs DB role)

The spec's first decision. The likely answer is "API covers it": the API has `get_subscription_request_history` (`app/external/remnawave_api.py:927`), `get_user_stats_usage` (:1597), `get_user_devices` (:1768), `get_user_accessible_nodes` (:1090) and `get_all_nodes` (:1401).

**Files:**
- Create: `docs/support-assist-panel-access.md` (short findings note)

- [ ] **Step 1:** On polite, call each method above read-only through a one-off script inside the bot container (`docker exec remnawave_bot python -c ...`) for one test user (bot user 14 / its panel id). GET only, no mutating calls.
- [ ] **Step 2:** For each spike signal, record in the note which API field gives it, or "missing": request history with timestamps and user agents, the total count, per-day traffic for the last 2 days, devices with model/OS/created_at, accessible nodes with online/disabled status, last-connected node and online_at (`get_user_by_id`).
- [ ] **Step 3:** If anything is missing, list the exact panel tables and columns in the note as the scope of a future read-only role. Do NOT create the role. That is a prod change for the rollout step.
- [ ] **Step 4:** Commit `docs: support assist panel data access findings`.

Later tasks assume the API covers everything. If the note says otherwise, stop and report to the owner before Task 3.

### Task 2: Settings, model, migration

**Files:**
- Modify: `app/config.py` (settings from Global Constraints)
- Modify: `app/database/models.py` (`TicketMessage` at :4364: add `is_system = Column(Boolean, default=False, nullable=False, server_default='false')`; new `SupportAssistCard`)
- Create: `migrations/alembic/versions/0142_support_assist.py`
- Create: `app/database/crud/support_assist.py`
- Modify: `pyproject.toml`, `requirements.txt`, `uv.lock` (add `anthropic`, latest release that supports `output_config` and `fallbacks`)
- Test: `tests/crud/test_support_assist_crud.py`

**Interfaces — Produces:**
- `SupportAssistCard` columns: `id` pk, `ticket_id` FK tickets.id indexed, `created_at`, `trigger` str (`new_ticket`|`new_message`), `facts` JSON, `flags` JSON, `card` JSON nullable, `model` str nullable, `input_tokens`, `output_tokens`, `cache_read_tokens` int nullable, `cost_usd` Numeric(10,5) nullable, `status` str (`shown`|`sent`|`edited`|`dismissed`|`fallback`|`failed`), `final_text` Text nullable, `admin_message_id` BigInteger nullable, `auto_ask_question_ids` JSON nullable, `auto_ask_sent_at` nullable, `decided_by` BigInteger nullable, `decided_at` nullable.
- Partial unique index: one row per ticket where `auto_ask_sent_at IS NOT NULL`.
- CRUD:
  - `create_card(db, **fields) -> SupportAssistCard`
  - `last_card_for_ticket(db, ticket_id) -> SupportAssistCard | None`
  - `claim_card_decision(db, card_id, from_status='shown', to_status, admin_id) -> bool`. Does `UPDATE … WHERE id=:id AND status=:from_status`; returns True only if 1 row was updated.
  - `mark_auto_ask_sent(db, card_id, question_ids) -> bool`. Returns False when the unique index rejects it (IntegrityError → rollback → False).

- [ ] **Step 1:** Write tests in the existing CRUD test style (mock session, see `tests/crud/`): `test_claim_card_decision_only_once` (second call returns False), `test_mark_auto_ask_sent_rejects_second_ticket_card`.
- [ ] **Step 2:** Run `uv run pytest tests/crud/test_support_assist_crud.py -v` → FAIL (import error).
- [ ] **Step 3:** Implement the model, CRUD, migration (upgrade + downgrade) and settings. Add the dependency.
- [ ] **Step 4:** Run the tests → PASS. Run `uv run alembic upgrade head --sql | tail -40` and check the DDL for both changes.
- [ ] **Step 5:** Commit `feat(support-assist): settings, card table, ticket_messages.is_system`.

### Task 3: Snapshot and flags (deterministic checks)

Port of spike `checks.py`. Split: async I/O collects a `Snapshot`; a pure function computes flags.

**Files:**
- Create: `app/services/support_assist/__init__.py`, `snapshot.py`, `flags.py`
- Test: `tests/services/support_assist/test_flags.py`, `tests/services/support_assist/fixtures.py`

**Interfaces — Produces:**
- `@dataclass Snapshot` (in `snapshot.py`): `as_of: datetime`; `account: dict | None`; `subscription: dict | None` (status, end_date, tariff name, device_limit, is_daily, is_daily_paused, is_trial); `panel_user: dict | None` (status, expire_at, hwid_limit, online_at, last_node_name, first_connected_at); `devices: list[dict]` (platform, os_version, device_model, user_agent, created_at); `sub_requests: list[dict]` (request_at, user_agent); `sub_requests_total: int | None`; `usage_by_day: dict[str, int]` (date → bytes); `squad_nodes: list[dict]` (name, is_connected, is_disabled); `cabinet_actions_7d: list[dict]` (button_id, at); `transactions_48h: list[dict]` (type, status, at; no amounts); `bot_errors_24h: int`; `past_tickets: list[dict]` (id, created_at, status); `other_tickets_2h: int`; `restrictions: list[str]`; `missing: list[str]` (names of sources that failed).
- `async collect_snapshot(db: AsyncSession, api: RemnaWaveAPI, ticket: Ticket, now: datetime) -> Snapshot`. Each source has its own try/except. On failure the source name goes into `missing`.
- `@dataclass(frozen=True) Flag`: `code: str`, `sev: Literal['high','mid','low','info']`, `text: str` (Russian, owner-facing).
- `compute_flags(s: Snapshot) -> list[Flag]` (in `flags.py`). Pure; the same codes and conditions as spike checks.py, including these rules from the spike:
  - `never_fetched` only if `sub_requests_total == 0`. If `sub_requests` is empty but the total is unknown or >24 → `sub_history_unavailable`.
  - Nodes with `is_disabled` are never "down".
  - No `desync_status` when `is_daily_paused`.
  - Traffic flags use the previous full day relative to `as_of`.
  - `button_click_logs.user_id` is the bot `users.id`.
- Fixtures: `fixtures.py` builds the `Snapshot` for synthetic scenarios S01–S15 as in spike `scenarios.py` (ground truth from spike `private/scenarios.json`). Synthetic values only, no copied prod data.

- [ ] **Step 1:** Write `test_flags.py`, parametrized over S01–S15. Each case asserts the set of high/mid codes, e.g.:
  - S01 ⊇ {never_fetched, never_connected}
  - S03 ⊇ {device_limit_full}
  - S04 ⊇ {revoked_recently, not_refetched_after_revoke}
  - S05 ⊇ {sub_expired}
  - S06 ⊇ {desync_status, desync_expire}
  - S08 ⊇ {last_node_down, all_squad_nodes_down}
  - S10 ⊇ {daily_paused} and desync_status ∉
  - S11 ⊇ {restricted}
  - S12 ⊇ {last_node_down, mass_tickets}
  - S13 ⊇ {tx_pending}

  Also add `test_disabled_node_not_down`, `test_history_truncated_gives_unavailable_not_never_fetched`, and `test_no_subscription_snapshot` (subscription=None, panel_user=None → exactly no_subscription, no exception).
- [ ] **Step 2:** Run `uv run pytest tests/services/support_assist/test_flags.py -v` → FAIL.
- [ ] **Step 3:** Implement `flags.py`, then `snapshot.py`. Sources: bot CRUD/models, plus the API methods confirmed in Task 1. Add a `collect_snapshot` test with a mocked API that raises on devices → `'devices' in s.missing` and the other fields are filled.
- [ ] **Step 4:** Run the tests → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): deterministic snapshot and flags`.

### Task 4: Privacy allowlist payload

**Files:**
- Create: `app/services/support_assist/payload.py`
- Test: `tests/services/support_assist/test_payload.py`

**Interfaces — Produces:**
- `FORBIDDEN_KEYS: frozenset[str]` = {'id','user_id','telegram_id','username','first_name','last_name','full_name','email','ip','request_ip','ips','subscription_url','sub_url','short_uuid','uuid','remnawave_id','amount','amount_kopeks','payment_id','card','phone'}
- `build_payload(s: Snapshot, flags: list[Flag]) -> dict`. An allowlist copy: only fields explicitly named in the function are copied. Dates become ISO strings. Devices keep platform/os_version/device_model/created_at. Transactions keep type/status/at.

- [ ] **Step 1:** Write `test_payload_has_no_forbidden_keys`: a snapshot where every dict also carries every forbidden key with a sentinel value `"LEAK"`. Walk the payload recursively and assert no forbidden key and no `"LEAK"` string anywhere. Also write `test_payload_keeps_device_model_and_flags`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): allowlist LLM payload`.

### Task 5: Bare-message rule and question picker

**Files:**
- Create: `app/services/support_assist/questions.py`
- Modify: `app/localization/locales/ru.json`, `en.json` (keys `SUPPORT_ASSIST_Q_<ID>` and `SUPPORT_ASSIST_Q_INTRO`). Use the format of the existing locale files.
- Test: `tests/services/support_assist/test_questions.py`

**Interfaces — Produces:**
- `QUESTION_IDS = ('device', 'app', 'since_when', 'network', 'scope', 'sites', 'screenshot')`
- `is_bare(text: str | None, has_media: bool) -> bool`. Port of cabinet `src/utils/ticketQuality.ts` (repo `~/dev/bedolaga-cabinet-support-quality`, branch `feat/support-reject-bare-complaint`): MIN_TICKET_MESSAGE_LENGTH=120, MIN_TICKET_MESSAGE_WORDS=15, MIN_TICKET_DETAIL_WORDS=6, and the same COMPLAINT_WORDS set, as module constants. Empty text with media → True.
- `pick_questions(text: str, has_media: bool, s: Snapshot, flags: list[Flag]) -> list[str]`. Rules:
  - Returns `[]` if any flag is in OUR_FAULT = {'sub_expired','desync_status','desync_expire','last_node_down','all_squad_nodes_down','daily_paused','no_subscription','restricted'}.
  - Drops 'device' if `s.devices` is non-empty and the text names no other device.
  - Drops 'screenshot' if has_media.
  - Drops 'app' if the text mentions an app name (Clash Mi, Happ, Rabbit Hole, FlClash, v2ray, Hiddify).
  - Order as QUESTION_IDS.
- `render_questions(ids: list[str], lang: str) -> str`. Uses `get_texts(lang).t(key)`.
- Russian question texts: draft from the spike KB entry «Стандартный набор вопросов». They are marked for owner approval before phase 3.

- [ ] **Step 1:** Write tests:
  - `is_bare('не работает', False)` is True.
  - A 40-word detailed message is False.
  - `is_bare('', True)` is True.
  - `pick_questions` with flag sub_expired → [].
  - With a photo → no 'screenshot'.
  - With known devices → no 'device'.
  - Text «в Happ не грузит» → no 'app'.
  - `render_questions(['device'], 'en')` returns English text.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement and add the locale keys.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): bare-message rule and fixed client questions`.

### Task 6: Claude card and rendering

Read the claude-api skill files for Python (`python/` messages, structured outputs, prompt caching, the fallbacks beta) before coding. Do not guess SDK names.

**Files:**
- Create: `app/services/support_assist/prompt.md` (spike `system_prompt.md` verbatim, minus the format block, which moves into the schema)
- Create: `app/services/support_assist/kb.md` (the 17 spike `kb.json` entries as `- ` bullets, without `src`, plus one entry: «Анонимный платёж Tribute в базе бота не виден — при жалобе на незачисленный платёж владельцу проверить Tribute вручную»)
- Create: `app/services/support_assist/card.py`, `render.py`
- Test: `tests/services/support_assist/test_card.py`, `test_render.py`

**Interfaces — Produces:**
- `class CardFact(BaseModel)`: `icon: Literal['✅','⚠️','🔴','🚨','❓']`, `text: str`
- `class Card(BaseModel)`: `facts: list[CardFact]` (max 7), `cause: str`, `owner_action: str`, `ask_client: list[str]`, `draft: str`
- `@dataclass CardResult`: `card: Card | None`, `model: str | None`, `input_tokens`, `output_tokens`, `cache_read_tokens: int | None`, `cost_usd: Decimal | None`, `error: str | None`
- `async generate_card(client: anthropic.AsyncAnthropic, payload: dict, client_messages: list[str]) -> CardResult`
  - `thinking={'type':'adaptive'}`, `output_config.format` = JSON Schema of `Card`.
  - The system is two blocks, prompt.md + kb.md. The last one has `cache_control={'type':'ephemeral'}`.
  - The user message holds the client messages, then the payload as JSON.
  - Fallbacks beta `server-side-fallback-2026-07-01` with `fallbacks='default'`.
  - The timeout comes from settings.
  - Validate the parsed JSON with `Card.model_validate`.
  - Any exception, refusal or invalid JSON → `CardResult(card=None, error=...)`.
  - Cost is computed from usage at Opus 5.5 prices per 1M tokens: in $4, out $20, cache read $0.20. Prices are constants in the module.
- `render_card(card: Card | None, flags: list[Flag], ticket_id: int) -> str`. HTML parse mode, html-escape all text.
  - Sections: 🔎 Разбор / 💡 Вероятная причина / 🛠 Действие владельца / ❓ Спросить у клиента / ✏️ Черновик.
  - When `card is None`: facts from flags (high→🔴, mid→⚠️, low/info→✅), the header line «разбор без модели», and no draft section.
  - Output ≤ 4096 chars; the draft is truncated first, then facts.
- `build_card_keyboard(card_id: int, show_send: bool) -> InlineKeyboardMarkup`. Callback strings `sa:send:<id>`, `sa:edit:<id>`, `sa:skip:<id>`. Send and edit only when show_send.

- [ ] **Step 1:** Write the tests, with a mocked `AsyncAnthropic`:
  - Valid JSON → Card parsed and cost > 0.
  - API raises → card None plus error.
  - Invalid JSON → card None.
  - The request kwargs contain model `claude-opus-5-5`, `thinking.type == 'adaptive'`, cache_control on the last system block, and no forbidden key in the user content (reuse the Task 4 walker).

  Render tests:
  - The fallback card contains «разбор без модели» and no «✏️».
  - A 10 000-char draft → len ≤ 4096.
  - `<script>` in the client text is escaped.
  - The keyboard with show_send=False has only `sa:skip:`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): Claude card with structured output and fallback render`.

### Task 7: Pipeline and hooks

**Files:**
- Create: `app/services/support_assist/pipeline.py`
- Modify:
  - `app/handlers/tickets.py` (after admin notify at :331 in `handle_ticket_message_input`; after notify at :872 in `handle_ticket_reply`)
  - `app/cabinet/routes/tickets.py` (after :249 in `create_ticket`; after :409 in `add_ticket_message`)
  - `app/cabinet/routes/support_ws.py` (`_handle_ticket_reply` :862)
  - the SLA query in `app/services/monitoring_service.py` (only if the test below shows the auto-ask message suppresses SLA)
- Test: `tests/services/support_assist/test_pipeline.py`

**Interfaces — Consumes:** Tasks 2–6.
**Produces:**
- `schedule_assist(bot: Bot, ticket_id: int, trigger: Literal['new_ticket','new_message']) -> None`. Returns immediately when `SUPPORT_ASSIST_ENABLED` is False. Otherwise `asyncio.create_task(_run_safe(...))` holding a strong ref in a module-level set (pattern: the `create_task` uses in `app/handlers/tickets.py:103`). `_run_safe` opens its own DB session (as background services do) and catches everything with `logger.exception`.
- `async run_assist(db, bot, api, client, ticket_id, trigger, now) -> SupportAssistCard | None`:
  1. Throttle: if `last_card_for_ticket` is newer than RECOMPUTE minutes → None.
  2. Collect the snapshot and compute flags.
  3. Auto-ask: if `trigger=='new_ticket'` and AUTO_ASK_ENABLED and `is_bare` and `pick_questions` is non-empty, then
     - `mark_auto_ask_sent` first; continue only on True;
     - add a TicketMessage with `is_from_admin=True, is_system=True`;
     - leave the ticket status unchanged (stays OPEN);
     - deliver the text to the client through `notify_user_about_ticket_reply` (`app/handlers/admin/tickets.py:1071`).
  4. `generate_card`.
  5. Render and send to `ADMIN_NOTIFICATIONS_CHAT_ID`, thread `ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID`, as a standalone message (the existing notification has no stored message_id, `admin_notification_service.py:1610` returns bool). Prefix with «Тикет #<id>».
  6. Store the card row with tokens, cost and `admin_message_id`. Status `shown`, or `fallback` when card is None.

- [ ] **Step 1:** Write the tests with mocked bot/api/client and DB stubs:
  - Flag off → `schedule_assist` creates no task.
  - Happy path → one bot.send_message to the topic and a stored card with status shown.
  - Client exception → status fallback and a message still sent.
  - Snapshot source failure → card still sent.
  - Second `run_assist` within 10 min → None and no second send.
  - Two concurrent `run_assist` for a bare new ticket → `notify_user_about_ticket_reply` called once.
  - Auto-ask off → no client message.
  - Our-fault flag → no client message.
  - `test_sla_still_reminds_after_auto_ask`: find the SLA selection in `monitoring_service.py`; a ticket whose only admin message is `is_system=True` must still be selected. Fix the query (ignore `is_system` messages) if it fails.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the pipeline, then add a one-line `schedule_assist(...)` call at each hook point after the existing notify call. The bot handlers have `message.bot`; the cabinet routes use the bot instance the routes already use for notifications.
- [ ] **Step 4:** Run the support_assist tests plus the existing ticket tests (`uv run pytest tests -k "ticket or support" -q`) → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): background pipeline hooked into ticket creation and replies`.

### Task 8: Owner buttons

**Files:**
- Create: `app/handlers/admin/support_assist.py`
- Modify: the router registration where `app/handlers/admin/tickets.py` handlers are registered (follow the existing pattern)
- Test: `tests/handlers/test_support_assist_buttons.py`

**Interfaces — Produces:** callback handlers for `sa:send:`, `sa:edit:` (FSM state `SupportAssistStates.waiting_edit`, the next text message from that admin), `sa:skip:`.

- Access: `settings.is_admin(user_id)` (`app/config.py:1961`) or `SupportSettingsService.is_moderator` (`support_settings_service.py:182`). Otherwise answer the callback «нет доступа».
- Send and edit:
  - `claim_card_decision(... to_status='sent'|'edited')`; on False, answer «уже обработано» and send nothing.
  - Then the exact manual-reply sequence from `handle_admin_ticket_reply` (`app/handlers/admin/tickets.py:356`: add the message with is_from_admin=True, status handling as there, `notify_user_about_ticket_reply`).
  - Store `final_text`, `decided_by`, `decided_at`.
  - Edit the card message: remove the keyboard, append «✅ отправлено».
- Skip: `claim_card_decision(to_status='dismissed')`; remove the keyboard.
- Send/edit callbacks are refused when `SUPPORT_ASSIST_SEND_BUTTONS_ENABLED` is False, even if an old keyboard is present.

- [ ] **Step 1:** Write the tests:
  - A non-admin is refused.
  - A double send → the reply path is called once.
  - The flag off → send refused.
  - Edit → the owner's text is sent, not the draft.
  - Skip → status dismissed, nothing sent.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Extract the shared part of `handle_admin_ticket_reply` into a function if needed, so both paths call one function. Do not copy it.
- [ ] **Step 4:** Run the new tests plus `uv run pytest tests -k ticket -q` → PASS.
- [ ] **Step 5:** Commit `feat(support-assist): owner send/edit/dismiss buttons`.

### Task 9: Manual eval script and docs

**Files:**
- Create: `scripts/support_assist_eval.py`
- Create: `docs/support-assist.md` (what it does, the settings, the 3 phases, cost, how to run the eval)

**Interfaces — Consumes:** `build_payload`, `generate_card`, `fixtures.py` scenarios.

- [ ] **Step 1:** The script runs `generate_card` on each S01–S15 fixture plus optional `--cases DIR` (markdown cases in the spike's format). It writes the cards and `usage` to `--out DIR`, plus a CSV of tokens and cost. It refuses to run without `--yes-spend` (real money).
- [ ] **Step 2:** `uv run python scripts/support_assist_eval.py --help` exits 0. Without `--yes-spend`, the script exits non-zero with a message.
- [ ] **Step 3:** Write the doc.
- [ ] **Step 4:** Run the full test suite `uv run pytest -q` → no new failures compared to the base commit (record base failures first: run the suite on `7922f85e` in a separate temporary worktree).
- [ ] **Step 5:** Commit `docs(support-assist): eval script and operator doc`.

---

## After the plan (not tasks)

- The owner approves the client question texts (Task 5 locales) and the KB (`kb.md`).
- One paid eval run on the 15 scenarios (≈ $0.50, estimate) with the owner's OK. It measures the real cost per ticket.
- Rollout of phase 1 (shadow: flags ENABLED=true, SEND_BUTTONS=false, AUTO_ASK=false) via bedolaga-prod-update, after the owner's «да», coordinated by «Пахан».
- Delete the local `support-spike-pg` container and the scratchpad copies with client data.
