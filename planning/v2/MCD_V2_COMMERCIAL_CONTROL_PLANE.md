# MCD2 — коммерческий control plane без входящего SSH

Дата решения: 2026-10-21. Инициатор: Александр.
Статус: утверждённая архитектурная граница; реализация ведётся по владельцам
компонентов и не запускается самим этим документом.

## Утверждённая схема

Коммерческий клиент не подключается напрямую к MCC и не открывает входящий SSH
для штатного управления. MCD на сервере клиента сам устанавливает исходящее
защищённое соединение с SalesSnap Portal через HTTPS/WSS на 443, получает
уведомления или забирает задания и отправляет результаты/receipts. В
коммерческом режиме MCD обращается только к Portal и не знает адресов или
credentials MCC. Portal сервер-сервером получает из MCC exact immutable
artifacts и передаёт их MCD.

SalesSnap Portal является обязательной коммерческой прослойкой:

- аутентифицирует клиента и его установки;
- хранит подписку, entitlement и разрешённые клиенту действия;
- принимает действия пользователя и передаёт MCD подписанные, ограниченные по
  установке задания;
- выдаёт короткоживущую scoped-авторизацию на получение разрешённых артефактов;
- принимает статусы, progress, результаты и аудит MCD.

Portal не заменяет MCC и не становится вторым publisher. MCC остаётся
единственным корпоративным хранилищем и источником
опубликованных SalesSnap-плагинов, fixes, patches, operation packages,
матриц применимости и immutable revisions. Portal хранит клиентскую проекцию
metadata и content-addressed cache уже полученных immutable revisions. При
недоступности MCC Portal продолжает работу на последнем согласованном срезе;
после восстановления связи обновляет cache. Browser, клиентский оператор и
коммерческий MCD не получают MCC credentials и не обращаются к MCC API.

Рекомендуемый поток:

1. MCD регистрируется в Portal с отдельной идентичностью установки.
2. MCD поддерживает outbound long-poll/WebSocket и периодический HTTPS pull.
3. Portal проверяет membership, подписку, capability и целевую установку.
4. Portal создаёт подписанное immutable-задание на точный MCC digest/revision.
5. Portal выдаёт MCD точный cached/proxied artifact; MCD проверяет
   digest/signature/applicability и исполняет его локальным общим executor.
6. MCD возвращает Portal receipts; Portal хранит клиентский audit/projection.

При потере связи MCD продолжает разрешённую локальную работу и повторяет
подключение. Команда не считается исполненной без terminal receipt. SSH/VPN
может существовать как отдельно согласованный аварийный канал, но не является
условием коммерческого сервиса.

## Границы владельцев

- **MCD2:** outbound agent protocol, local policy enforcement, signed-job и
  artifact verification, idempotency, retries, receipts, offline behavior.
- **SalesSnap Portal:** клиентская identity/membership/subscription, UI/API,
  broker очереди команд, scoped grants, клиентский audit и projections.
- **MCC:** один каталог/publisher и artifact endpoint; immutable revisions,
  applicability metadata и проверяемая выдача по scoped grant. MCC не хранит
  коммерческую UI-логику и не обслуживает клиента напрямую.
- **Mautic RFP:** связанный потребитель общей основы и среда для требований к
  customer-managed deployment, tenant isolation, security/operations и
  совместной проверке контрактов. RFP-specific policy остаётся в MauticRFP и
  не переносится в MCD2/Portal/MCC как hardcode.

Все стороны используют один versioned job/package/receipt contract. Запрещены
копии каталога, отдельный Portal executor, второй формат пакета и прямой доступ
браузера к MCC или клиентскому хосту.

### Внешние сценарии и автоматический detector — решение 2026-10-25

Сценарии внешнего/self-host развёртывания и правила определения системы никогда
не встраиваются в код Portal, MCC или MCD. Они являются внешними
версионируемыми артефактами Portal: draft проходит проверку, после публикации
получает immutable revision, digest и подпись; изменение создаёт новую revision.
MCD получает артефакт только через Portal и повторно проверяет
revision/digest/signature. MCC не является владельцем, редактором, resolver или
publisher этих сценариев.

В коде компонентов допустимы только общий schema/parser, детерминированный
evaluator внешних detector rules, проверка подписи и applicability, безопасный
executor фиксированных allowlisted primitives, capability advertisement, API/UI
управления и audit. Запрещены runtime seed-массивы, OS-specific ветвление,
встроенные YAML/JSON/shell/Ansible сценарии и генерация начальных сценариев при
старте. Test fixtures не являются runtime-сценариями.

