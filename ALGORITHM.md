# Алгоритм MCD: admission и dispatch сегментов

Этот документ является рабочим каноном для MCD scheduler. Он описывает
фактическую схему после исправления admission и должен обновляться вместе с
кодом, тестами и runtime receipt. Документ не является разрешением на
ручной `segments:update`, изменение профиля, capacity или данных на хосте.
Актуальная редакция этого канона — MCD 1.5.80 (immutable release поверх
принятого e910f67b; прямой follow-up от 1.5.79 без дополнительного изменения
execution behavior).

Для instance-scoped desired state локальный inventory UID является стабильной
основой, но transport запрашивает также динамически полученный
`<uid>@<mcc-host>` alias. Если MCC вернул legacy/base и immutable qualified
строки одной instance, MCD выбирает qualified текущую revision, применяет
нормализацию special rings и ack-ит ровно эту строку; host/domain в коде не
зашиваются.

Локальный desired push сначала читает свежий MCC snapshot и сохраняет все
instance-rows, которых нет в локальном delta. Сравнение выполняется по полям
относительно persisted local baseline: отсутствие ключа в baseline не является
командой удаления, а исчезновение ключа, который был в baseline, очищает только
этот ключ. Поэтому restart/trigger не стирает удалённое кольцо, а прямое
локальное добавление и явное удаление проходят одним и тем же безопасным
rebased payload.

Синхронизация двусторонняя и использует durable metadata
`mcd-runtime-sync-metadata-v1`: для каждого host-поля и каждого поля
instance-row сохраняются `updated_at`, logical `revision`, `source`,
`event_id` и `tombstone`. При конфликте побеждает более новый сохранённый
event: сначала сравнивается `updated_at`, при отсутствии timestamp —
`revision`; точная ничья разрешается детерминированно в пользу MCC, затем по
`event_id`. Эти значения живут в state backend и не вычисляются из mtime
файла, поэтому offline-изменения переживают restart. MCD CLI/локальный config
пушит выигравшее локальное поле в MCC, а более новое поле из MCC materialize-ся
в config MCD. Отсутствие поля в старом/неполном snapshot не удаляет значение;
удаление действительно только при явном более новом tombstone. Accepted merge
сохраняет metadata и следующий poll не создаёт ping-pong или duplicate push.

Перед отправкой `desired` envelope MCD проецирует host- и instance-runtime
только через canonical authority contract `runtime_remote_allowed_keys()`.
Local-only/bootstrap значения и будущие неизвестные ключи не записываются в
MCC desired state; observed telemetry envelope остаётся отдельным потоком.
Это contract-level правило, а не список исключений по именам ключей.

Bootstrap после approved update сначала восстанавливает отсутствующие
instance-rows из durable baseline, затем создаёт ровно один pending delta до
первого accepted desired revision; remote apply до этого момента запрещён.
При restart первым read-only шагом выполняется свежий canonical MCC fetch с
instance UID aliases и field metadata. Его более новый qualified row
materialize-ся в mutable config/effective runtime до локальной deletion
classification; принятый instance row не очищается неполным host snapshot.

После startup/poll persistence строится также из canonical MCC
`instance_desired_states`, а не только из profile-shaped effective config.
Qualified UID и его priority/super-priority lane maps сначала materialize-ся в
локальном runtime, затем именно этот snapshot становится baseline; restart не
создаёт ложную local deletion и не отправляет пустой instance-row обратно в
MCC.

Если локальный config-файл изменился во время remote poll/apply, MCD сначала
сохраняет пару `previous/current` в durable `local_runtime_push_pending`, а
remote baseline не продвигает. Проверка выполняется перед fetch, после network
round-trip и непосредственно перед baseline refresh; pending переживает restart
и failover state backend. Только ответ MCC с принятой desired-state `revision`
разрешает сохранить новый local baseline и удалить pending; conflict/error или
неполный ответ оставляют delta для следующего ровно одного rebased push.

## 1. Область и границы

Планирование выполняется по каждому instance root, но capacity и защита
запуска учитывают общий host. Нельзя бесконечно обслуживать один host или
instance: dispatch использует ротацию instance и общие host slots.

Обычный segment cycle — persisted состояние в `TaskStore.runtime_sync`.
Он переживает restart и содержит уникальный ordered набор элементов,
`claimed`, `running`, `done`, `errors`, статус цикла и время admission.
Источником кандидатов остаётся read-only DB planning; production data,
profile/config и manual `segments:update` для admission не меняются.

## 2. Три execution-lane и две статические MCC-линии

В MCD ровно три execution-lane:

1. `ordinary` — автоматический persisted cycle;
2. `priority` — статический MCC список, историческое имя которого
   `segment_whitelist` является только migration alias;
3. `super-priority` — второй статический MCC список с максимальной частотой
   admission.

