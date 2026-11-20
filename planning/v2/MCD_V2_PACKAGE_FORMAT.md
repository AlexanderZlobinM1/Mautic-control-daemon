# MCD — универсальный пакет операций, проект формата

Дата: 2026-10-01. Автор постановки: Александр.
Статус: проект спецификации для совместного review MCD/MCC, не реализованная
схема и не доказательство выпуска. Рабочее обозначение: `mcd-operation-package/v1`.
Точные имена согласовать с уже подготовленным producer/consumer контрактом,
а не создавать параллельный протокол.

[Решения по сценариям](MCD_V2_SCENARIO_PACKAGES.md) ·
[Общая карта](MCD_V2_CAPABILITY_BOUNDARIES.md) · [Обновления](MCD_V2_UPDATES.md)

Отдельная необязательная модель: [защищённая доставка материалов сервиса](MCD_V2_PRIVATE_PACKAGE_DELIVERY.md).
Не вводит подписочный gate для пользовательских/community пакетов.

## Назначение

Один пакет описывает операции над файлами инстанса и его БД, разрешённые
системные действия, расширение команд MCD, представление в любом совместимом
менеджере и передачу конкретных cron-операций планировщику MCD. Пакеты доступны
для создания, редактирования и обмена пользователями, без подписочного gate.
MCC — корпоративный оркестратор и единый SalesSnap-каталог/publisher. Отдельный
коммерческий Portal является посредником подписки и команд: клиентский MCD
подключается к нему исходящим соединением, но разрешённый пакет получает из MCC
по scoped grant. Portal не хранит копию каталога и не имеет отдельного
исполнителя. Предустановленный, локально импортированный и удалённый пакет имеют
одинаковый формат и executor. См.
[коммерческий control plane](MCD_V2_COMMERCIAL_CONTROL_PLANE.md).

Пакет — единица доставки возможностей. Операция — единица запуска. Run — один
запуск операции на конкретном инстансе. Пакет может содержать несколько операций,
но запуск одной не означает применение всех остальных или выдачу всех прав.

## Состав и редактирование

Предложение: `.mcdpkg` — ZIP с manifest и относительными ресурсами. Для ручной
разработки допускается распакованный каталог той же структуры. YAML удобен для
редактирования; издатель нормализует его в JSON по единой схеме. Никаких YAML
тегов исполнения, неявных выражений или подстановки shell. Обсудить обязательную
канонизацию JSON и ограничение YAML parser до принятия формата.

```text
example.mcdpkg/
  manifest.yaml             # идентичность и ссылки на разделы
  operations/repair.yaml    # условия, последовательность, результат
  commands.yaml             # новые именованные команды/параметры MCD
  ui.yaml                   # плитки, формы и локализация
  schedules.yaml            # операции планировщика и native cron mapping
  resources/
    fix.patch
    migration.sql
    template.conf
  extensions/               # необязательно; отдельный доверенный driver
  docs/                     # описание, ограничения, тестовые результаты
```

Разделы можно хранить в manifest для небольшого пакета. Они логически одни и
те же — не разные форматы. Большие неизменяемые артефакты (ZIP, vendor) можно
поставлять по digest-ссылке с origin, size и immutable locator, а не включать
в каждый архив. Никаких mutable URL как единственного идентификатора.

Manifest содержит `schema`, `id`, `revision`, описание, `requires` (версии
протоколов/драйверов), resource index с SHA-256 и список объявленных операций.
Digest вычисляется по каноническому manifest без собственного digest/signatures
и по хешам всех исполняемых разделов/ресурсов; для конкретного транспортного
архива можно дополнительно хранить archive SHA-256. Это разные значения.
Изменение любого шага, driver или файла создаёт новую revision/digest.
Подпись издателя возможна, но обязательная подпись SalesSnap не требуется.

Распаковка ограничивает размер/число файлов; запрещены абсолютные пути,
`..`, дубли конфликтующих имён и неожиданные ссылки на внешние пути.

## Локальный индекс и загрузка по требованию

Уточнение Александра, 2026-10-01: при установке пакет регистрируется в локальной
БД MCD. Агент знает его наличие и дерево возможностей, включая новые CLI
команды, не перечитывая содержимое всех пакетов. При запуске выбранной операции
обращается непосредственно к её пакету и исполняет описанные там действия.

