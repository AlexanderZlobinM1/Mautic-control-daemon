# Segment scheduler liveness test algorithm

Этот документ описывает исполняемую матрицу M6/M7 для ordinary segment
cycles. Локальная unit-матрица не использует production host, не запускает
реальный Mautic и не меняет profile/config/data; её зелёный результат не
является доказательством live acceptance. Полный gate дополнительно требует
owner-labelled disposable M6/M7 sandbox, штатные Mautic commands, exact
package/runtime SHA и реальный MCC/package/install receipt.

## Mandatory self-update lifecycle gate

1. Живой обычный PHP/Mautic console worker не блокирует self-update. Тест
   обязан подтвердить, что `segments`, `campaigns`, `import` и произвольный
   `bin/console` process переживают supervisor restart/reinstall с той же
   PID/start identity, а после старта MCD TaskStore re-adopts его без duplicate
   spawn. Проверяется реальный `KillMode=process` и process/cgroup поведение,
   не только mock self-update gate.
2. Active backup lock, MCD source/update lease и MCD-owned migration/deployment
   critical lock блокируют update fail-closed и возвращают явный retry/status
   reason. Обычные task locks, campaign pressure и process-name observations
   не могут стать update blockers.
3. Acceptance receipt обязан раздельно показать сохранность TaskStore,
   `runtime_sync`, ordinary/static/import очередей, claims/running/follow-up,
   PID adoption/terminal receipt и каждый MCD-owned critical barrier. Любое
   расхождение source ↔ tests ↔ systemd ↔ receipt блокирует push/install
   acceptance.
4. `scheduler.pause` проверяется по владельцу, а не по факту существования
   файла: новый update barrier содержит operation/PID/PID-start identity;
   живой owner или source/update lease сохраняет barrier, orphaned legacy
   `mcd-update-admission` восстанавливается, а ручной `paused` не удаляется.

## State fixture

Для каждого major (`6`, `7`) создаётся persisted `mcd-segment-cycle-v1`:

```text
items = [first, ..., last]
done = [first, ..., second-last]
errors = []
claimed = []
running = []
status = active
```

TaskStore fixture должен сохранять `cycle_id`, `updated_at` и spawned task с
`pid`, `started_at`, `entity_id` и `state=running`. Свободная capacity означает
`scheduler paused=false`, нет running task в root, host/instance guard
возвращает свободный slot, а dependency/TaskStore/execution locks не заняты.

## Mandatory scenarios

1. На следующем dispatch `last` атомарно claim-ится и стартует без нового
   scan, minute reset или чужого global baseline tick. До terminal callback
   cycle остаётся `active`; после callback `last` добавляется в `done`, cycle
   становится `terminal`; только следующий planning snapshot может создать
   новый `cycle_id`.
2. Кандидат, удержанный campaign/admission pressure, не добавляется в
   `cycle.items`, не появляется в regular ring и не проходит claim guard.
   После снятия pressure следующий plan атомарно добавляет его в durable
   cycle, после чего он появляется в ring и запускается.
3. При restart volatile planner cache пуст: pending projection читается
   напрямую из durable `runtime_sync`, сохраняет порядок `cycle.items`, один
   раз claim-ит и запускает последний item, а terminal callback завершает тот
   же `cycle_id` без ring reset. Уже сохранённый pending ID всё равно
   claim-ится и стартует. После снятия pressure новый ID может быть добавлен
   по обычному FIFO/never-built правилу.
   Если новый never-built ID обнаружен уже после admission active cycle, он
   не меняет `cycle.items`: сохраняется в durable `follow_up` FIFO и получает
   следующий свободный ordinary worker раньше следующего frozen-cycle ID.
   После restart порядок follow-up сохраняется, duplicate не создаётся.
   Exact M6/M7 fixture с durable pending `[154, 147, 144, 149, 151]`, пустыми
   volatile rings и независимым active super-priority lane обязан после
   restart запустить реальный batch через `_fill_from_ring`, записать PID и
   `TaskStore.state=running`, а после terminal callback немедленно refill-ить
   следующий batch, сохраняя исходный `cycle_id`. Unpressured profile
   использует regular budget `3`; whitelist-throttled profile с regular
   budget `0` сохраняет один shared slot.
4. Несколько roots/instances с `pending>0` и `running=0` получают свободные
   slots по очереди dispatch. Один root не может навсегда удерживать cursor
   active только потому, что другой root первым использовал global baseline.