Настраиваемых списков ровно два: `priority` и `super-priority`. Нельзя
создавать третью whitelist/dynamic-priority сущность. Membership взаимоисключающая:
`super-priority → priority → ordinary`. Любой опубликованный segment/closure
принадлежит только одной линии; статические линии полностью исключены из
ordinary snapshot. Обе статические линии имеют собственные persisted queues,
но используют общие host-profile slots, instance locks и execution locks.

`super-priority` проверяется чаще `priority`; частота не меняет precedence и
не убивает running job. Если worker свободен, независимая работа другой линии
может продолжаться; общая capacity и dependency lock обязательны.

### Канонический runtime envelope

MCC может передать per-instance envelope `segment_special_rings_v1` с двумя
линями `priority` и `super_priority`. MCD не создаёт отдельную третью
конфигурацию: envelope нормализуется в поддерживаемые
`segment_priority_instance_settings` и
`segment_super_priority_instance_settings` по immutable `instance_uid`.
Положительные segment IDs дедуплицируются, `slots_override` проверяется и
применяется к соответствующей static lane. Невалидная форма или неизвестное
runtime key остаётся rejected и не получает applied revision; `applied`
acknowledgement отправляется только для принятого effective payload и содержит
его digest/снимок.

Профиль является отдельной canonical authority. При каждом runtime poll или
trigger MCD сначала сохраняет выбранный MCC profile, затем rebases свежий
config на его profile baseline и только после этого применяет runtime tunables.
Устаревший observed `passive` не может сбросить выбранный active/farm profile;
явный desired profile, включая `passive`, имеет приоритет.

## 3. Построение ordinary cycle и порядок

1. Планировщик получает due IDs или bounded full snapshot опубликованных
   сегментов и удаляет union статических `priority`/`super-priority` IDs.
2. Read-only definitions добавляют опубликованные never-built IDs, которых не
   было в due query. Ordinary snapshot строится строго по категориям:
   `never-built → changed → stale`, где stale threshold берётся из выбранного
   host profile (`segment_stale_after_sec`). Fresh/current published rows,
   обнаруженные bounded full scan, являются только discovery и не становятся
   ordinary work. После первого полностью завершённого цикла fresh/current
   определяется относительно persisted bounded source fingerprint предыдущего
   terminal ordinary cycle, а не относительно `last_built_date` текущей строки.
   Если built segment отсутствует в этом baseline либо baseline пустой/
   повреждён, действует fail-open bootstrap: такой segment считается `changed`
   и попадает во вторую категорию (после never-built) в ближайшее новое
   ordinary ring, не ожидая stale threshold, кроме static lanes.
   Внутри каждой категории сохраняется FIFO стабильного observed order. Weight,
   recent activity, numeric ID, stale promotion и threshold не могут
   переставлять ordinary.
3. IDs и dependency edges дедуплицируются. На Mautic ≤6 closure превращается в
   одну topological последовательность на worker: родители перед детьми. На
   Mautic 7 MCD оставляет только native terminal/root plan; внутренние
   dependencies не запускаются отдельными MCD элементами.
4. Новый never-built не ждёт следующего snapshot. В active ordinary cycle он
   атомарно добавляется после уже стоящих pending never-built и перед pending
   changed/stale. Уже claimed/running и terminal элементы не двигаются,
   существующий cursor не сбрасывается, duplicate не создаётся. Прочие новые
   observed IDs добавляются в хвост pending очереди.
5. Invalid filters, logical issues, failure cooldown и DB/backup/scheduler
   pause являются блокираторами admission, а не поводом пересортировать или
   потерять persisted cycle.
6. После полного terminal ordinary cycle следующий snapshot создаёт новый
   cycle. Пока цикл не terminal, его порядок не пересобирается.
7. Cycle хранит отдельно `observed_snapshot`/generation и применённый
   `baseline`/generation. Наблюдения во время active cycle не продвигают
   baseline; он фиксируется только при переходе в terminal. Успешные элементы
   продвигают свои fingerprints, а terminal-error элементы сохраняют старый
   fingerprint (или отсутствие записи) и retry-ятся в следующем cycle, даже
   если их source timestamp не изменился. Partial/error до terminal не могут
   создать новый completed-cycle baseline; state переживает restart.

Статические queues строятся отдельно: общие dependencies двух линий принадлежат
линии с более высоким precedence и выполняются ровно один раз. Например,
`super-priority 58` и `priority 59` с общим parent 57 дают единый plan `57,
58` в super и `59` в priority на Mautic ≤6. Для Mautic 7 правило строгое:
MCD планирует и запускает только опубликованные terminal/root IDs, а каждый
root запускается не более одного раза в своей lane. Общий parent 57 не
создаётся отдельной MCD task и не дублируется между sibling commands; его
покрытие допускается только внутри native Mautic closure. Source/CLI и
TaskStore receipt обязаны фиксировать deduplicated root plan и union closure,
без неоднозначного ручного подтверждения алгоритма.