БД хранит компактный discovery index, а не вторую копию последовательностей
и patch payload. Источник определения операции — файлы конкретной revision
пакета. Локальный реестр установленных пакетов не является копией управляемого
каталога MCC. Использовать существующее локальное хранилище MCD после source
review, не вводить отдельный daemon или БД ради этого индекса.

| Запись индекса | Назначение |
| --- | --- |
| Package ID/revision/digest, state | Найти установленную активную ревизию |
| Canonical local locator / immutable origin | Найти её содержимое без обхода каталогов |
| Namespaced capabilities/commands | Быстрый discovery CLI/API и разрешение имени |
| Input/help/UI metadata | Общий help, формы и плитки без загрузки executable payload |
| Operation → relative definition path | Загрузить только нужное описание при выполнении |
| Schedule/dependency registrations | Найти зарегистрированного владельца операции |

Предложенный lifecycle:

1. Install/import проверяет manifest и ресурсы, размещает immutable revision,
   компилирует compact index и выявляет конфликты имён.
2. Одна транзакция активирует согласованный набор регистраций; неполный импорт
   не становится доступной CLI командой. Файлы staged до активации; прерванная
   установка восстанавливается по install state, а не оставляет dangling index.
3. CLI/API discovery читает индекс. Он не делает network request или full file
   scan каждого пакета. Холодный запуск не импортирует все extension drivers.
4. Run разрешает имя → package/revision/definition path, загружает конкретную
   операцию и необходимые ресурсы, проверяет digest/права/реальный instance
   context, исполняет именно эту ревизию. Никакой повторной mutable resolution.
5. Update атомарно переключает активную revision; уже начатый run остаётся на
   старой. Remove деактивирует discovery/расписания, но не удаляет ресурсы
   выполняющегося run до завершения или согласованного stop.

При исполнении проверяются только требуемые ресурсы; не перечитывать всё
содержимое остальных пакетов. Для большого доставляемого артефакта hash можно
проверять поточно при получении/распаковке. Не считать mtime единственным
доказательством целостности executable ресурса.

Локальное редактирование требует validate/reindex с новой revision/digest;
старый индекс не должен разрешать исполнение изменённого файла. Пользователь
редактирует распакованный draft, затем публикует его локально тем же способом.
При потере/повреждении БД индекс восстанавливается из проверенных установленных
manifest по явной repair операции; это не сканирование на каждом CLI запросе.

В индексе можно кешировать статическое дерево возможностей, но не выдавать
устаревшие facts за актуальную применимость: версия инстанса, активный run,
DB состояние и latest-source evidence проверяются по фактическому контексту
перед выполнением. Наличие команды и возможность выполнить её сейчас — разные
вещи. Registry обслуживает MCD; пакет/web user не записывает себе grants или
подменённые paths в БД напрямую.

Acceptance: множество пакетов не вызывает O(суммарный объём всех payload)
для help/list/discovery; один run не загружает все drivers. Число пакетов и
время проверки фиксируются в benchmark evidence, без обещания неизвестного
порога производительности. Install одного пакета тоже не парсит заново все
payload остальных — зависимости/конфликты ищутся по индексированным метаданным.

## Разделение декларации, контекста и полномочий

В пакете — source/target predicates, нужные возможности и действия.
В run binding — фактически обнаруженные instance ID, filesystem roots,
runtime user, БД/префикс, source version, latest evidence и выбранная revision.
В локальной политике MCD — полномочия, предоставленные оператором. Пакет не
может сам выдать себе разрешение, подменить web user на root или выбрать
чужой инстанс. Один reusable пакет не содержит наблюдения конкретного клиента.

| Область | Контекст исполнения | Примеры действий |
| --- | --- | --- |
| `instance.files` | Обнаруженный пользователь web/runtime и разрешённые roots конкретного инстанса | Проверенный patch, замена/создание/удаление файла, шаблон, права внутри области |
| `instance.db` | Connection broker MCD для выбранного инстанса | Query, DML, DDL/индексы, SQL migration, проверка результата |
| `instance.process` | Web/runtime user, executable/argv и cwd из общего адаптера | Mautic console, rebuild assets, instance-specific worker |
| `host.packages` | Ограниченный привилегированный адаптер | Установка объявленных apt-пакетов |
| `host.services` | Ограниченный привилегированный адаптер | Reload/restart объявленных service units |