5. Повторить пункты 1–4 для Mautic 6 и Mautic 7. M6 сохраняет dependency
   closure order. Для M6 fork/diamond receipt обязан показать один
   TaskStore running row/PID на всю deduplicated closure, все IDs в
   `claimed/running`, sequential topological command execution и terminal
   `done/errors` для каждого ID; независимые closures могут идти параллельно.
   M7 сохраняет native traversal: каждая overlapping closure получает один
   TaskStore running row/PID до terminal всех IDs, а несколько native leaf-root
   commands идут последовательно в этом worker с `--exclude=<covered-parent>`.
   Независимые closures остаются отдельными workers. Receipt обязан показать
   read-only help capability `--exclude`, exact parent rebuild count, durable
   covered IDs и отсутствие второго PID для одной closure; `--exclude`
   передаётся повторяемым option, не CSV. Failed/timeout root не считается
   covered. Канонический native fixture для этого gate фиксирован: `19 ->
   (20, 21) -> 22`. Его receipt обязан быть получен через реальный
   `_fill_from_ring` + `TaskStore`, показать один worker/PID,
   `claimed/done=[19,20,21,22]`, ровно один запуск общего parent `19`,
   `--exclude=19` во второй native-команде и durable `native_covered_ids`
   для всей closure. Равенство списков без dispatcher receipt и отдельные
   native CLI-запуски acceptance не заменяют.

6. Для host с тремя и более готовыми roots проверяется aggregate capacity:
   shared/host/instance counters считаются по фактически running tasks,
   включая baseline и SQL. Ротация и fairness должны передавать доступный
   слот следующему root, но не могут обойти явный
   `scheduler_host_max_parallel` или `scheduler_instance_max_parallel`.
   Один root не может занять больше hard cap, а несколько roots не могут
   превратить host cap в сумму своих baseline slots.

7. Проверяется sliding regular distribution: при 40 ready roots и 12 workers
   первые 12 regular launches принадлежат 12 разным roots; после освобождения
   slot admission продолжает durable round-robin. Если ready roots меньше
   workers, независимые workers одного root разрешены только в пределах
   instance/profile cap. Long-live root не ставит all-roots barrier.

8. Проверяется durable tier ordering для full/nonlazy cycle: `important`
   (never-built, changed/unknown, retry, stale и нужные dependency parents)
   сохраняется отдельно от `remaining` fresh tail. Global dispatcher назначает
   ready unclaimed important всех roots раньше remaining; claimed/running
   important не удерживает remaining после assignment. Pending important root,
   который фактически не может получить admission из-за instance cap или
   текущего round-robin ownership, не блокирует свободный host-slot другого
   root; после освобождения capacity precedence восстанавливается. Lazy cycle
   не создаёт remaining.

9. Проверяется mixed per-instance lazy policy на одном host: sparse
   `segment_lazy_update_instance_settings` задаёт для разных immutable UID
   комбинацию `true/false/true`, а отсутствующая строка использует host
   default. Для lazy roots due/never-built/changed/retry/stale и нужные
   dependency parents остаются в `important`, но fresh `remaining` не
   добавляется; full root получает тот же `important` перед своим fresh tail.
   Receipt обязан показать effective policy и dispatch context отдельно для
   каждого root, отсутствие cross-root movement и независимое заполнение
   разрешённых slots. Toggle во время active cycle не меняет frozen
   `cycle.items`, claims, running или follow-up; только следующий cycle этого
   root использует новое значение. После restart/reconcile UID-scoped desired
   row и host fallback восстанавливаются без сброса очереди. Отдельно
   проверяется clear-контракт: удаление последнего локального поля публикует
   явную пустую UID-строку с текущей revision и верхнеуровневую пустую
   `segment_lazy_update_instance_settings={}`; при удалении A sparse map B с
   более новой revision сохраняется, а простое отсутствие UID не используется
   как команда очистки. Очистка всей map — отдельная явная операция. Реальный
   dispatcher/TaskStore regression `test_mixed_mode_toggle_uses_dispatcher_and_defers_reverse_tail`
   обязан проверить: active full A с admitted `remaining` после A→lazy
   действительно доходит до terminal; следующий A lazy-cycle не получает
   fresh tail; full B продолжает независимый dispatch; lazy→full не добавляет
   tail в active lazy-cycle, а принимает его только в следующем собственном
   full-cycle.