Первые внешние артефакты покрывают Ubuntu 22.04 и 24.04 на поддерживаемых
архитектурах. Superadmin управляет ими в разделе Portal «Сценарии» через
Portal-owned versioned registry и может добавить detector и сценарий для другой
системы без выпуска Portal. Опубликованная revision не редактируется на месте.
Сценарий может ссылаться на точные MCC plugins/images/compatibility artifacts,
но MCC остаётся владельцем только этих собственных артефактов и managed-cloud
deployment API, а не правил внешней установки.

Универсальность относится к self-host пути Portal, а не к внутреннему MCC
контуру. Managed `name.sales-snap.com` использует поддерживаемые внутренние
Ubuntu worker hosts и существующий поток Portal → shared allocator → MCC →
corporate MCD; Portal не показывает клиенту выбор worker host, а MCC не
превращается в универсальный установщик внешних систем. Self-host использует
отдельный поток Portal → commercial MCD и Portal-owned scenario registry.
Ubuntu 22.04/24.04 — только первые внешние data artifacts; engine не имеет
Ubuntu-ограничения и допускает новую систему после публикации её detector и
сценария и подтверждения требуемых MCD primitive capabilities, без выпуска
нового application-кода Portal.

Self-host preflight автоматически считывает фактические OS id/version,
architecture, package manager и требуемые capabilities. Deploy разрешается
только при единственном точном совпадении с опубликованным поддерживаемым
сценарием и успешных обязательных проверках. Отсутствие совпадения,
неоднозначность, неподдерживаемая система или capability mismatch дают понятную
типизированную ошибку и не выполняют никаких изменений. После установки MCD
повторно сверяет локальные факты с pinned detector/scenario до первой mutation.

## Принятые продуктовые решения 2026-10-24

- Корпоративный MCC↔MCD режим сохраняется без регрессий. Без внешнего
  enrollment MCD работает standalone. Portal credential включает commercial
  mode и имеет приоритет над оставшимся MCC credential; после удаления
  commercial credential MCD переходит в standalone, а не возобновляет MCC
  автоматически.
- Account activation code может зарегистрировать несколько установок в пределах
  лимита. Каждый MCD host получает собственную agent identity; каждый Mautic —
  отдельный installation ID. Лимит считается по активированным Mautic-инстансам,
  а пассивно обнаруженные инстансы слот не занимают.
- Новый MauticRFP deployment регистрируется как commercial installation. Старые
  RFP deployments в корпоративном режиме не мигрируются и завершают жизненный
  цикл по прежней схеме. После подтверждения email новый RFP-пользователь имеет
  общую Portal identity; повторный вход старого клиента ведёт в Portal без
  повторной регистрации и подтверждения.
- MCD на существующем сервере после enrollment остаётся в уже существующем
  passive mode: inventory и health разрешены, расписание и cron не
  перехватываются. Любая поддерживаемая операция может быть явно запущена из
  Portal или CLI. Передача конкретной scheduled-функции MCD выполняется отдельно.
- Portal bootstrap worker использует SSH password/key только один раз для
  проверки и установки MCD. Credential не пишется в обычную БД/логи и удаляется
  после enrollment; клиенту рекомендуется выдать временный credential и удалить
  его также на своей стороне.
- Для нового self-hosted сервера default: Ubuntu 24.04, Nginx, MariaDB 11.4,
  Redis и текущая рекомендованная MCC версия Mautic через Composer. Ubuntu 22.04
  разрешена с warning; amd64 и arm64 равноправны при наличии проверенного
  сценария. MySQL, Percona и external DB доступны как advanced choices. Почтовые
  credentials и backup self-hosted установки Portal не настраивает.
- На чистом сервере связь SSH target и DNS имени доказывается временным лёгким
  HTTP responder/token через указанное имя. При существующем web server
  используется временный exact route; IP не обязан совпадать из-за proxy/LB.
- Managed `name.sales-snap.com` использует существующий deployment workflow и
  defaults без нового advanced-раздела; действующий автоматический backup
  сохраняется.
- Mautic admin password генерируется и показывается один раз без хранения
  plaintext в Portal. Позднее reset выполняется существующей MCD-операцией.
- Один клиент имеет один живой изменяемый план. Изменения общего плана сразу
  действуют для связанных клиентов. Опция фиксации создаёт отдельный клиентский
  snapshot с тем же названием и отметкой `зафиксирован`; он больше не входит в
  множество общего плана. Неиспользуемый общий план можно удалить, snapshots
  сохраняются.
- Галка плагина в плане только делает совместимый MCC-плагин видимым в каталоге
  клиента. Установка остаётся решением пользователя. Каталог, entitlement и
  установка/обновление входят в первый этап; коммерческая регистрация и
  plugin-specific licensing — следующий этап.