Перечень запрошенных прав показывается при подключении пакета. Оператор может
заранее разрешить доверенному источнику конкретные действия; это не требует
подтверждения каждого cron run. Новая revision не расширяет ранее выданные права
автоматически. Не смешивать это с платным entitlement или подпиской.

## 1. Файлы инстанса

Базовые drivers: `files.patch`, `files.write`, `files.template`, `files.remove`,
`files.copy`, `files.permissions`, `archive.stage`, `artifact.switch`.
Имена примитивов здесь предложены, их реализация ещё не подтверждена.

Пути заданы относительно named roots (`application`, `project`, `media`,
`plugins` и т. п.), которые MCD определяет для ZIP/Composer из реального
контекста. Не предполагать, что project root равен document root. Правило
сохранения config/media/uploads объявляется в операции.

Предикаты patch поддерживают before/after digest или сигнатуру структуры;
повторный запуск различает already-applied и incompatible. Замены атомарны
там, где это возможно; права и runtime ownership сохраняются. Проверять
разрешённую область после разрешения symlink и непосредственно при записи,
не только строковый prefix. Пользователь может явно зарегистрировать внешний
instance-owned каталог; это не открывает всё дерево хоста.

Произвольный доверенный filesystem driver может выполнять сложную трансформацию,
но остаётся в области инстанса и не импортируется внутрь root-процесса агента.
Обещание «любые патчи» относится к выразительности driver-интерфейса, а не к
праву менять чужие/system файлы.

## 2. База выбранного инстанса

Шаг указывает `connection: instance` и resource SQL или typed query. Credentials
не входят в пакет, его параметры, argv, журнал или result. Используется
существующий способ MCD получить подключение для конкретного инстанса через
единый connection broker. Не требуется отдельный логин в каждом сценарии.

Drivers: `db.query`, `db.execute`, `db.migrate`, `db.ensure_index`, проверка
postcondition. Параметры связываются драйвером; table prefix и identifiers
передаются как типизированные идентификаторы, а не свободная shell-строка.
Большие операции могут быть порционными, с declared lock/timeout/expected scope.

Важно: выбор default database у системного SQL-пользователя НЕ ограничивает
доступ к другим БД. Connection broker обязан обеспечить реальные права только
на instance database (существующая scoped учётная запись или временный scoped
исполнитель). Пакету не передаётся системное подключение с глобальными правами.
Не считать regex на тексте SQL защитой от чужой БД, server-admin commands,
вывода в файл или привилегированных DEFINER объектов.

Если несколько инстансов разделяют одну БД по table prefix, database-level
grant недостаточен. Требуется изоляция разрешённых объектов/таблиц и новых
имён либо специализированные scoped adapters. До её реализации произвольный
SQL для такого случая недоступен; нельзя обещать «только этот инстанс» без
реальной границы. Это техническая граница, не коммерческое ограничение.

DDL может не откатываться транзакцией: backup/recovery и проверка before/after
указываются явно. Не объявлять любую SQL-операцию автоматически обратимой.
Произвольные изменения users/grants/server конфигурации не относятся к
`instance.db`; они требуют отдельного административного механизма.

## 3. Системные действия

Пакет запрашивает named operation с параметрами, например
`host.packages.ensure(names=[...])` или `host.service.reload(unit=...)`.
Не принимает `root_shell`, `sudo_command`, `sh -c` или произвольную строку
системной команды из UI/manager. Привилегированный локальный helper имеет
узкий protocol и проверяет предоставленные оператором grants.

Для apt: без добавления произвольных репозиториев/ключей, изменения package
sources и широкого upgrade как неявных побочных действий. В manifest указан
набор/версии пакетов; helper использует контролируемый argv, lock package
manager и показывает proposed changes. Установка пакета сама выполняет
его системные install scripts — доверие к репозиторию действительно необходимо.

Для services: разрешённые unit names/patterns задаёт локальный администратор;
пакет не выбирает любой service хоста. Учитывать общий service нескольких
инстансов, последовательность restart/reload, health и журнал результата.
Не разрешать root restart собственного агента или сети лишь потому, что
это указано в downloaded package.