10. Проверяется dedicated super lane: четыре enabled super-instance worker-а
   работают outside shared regular `11` capacity (`4 outside 11`), не
   вычитаются из host/instance regular counters и не дублируют closure/lock.

11. Проверяется retry contract: `runtime.segment_retry_count` — inclusive
    total-start cap, несмотря на историческое имя ключа. Значение `3` даёт
    ровно три фактических запуска одного segment item, а не initial плюс три;
    значение `0` даёт один безопасный effective start. Receipt и TaskStore
    history обязаны показать attempts `1..3` и terminal callback после третьего
    неуспешного запуска, после чего следующий item получает admission.

12. Проверяется distinction между диагностикой и результатом выполнения:
    heuristic `segment_filter_safety` (например, `today -1`) только пишется
    как hint и не удаляет executable segment до profile-bound Mautic start;
    настоящий non-zero Mautic rc проходит обычный retry/terminal path. Для
    proven topology fixture `bad parent ↔ bad parent`, его published
    descendant и healthy tail должны получить соответственно
    `data_error:dependency_cycle` terminal receipts для bad branch, успешный
    terminal для healthy tail, полный root `status=terminal`, после чего
    следующий planning snapshot создаёт новый `cycle_id`. Ни один bad ID не
    может остаться assigned `pending` навсегда, а topology data-error не
    является self-update barrier.

## Guard assertions

Для pending cycle bypass-ится только global new-admission baseline cadence.
Persisted ordinary pending не получает static priority capacity: его launch
limit равен effective regular budget, с fallback ровно в один shared slot при
нулевом throttled regular limit.
Обязательны и проверяются отдельно: atomic cycle claim, `done/errors/claimed/
running`, dependency conflict, host/instance capacity, import reservation,
repeat interval, TaskStore duplicate key и OS execution lock. Pending запуск не
обновляет global baseline timestamp. New admission при том же baseline guard
остаётся заблокированным.

Capacity invariant: при явном `scheduler_host_max_parallel > 0` это hard
shared regular host-wide ceiling для штатного ordinary/priority/import/campaign/
SQL work, включая baseline; dedicated super-priority lane считается отдельно.
При явном `scheduler_instance_max_parallel > 0` это hard shared regular
per-root ceiling, также включая baseline; super lane ограничен только своим
lane budget. Implicit per-root baseline разрешён только при отсутствии
соответствующего explicit cap. Fairness reservation может лишь выбрать
следующий root среди реально свободных slots и не может добавить capacity
сверх cap. Manual/operator override — единственное документированное
исключение: один extra slot на instance без host-wide manual overflow cap,
но manual worker всё равно входит в occupancy, поэтому normal refill ждёт
возврата ниже hard cap. Receipt выше explicit ceiling без такого manual
evidence — FAIL.

Для каждого ordinary tick дополнительно проверяется инвариант:
`regular_ring == ordered(durable cycle.items - done - errors - claimed - running
- static/sql-managed IDs)`. ID, отсутствующий в `cycle.items`, не может быть в
ring или TaskStore claim path. Если segment launches отсутствуют на всём host,
receipt обязан отдельно показать состояние global scheduler/backup pause,
cluster cron route, root-local DB circuit-breaker, campaign pressure и
effective segment slots; пустой ring без причины считается FAIL.

Manual/operator override проверяется двумя уровнями N+1: при явных
host/instance cap `4`, четырёх обычных running tasks и root, который уже был
served или не является текущей round-robin head, manual request обязан занять
пятый TaskStore/PID slot. После завершения одной обычной task (occupancy `4`)
обычный launch остаётся заблокирован; после второй (occupancy `3`) ordinary
admission возвращается до `4`. Отдельный per-instance сценарий проверяет
normal host cap `11`: 11 обычных workers и по одному manual request на трёх
разных instances дают `14`, normal refill не срабатывает при `14→13→12→11` и
возвращается при `10→11`; dedicated super lane исключён. Manual bypass-ит
только shared capacity и fairness gate; duplicate key, dependency/closure,
import singleton, OS execution lock, scheduler pause и self-update critical
barrier остаются обязательными.

## Rule → code → test/live receipt matrix

