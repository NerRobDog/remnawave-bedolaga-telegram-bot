<!-- GENERATED FILE: do not edit. Source: docs/support-kb/clients.yaml and docs/support-kb/source/*.json.
     Regenerate: uv run python scripts/build_support_kb.py -->
# Знания о клиентских приложениях (проверенные)
Правило: называть экран, кнопку или настройку приложения можно только если они перечислены здесь для этого приложения. Язык названий — язык интерфейса клиента: RU для русскоязычных, иначе EN. EN=RU: английское название слева, русское справа. «*» = пункт виден только при активном подключении. «вживую» = прочитано в запущенном приложении; «по исходникам / по документации» = сверено с кодом или докой выпуска, не открывалось на устройстве, версия клиента может отличаться. Приложения и платформы, которых здесь нет, не проверены: давать общие шаги без названий кнопок и пометить для владельца.

## Важно (проверено вживую)
- Режим «Глобально» (Clash Mi) / «Глобальный» (FlClash(X)) — способ выбрать конкретный сервер: весь трафик, включая российские сайты, идёт через VPN на сервер, выбранный в Прокси → GLOBAL. В GLOBAL только серверы подписки, по умолчанию первый. Основной режим остаётся «Правила» / «По правилам»: там российские сайты идут напрямую. Если в GLOBAL выбран DIRECT или видны группы вместо серверов — подписка старая, нужно обновить подписку.
- Группа «Остальные сайты» предлагает выбрать не сервер, а пул: «Зарубежные серверы (баланс)» или «Обход блокировок РФ (авто)»; в FlClashX есть ещё «Напрямую» (DIRECT, без VPN, не советовать). Конкретную страну или сервер в ней не выбрать — для этого режим «Глобально».
- Каждое приложение на одном телефоне считается отдельным устройством. При превышении лимита приложение получает подписку с сервером-заглушкой «Слишком дофига устройств подключено к подписке»; Happ ещё пишет «Достигнут лимит подключённых устройств в подписке». Решение: удалить лишние устройства (мини-апп или кабинет → Подписка) и в приложении обновить подписку.

## Проверено вживую
### Clash Mi macos 1.0.22: проверено вживую 2026-11-07
- [отключено] main: Disconnected (toggle on the right); Rule; Global; Direct; My Profiles (+ and >); App Settings; Core Settings; Core Log; Backup and Sync; Help; About
- [отключено] my_profiles: cloud with arrow (top right): update all profiles; + (top right): add profile; ⋮ next to a profile: profile menu
- [отключено] add_profile_sheet: Get Profile / Buy Profile; Add Profile Link; Import From Clipboard; Scan QR Code; Import Profile File
- [отключено] profile_menu: View; Update; Copy Link; QR Code; Profile Edit; Remove
- [отключено] add_subscription: My Profiles → "+" → "Import From Clipboard" (link copied first) or "Add Profile Link"
- [отключено] update_subscription: My Profiles → cloud-with-arrow icon (all), or "⋮" → "Update" (one profile)
- [отключено] share_subscription: My Profiles → "⋮" → "QR Code" or "Copy Link"
- [отключено] mode: main screen: "Rule" (default, RU sites direct). "Global" sends everything, RU sites too, through VPN to the server picked in Proxies → GLOBAL (servers only, first by default); use it to pick a specific server. "Direct" is no VPN.
- [подключено] main: Подключено (тумблер справа, зелёный); счётчики трафика и скорости под статусом; Правила; Глобально; Напрямую; Профили (+ и >); Прокси (текущая группа, например «Остальные сайты -> Зарубежные серверы (баланс)», и >); Панель; Конфигурация времени в…; Проверка сети
- [подключено] proxies: список групп: Остальные сайты, TikTok (чистые форин), TikTok, Недоступные из РФ, Торрент-трекеры, YouTube, Telegram, Instagram…; у группы тип (Selector/URLTest/Fallback), задержка и выбранный сервер справа; сортировка и молния (тест задержки) вверху
- [подключено] network_check: Домен; Проверить; 1. DNS-запрос; 2. HTTP (через TUN, сначала включите TUN); 3. HTTP (через прокси, порт: 7890); 4. Таблица маршрутов
- [подключено] choose_server: главный экран → Прокси → группа «Остальные сайты» → выбрать «Зарубежные серверы (баланс)» или «Обход блокировок РФ (авто)» (если есть пункт «Напрямую» — это без VPN, не советовать). Отдельных серверов и стран в этой группе нет
- [подключено] self_check: главный экран → Проверка сети → домен → Проверить (только при подключении)
- Русский интерфейс: App Settings → Language → Русский (в русском: Настройки приложения → Язык). Не переведено: заголовок экрана настроек приложения остаётся «App Settings»; Pprof, Geo RuleSet, TUN, UserAgent не переведены.
- EN=RU: Disconnected=Отключено; Connected=Подключено; Rule=Правила; Global=Глобально; Direct=Напрямую; My Profiles=Профили; App Settings=Настройки приложения; Core Settings=Настройки ядра; Core Log=Журнал ядра; Get Profile / Buy Profile=Получить профиль / Купить профиль; Add Profile Link=Добавление подписки; Import From Clipboard=Импорт из буфера обмена; Scan QR Code=Сканировать QR-код; Import Profile File=Импорт файла конфигурации; View=Просмотр; Update=Обновить; Copy Link=Скопировать ссылку; QR Code=QR-код; Profile Edit=Редактирование профилей; Remove=Удалить; Language=Язык; Auto Set System Proxy when Connected=Установить системный прокси после подключения; Auto Connection after Launch=Автоматическое подключение после запуска; Overwrite=Переопределить; TUN → Enable=Включить; Proxies=Прокси; Network check=Проверка сети
### Clash Mi android 1.0.31: проверено вживую 2026-11-07
Примечание: same screens as macOS; the settings screen title is translated here («Настройки приложения»); the add sheet has an extra item «Вход»
- [отключено] main: Отключено (тумблер справа); Правила; Глобально; Напрямую; Профили (+ и >); Настройки приложения; Настройки ядра; Журнал ядра; Резервное копирование и синхронизация; Помощь; О приложении
- [отключено] my_profiles: облако со стрелкой вверху справа: обновить все профили; + вверху справа: добавить; ⋮ справа на карточке профиля: меню профиля
- [отключено] add_profile_sheet: Получить профиль / Купить профиль; Добавление подписки; Импорт из буфера обмена; Сканировать QR-код; Импорт файла конфигурации; Вход
- [отключено] profile_menu: Просмотр; Обновить; Скопировать ссылку; QR-код; Редактирование профилей; Удалить
- [отключено] connect_ru: главный экран → тумблер справа от «Отключено» (нажимать сам тумблер)
- [отключено] add_subscription_ru: Профили → «+» → «Импорт из буфера обмена» (ссылка скопирована) или «Добавление подписки»
- [отключено] update_subscription_ru: Профили → облако со стрелкой (все) или «⋮» → «Обновить»
- [отключено] share_subscription_ru: Профили → «⋮» → «QR-код» или «Скопировать ссылку»
- [подключено] main: Подключено (тумблер зелёный); счётчики трафика и скорости; Правила; Глобально; Напрямую; Профили; Прокси (текущая группа и >); Панель; Конфигурация времени в… (подпись обрезана на экране); Проверка сети
- [подключено] proxies: вверху: «Отсортировать», «Обнаружение задержки» (молния); список групп с типом (Selector/Fallback/URLTest) и выбранным вариантом справа; нажатие на группу открывает список вариантов снизу
- [подключено] network_check: поле домена (example.com); Проверить; 1. DNS-запрос; 2. HTTP (через TUN, сначала включите TUN); 3. HTTP (через прокси, порт: 7890); 4. Таблица маршрутов
- [подключено] choose_route_ru: Прокси → «Остальные сайты» → «Зарубежные серверы (баланс)» или «Обход блокировок РФ (авто)»
### FlClashX android 0.4.2: проверено вживую 2026-11-07
Примечание: dark theme; bottom tabs; shows the panel announcement and the subscription card on the main screen. On first launch after install Android asks «Прекратить оптимизацию расхода заряда?» (Запретить / Разрешить)
- [отключено] main: Главная (вкладка); объявление сервиса сверху; карточка подписки: имя, трафик, «Истекает …», значки ? и ↻ (обновить); По правилам; Глобальный; Прямой; большая кнопка ▶ внизу (подключить); вкладки снизу: Главная, Прокси, Профили, Настройки
- [отключено] my_profiles: вверху: «Обновление» (↻), «Скрипт», «Сортировка профилей»; карточки профилей с «Истекает …» и «Обновлено …»; ⋮ на карточке: меню профиля; + внизу справа: «Добавить профиль»
- [отключено] add_profile_sheet: QR-код; Файл; URL
- [отключено] profile_menu: Редактировать; Обновление; Переопределение; Экспорт файла; Удалить
- [отключено] add_subscription_ru: Профили → «+» → «URL» (вставить ссылку) или «QR-код»
- [отключено] update_subscription_ru: Главная → ↻ на карточке подписки, или Профили → «Обновление» (↻ вверху), или «⋮» на карточке → «Обновление» (пункты видны, нажатие не проверялось)
- [отключено] language_ru: Настройки → Язык
- [подключено] main: кнопка внизу зелёная: ⏸ и таймер подключения (нажать — отключить)
- [подключено] proxies: вверху: «Переключить режим», «Проверить задержку», «Развернуть всё», ⋮, поиск; группы раскрываются: «Остальные сайты» → «Зарубежные серверы (баланс)», «Обход блокировок РФ (авто)», «Напрямую» (DIRECT, без VPN)
- [подключено] choose_route_ru: Прокси → раскрыть «Остальные сайты» → «Зарубежные серверы (баланс)» или «Обход блокировок РФ (авто)»
### Happ android 4.9.0: проверено вживую 2026-11-07
Примечание: on launch offers an update (4.10.1: «Позже» / «Обновить»). Connected state not checked: the test subscription returned the device-limit stub
- [отключено] main: шестерёнка вверху слева: Настройки; + вверху справа: добавить; большая круглая кнопка питания (подключить); блок подписки: имя, «Автообновление», значки обновления (круговые стрелки), пинга и ⋯, трафик и «Истекает»; при превышении лимита устройств в подписке текст «Достигнут лимит подключённых устройств в подписке…» и сервер-заглушка «Слишком дофига устройств подключено к…» вместо серверов
- [отключено] add_menu: Добавить подписку; Вставить из буфера обмена; QR-код; Ручной ввод; Вставить JSON; Копировать JSON
- [отключено] add_subscription_ru: «+» → «Вставить из буфера обмена» (ссылка скопирована) или «Добавить подписку» (пункты меню видны, нажатие не проверялось)
- [отключено] update_subscription_ru: круговые стрелки в блоке подписки на главном экране (значок виден, нажатие не проверялось)
### FlClash macos 0.9.3: проверено вживую 2026-11-07
Примечание: FlClash upstream, not FlClashX from the subscription page; FlClashX is a fork, labels may differ
- [подключено] sidebar_ru: Панель; Прокси; Профили; Запросы; Инструменты
- [подключено] panel_ru: Статус: Работает; IP выхода и страна; Время работы; Организация; кнопка пауза (остановить); Отдача; Загрузка; Режим исходящего…: Правило / Глобальный / Прямой; Системный прокси (Порт 7890); TUN; Внутренний IP; Разрешить LAN; Профиль (обновить ↻, >); Состояние сети; Память
- [подключено] proxies_ru: вкладки групп сверху (Остальные сайты, TikTok…), карточки серверов с задержкой; поиск, молния, ⋮
- [отключено] panel_ru: Статус: Не запущено; большая кнопка ▶ (запустить); IP — реальный, без VPN
- add_subscription_ru: Профили → ⊕ (вверху справа) → Ссылка (или QR-код, Файл, Пользовательский)
- update_subscription_ru: Профили → ↻ вверху (все) или Панель → Профиль → ↻
- language_ru: Инструменты → Язык

## Сверено с исходниками / документацией (платформы не уточнены; * — только при подключении)
### Clash Mi (по исходникам, не вживую; v1.0.31.1703)
- добавить подписку: Add=Добавить
- подключить/отключить: Board=Панель*
- выбор сервера: Latency Checks=Обнаружение задержки*; Reorder=Отсортировать*
- системный прокси / TUN: TUN; Enable=Включить
### FlClash X (по исходникам, не вживую; v0.4.2)
- добавить подписку: Profiles=Профили; Add Profile=Добавить профиль
- режим: Switch mode=Переключить режим
- выбор сервера: Proxy group=Группа прокси; Test latency=Проверить задержку
- пути (EN | RU):
  - Connect (enable VPN): Dashboard > press the large play/pause button at the bottom (classic look) or the play button (New look). Desktop alternative: tray icon > Start | Главная > нажать большую кнопку play/pause внизу (классический вид) или кнопку play (Новый вид). На десктопе: значок в трее > Старт
  - Update all subscriptions: Profiles > Sync (circular-arrows icon in top bar, tooltip 'Sync') | Профили > значок обновления в верхней панели (подсказка 'Обновление')
  - Update one subscription: Profiles > three-dot menu on the profile card > Sync (URL profiles only); New look: Dashboard > Update | Профили > меню (три точки) на карточке профиля > Обновление; Новый вид: Главная > Обновить
  - Test latency: Proxies > Test latency (network_ping icon in the top bar); list view also has a per-group button | Прокси > Проверить задержку (значок в верхней панели)
  - VPN toggle (Android; replaces TUN): Settings > Basic configuration > Network > VPN (switch) ; or Dashboard card 'VPN' > VPN | Настройки > Конфигурация ядра > Сеть > VPN (переключатель); или карточка 'VPN' на Главной
  - Change language to Russian: Settings > Language > Russian (options: Default, English, Japanese, Russian, Simplified Chinese) | Настройки > Язык > Русский (варианты: По умолчанию, Английский, Японский, Русский, Упрощенный китайский)
### Koala Clash (по исходникам, не вживую; 1.5.0)
- добавить подписку: Import Remote Config=Импорт удалённой конфигурации; Import=Импорт
- подключить/отключить: TUN Mode=Режим TUN; Proxy Mode=Режим прокси
- режим: Rules=Правила
- выбор сервера: Proxy Groups=Группы прокси
- системный прокси / TUN: Proxy Mode=Режим прокси
- пути (EN | RU):
  - Connect / disconnect: Home > big power button (status shows Connected / Disconnected). What it turns on is set in Settings > Main Switch (TUN Mode or Proxy Mode) | Главная > большая кнопка питания (статус Подключено / Отключено). Что именно включается, задаётся в Настройки > Основной переключатель (Режим TUN или Режим прокси)
  - Connect / disconnect from tray: Tray icon > Enable / Disable | Значок в трее > Включить / Выключить
  - Update all subscriptions: Profile Management > refresh icon in the header (Update all profiles) | Профили > значок обновления в заголовке (Обновить все профили)
  - Update one subscription: Profile Management > refresh icon on the profile card (remote profiles only); also the refresh icon next to the profile name on Home | Профили > значок обновления на карточке профиля (только удалённые); также значок обновления рядом с названием профиля на Главной
### Flowvy (по документации/сборке, не вживую; 0.3.5)
- добавить подписку: Add=Добавить; Check=Проверить; Import=Импортировать
- обновить подписку: Update All=Обновить все
- подключить/отключить: Connect=Подключиться; Disconnect=Отключиться*; Connections=Соединения*
- выбор сервера: Auto select=Автовыбор; Test All=Пинг всех
- системный прокси / TUN: System Proxy=Системный прокси; TUN mode=Режим TUN
- пути (EN | RU):
  - Connect: Tray icon > Popup > Connect | Значок в трее > Popup > Подключиться
  - Switch mode: Popup > Rule / Global / Direct | Popup > По правилам / Глобальный / Прямой
  - Add subscription by link: Dashboard > Profiles > Add > paste link > Check > Import | Dashboard > Профили > Добавить > вставить ссылку > Проверить > Импортировать
  - Update subscriptions: Dashboard > Profiles > update button on card, or Update All | Dashboard > Профили > кнопка обновления на карточке, или Обновить все
  - Choose server and test latency: Popup > open group > click host; lightning icon or Test All | Popup > открыть группу > выбрать хост; значок молнии или Пинг всех
  - Toggle System Proxy: Settings > Connection > System Proxy | Настройки > Подключение > Системный прокси
  - Enable TUN: Settings > Connection > TUN mode > Enabled | Настройки > Подключение > Режим TUN > Включён
  - Change language: Settings > Appearance > Language | Настройки > Интерфейс > Язык
### Prizrak-Box (по исходникам, не вживую; v2.0.0-rc01)
- добавить подписку: Add=Добавить; Import from Clipboard=Импорт из буфера обмена; Import from file=Импорт из файла; Add Profile=Добавить профиль
- обновить подписку: Refresh profile=Обновить профиль; Refresh=Обновить
- выбор сервера: Latency Test=Тест задержки
- системный прокси / TUN: Tun; TUN Mode=Режим TUN; System Proxy Mode=Режим системного прокси
- пути (EN | RU):
  - Enable the system proxy: Settings (sidebar) > App Settings tab (gear) > turn on 'System Proxy Mode', then turn on sidebar > Proxy | Настройки > вкладка 'Настройки приложения' (шестерёнка) > включить 'Режим системного прокси', затем включить в боковом меню 'Прокси'
  - Test latency: Proxies > Latency Test (header icon) for all groups, or Test Group Latency in a group | Прокси > Тест задержки (иконка в шапке) для всех групп, либо Тест задержки группы в нужной группе
  - Update one subscription: Home > Refresh profile (toolbar above the active profile), or Profiles > Refresh (circular arrow on the profile card) | Главная > Обновить профиль (панель над активным профилем), либо Профили > Обновить (круглая стрелка на карточке профиля)
  - Update all subscriptions at once: No such button found. Only the per-profile refresh and the automatic 'Update Interval (hour)' in the profile edit dialog | Кнопки 'обновить все' нет. Есть обновление по одному профилю и автообновление по полю 'Интервал обновления (часы)' в окне редактирования профиля
### Rabbit Hole (по документации/сборке, не вживую; none (closed source); App Store version 1.7.1)
- добавить подписку: Configs=Конфиги; Add config=Добавить конфиг; Paste via clipboard=Вставка из буфера; Add via URL=Добавить по URL; Scan QR=Сканировать QR; Upload YAML=Загрузить YAML; Add sub link=Добавить ссылку на подписку
- обновить подписку: Refresh=Обновить
- подключить/отключить: Connect=Подключиться; Disconnect=Отключиться*; Toggle VPN=Переключить VPN [macOS only]
- режим: Routing=Маршрутизация; Default=По умолчанию; Off=Выкл; Excluded=Исключено; Included=Включено
- выбор сервера: Sorting & Filters=Сортировка и фильтры; Ping=Пинг
- пути (EN | RU):
  - Add a subscription by provider deep link: Open rabbithole://add/<URL>, confirm adding, then Connect | Открыть ссылку rabbithole://add/<ссылка>, подтвердить добавление, затем Подключиться
  - Connect / disconnect: Configs > select config (and proxy if needed) > Connect / Disconnect | Конфиги > выбрать конфигурацию (и прокси при необходимости) > Подключиться / Отключиться
  - Change routing mode: Settings > Routing > Default | Off | Excluded | Included | Настройки > Маршрутизация > По умолчанию | Выкл | Исключено | Включено
### Happ (по документации/сборке, не вживую; android 4.10.1; desktop 4.8.1; ios: no GitHub releases)
Подтверждённых названий кнопок нет (ни подключения, ни режима, ни обновления подписки). Давать общие шаги без названий кнопок и пометить для владельца.