Администратор хоста и так может менять систему, но импорт пользователем пакета
не равен разрешению будущему удалённому источнику менять root-окружение.
Поэтому admin grant — при подключении/расширении прав пакета, затем обычное
выполнение в согласованной области. Произвольный root-driver, если когда-либо
понадобится, — отдельно установленное системное расширение администратора,
а не автоматически исполняемый файл внутри обычного пользовательского пакета.

## 4. Расширение команд MCD

Два уровня без изменения основного агента при каждом новом сценарии:

1. Декларативная команда: namespaced имя, help, typed параметры и ссылка на
   operation/composition уже установленных drivers. Например,
   `mcd-cli operation run acme.points.recalculate --instance ...`.
   Это расширяет список поддерживаемых операций, не создавая новую программу.
2. Расширяемый driver protocol: пакет может содержать driver для действительно
   нового действия. MCD запускает его отдельным worker-процессом под runtime
   user с versioned request/result protocol и ограниченными grants. Не import
   произвольного Python внутрь root-демона. OS isolation и ресурсные ограничения
   должны быть реализованы и доказаны, иначе extension пока недоступен.

Database/system requests такого worker идут через broker/helper; root/SQL
credentials не передаются. Если нужен новый привилегированный driver, его
устанавливает администратор отдельно. Если нужен новый protocol/runtime,
первичная реализация и выпуск MCD всё равно требуются — пакет не может
расширить возможности уже выпущенного несовместимого ядра магически.

Command registry общий для CLI/API/менеджеров; namespace conflicts явные,
пакет не переопределяет встроенные команды или команды другого издателя
молча. Install/remove активирует/деактивирует именно его регистрацию;
не удаляет уже выполняющийся run без согласованной stop policy.

## 5. Плитки менеджера

Общий `ui` descriptor: локализуемые title/help, category/icon из допустимого
набора, visible/applicable predicates по типизированным facts, форма параметров,
action → operation ID, progress/result view. Нет произвольных HTML/JS,
удалённого eval и секретов в UI. Пакет не поставляет свой root endpoint.

Одинаковый API позволяет MCC и отдельному коммерческому порталу отрисовать
описанную возможность; локальный CLI обнаруживает её без обязательного UI.
Не новая UI-ветка для каждого плагина. Для migration сохраняются latest source
+ доступный подходящий сценарий; это не global latest requirement для любого
патча, SQL запроса или plugin scheduled operation.

## 6. Планировщик и native cron

`schedules` ссылается на operation ID, описывает интервал/cron expression/timezone,
locks, timeout, overlap/misfire policy и disable behavior. Пакет не создаёт
самостоятельный скрытый daemon или competing cron. Конкретные scheduler
значения лежат в данных и сохраняют существующие пользовательские настройки.

`native_equivalents` содержит нормализованный executable, argv, cwd/root и
runtime user для конкретной операции. Не удалять по широкому regex все
`mautic:*`, все строки www-data или чужие crontabs. Если matching ambiguous,
оставить native owner и показать точную неоднозначность, не double-run.

Передача ownership выполняется управляемо: проверить доступность handler и
первый успешный проверочный run (если безопасен) или допустимый dry-run; под
instance+operation lock сохранить exact native entries, дождаться окончания
старого запуска, отключить только совпавшие строки и включить MCD owner.
Журнал handoff хранит состояние стадий для восстановления после сбоя.
MCD не запускает scheduled operation, пока ownership не подтверждено.

При удалении/отключении пакета восстановить original native entries без дублей
только согласно declared policy и если команда всё ещё существует. Не выдумывать
cron для удалённого плагина. Смена revision сохраняет effective settings и не
перезапускает завершённые операции. Health failure не включает fallback cron
автоматически, пока не определена безопасная ownership policy.

Плагин может поставляться вместе с operation package либо ссылаться на его
exact revision/digest. Установка плагина и активация пакета — связанные, но
отдельно отслеживаемые этапы. Частичная установка не считается success; при
ошибке нельзя выключить работающий native cron и оставить только неработающий
MCD handler. Уже существующие plugin operations должны быть отображены в этот
формат с сохранением настроек/команд/результатов, а не продублированы.

## Дополнения, необходимые для рабочего механизма

1. **Параметры и секреты.** Typed input schema, defaults, required/range/enum,
   одинаковая валидация CLI/API/UI; параметры не становятся shell fragments.
   Для необходимых секретов — только refs к существующему хранилищу MCD,
   никогда значения в архиве, форме результата или обычном журнале.