- Billing и выпуск счетов входят в первый этап только для коммерческих
  установок; реальные платёжные шлюзы пока не подключаются. MauticRFP остаётся
  бесплатным trial и не создаёт подписку, счёт или платёжное событие.
- Для self-hosted установки действует месячный фикс EUR 60. Это начальные
  редактируемые данные тарифного плана, а не число в расчётном коде.
- Managed cloud `name.sales-snap.com` тарифицируется по размеру базы контактов и
  фактически успешным отправкам. Ставки, диапазоны, способ агрегации размера
  базы, период и даты действия принадлежат редактируемой rate-card Portal и не
  зашиваются в код.
- Portal собирает managed-cloud показатели почти в реальном времени. Поскольку
  входящих соединений к клиентскому серверу нет, MCD отправляет по своему
  исходящему защищённому каналу частые подписанные snapshots: raw current
  contact count и cumulative confirmed-successful sends. Очередь, попытка или
  ошибка отправки не считается успешной отправкой. Частота является внешней
  настройкой, а не константой исходника.
- Portal немедленно дедуплицирует snapshots, хранит их как неизменяемые
  доказательства usage и строит текущую проекцию. Расчёт счета выполняется из
  этого ledger; Portal и MCD не сбрасывают и не изменяют счётчики Mautic.
- Планы, entitlements, назначения, billing terms и rate cards управляются
  менеджером. MFA откладывается до периода после активной разработки.
- Portal поддерживает Google OpenID Connect как дополнительный способ входа,
  не удаляя существующие session/password/service-auth пути. Подтверждённый
  Google email домена `m1.rs` разрешён и при первом входе автоматически создаёт
  Portal user с ролью `manager`; `alexander.zlobin@m1.rs` получает роль
  `superadmin`. Для внешнего Google Workspace/Gmail пользователя Google-кнопка
  совмещает sign-up и sign-in: при первом входе Portal атомарно применяет
  серверные инварианты штатной регистрации, создаёт обычного пользователя и его
  Space из подтверждённых Google-данных и сразу открывает этот Space — без
  отдельной формы, повторного подтверждения email или второго входа. Служебные
  роли не выдаются. Уже зарегистрированный пользователь связывается по тому же
  подтверждённому email; его роль и memberships не меняются. Allowed domains,
  default roles и bootstrap superadmin identities являются внешней auth policy
  Portal, а не условиями в исходном коде.
- После первичной регистрации внешний пользователь входит той же Google-кнопкой
  в существующую Portal identity и свой Space. Стабильный provider subject
  возвращает тот же user/account/Space; повторный вход не создаёт новый
  onboarding, пользователя, проект, billing identity и не меняет роли.
- Google login проверяется сервером: Authorization Code + PKCE, state/nonce,
  issuer/audience/JWKS/expiry и `email_verified`; связь хранится по стабильному
  provider subject. Заблокированный пользователь не восстанавливается
  автосозданием, а повторный вход не понижает уже назначенную роль.
- Профильные данные берутся только штатным Google OIDC-механизмом со scopes
  `openid email profile`: из проверенного ID token и, если необходимо,
  стандартного UserInfo endpoint. People API, scraping, переданный браузером
  профиль и дополнительные Google permissions не используются; access/refresh
  tokens не становятся долговременными данными Portal.
- Для пользователя без Google действует полная Portal-регистрация с
  подтверждением email, по тем же продуктовым инвариантам, что проверенный путь
  MauticRFP, но как Portal-owned реализация без копирования RFP-кода. До
  одноразового подтверждения создаётся только pending identity: вход, Space и
  коммерческий доступ недоступны. После подтверждения атомарно активируются
  обычный пользователь, membership и его Space. Google- и password-identity с
  одинаковым нормализованным подтверждённым email сходятся в одного пользователя.
- Реальный Portal разворачивается на сервере действующего MauticRFP рядом с ним,
  с общей identity/admin основой и раздельными очередями workers. Старая RFP
  admin поверхность временно остаётся по старым URL. Текущий статический demo
  переносится с portal.sales-snap.com на demoportal.sales-snap.com и позднее
  удаляется. Перед развёртыванием обязателен backup всего RFP/Portal host.
- Production runtime реального Portal — изолированный Node.js service на этом
  сервере с PostgreSQL и локальным sendmail/Postfix adapter. Cloudflare остаётся
  DNS/reverse-proxy и не является Worker/D1 runtime Portal. Существующий
  Worker/reference слой может использоваться для тестов, но production readiness
  требует реальные DB transaction/migration checks и фактическую готовность MTA,
  а не наличие строк конфигурации.
- Первый общий план создаётся пустым для проверки manager UI и остаётся полностью
  редактируемым. План назначается клиенту/account; совместимые разрешённые
  плагины видны на его активированных установках.