## 4. Claim, worker и slots

Перед spawn worker атомарно claim-ит следующий eligible item из persisted
cycle или статической queue. Claim проверяет `done/errors/claimed/running`,
глобальную dependency closure, failure cooldown, host slots, instance lane
slots, import reservation и launch guard. `super-priority` выбирается раньше
`priority`, затем ordinary; приоритет не вытесняет уже running работу.
При отказе spawn claim освобождается.

После terminal одного item worker/dispatcher немедленно получает сигнал и
claim-ит следующий eligible item текущей очереди; ожидание минутного tick не
является частью алгоритма и tick не является per-item dispatcher.

TaskStore task key и OS execution lock остаются последней защитой от duplicate
claim, включая restart и concurrent workers.

Для ordinary cycle есть два разных решения, которые нельзя смешивать:
`admission` добавляет новые IDs в persisted cycle, а `dispatch` запускает уже
сохранённые pending IDs. Campaign/queue pressure блокирует только первое:
при active pressure новый snapshot не расширяет active cycle, но его уже
сохранённый pending cursor продолжает dispatch. Если обычный ring временно
получил нулевой throttled limit, для такого cursor сохраняется один shared
slot при его фактической доступности.

Глобальный пятисекундный baseline cadence относится к новым baseline
admissions. Pending item, который существовал до текущего plan refresh,
атомарно проходит `claim -> start` на следующем dispatch без ожидания чужого
root/instance baseline. Этот запуск не продвигает global baseline timestamp:
следующий новый admission не теряет свою собственную cadence. При этом
persisted cycle claim, dependency/conflict, host/instance capacity, TaskStore
duplicate, repeat interval и OS execution lock остаются обязательными.

Host fairness работает поверх этого правила: каждый root с persisted pending
работой получает свободный baseline/shared slot, а terminal callback закрывает
item до начала следующего cycle. Ни один active cycle с `pending > 0` и
`running = 0` не может оставаться только из-за порядка dispatch или pressure.

Import — отдельный host-wide singleton worker. Независимо от количества
instances одновременно допускается максимум один `task_type=import`; он
занимает ровно один общий segment slot. Pending imports наблюдаются в FIFO
порядке: первый eligible import получает первый освободившийся slot, а
остальные roots ждут в очереди. `scheduler_pause`, import route, DB
capacity, duplicate/task lock и instance guard остаются обязательными.

Этот singleton не превращает import в host-wide pause: остальные свободные
slots продолжают принимать segment/SQL/dependency closure work. Import не
подчиняется generic `command_timeout_sec` или `worker_watchdog_sec` и не имеет
hard total-duration cap: штатный импорт может работать часами. Его единственный
таймер — profile-managed `runtime.import_stall_timeout_sec` (по умолчанию
1800 секунд, с безопасным минимумом 60 секунд), который измеряет отсутствие
независимых признаков прогресса. Lease продлевают изменения Mautic import
status/counters или активный DB import query, движение upload/log file/offset,
либо изменение kernel CPU/IO counters процесса. Один только PID, живое дерево
процессов или persisted self-marker не являются heartbeat. После превышения
stall timeout MCD делает grace/recheck; только повторное отсутствие сигналов
позволяет kill/reap и terminalizes import как
`timeout/killed_by_import_stall_watchdog`. Terminal process/DB outcome,
failed, cancelled/stopped и lost-PID освобождают singleton slot немедленно;
failed/timeout не порождают stale follow-up или автоматический duplicate
retry. После terminal следующий FIFO import может быть admitted. Completed
import follow-up оставляет минимум один slot для unrelated ordinary/closure
work, когда такой work уже pending. Daemon restart сначала reconcile/adopt-ит
живой import; при adoption текущий независимый сигнал создаёт новый stall
lease, а lost PID закрывается как terminal/lost и не оставляет permanent slot,
lock или PID ownership.

`import_stall_timeout_sec` проходит через обычный runtime contract: он входит в
`AgentConfig`, `_RUNTIME_TO_ATTR`, profile-managed/locked runtime keys и
`PROFILE_MANAGED_RUNTIME_KEYS`. Поэтому profile → local runtime и MCC/local
last-writer-wins reconciliation используют один и тот же ключ; ручное значение
не может незаметно пережить profile authority.