2. **Установка, обновление, удаление.** Validate → stage → activate регистрации
   команды/UI/scheduler с журналом результата; partial failure не оставляет
   полуподключённую возможность. Удаление регистрации не означает удаление
   клиентских данных. Предыдущая revision сохраняется для операционного recovery.
3. **Зависимости и конфликты.** Exact/range compatibility package/driver/plugin,
   namespaced operation IDs, запрет циклов и тихого overwrite. Не менять
   dependency/runtime из неизвестного upstream во время customer run.
4. **Повторный запуск и обрыв.** Step ID, declared idempotency, наблюдаемый
   postcondition и durable checkpoint. После рестарта сначала установить факт,
   был ли шаг применён; не повторять destructive SQL/install/switch вслепую.
   Перед продолжением подтвердить тот же инстанс и immutable package revision.
5. **Параллельность и отмена.** Locks по instance/operation/resource, более
   широкий host lock для apt/shared service. Timeout и безопасные точки stop;
   request cancel не означает kill -9 во время необратимой операции. Новая
   заявка не снимает lock с реально работающего процесса; abandoned state
   определяется по фактам процесса и checkpoints, а не одному статусу очереди.
6. **Результат и наблюдение.** Общий typed result: success/failed/cancelled/
   needs_attention, реальные steps, progress, error, revision и instance binding.
   Read-only fact providers расширяются через тот же driver protocol; unknown
   не равен false или success. UI показывает нормальный progress/actual error,
   подробные технические receipts доступны отдельно, без clutter плиток.
7. **Предварительный просмотр и восстановление.** Plan/dry-run объявляет,
   что действительно проверено и где действие нельзя симулировать. Recovery
   шаги не равны обязательному автоматическому rollback; DB/files могут быть
   необратимы. Нужны backup binding и явный исход, если rollback невозможен.
8. **Происхождение и тесты.** Кто создал пакет, какой код/ресурсы и baselines
   проверены, known limitations, дата проверки. Community origin не блокируется,
   но не выдаётся за проверенный SalesSnap сервис. Локально отредактированный
   пакет получает новый digest. Root-level extension требует отдельной проверки,
   подпись сама по себе не доказывает безопасность.

Это части одного контракта, не дополнительные approval очереди и не повторные
подтверждения каждого шага. Для минимального первого выпуска можно не включать
code extensions/root helpers, но их будущая граница должна быть предусмотрена.
Свежая source-инвентаризация определит, какие перечисленные возможности уже
существуют и подлежат переиспользованию.

## Минимальный пример (эскиз, не валидный выпущенный контракт)

```yaml
schema: mcd-operation-package/v1
id: community.example-plugin.operations
revision: 1
requires:
  protocol: operation-worker/v1
operations:
  refresh:
    applies:
      plugin_installed: ExampleBundle
    grants:
      - instance.process
      - instance.db
    steps:
      - id: rebuild
        uses: mautic.console/v1
        with:
          command: example:refresh
          arguments: ["--no-interaction"]
    result:
      check: operation.completed
commands:
  example.refresh:
    operation: refresh
ui:
  tile:
    title_key: example.refresh.title
    action: refresh
schedules:
  refresh:
    operation: refresh
    interval_seconds: 600
    overlap: forbid
    native_equivalents:
      - executable: instance.php
        argv: [instance.console, "example:refresh", "--no-interaction"]
        user: instance.runtime_user
```

Здесь `uses` — доверенный установленный driver, `with` — его typed параметры,
а grants лишь запрос, не самостоятельно выданные права. Credentials и root
command в пакете отсутствуют. Число600 — пример настройки, не новый default
для существующих операций.

## План проверки владельцами

| Требование | Сейчас | Проверка перед выпуском |
| --- | --- | --- |
| Existing file Assets | Частично реализованы; нужен mapping | Реальный patch и повторный run в ZIP/Composer; запрет escape из instance roots |
| SQL instance scope | Способ получения creds есть; isolation не доказана здесь | DML+DDL, запрет чужой DB/таблиц, префиксы и result без secret leak |
| System helpers | Предложено | Разрешённый apt/service и denied escalation на disposable host, не customer |
| Dynamic commands/drivers | Предложено | Новая команда из пакета без rebuild MCD; driver grant/protocol boundaries |
| UI descriptor | Есть plugin наработки; нужна инвентаризация | Одинаковое действие через CLI/API и общий renderer MCC; без vendor binding |
| Cron handoff | Есть plugin operation механизмы; нужна инвентаризация | Native→MCD без overlap/miss; crash recovery; remove/disable; чужой cron неизменён |