| Обязательное правило | Канонический код | Проверка и live evidence |
|---|---|---|
| ordinary Mautic/PHP work не блокирует update; MCD critical barriers блокируют | `mcd_agent/self_update.py`, `mcd_agent/daemon.py`, `systemd/mcd.service` | self-update lifecycle tests + fresh M6/M7 receipt with worker PID adoption and barrier reasons |
| stale scheduler admission не превращается в вечный blocker | `mcd_agent/update_admission.py`, `mcd_agent/self_update.py` | owner/lease/recovery tests in `tests/test_update_admission_race.py` + read-only stale-barrier receipt |
| TaskStore/runtime_sync queues, claims/running/follow-up/retry/monitor state переживают restart/reinstall | `mcd_agent/task_store.py`, `mcd_agent/daemon.py` | restart/adoption tests + fresh M6/M7 seeded-cycle receipt |
| roots/instances независимы; нет all-roots barrier/loop одного root | `_iter_instances_with_root_dispatch_gate`, root-local monitor/refill workers | `tests/test_segment_liveness.py` + fresh M6/M7 receipt with every B/C root cycle_count and terminal seeded IDs |
| explicit caps, sliding fairness и dedicated super lane считают правильный budget | `_scheduler_host_slots_available`, `_scheduler_instance_slots_available`, `_scheduler_round_robin_order` | `tests/test_campaign_ring_dispatch.py` hard-cap/3+ root/40+12/4-outside-11 rows + fresh aggregate slot receipt |
| important tier получает precedence только при фактической admission-ready capacity; full root не ставит global barrier | `_scheduler_segment_entity_allowed`, `_scheduler_important_root_dispatchable`, `mcd_agent/segment_admission.py` | `test_remaining_root_uses_free_host_slot_when_important_root_is_instance_full` + durable tier receipt |
| host/instance lazy policy не смешивает roots и не пересобирает active cycle при toggle | `config.py`, `runtime_overrides.py`, `segment_admission.py`, оба ordinary planner path в `daemon.py` | `test_mixed_mode_toggle_uses_dispatcher_and_defers_reverse_tail` через dispatcher/TaskStore + M6/M7 receipt с effective policy, frozen cycle и restart/ACK readback |
| lazy empty plan остаётся пустым, full mode сохраняет observed IDs | ordinary snapshot/planning paths in `mcd_agent/daemon.py` | lazy/full planner tests + fresh M6/M7 planner receipt |
| manual N+1 override — per-instance exception без host-wide overflow и с сохранением locks | `_submit_if_slot_uncoordinated`, manual admission path | `tests/test_segment_admission.py` cap-4→5→4→3 и per-instance `11+3→14→...→10→11` receipts |
| M6 closure занимает один worker/PID, M7 covered roots не параллелятся | `dependency_execution_chains`, `_segment_chain_command_args`, `_segment_chain_runtime_*`, static closure lock | `tests/test_segment_chain_runtime.py` actual TaskStore/PID receipt + fresh M6/M7 sandbox evidence |
| M7 native parent coverage дедуплицируется через capability-backed `--exclude` | `_segment_native_coverage_*`, `_segment_native_exclude_capability`, `_append_segment_native_excludes` | read-only `help ... --format=json` receipt + actual parent-count/PID receipt |
| dedicated super-priority ticks не подавляются native coverage предыдущего ordinary cycle | `_segment_successful_coverage_ids`, `_static_lane_successful_coverage_ids`, `static_segment_lanes.py` | `test_new_super_cycle_does_not_reuse_previous_ordinary_native_coverage` + natural Farm02 static-lane marker series |
| завершившийся super-priority cycle не ждёт завершения priority lane для следующего тика | `static_segment_lanes.reconcile_state` | `test_recurring_m6_m7_super_cycle_state_model_preserves_ordinary_cycle` (unit state model only) + private recurring M6/M7 Docker/full-run receipt with live shared-budget wait |
| filter hints не подменяют Mautic rc; unsafe topology terminalizes explicitly | `segment_filter_safety.py`, `logical_issues.py`, `_terminalize_segment_data_errors` | `test_topology_data_errors_finish_root_and_allow_next_cycle` + filter-hint regression |

## Acceptance gate

Минимальный gate:

```text
python3 -m unittest tests.test_segment_liveness
python3 -m unittest tests.test_segment_liveness tests.test_segment_admission tests.test_campaign_ring_dispatch tests.test_segment_full_scan_pending
```

Затем запускается полный M6/M7 base+extended suite из CI/release harness и
read-only real-MCC verification. Receipt обязан содержать commit/source SHA,
major, cycle/task IDs, PID, `started_at`, `finished_at`, claim/terminal states,
guard reason и результат каждого gate. Любое расхождение между этим
алгоритмом, source, tests и runtime receipt блокирует push/install acceptance.