Ручной MCC `segments:update -i <id>` не является четвёртой линией. До spawn
MCD фиксирует observed fingerprint/generation цели и dependency closure,
атомарно claim-ит существующий элемент ordinary/priority/super, если он есть,
и блокирует любое пересечение с другой линией, running task или execution
lock. Pending automatic item тем самым выполняется manual request-ом и не
запускается второй раз. При успехе terminal state помечает захваченные items
как done и согласует applied baseline; при ошибке сохраняет items/errors или
manual retry marker как due. `manual_request_id` сохраняется в TaskStore, а
persisted admission receipt переживает restart.

Ручной dispatch может временно превысить только общую segment capacity: при
host-profile limit 4 и четырёх обычных running tasks manual request может
запустить пятый. Пока общее число равно 5 или после завершения одной обычной
task возвращается к 4, обычный dispatcher не делает refill. Когда общее число
снизится до 3, обычный admission может восстановить его до 4. Этот временный
slot не меняет profile, config или data и по-прежнему соблюдает same-entity,
dependency и execution locks.

## 5. Retry и terminal/error

`done` записывается только после успешного process exit. Для automatic segment
`runtime.segment_retry_count` — внешняя host-profile policy и означает число
повторов после initial attempt: approved default `2` даёт максимум три
фактических запуска одного item, без хардкода хоста или segment ID. До
исчерпания bound item остаётся claimed для respawn.

Код Mautic с `rc != 0` сам по себе не является ошибкой MCD. Если MCD честно
запустил process, выполнил bounded retries, записал terminal outcome текущего
cycle и немедленно продолжил следующий eligible item, алгоритм соблюдён.
Исчерпанный bounded retry — только terminal Mautic failure этого item, а не
scheduler error/FAIL и не permanent dead-letter: независимые items
продолжаются, item остаётся due для следующего cycle. Ошибка MCD — это падение
daemon, потеря/сброс persisted cycle, неверное число attempts, duplicate/
overlap, зависание или отсутствие следующего claim после terminal outcome.
После исчерпания retries segment получает terminal error текущего cycle с
`mautic_failed`, его applied baseline не продвигается и он остаётся due для
следующего cycle; terminal callback будит dispatcher без ожидания следующего
tick. Потеря PID/command ownership — отдельный scheduler error: перед
terminalization MCD удерживает claim только bounded grace window, измеренную от
последнего независимо подтверждённого process/DB heartbeat. Один лишь
persisted marker эту window не продлевает. Error не блокирует независимые
items, но учитывается failure cooldown и receipt.
Цикл terminal, когда каждый его item находится в `done` или terminal error и
нет running/claimed работы.

## 6. Наблюдаемость и acceptance

Каждый цикл виден в scheduler monitor receipt (`queued`, `running`, `done`,
`errors`, `total`, `cycle_id`, `updated_at`). Для acceptance нужны source,
test, package/install и TaskStore receipts. Natural acceptance на b2bss
проверяет, что новые сегменты 151/152 получают start и terminal в текущем
cycle без ручного `segments:update`, profile/capacity/data changes. Adonis
проверяет отсутствие regressions ordinary/dependency/parallel workers на
существующей естественной работе.

## 7. Трассировка правила к реализации

| Правило | Source | Проверка/receipt |
|---|---|---|
| две статические линии и исключение из ordinary | `mcd_agent/static_segment_lanes.py`, `daemon.py` | `tests/test_static_segment_lanes.py` |
| ordinary категории, FIFO и mid-cycle admission | `mcd_agent/segment_admission.py`, `daemon.py` | `tests/test_segment_admission.py` |
| dedupe и сохранение порядка ring | `mcd_agent/ring_utils.py`, `daemon.py` | `tests/test_segment_ring_reconcile.py` |
| dependency closure и terminal plan | `mcd_agent/segment_dependencies.py`, `daemon.py` | `tests/test_segment_dependencies.py` |
| persisted cycle, never-built admission и atomic claim | `mcd_agent/segment_admission.py`, `daemon.py` | `tests/test_segment_admission.py` |
| host/instance slots и rotation | `mcd_agent/daemon.py` | `tests/test_campaign_ring_dispatch.py` |
| profile retry budget, three starts and bounded orphan expiry | `mcd_agent/config.py`, `mcd_agent/daemon.py` | `tests/test_segment_admission.py` |
| monitor queued/running/done/error receipt | `mcd_agent/daemon.py` | `tests/test_scheduler_monitor_cycle.py`, `tests/test_signals_scheduler_history.py` |
| authority-safe desired runtime projection | `mcd_agent/config.py`, `mcd_agent/runtime_overrides.py` | `tests/test_runtime_override_direction.py`, `tests/test_runtime_stable_keys.py`, `tests/test_runtime_migration_adapter.py` |
| natural b2bss/Adonis evidence | runtime TaskStore/MCC receipts | rollout acceptance (после package/install) |

Любое расхождение между этой схемой, source, тестом и runtime receipt
блокирует acceptance; его нужно явно зафиксировать и исправить до handoff.