Все Mautic-specific сценарии проверяются в канонических Mautic-Operations
песочницах; host/package-manager/service проверки требуют отдельной disposable
системной среды владельца, не полномочий root на customer hosts. Код и evidence
сохраняются вне среды до vanilla reset. Точная schema, drivers, isolation,
порядок расширения продукта и acceptance evidence согласуются владельцами.
Создание документа не объявляет перечисленные drivers уже существующими.

## Source-backed reuse и безопасный первый scope

### Единый контракт

`mcd-operation-package/v1` — единый контракт для scenario package и operation
package. `mcd-mautic-scenario-package/v1` остаётся прежним review-обозначением
сценарной части, а не вторым registry или вторым execution API. В итоговой
схеме должен остаться один manifest, один revision/digest binding, один
producer/consumer transport и один result protocol. MCC, локальный MCD и
будущий другой портал используют тот же API.

### Что уже можно переиспользовать

| Область | Существующий MCD источник | Что переиспользуется | Чего он пока не доказывает |
| --- | --- | --- | --- |
| Identity/context | `mcd_agent/runtime_descriptor.py`, `mcd_agent/inventory.py` | install type, runtime user, instance identity, declared capabilities | package grant isolation и complete run binding |
| Immutable plan/evidence | `mcd_agent/mautic_patch_resolution.py`, `mcd_agent/mautic_patch_plan_v2.py`, `mcd_agent/mautic_patch_plan_v3.py` | schema validation, target/install binding, gates, payload SHA, plan/result evidence | общий package envelope для не-Mautic операций |
| Composer/ZIP artifacts | `mcd_agent/composer_target_manifest.py`, `mcd_agent/composer_prepared_target.py` | manifest, prepared target, artifact digest и recheck | универсальный resource origin и общий activation lifecycle |
| Files/backup | `mcd_agent/backup.py`, patch-plan file records | backup assets, before/after hashes, rollback receipts, safe relative paths | arbitrary instance-owned file driver с named-root grants |
| Instance process | `mcd_agent/executor.py`, `mcd_agent/runtime_descriptor.py` | runtime-user execution, cwd/argv boundary, capability checks | package-defined typed command registry и extension worker protocol |
| Plugin operations/cron | `mcd_agent/plugins.py`, `mcd_agent/mode.py` | existing operation descriptors, schedule parsing, exact managed cron markers and migration evidence | generic native-equivalent handoff ledger and crash-safe ownership transfer |
| State/result | `mcd_agent/state_push.py`, daemon task state and existing receipts | async state publication, progress/result transport, instance binding | one package-wide lifecycle for install/activate/run/remove |
| Host profiles | `mcd_agent/service_profiles.py`, `mcd_agent/apt_profile.py` | existing service/apt adapters as implementation candidates | package grants, helper protocol and denial of cross-instance/shared-host impact |

Эти источники являются reuse candidates, а не доказательством готовности нового
универсального driver. Нельзя оборачивать существующие privileged functions в
пакет без проверки grants, scope, idempotency, recovery и evidence.

### Недостающие механизмы до runtime implementation

1. `package_loader`: canonical JSON, revision/digest coverage, bounded archive
   extraction, resource origin and immutable fetch/preinstalled identity.
2. `operation_registry`: namespaced operation IDs, typed parameter schema,
   driver/capability ownership, dependency/conflict checks and lifecycle states.
3. `run_binding`: exact instance/context/resource/package binding, durable
   checkpoint, idempotency/postcondition and resume/reconciliation semantics.
4. `instance_db_broker`: real per-instance grants; for shared-prefix databases
   an object/table scope or dedicated adapter is mandatory. A default database
   on a system SQL user is not an isolation boundary.
5. `host_helper_protocol`: narrow local admin protocol for declared apt/service
   actions, with operator grants, package-manager/service locks and denied
   escalation. Manager input never becomes shell or sudo text.