## Import liveness matrix

Все import fixtures и real-command rows запускаются только в owner-labelled
disposable Mautic 6/7 sandboxes. Production не используется даже для fake
state. Для host с несколькими instances receipt обязан подтверждать:

1. `max_concurrent_imports == 1` на каждый instance/root. Imports разных
   instances могут идти параллельно в пределах host/profile capacity; второй
   pending import того же instance остаётся FIFO pending.
2. Pending, healthy-running, completed, paused, cancelled/stopped,
   failed/broken, retry/recovery и invalid/missing DB-row outcomes с exact
   TaskStore state, PID, timestamps, slot count и terminal reason.
3. Generic `command_timeout_sec`/`worker_watchdog_sec` не останавливают import:
   long-running import с progress signal может идти дольше старого timeout и
   даже часами. Stall watchdog использует только независимые signals:
   Mautic counters/status or active DB query, upload/log file movement/offset,
   kernel CPU/IO counters. PID/process tree и persisted self-marker отдельно
   не считаются heartbeat. Live import без сигналов дольше configurable
   `import_stall_timeout_sec` проходит grace/recheck и получает
   `timeout/killed_by_import_stall_watchdog`; terminal rc/error освобождает
   slot немедленно. Ни один outcome не оставляет stale import follow-up,
   automatic duplicate retry, lock или PID leak.
4. Во время active/hung import другие свободные slots запускают manual
   segment N+1 и dependency closures. Completed follow-up оставляет один slot
   для unrelated ordinary work. После terminal следующий FIFO import получает
   первый свободный slot.
5. Restart/reconcile живого import сохраняет singleton; исчезнувший PID
   закрывается и не блокирует следующий import. M6 и M7 повторяют одну и ту
   же матрицу.

Карта disposable M6/M7 rows (каждая строка выполняется для обоих major):

| Row | Проверка |
|---|---|
| 1 | pending imports FIFO и первый eligible каждого instance получает свой slot |
| 2 | active import на одном root не блокирует другой root при свободной host/profile capacity |
| 3 | healthy import не допускает второй process-level `mautic:import` на том же instance |
| 4 | completed import немедленно освобождает slot |
| 5 | paused route не запускает import |
| 6 | cancelled/stopped row снова виден как pending |
| 7 | failed/broken DB row terminalizes без host lock |
| 8 | missing/invalid DB rows не создают phantom worker |
| 9 | long import переживает generic command/worker timeout при DB progress |
| 10 | hours-old import с active DB signal сохраняет lease |
| 11 | CPU/IO или file/log progress обновляет lease до stall threshold |
| 12 | live PID без progress после stall timeout проходит grace/recheck и kill |
| 13 | progress resume до recheck отменяет kill |
| 14 | stale self-marker не продлевает lease |
| 15 | terminal non-zero rc освобождает slot без automatic duplicate retry |
| 16 | restart/adopt живого import сохраняет singleton и heartbeat lease |
| 17 | lost PID/reconcile закрывает ownership и разрешает следующий FIFO import |
| 18 | active import оставляет slots для manual segment/dependency/follow-up |

Runtime/profile contract row дополнительно проверяет, что
`import_stall_timeout_sec` присутствует в config attribute mapping, profile
managed keys и двунаправленной local/MCC runtime reconciliation; отсутствие
ключа или несовпадение default/minimum блокирует acceptance.

Минимальный локальный import gate:

```text
python3 -m unittest tests.test_import_liveness
python3 -m pytest -q tests/test_import_liveness.py tests/test_segment_liveness.py tests/test_campaign_ring_dispatch.py tests/test_segment_admission.py
```

Disposable real-command rows должны использовать штатную Mautic import
команду в M6/M7 runtime и owner-labelled malformed fixtures: malformed CSV /
unclosed quote, invalid bytes or encoding, missing/unreadable upload,
invalid/missing mapping/header, parser/validation failure, hung watchdog и
mid-import process crash. Для каждого row сохраняются exact source SHA/path,
Mautic major, fixture IDs, command, PID/timeline, TaskStore/DB terminal state,
slot timeline и cleanup proof. Приёмка запрещена, если matrix не показывает
`PASS` по обеим major, не подтверждает ровно один active import на instance
(и допустимый parallelism между instances), или переиспользует старый
receipt/source.