6. `cron_handoff_ledger`: exact native mapping, preserved original entries,
   first safe verification run, ownership transition, crash recovery and
   duplicate prevention. Ambiguous mapping leaves native ownership unchanged.
7. `descriptor_consumer`: common UI/CLI/API descriptor output; renderers remain
   clients of the same API and are not package-provided code.
8. `extension_worker`: separately installed, versioned driver protocol for a
   genuinely new primitive. No Python import into the root daemon and no
   arbitrary package-contained privileged extension.

### Minimal first scope

Первый runtime scope не реализует все шесть подсистем одновременно:

1. Canonical package envelope, resource verification, instance/context binding,
   typed parameters, durable lifecycle/result and existing integrity checks.
2. One existing instance-owned file operation mapped to the generic package
   format, preferably `core_asset_patch/import-tag-detach` with its existing
   vulnerable/fixed gates, patch digest and rollback evidence.
3. One package-driven Mautic/plugin method using an already supported MCD
   runtime-user driver, without a new extension worker or privileged helper.
4. Descriptor output and local/manager transport for the same package revision;
   no second execution API and no new manager-specific executor.
5. ZIP/Composer artifact acceptance in disposable Mautic-Operations sandboxes,
   including exact artifact binding, negative applicability, restart/recovery and
   vanilla reset.

DB operations, apt/services, generic extension workers and native cron ownership
remain separately gated follow-up tracks. Their contracts are designed now, but
none becomes available merely because the manifest has a corresponding section.
For every later track the first acceptance must prove scope denial as well as
successful execution. This keeps the cleanup incremental and avoids a rewrite of
MCD's root daemon or a dormant executor activation.

Пакет может быть пользовательским, локальным, предустановленным или доставлен
MCC/другим менеджером. Техническая schema/integrity/context/capability проверка
одинакова для всех источников; доверие к издателю и host grants выбирает
оператор. Это не entitlement gate и не ограничение cross-line операций.

## Performance acceptance

Приёмка локального registry/index должна подтвердить на измеряемом наборе
пакетов:

- install/activate обрабатывает manifest и static metadata устанавливаемого
  пакета, а не полный payload всех уже установленных пакетов;
- `help`, `list` и discovery читают compact index и capability/CLI mapping без
  network request, полного обхода файлов или импорта всех drivers;
- run по имени разрешает exact package ID/revision/definition locator и
  загружает только выбранную operation и необходимые resources;
- missing или изменённый package file не исполняется из cached definition;
  index entry остаётся диагностикой до validate/reindex;
- activation/update атомарны, interrupted install не оставляет dangling
  registrations, а runs остаются pinned к immutable revision;
- applicability, grants, instance facts и latest-source evidence читаются
  заново перед run и не принимаются из cached index metadata;
- benchmark evidence содержит число пакетов, размер index, install/help/run
  timings и cold-start behavior. Фиксированный performance threshold не
  придумывается до получения baseline, но fullscan/import-all behavior считается
  failure независимо от времени.

Это acceptance локального installed registry, а не новый catalog MCC и не
новая инфраструктура. Package files остаются source of truth.

## Optional confidential resource provider extension

Confidential resources are an optional future provider extension, not a
minimum requirement of `mcd-operation-package/v1` and not a global licence or
MCC gate. Open/community packages remain valid without it. The detailed design
is in `MCD_V2_PRIVATE_PACKAGE_DELIVERY.md`.

The shared resource contract may declare an optional provider delivery such as
`resource.stream-decrypt-v1` with encrypted-origin identity, envelope identity,
content digest and the required in-memory/stream capability. The provider
returns content-key material through an authenticated API for the selected
revision/resource. An API credential is never used directly as a cipher key;
key material is not written to package files, SQL, argv, logs or disk cache.

MCD opens only the selected resource in memory and passes it through a
compatible scoped driver. A driver that requires a plaintext pathname and has
no in-memory/stream mode returns `capability_required`; it must not silently
create a plaintext temporary file. This is not a DRM promise: a host
administrator may inspect process memory, and applied Mautic files, DB effects
and ordinary runtime artifacts remain visible by design. Confidential delivery
can stop new key/resource issuance, but cannot retract content already exposed
by the client.

The extension requires its own provider/driver and sandbox review. It does not
change package selection, applicability, grants or the first runtime scope,
and it must never make public package execution depend on a commercial service.
