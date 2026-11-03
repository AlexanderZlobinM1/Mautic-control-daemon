# Алгоритм MCD: admission и dispatch сегментов

Этот документ является рабочим каноном для MCD scheduler. Он описывает
фактическую схему после исправления admission и должен обновляться вместе с
кодом, тестами и runtime receipt. Документ не является разрешением на

Для Mautic patch lifecycle канонический порядок фаз внутри upgrade barrier:
`before_campaign_trigger` -> `before_campaign_execution` ->
`before_background_imports`. `before_campaign_trigger` является отдельной
first-class фазой для patch records, которые должны завершиться до передачи
управления trigger-consumer; она не подменяется более широкой фазой
`before_campaign_execution`. Контракт, runtime executor, fixture и receipt
должны рекламировать и проверять один и тот же порядок.

Каждый Mautic patch-plan v3 upgrade завершает lifecycle ровно одним
plan-bound evidence receipt: `success`, `noop` или `error`. При ошибке
target-stage, source/apply check или fixed-gate MCD сохраняет фактически
наблюдённые gates и уже завершённые records, затем отправляет этот receipt в
MCC через существующий `record_mautic_patch_evidence`; stdout marker является
только диагностическим fallback. Для inline-плана MCD по исходному `run_id`
читает уже выданный MCC envelope, проверяет canonical `plan_sha256` и точное
соответствие плана, но никогда не выбирает второй catalog plan и не объявляет
успех без фактической проверки. Повторный запуск использует тот же immutable
plan и остаётся идемпотентным.

Upgrade maintenance fencing is a cross-process admission barrier, not only a
marker file. The upgrade owner acquires the exclusive
`maintenance.dispatch.fence.lock` beside `scheduler.pause` before publishing
the pause flag and holds it through the complete drain and upgrade. Every
scheduler submit, running-row reconciliation, and external-console adoption
must acquire the same fence in shared non-blocking mode and skip admission
when the exclusive owner is present. This closes the check-then-spawn and
restart/adoption race after pause publication.

The maintenance drain repeatedly observes tracked and external tasks, stops
newly appearing work, releases dead tracked rows as `lost` with a bounded
maintenance note, and requires three consecutive empty snapshots before
entering the upgrade. A bounded typed
`mcd-maintenance-guard-evidence-v1` is emitted on every task-quiescence
failure with task identities, stable-snapshot counts, timeout state, and
truthful cron/pause cleanup. Patch evidence is not emitted when the guard
fails before the source/package stage, and no patch rollback is attempted.

Каталоговая преднастройка перед native plugin reload остаётся fail-closed:
MCD не применяет план при `agent_patch_contract_mismatch`,
`agent_patch_contract_fields_mismatch` или `agent_version_below_minimum` и не
подменяет строгую проверку MCC. Такой отказ совместимости не отменяет уже
успешно применённые файлы и сброс кеша: MCD фиксирует предупреждение и
продолжает штатный `mautic:plugin:install`, после чего обычный reload ACK и
post-step могут завершиться. Следующая совместимая reconciliation повторяет
только незавершённую каталоговую remediation; native plugin operation не
переисполняется из-за этого отказа.
ручной `segments:update`, изменение профиля, capacity или данных на хосте.
Актуальная dev-редакция этого канона — MCD 1.6.0 (продолжение текущей MCD-линии,
prepared-target lineage). MCD 1.5.268 сохраняется только как историческая точка
rollback/audit, а не как альтернативная реализация; follow-up после immutable
1.5.88. В этой редакции startup/profile rebase не считается локальной
операторской правкой без durable provenance, а runtime ACK ограничен
запрошенными per-key наблюдениями. Host ACK строится только по ключам
canonical desired envelope; expanded effective snapshot не добавляет ключи в
chunk. Recurring super-priority admission сохраняет резерв для уже admitted
ordinary cycle. Active dedicated super-priority work не вычитается из ordinary
shared capacity: постоянный super-ring не может голодать обычный tail; обычная
priority остаётся частью shared regular accounting.

Plugin inventory is an observed, durable per-instance state, not a destructive
filesystem listing. A complete scan canonically replaces the stored
last-known inventory and records `mcd-plugin-inventory-observation-v1`. A scan
error retains the last-known rows, marks the observation `complete=false` and
`last_known=true`, and carries a typed `scan_error`; MCC must not prune from
that observation. After a successful install, update, remove, purge, or reload,
MCD persists the observation and a single deduplicated pending item before
emitting the terminal receipt. It attempts an immediate normal state push; the
receipt reports `inventory_sync_status=ok`, `pending`, or `error`, with the
inventory digest and observation time. A failed push remains in the durable
`plugin_inventory_sync_pending` runtime key and is retried by the ordinary
state-push loop after errors and restarts. HTTP success ACKs only the exact
instance/digest/observation tuple sent, so a newer local change cannot be
cleared by an older response. The wire envelope is
`mcd-plugin-inventory-sync-v1`; it is additive to `mcd-state-v1` and does not
require Docker or alter native runtime execution.

Commercial scenario failures are terminally observable: after a validated job
has an execution error, MCD creates a bounded, secret-free
`mcd-self-hosted-terminal-receipt-v2` with `status=failed` and explicit
`rollback` before advancing the Portal cursor. If Portal receipt delivery
fails, the exact unsigned receipt payload is stored in the ordered durable
pending queue and retried after restart; exception text,
subprocess output, SSH credentials and DNS credentials are never copied into
the receipt. This is only transport/execution/retry behavior; external Portal
artifacts remain the sole owner of bootstrap, OS, Mautic and scenario policy.

Fail2ban startup safety is an MCC-owned external Debian-family scenario/recipe,
delivered and coordinated through the Portal registry/outbox; it is not an MCD
daemon startup side effect. The daemon no longer imports or calls the legacy
`fail2ban_guard` at `run_loop` entry, and the obsolete MCD module/test boundary
is removed. The external MCC scenario carries its own pinned guard source; it
does not install, start, or own the system Fail2ban service. Portal only
delivers the immutable artifact and coordinates stage/job/ACK/lifecycle
receipts. The Debian-family helper is installed through the ordinary
stage-selected `scenario.apply` path and is enabled from `mcd.service.wants` with
`After=mcd.service fail2ban.service` and `PartOf=mcd.service`. A completed
oneshot can run again after MCD restart. Missing client/filter is a typed
non-blocking skip; guard/reload errors remain typed results with wrapper exit
zero, so MCD, its scheduler, and unrelated tasks continue. The helper uses
the exact selected cached artifact and can execute with the network namespace
unavailable; no package installation, system Fail2ban start, firewall action,
or DNS operation is implied. Native DEV135 evidence for applied/noop,
ordering, restart preservation, offline execution and warning-only failure is
recorded in `fail2ban-native-replacement-proof-20261101.json`; the earlier
private native parity receipt remains the evidence for preserving an unrelated
ban while releasing only the unsafe jail's ban. Legacy double execution is
not replacement evidence.

Super-priority ticks are a dedicated recurring lane: each tick advances its
own static round-robin cycle and is not gated by completion, refill, or fair
turn state of the ordinary lane. Ordinary shared round-robin has the opposite
rule for a locally blocked persisted root: when the shared/instance capacity
snapshot is free but dependency, claim, or closure guards leave no launchable
item, that root completes its ordinary fair turn without a launch; the next
ready root may use the slot. A full-capacity observation is not a skip and
preserves the root-local retry priority. Neither rule bypasses admission,
dependency, claim, closure, or update barriers.

Каждый новый Mautic 7 super-priority tick обязан нативно перестроить каждый
внутренний parent отдельной первой командой; terminal leaves получают
`--exclude` только для parent-ов, уже перестроенных в этом же native
projection. Исторический `successful_coverage` разрешает безопасный handoff
priority только через доказанно непересекающийся native projection и никогда
не подавляет parent свежего super-tick.

При materialization instance rows bare modern empty `segments: []` не очищает
непустой legacy whitelist: IDs переносятся в canonical priority lane. Очистка
допускается только с явным `tombstone: true`.
ACK evidence для modern priority и legacy whitelist использует один
canonical `segments` codec; статус не подменяется при несовпадении desired и
effective.
Priority and super-priority IDs are deduplicated stably; execution order is
semantic and is never normalized by sorting or set comparison.
`pending_full_scan` reconciles before `segment_cycle_sync`, поэтому persisted
ordinary IDs are present in `cycle.items` before claim validation and survive
restart without starvation. Persisted full-scan items bypass only stale
per-entity repeat cadence; capacity, running, dependency, and durable claim
guards remain enforced.

Static dependency ownership is strongest-lane first. A weaker priority lane
waits while an overlapping super-priority closure is pending/running, passes
repeated native `--exclude=<id>` options only for IDs with a durable successful
stronger-lane receipt, and terminalizes its own overlapping item with a
dependency error when the stronger owner exhausts its bounded attempts. A red
stronger owner is never presented as successful native coverage and never
causes an unbounded parent rebuild in the weaker lane.
Qualified instance ACK scopes resolve base-UID effective projections before
canonical lane encoding; missing data is not confused with a populated lane.
Legacy alias ACK reads its stable legacy projection; modern priority ACK reads
the merged canonical priority projection.
restart without starvation.

### Durable stale-task reconciliation

Каждый scheduler tick и команда `scheduler status` являются lifecycle
boundary: MCD читает `TaskStore.running` и проверяет PID existence и полное
command identity. Возраст задачи используется только как evidence в receipt и
alert; сам по себе возраст никогда не является причиной остановки живого
worker. Для segment runtime дополнительно сохраняется PID start identity и
независимый runtime evidence.

Если PID отсутствует, command identity не совпадает, а независимый segment
evidence не подтверждает работу, MCD атомарно переводит только эту durable
строку в `lost`, пишет `mcd-scheduler-reconciliation-receipt-v1` с root,
task key, PID, возрастом и причиной, и удаляет только matching owner из
in-memory registry. Stale admission не удерживается наличием lock-файла:
execution lock проверяется через `flock`, а смерть владельца освобождает
kernel lock; чужой живой lock не удаляется. Удаление одного stale owner не
останавливает другие segment или campaign rings.

После reconciliation dead/stale row не считается capacity, а новый
campaign rebuild/trigger может быть admitted при свободном campaign/host
slot, пока независимый segment worker продолжает работу. Live long-running
worker остаётся running независимо от возраста и переживает daemon restart
через adoption из durable row.

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

При startup/restart MCD сначала сохраняет исходную durable metadata и только
потом нормализует новый локальный snapshot. Разница, возникшая из-за profile
rebase, восстановления config или применения MCC desired state, не получает
новый `source=mcd` event и не становится local-wins push. Такой snapshot
становится baseline без новой revision. Local push разрешён только для
реального изменения с сохранённым non-bootstrap MCD event; повторный natural
poll после этого не создаёт второй push.

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

Если локальный runtime include потерян или обнулён, любой remote winner вне
stable-проекции переводит startup или обычный poll в full-snapshot режим: в
config materialize-ся весь `runtime_remote_allowed_keys()` MCC snapshot,
включая `false`, `0`, пустые структуры и явные tombstone. Такое
materialize-ние не создаёт локальную revision и не отправляет потерю обратно в
MCC. После MCD update этот режим включается всегда: reset локального файла не
считается изменением настроек, а источником истины остаётся полный
сохранённый snapshot MCC. Поэтому восстановление после потери не ограничено
стабильной частью runtime и одинаково работает до и после перезапуска.

Если локальный config-файл изменился во время remote poll/apply, MCD сначала
сохраняет пару `previous/current` в durable `local_runtime_push_pending`, а
remote baseline не продвигает. Проверка выполняется перед fetch, после network
round-trip и непосредственно перед baseline refresh; pending переживает restart
и failover state backend. Только ответ MCC с принятой desired-state `revision`
разрешает сохранить новый local baseline и удалить pending; conflict/error или
неполный ответ оставляют delta для следующего ровно одного rebased push.

После принятого local runtime push MCD не выполняет remote poll в том же
main-loop tick. Принятый ответ уже является canonical результатом записи;
немедленный poll может вернуть lagging/неполный snapshot, снова положить
`previous` в baseline и превратить один delta в бесконечный повторный push.
Startup-флаг и ручной poll-trigger сбрасываются, а следующий poll назначается
по обычному `mcc_push_interval_sec` (с теми же границами 15--300 секунд).
Таким образом, durable pending сохраняется при реальном втором локальном
изменении, а независимые изменения MCC будут приняты следующим штатным poll.

## 1. Область и границы

Планирование выполняется по каждому instance root, но capacity и защита
запуска учитывают общий host. Нельзя бесконечно обслуживать один host или
instance: dispatch использует ротацию instance и общие host slots.

Обычный segment cycle — persisted состояние в `TaskStore.runtime_sync`.
Он переживает restart и содержит уникальный ordered набор элементов,
`claimed`, `running`, `done`, `errors`, bounded `active_meta`, статус цикла и
время admission.
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
собственный lane accounting и execution locks.

`super-priority` для каждой включённой instance — отдельное dedicated worker/ring
вне общего regular host/instance budget. Поэтому при общей capacity `8+3` и
четырёх super-instance общий regular budget остаётся `8+3`, а не уменьшается до
`8+3−4`. Super-ring может работать на каждом minute tick независимо от
обычной очереди; `slots_override` меняет только его собственный per-instance
лимит. Dependency closure, overlap, exact-task lock, durable claim и duplicate
защиты сохраняются и для dedicated lane. Обычная `priority`-работа остаётся в
shared regular accounting.

`super-priority` проверяется чаще `priority`; частота не меняет precedence и
не убивает running job. Если ordinary worker свободен, независимая работа
super-priority может продолжаться параллельно; active super-priority не
уменьшает ordinary capacity, а dependency overlap и execution lock остаются
обязательными.

Native coverage receipt scoped to an ordinary cycle is never used to suppress
a fresh static cycle. Within one static cycle, successful `done` receipts of
the current lane and its stronger static lane are sufficient to exclude a
shared Mautic 7 parent from a sibling leaf command. When the static lane
reconciles a terminal snapshot into its next independent tick, that local
coverage is intentionally discarded, so configured super-priority segments
continue to execute by their own ring cadence instead of running only once per
ordinary cycle.

Static-lane reconciliation is lane-local as well: a completed super-priority
snapshot rolls into its next cycle without waiting for the weaker priority
lane to finish, and a priority lane may do the same independently. A claim or
closure that is still active is preserved in its own lane; independent
cadence never removes overlap, ownership, or durable execution-lock guards.

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

1. По умолчанию (`segment_lazy_update_enabled = false`) планировщик получает
   bounded full snapshot опубликованных сегментов; он удаляет union статических
   `priority`/`super-priority` IDs и строит полный текущий ordinary cycle.
   Канонический per-instance map
   `segment_lazy_update_instance_settings` переопределяет host default по
   immutable instance UID и его допустимым root/name/domain или
   `<uid>@<mcc-host>` alias; при отсутствии строки действует host default.
   Частичная UID-запись мержится транзакционно; пропуск UID или поля не
   означает clear. При удалении последнего локального поля MCD обязан
   отправить явную пустую `instance_desired_states[uid]` с текущей revision,
   и верхнеуровневую `segment_lazy_update_instance_settings={}` при очистке
   последней записи. При удалении только A полная sparse map для остальных
   UID, например B с более новой revision, сохраняется без A; отдельная
   операция очистки всей map всегда выражается явным `{}`.
   Поэтому один host может одновременно иметь `lazy=true`, `false`, `true`
   для трёх разных roots. Явный lazy-флаг instance `true` переключает только
   новый план этого root на due-only SQL и исключает fresh/current remaining
   IDs из этого нового admission. Пустой lazy-plan является штатным результатом и не имеет
   fallback на fresh/current tail. Full mode, напротив, имеет fail-safe fallback:
   если observed published IDs есть, а вторичное snapshot/definition чтение
   вернуло пустой список, observed IDs всё равно материализуются в durable plan,
   чтобы запаздывающее dependency-чтение не стёрло полный цикл. Ни один режим
   не удаляет и не пересобирает уже сохранённые
   `cycle.items`, `follow_up`, claims или running state в TaskStore.
   В mixed host important work всех roots получает precedence до full-mode
   `remaining`; lazy root не получает fresh `remaining`, но его уже assigned
   important работа и frozen active cycle не пересобираются при toggle. Новый
   cycle root-а вычисляет только его новое effective значение и не переносит
   policy на другой root.
   Effective `lazy/full` mode is persisted with the admitted cycle. A UID
   toggle while that cycle is active does not append fresh rows to its
   `follow_up`: the dispatcher continues the already-admitted TaskStore
   cursor, and only the next terminal cycle is admitted under the new mode.
2. Read-only definitions добавляют опубликованные never-built IDs, которых не
   было в due query. Ordinary snapshot строится строго по категориям:
   `never-built → changed → stale`, где stale threshold берётся из выбранного
   host profile (`segment_stale_after_sec`). В full snapshot fresh/current
   published rows не теряются: после трёх due-категорий они дописываются в
   стабильном observed order и становятся частью полного ordinary cycle. В
   lazy snapshot они остаются discovery-only и не становятся новым admission.
   После первого полностью завершённого цикла due-классификация определяется
   относительно persisted bounded source fingerprint предыдущего terminal
   ordinary cycle, а не только относительно `last_built_date` текущей строки.
   Если built segment отсутствует в этом baseline либо baseline пустой/
   повреждён, действует fail-open bootstrap: такой segment считается `changed`
   и попадает во вторую категорию (после never-built) в ближайшее новое
   ordinary ring, не ожидая stale threshold, кроме static lanes.
   Внутри durable cycle сохраняются две явные группы: `important` (never-built,
   changed/unknown, retry и stale) и `remaining` (только fresh/current tail
   full-mode). Lazy mode исключает `remaining`. Dispatch сначала назначает
   готовую неназначенную `important`-работу всех ready roots; уже claimed или
   running important не образует host-wide барьер. Remaining может начаться,
   когда вся ready important уже назначена, даже если часть important ещё
   выполняется. Это не terminal barrier и не сброс cycle.
   Важной считается только admission-ready работа: pending important root с
   заполненным instance cap или временно не принадлежащий его RR admission
   не удерживает свободный slot другого root; после освобождения capacity
   precedence этого root восстанавливается.
   Внутри каждой категории сохраняется FIFO стабильного observed order. Weight,
   recent activity, numeric ID, stale promotion и threshold не могут
   переставлять ordinary.
3. IDs и dependency edges дедуплицируются. На Mautic ≤6 closure превращается в
   одну topological последовательность на один worker/slot: родители
   выполняются перед детьми внутри одного chain command, а все IDs closure
   одновременно имеют durable `claimed/running` state и один TaskStore
   running row/PID. Поэтому fork/diamond с общим родителем не порождает
   отдельные процессы на каждый `-i`; независимые connected closures получают
   отдельные workers. Если parent уже terminal/running в предыдущей admission,
   child остаётся отдельным pending item и запускается после dependency guard.
   На Mautic 7 MCD даёт connected overlapping roots одну durable closure
   ownership с одним TaskStore running row/PID до terminal всех IDs, но не
   считает leaf-only native cascade доказательством rebuild родителей:
   ordinary projection запускает каждый internal parent и native leaf ровно
   один раз в topological order, а последующие команды получают повторяемый
   `--exclude=<already-executed-parent>` (не CSV). Independent connected
   closures получают отдельные workers; successful coverage содержит только
   реально запущенные command IDs. Capability
   `mautic:segments:update --format=json --no-interaction` читается read-only и
   должна подтвердить `--exclude` с `is_multiple=true`. Уже покрытый root
   закрывается terminal receipt без нового процесса, а failed/timeout root не
   добавляет coverage. Если capability явно не подтверждена, пересечение
   fail-closed ждёт свежего read-only probe, а не повторяет parent молча.
   `mcd-segment-dispatch-context-v1` хранит признак authoritative dependency
   snapshot и generation. После restart/update active cycle со старым или
   пустым context не трактует graph как пустой: root-local refill перечитывает
   опубликованные definitions того же Mautic-root и capability, сохраняет
   новый context только после успешного read-only чтения и не меняет
   `cycle.items`, cursor, claims, running или terminal receipts.
   Все медленные DB/help/capability reads завершаются до финального merge:
   перед записью MCD под существующим `TaskStore` lock ещё раз читает текущие
   cycle/context, сравнивает generation и мержит только graph/capability
   metadata. Новая policy (pause, priority, throttle, slot limit и lane
   state) не восстанавливается из старого snapshot; при смене generation
   refresh отбрасывается и ставится bounded retry.
   Planner сохраняет graph и provenance read как один snapshot: ветка без
   успешного same-root definition read переиспользует graph и timestamp/count
   предыдущего того же cycle, а успешный read с нулём edges может честно
   записать пустой graph для независимого root.
   `dependency_snapshot_verified` отличает такой подтверждённый empty snapshot
   от legacy empty graph (включая `preserved_previous`, записанный старой
   версией): legacy-вариант один раз перечитывается root-local до refill, после
   успешного read не запускает бесконечный rehydrate.
   Недоступный/неполный graph удерживает только этот root до retry; другие
   ready roots продолжают dispatch в своих profile slots.
4. Новый/never-built ID не ждёт нового frozen snapshot. Если он найден
   уже после admission active ordinary cycle, `cycle.items` и его cursor не
   меняются: ID атомарно добавляется в durable FIFO `follow_up`. Follow-up
   конкурирует за следующий свободный ordinary worker и выбирается раньше
   следующего pending ID frozen cycle; он не создаёт duplicate и не меняет
   порядок исходного `cycle.items`. Позднее изменение уже существующего
   built-сегмента в active cycle не добавляется в этот FIFO: оно попадёт в
   следующий terminal snapshot, иначе один редактируемый сегмент мог бы
   бесконечно переставлять/расширять текущую очередь. Уже claimed/running и
   terminal элементы не двигаются. Follow-up state переживает restart, а
   terminal success/error, retry и dead-letter остаются привязаны к его
   текущему generation.
   После `segment_cycle_sync` dispatch строит projection из durable
   `follow_up` FIFO, затем pending `cycle.items`, исключая
   `done/errors/claimed/running`, static и SQL-managed IDs. Наблюдённый, но
   удержанный campaign/admission pressure кандидат не попадает в follow-up и
   claim path до атомарной записи; он остаётся кандидатом следующего
   планирования, а не orphan ring entry. После restart порядок обеих очередей
   сохраняется.
5. Эвристика `segment_filter_safety` (например, `today -1` или `now -N`)
   является только diagnostic hint: она не исключает segment из ordinary
   ring, не ставит его terminal до запуска и не подменяет фактический rc
   profile-bound Mautic-команды. Обычный отказ Mautic проходит честный
   `TaskStore` start/retry и красный terminal receipt, после чего healthy tail
   продолжает цикл. Исключение только для доказуемо небезопасной topology:
   `dependency_cycle`/`self_reference` и их опубликованные descendants уже
   после durable admission получают явный terminal error с note
   `data_error:<code>`, поэтому root не остаётся навечно `blocked/pending`;
   это data-error задачи, а не MCD self-update blocker. Failure cooldown и
   DB/backup/scheduler pause являются временными admission gates и не должны
   пересортировать или потерять persisted cycle.
   Campaign pressure может удержать только новую admission; уже pending
   durable cycle продолжает dispatch при доступном slot. Global scheduler/
   backup pause, cluster cron-route mismatch и root-local DB circuit-breaker
   являются отдельными явными stop-gates и должны иметь reason в runtime
   receipt; они не должны маскироваться пустым ordinary ring.
6. Release packaging and self-update use a detached runtime payload. The
   production archive contains the installed `mcd_agent` runtime package,
   resources, requirements, wheel and `mcd-runtime-manifest.json`, but no
   checkout docs/tests/build files or `.git` data. The source checkout is
   retained only as bounded `src`/`src.next` staging or rollback material;
   after runtime import/smoke, systemd verification and adoption proof it is
   not an active production runtime or duplicate test tree. The manifest binds
   payload hashes while an external receipt binds archive/source/wheel
   identity, avoiding self-referential provenance. See
   `docs/mcd-update-invariants-v1.md` for the release evidence contract. A
   host running the pre-runtime-only updater transitions through the
   separately transported `tools/mcd_runtime_bootstrap.py` bridge and its
   read-only `tools/mcd_profile_authority_probe.py` sidecar: the bridge
   verifies the exact archive/manifest, uses the same update lease and
   scheduler barrier, installs and imports the wheel before the atomic source
   switch, retains bounded rollback material, and records a bootstrap receipt.
   The bridge is delivered as the detached HTTPS artifact
   `mcd-runtime-<version>.bridge[-rN].py`; `mcd-runtime-bridge-package-v1` binds
   its SHA-256, source commit, runtime archive SHA, runtime manifest SHA, and
   bridge-QA sidecar SHA. The host accepts it only after exact SHA checks and
   runs the bridge locally; the runtime archive remains unchanged.
   An already runtime-only DEV host may replace a build with the same semantic
   version only through the explicit `--allow-same-version-rebuild` path. That
   path requires exact target, archive and manifest pins, requires the incoming
   manifest to differ from the active manifest, uses the same lease/barrier,
   atomic runtime swap and rollback, and emits
   `runtime-to-runtime-pinned-rebuild` in the receipt. It is not a channel or
   implicit production update rule; without the explicit flag every equal or
   older runtime remains rejected.
   The sidecar must accept the complete valid split TOML graph, including
   array-of-table sections, while never writing configuration.
   The MCD runtime always supplies an MCD-owned default
   `runtime.result_signing_key_path` (`/opt/mcd/var/keys/result-signing.key`)
   when the operator has not overridden it. The first state push creates the
   Ed25519 key with mode `0600`; MCC must acknowledge the public key before
   any signed control terminal receipt can execute.
   The `mcd-state-v1` payload also always carries a typed, non-empty
   `agent_id`: an explicit `[mcc].agent_id` is authoritative; otherwise MCD
   derives a stable `mcd-agent-<sha256>` identity from the canonical MCC host
   name (or local hostname in standalone mode). The value is stable across
   restart and is sent together with the host fingerprint and result key.
   MCD unwraps the MCC state response before processing its control envelope
   and persists a matching key ACK even when the response carries no control
   commands; command-envelope validation remains independent and invalid
   commands are never executed because an ACK was received.
  A valid command received in a successful state response is ingested and
  executed in the same state-push cycle after the response is parsed; the
  resulting signed terminal receipt is marked for the next state push instead
  of waiting for the normal 300-second heartbeat. This preserves the
  accepted-to-terminal lifecycle without shortening command TTLs or creating
  duplicate jobs.
  Docker preflight failures are typed at the identity and fixed argv
  boundaries: a missing or malformed `runtime_image_digest` (including an
  MCC `image_digest` artifact/files digest) is rejected before Docker with a
  bounded `error_code`, `stage=identity`, empty `argv` and redacted `detail`;
  image availability, Docker access, timeout, PHP CLI/FPM observation, exit
  code, bounded redacted stderr, argv, stage and cleanup state are retained in
  the terminal receipt. PHP-FPM observation tries only a fixed allowlist of
  unversioned and versioned `/usr/sbin`/`/usr/bin` binaries, so images exposing
  `/usr/sbin/php-fpm8.4` are supported without shell, `find` or arbitrary argv;
  stale `Created` containers matching the MCD preflight name prefix are
  reconciled with fixed `docker ps`/`docker rm` argv before a new probe, and
  the finally path marks transient-container cleanup `clean` or `failed` in
  the receipt. An exhausted allowlist is the typed
  `docker_preflight_php_fpm_binary_not_found` result. A missing digest image is
  therefore an actionable
  `docker_preflight_image_unavailable` evidence result, not an opaque
  `RuntimeError` or `ValueError`; no pull, arbitrary command or host mutation
  is attempted.
  The normal `mcd-state-v1` payload also exposes a signed generic
  `mcd-docker-runtime-image-inventory-v1`: each local image carries its
  repository tag, fixed `image_ref@runtime_image_digest`, Docker `image_id`,
  actual RepoDigests, bounded semantic labels and normalized `files_sha256`,
  bound to `agent_id` and `host_fingerprint`. The inventory is read-only and
  never pulls or stages images. `docker_runtime_preflight_v1` accepts only
  this explicit `runtime_image_digest`; an MCC artifact-pair/files digest is
  not a runtime image identity.
  Consumers validate the observed timestamp/freshness window, signature,
  agent/host binding, inventory hash and `fixed_runtime_ref`↔digest equality;
  ambiguous or tampered rows are rejected before any runtime decision.
  If MCC rejects a previously terminal local control receipt with the exact
  immutable-terminal 409, MCD records a local
  `mcd-control-conflict-reconciliation-v1` audit containing the original
  receipt, its SHA-256 and the bounded conflict reason/remote-response hash.
  That one `conflict_acknowledged` row is omitted from subsequent outbound
  `control_receipts`; no SQLite row is deleted, no MCC row is changed, and
  other jobs remain eligible. Reconciliation requires a machine-readable MCC
  envelope with `code=immutable_terminal_conflict` and an exact `job_id`
  (including MCC's canonical nested `detail` object); a missing or mismatched
  identifier remains untouched. An unrelated 409 is never treated as this
  reconciliation. A reconciled local receipt is terminal audit state and is
  excluded from execution even if MCC replays the same immutable command
  envelope.
  The safe semantic label set preserves the `com.sales-snap.mauticrfp.*`
  image selector/runtime metadata and OCI labels while excluding
  secret-shaped keys.
   Self-update сначала публикует persisted `scheduler.pause` под общим
   межпроцессным `scheduler.pause.lock`. Каждый новый запуск, включая
   ordinary, priority, super-priority, manual, import и retry, перед
   фактическим `spawn` получает тот же lock и повторно проверяет barrier.
   Поэтому после момента публикации barrier новые MCD-managed процессы не
   появляются; уже запущенные обычные Mautic/PHP процессы продолжают работу
   независимо от update и не являются условием его завершения. Supervisor
   restart сохраняет их через `KillMode=process`, а startup re-adopts их из
   TaskStore. Update повторяет попытку только для явных MCD-owned barriers
   (backup/source/update/migration), а не ждёт завершения app process. Barrier
   переживает scheduler tick и restart. Новый update barrier обязан сохранять
   operation/PID/PID-start identity; после успешного или неуспешного terminal
   apply он снимается штатной cleanup-операцией. После crash legacy или
   orphaned `mcd-update-admission` flag снимается только если owner не жив и
   source/update lease свободен; `paused` и прочие manual/maintenance flags
   не трогаются recovery-кодом.
7. Обязательная и неотменяемая граница self-update: живой обычный PHP/Mautic console process
   (`segments`, `campaigns`, `import` и любой другой application command) не
   является blocker-ом. Он должен пережить restart/reinstall MCD, сохранить
   PID/start identity, а после старта supervisor быть переусыновлён из
   durable TaskStore без duplicate spawn. Все durable очереди и оперативное
   состояние (`TaskStore`, `runtime_sync`, ordinary/static/import queues,
   claims, running/follow-up, retry и monitor projections) должны сохраниться
   без сброса и быть подхвачены после обновления. Нельзя превращать список
   process names или длительность Mautic-задачи в update gate.
8. Blocker-ами являются только MCD-owned critical operations: active backup
   lock, source/update lease для deployment/self-update и MCD-owned migration,
   а также любой новый critical operation только после явного добавления его
   lock/barrier в этот контракт. Внутренние locks должны fail-closed и иметь
   durable retry/status reason; отсутствие такого lock — дефект реализации,
   а не повод останавливать обычные Mautic PID. Список не ограничен тремя
   примерами, но расширяется только через explicit MCD lock/barrier.
   Это правило проверяется source, behavioral-тестами, runtime-артефактом и
   target receipt; любое расхождение блокирует acceptance.
9. `runtime.mcd_update_defer_during_campaigns` сохраняется только для
   backward-compatible чтения конфигурации и не меняет admission. Это правило
   проверяется одновременно source, tests, systemd unit и runtime receipt.
10. После полного terminal ordinary cycle следующий snapshot создаёт новый
   cycle только для этого root. Пока цикл не terminal, его порядок не
   пересобирается; unfinished cycle одного root не задерживает следующий
   terminal/nearest-tick cycle другого root. Pending `remaining`, claims,
   running и retry сохраняются durable до следующего свободного slot.
11. Cycle хранит отдельно `observed_snapshot`/generation и применённый
   `baseline`/generation. Наблюдения во время active cycle не продвигают
   baseline; он фиксируется только при переходе в terminal. Успешные элементы
   продвигают свои fingerprints, а terminal-error элементы сохраняют старый
   fingerprint (или отсутствие записи) и retry-ятся в следующем cycle, даже
   если их source timestamp не изменился. Partial/error до terminal не могут
   создать новый completed-cycle baseline; state переживает restart.
12. Тяжёлый full inventory/DB/filter planner не имеет права удерживать
   dispatch для root, пока у этого root существует durable ordinary cycle с
   `pending`, `claimed` или `running` работой. Для него dispatcher продолжает
   брать уже admitted IDs из `TaskStore.runtime_sync`, а root-local full plan
   откладывается до terminal cycle; другие roots продолжают независимое
   планирование. Ограниченный due-only discovery (не более bounded batch)
   может добавить поздние IDs в durable `follow_up`, но не сбрасывает cursor,
   claims или FIFO. Каждый такой проход записывает
   `mcd-segment-pending-guard-v1` с `eligible_pending_count`,
   `projected_pending_count`, blocked IDs/reason, SQL/static exclusions,
   DB/pause reasons, absolute limit, free launch budget и фактическим
   `launched` count. Этот receipt является обязательным доказательством
   того, почему pending был запущен или удержан, и должен обновляться после
   restart/update вместе с остальным runtime state. Для исполнения worker
   читает отдельный полный `mcd-segment-dispatch-context-v1`: display-receipt
   guard может ограничивать списки до 100 элементов, authoritative context —
   нет. Непрерывный persisted refill worker обслуживает каждый root
   независимо, использует тот же monitor/claim/spawn/terminal lifecycle и
   не уступает main dispatcher: пока он планирует тот же root, уже admitted
   pending item может быть запущен тем же canonical claim/submit lifecycle;
   slow root не останавливает другие roots, а global all-roots barrier
   запрещён. Main
   dispatcher помечает busy только текущий root через
   `_iter_instances_with_root_dispatch_gate`; canonical lifecycle monitor и
   persisted refill запускаются root-local slices через отдельные executors,
   а `_SCHEDULER_RUNTIME_LOCK`/`_SchedulerRuntimeRegistry` сериализуют только
   claim-to-spawn и общий running registry, не planning/DB работу. Refill не
   превращает `main_dispatch_busy_roots` или `lifecycle_monitor_busy_roots` в
   root-wide запрет исполнения: он продолжает admitted queue через общий
   claim/submit boundary. Capacity перед claim сверяется с live TaskStore
   rows и PID/process identity; live durable row учитывается даже после
   restart без in-memory registry, а dead/stale row не занимает реальный
   slot. Это read-only reconciliation и не освобождает claim самовольно —
   terminal transition остаётся обязанностью canonical monitor. Terminal
   callback немедленно будит refill и ставит только освободивший root в
   одноразовый fair hand-off; rotating cursor отдаёт следующий slot другому
   root после этого хода, поэтому повторные terminal events одного root не
   создают hot-root loop/starvation. Slow probe одного root не может
   удерживать terminal/adoption/refill другого. Если static dependency graph
   конкретного root недоступен, сохраняются его static queues/claims, но
   static admission этого root получает `0` launch; ordinary work этого root
   и независимые roots продолжают dispatch.

Статические queues строятся отдельно: общие dependencies двух линий принадлежат
линии с более высоким precedence и выполняются ровно один раз. Например,
`super-priority 58` и `priority 59` с общим parent 57 дают единый plan `57,
58` в super и `59` в priority на Mautic ≤6. Для Mautic 7 persisted static
ownership также содержит полный ancestor closure: при `super=[58,59]` parent
57 исключается из ordinary/priority, но command plan остаётся `[58,59]` как
native leaf roots. Parent 57 не создаётся отдельным MCD process/TaskStore
worker; его terminal coverage фиксируется только фактическим результатом
super closure. Если parent был уже frozen/pending в ordinary cycle до
обновления графа, static terminal callback reconciles эту durable assignment
по фактическому success/error receipt — без synthetic-done и без вечного
pending исключённого ID. Source/CLI и TaskStore receipt обязаны фиксировать
deduplicated root plan, union ownership closure и native command roots.
Пересекающиеся native closures (например sibling `58`/`59` через parent `57`)
получают одну execution ownership/slot на всю closure и выполняются
последовательными native commands с deduplicated parent coverage; независимые
closures сохраняют параллелизм.
Это же правило действует для третьей линии: ordinary является слабее обеих
static lanes. Ordinary item, зависящий от pending/running higher owner, ждёт
только эту зависимость, а независимый ordinary item продолжает dispatch.
Successful `super-priority`/`priority` receipts входят в ordinary native
coverage только своими фактически запущенными command IDs и передаются как
`--exclude=<parent>`; bounded higher error
terminalizes пересекающийся ordinary item с `static_dependency_failed`, не
создавая четвёртый parent start и не оставляя current cycle pending.

Static closure attempts используют ту же effective inclusive
`segment_retry_count` policy профиля: `3` означает максимум три реальных
Mautic starts для текущей closure assignment. `flock` rc=75 не является
Mautic start и не расходует попытку; он только освобождает claim. Ненулевой
Mautic result до exhaustion освобождает claim для следующего запуска, а
переход в `errors`/красный terminal receipt происходит только на последней
попытке. Успешный closure callback закрывает только те IDs, которые были
фактически assigned в захваченном ordinary cycle generation; static-only
members и новый cycle старым callback не переписываются.

### Reconcile persisted static claims after restart/update

Каждый startup и scheduler tick перед dispatch сверяет `claimed`/`running`
элементы статической линии с независимыми признаками исполнения: активной
задачей соответствующего `priority_executor`, non-terminal строкой TaskStore
и read-only process/DB evidence сегментного runtime. Доказанная живая работа
adopt-ится и остаётся в своей lane; marker-only snapshot не считается живым.

Для каждого активного элемента сохраняется `active_meta` с `claimed_at`,
`running_at`, последним независимо подтверждённым heartbeat и
`orphan_since`. Если evidence исчезло, claim не сбрасывается мгновенно:
сохраняется bounded orphan grace (60 секунд по умолчанию), anchored к
последнему независимому marker/start. Повторное чтение persisted marker или
его `heartbeat_at` не продлевает grace. По истечении grace scheduler под
static-lane lock атомарно удаляет ID только из `claimed`/`running`, оставляя
текущий порядок `items`, `done`, `errors` и lane closure без cycle reset,
reorder или повторного выполнения. Следующий dispatch в том же tick может
сразу claim/start возвращённый pending item; до expiry claim блокирует
duplicate.

## 3.1 Кампании: rebuild и trigger

Кампания планируется отдельно для каждого Mautic-root. Для `campaign_trigger`
и `campaign_rebuild` существуют независимые пары колец
`campaign_trigger_prio_rings`/`campaign_trigger_reg_rings` и
`campaign_rebuild_prio_rings`/`campaign_rebuild_reg_rings`. Они
reconcile-ятся без потери стабильного порядка через
`_reconcile_campaign_rings`; `campaign_update` является синонимом
`campaign_rebuild` и не получает отдельный третий проход. В `ring_mode =
"single"` обе пары сворачиваются в один regular ring.

Разделение на priority и regular строится из weight cache, campaign
whitelist (`campaign_whitelist` и
`campaign_whitelist_instance_settings`), `campaign_priority_size` и
`campaign_latest_priority_count`. Due-trigger IDs принудительно поднимаются
в начало priority через `_force_due_campaigns_to_priority`; периодический
audit добавляет опубликованные кампании обратно в trigger plan, чтобы узкий
due SQL не терял новый контакт. Поэтому rebuild и trigger имеют собственные
FIFO/round-robin кольца, но для одной campaign их порядок не произволен:
trigger ждёт running или ещё не завершённый после планирования rebuild, а
rebuild ждёт active trigger той же campaign. Успешный rebuild оставляет
`_CAMPAIGN_REBUILD_FINISHED_AT`, после чего соответствующий trigger может
пройти; завершённый trigger очищает этот маркер.

Профиль задаёт отдельные limits: `campaign_trigger_priority_parallel`,
`campaign_trigger_regular_parallel`, `campaign_rebuild_priority_parallel`,
`campaign_rebuild_regular_parallel`, а для синонима update —
`campaign_update_priority_parallel` и `campaign_update_regular_parallel`.
Базовая campaign lane использует `campaign_priority_parallel` и
`campaign_regular_parallel`.
На каждом tick `_campaign_lane_limits`
ограничивает priority/regular и общий лимит текущей lane. Если задан
`campaign_total_parallel`, он дополнительно ограничивает суммарную
campaign-ёмкость root; иначе effective campaign cap вычисляется как максимум
из сумм priority/regular для базовой campaign lane, trigger, rebuild и
update. Rebuild-liveness сканирует due с
`campaign_rebuild_poll_interval_sec`, ограниченным реализацией диапазоном
30--300 секунд, и применяет `campaign_rebuild_min_repeat_sec`. Trigger
использует максимальный из `campaign_trigger_min_repeat_sec` и
`campaign_trigger_audit_interval_sec`; при активном профиле и наличии trigger
slots audit policy нормализуется к 60 секундам.
`campaign_trigger_due_guard_enabled` проверяет фактический DB
progress/future wake, а `campaign_trigger_progress_watchdog_enabled` с
`campaign_trigger_progress_watchdog_grace_sec`,
`campaign_trigger_progress_watchdog_interval_sec` и
`campaign_trigger_progress_watchdog_stable_checks` ограничивает зависший
trigger по независимому progress evidence.

Координация с сегментами выполняется через root-local dispatch guard и общий
submit/admission boundary: `_iter_instances_with_root_dispatch_gate` помечает
busy только текущий root, `_SCHEDULER_RUNTIME_LOCK` сериализует claim-to-spawn,
а `_submit_if_slot`/`_PriorityTaskExecutor.launch` повторно проверяют
`running`, durable TaskStore rows, host/instance slots, repeat guards,
`scheduler_admission_lock` и update barrier. Для exact campaign task
`_task_execution_lock_key` использует общий ключ `root|campaign|id`, а
`_campaign_dispatch_begin/end` не допускает пересечения exact campaign worker
с native fallback. Priority/liveness campaign task освобождён от ordinary
segment RR-turn (`_SCHEDULER_SEGMENT_RR_CAMPAIGN_EXEMPT_TASK_TYPES`), но не от
host/instance/campaign capacity, duplicate и execution locks.

Это не жёсткий безусловный root lock. Guard управляет числом доступных
«касс» по profile limits, `campaign_total_parallel`,
`scheduler_host_max_parallel`, `scheduler_instance_max_parallel` и текущей
live/durable занятости. Зависший или занятый trigger может занять только свою
разрешённую campaign-ёмкость; он не должен обнулять свободный segment budget
и не должен останавливать всю root queue. Уже admitted durable segment cycle
продолжает dispatch при доступном segment slot; новая campaign admission может
быть отложена, но это не является global stop для безопасных сегментов.

Для segment topology правило узкое: только уже admitted pending items с
явным unsafe-topology note передаются в `_terminalize_segment_data_errors`;
они получают terminal note вида `data_error:<code>` (например,
`data_error:logical_issue`). Это не общий root-stop и не MCD update blocker:
безопасные eligible items должны продолжить очередь, а
dependency/data-error terminalization не должна останавливать остальные
элементы.

**Наблюдение acceptance, отдельное от нормативного алгоритма.** Было
зафиксировано расхождение: общий guard фактически остановил segments при
зависшем trigger; затем после запуска одного segment оставшиеся 31 элемента
получили `data_error`/unsafe-topology, и очередь снова не продолжилась. Это
evidence дефекта относительно правил выше, а не разрешение исправлять его в
рамках этой документальной правки.

## 4. Claim, worker и slots

### Явные capacity caps и fairness

`scheduler_host_max_parallel > 0` — жёсткий shared regular host-wide ceiling.
В его фактическую занятость входят ordinary, обычная priority, import,
campaign и SQL, включая implicit baseline; `super-priority` в этот shared
счётчик не входит, потому что имеет dedicated budget. Поэтому baseline и
super-ring не превращают regular host limit в сумму per-root лимитов.
`scheduler_instance_max_parallel > 0` аналогично является hard shared regular
ceiling конкретного root; dedicated super-instance worker учитывается только в
своём super lane limit. Значение `0` означает отсутствие именно этого явного
cap; только в этом режиме действует implicit baseline lane профиля.

Shared regular admission скользит по ready roots, а не заполняет один root до
его локального лимита: при 40 ready roots и 12 workers первые 12 назначений
получают 12 разных roots, затем освободившийся slot идёт следующему root в
durable round-robin. Если ready roots меньше workers, несколько независимых
workers одного root разрешены в пределах shared instance/profile cap. Long-live
root не задерживает другие roots; root с завершённым cycle строит только свой
следующий cycle на ближайшем tick. Fairness rotation никогда не создаёт слот
сверх hard cap. Профильный fairness-watchdog является только recovery-сигналом
(включая исторический 300-секундный default), а не обычным порядком выдачи:
ready persisted root должен получить следующий свободный slot через
canonical served-root round даже до watchdog. Неудачный/full-cap проход не
считает root обслуженным, если не было фактического claim/submit. Это
проверяется unit-сценарием 40/12, сценарием roots<workers,
super `4 outside 11`, closure overlap и disposable M6/M7 receipt.

`important` имеет precedence только там, где его root реально может получить
admission. Если у root A уже занят explicit instance cap (или текущий
round-robin ownership не даёт ему admission), его pending important не
превращается в глобальный barrier для fresh `remaining` root B: свободный
host-slot может обслужить B. Когда slot A освобождается, его important снова
становится первой eligible работой. Это не tier bypass: dependency/claim,
host/instance cap, fairness, pause и MCD critical locks по-прежнему проверяются
на фактическом submit.

Для admission используется единый `_scheduler_actual_work_items`: он
канонизирует capacity key по `root/lane/entity`, объединяет обычные rows,
adopted/static/native priority workers и SQL workers, а wrapper/повторную
проекцию одной логической работы дедуплицирует. Для внешних Mautic console
процессов canonical key равен `root/task_type/entity_id`, без PID; PID только
наблюдаемая runtime identity, поэтому смена `flock/sudo/php` wrapper не создаёт
второй slot. Если одновременно видны несколько процессов одного logical key,
в receipt сохраняется самый старший по elapsed time представитель, без
возрастного kill легитимной долгой работы. Dedicated super workers
учитываются отдельным lane counter и не вычитаются из shared regular cap.
Остальные priority reservations записываются под тем же
`_SCHEDULER_RUNTIME_LOCK`, что и обычный submit, поэтому concurrent ordinary и
priority launch не могут вместе пересечь explicit cap; независимые lane
budgets при этом не сливаются. Когда regular ring содержит eligible pending
work, ordinary dispatch резервирует её профильное число slots и ограничивает
priority lane остатком instance/host budget; если regular ring пуст, priority
может временно занять неиспользованную regular capacity. Это не меняет
whitelist policy и не отменяет priority precedence на реально eligible работе.

Adopted external segment worker проходит тот же safe liveness contract: MCD
проверяет PID, process-group, command identity, read-only SQL query fingerprint
и независимые CLI CPU/IO counters. `segment_sql_orphan_after_sec` задаёт
подтверждённое окно отсутствия progress; после него выполняется recheck grace,
и только при сохранении matching process-group без progress выполняется group
kill. Долгий rebuild с меняющимся SQL/CLI evidence сохраняется. Stale release
пишет reconciliation receipt, освобождает logical admission slot и не отмечает
segment `done`; поэтому следующий scheduler tick может безопасно re-admit item.

### Жизненный цикл import и владение сегментами

Владение импортом определяется признаками жизненного цикла, а не только
числовым status. Для Mautic 4–7 непустой `date_ended` — terminal evidence,
который перекрывает устаревший `queued` или `running` status. Failed, paused,
cancelled и completed imports остаются видимыми в lane `done`, но их target
segments не попадают в `active_import_target_ids` и не блокируют ordinary
segment или import follow-up. Monitor по-прежнему считает непустой
`date_ended` terminal evidence, но admission не может использовать этот
признак как единственный критерий: Mautic выставляет `date_ended` и на
partial batch с незавершённой позицией. Configured pending SQL требует
launchable status и `line < line_count`, а строки с `date_ended IS NULL` либо
с `date_ended >= import_pending_cutoff_utc` допускаются к pending count. Cutoff
один раз сохраняется per-root в `runtime_sync` под schema
`mcd-import-pending-baseline-v1`; поэтому старый pre-upgrade partial не
переигрывается после установки, а новый partial после baseline не теряется.
Если профильный configured count положителен, временно пустой status
cross-check не имеет права обнулить durable pending/follow-up состояние — он
остаётся диагностическим сигналом до следующего monitor pass.
Сам pending count запускает только import worker и не доказывает, что segment
DB уже изменилась; segment full-scan разрешён только для подтверждённого
successful terminal completion follow-up.

Только импорт со статусом `queued` или `running` с незавершённой position может
зарезервировать явно указанные в `defaults.list` segment targets. Непустой
`date_ended` сам по себе не отменяет admission post-baseline partial batch;
terminal monitor evidence и admission count остаются разными проекциями. Это
сохраняет FIFO среди eligible imports и позволяет исторической строке,
например со status `1`, освободить свой singleton после terminal outcome. Retry
и recovery остаются в существующем import recovery path и используют
независимые признаки живого процесса.

### Runtime override ACK truthfulness

Runtime ACK — это наблюдение по каждому ключу, а не эхо запрошенного payload.
Каждый ключ сообщает `status` (`applied`, `profile_locked`, `rejected` или
`unchanged`), `desired`, `effective`, `source`, `revision` и явную причину.
ACK содержит только запрошенные keys; `effective_runtime` присутствует
только как per-key readback с теми же keys и не является whole-host snapshot,
поэтому размер остаётся в пределах лимита MCC. Пустой instance desired state
получает `unverified` ACK без runtime dump.
Для host ACK MCD берёт запрошенные значения из canonical
`desired_state.runtime_overrides`, если этот envelope присутствует в ответе
MCC; top-level `runtime_overrides` не является заменой desired map, потому что
может содержать уже materialized effective values. Поэтому, например,
`desired.plugin_operations = {}` остаётся `{}` в evidence, даже если live
effective `plugin_operations` содержит большой structured payload; digest/bytes
вычисляются отдельно для каждого из этих двух значений.
При частичном или instance-scoped runtime response пустой
`plugin_operations` не считается командой удалить catalog: MCD сохраняет
последний принятый непустой catalog, иначе сохранённые per-instance настройки
(`enabled` и `interval_sec`) молча перестанут планироваться. Очистить catalog
может только явно authoritative full snapshot или host tombstone; после
восстановления catalog scheduler снова применяет обычные
bundle-registration, slot и resource guards.
После смены версии/источника MCD первый запуск помечается как post-update
recovery. Пропавший или обнулённый mutable runtime include в этом запуске и
при обычном восстановлении после сброса не является изменением оператора: MCD
восстанавливает полный durable runtime snapshot и свежий MCC snapshot,
причём восстанавливаются все MCC-allowed host- и instance-runtime keys, а не
только scheduler/plugin subset; false/zero остаются валидными значениями,
пустой контейнер считается потерянной projection-частью, если нет явного
локального tombstone. Явный MCC tombstone имеет приоритет. Этот полный snapshot
материализуется обратно в mutable include до перезагрузки конфига и старта
scheduler, поэтому потеря части файла не может выглядеть как намеренное
удаление настроек.
Этот режим включается до первого MCC poll: сброшенный локальный projection не
может получить свежий `mcd` timestamp и победить canonical MCC snapshot.
Реальное локальное изменение MCD по-прежнему фиксируется своим
timestamp/event и проходит через тот же LWW: более новый источник становится
истиной и синхронизируется в обратную сторону.
Runtime-only release gate также проверяет обязательный import closure
(`daemon.py`, `state_push.py`, `mcd_control.py`); архив без любого из них не
может пройти bridge QA и не допускается до host switch.
Если MCC bootstrap после обновления временно отложен, MCD всё равно сначала
материализует полный durable snapshot, затем перечитывает include в память;
scheduler не стартует с пустой или частичной post-update projection.
Если отдельное `desired` или `effective` значение само превышает safety
budget, оно заменяется строго проверяемой ссылкой
`{encoding:"canonical-json-sha256",sha256,bytes}`; key не отбрасывается.
Для profile-locked workload keys выбранный profile остаётся authority,
а ACK сообщает effective profile value; такие ключи никогда не помечаются как
применённые из MCC. `jobs_max_workers` и другие hot-effective keys читаются из
effective runtime map. Если scope содержит больше 128 запрошенных keys или
ограниченный ACK превысил бы лимит MCC по размеру, MCD выдаёт детерминированные
`mcd-runtime-key-observation-chunk-v1` parts со стабильным batch ID и после
restart продолжает только отсутствующие parts. Верхнеуровневый ACK получает
`applied` только когда effective стали все запрошенные keys; расхождение
profile или rejection остаётся видимым MCC.

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

Static-lane runtime marker пишется на actual spawn/terminal callback. Он
помогает привязать grace к независимому старту, но сам по себе не удерживает
claim. Поэтому restart с живым executor/process принимает существующую работу,
а restart с phantom `claimed/running` гарантированно возвращает item в тот же
pending ring после bounded grace.

Обычный cycle применяет ту же liveness-грань, но с обязательной identity
проверкой. Перед каждым pending projection MCD сверяет каждый persisted
`claimed/running` item с текущим TaskStore/executor/process evidence и с
`cycle_id`/`admitted_at` runtime marker-а. Для PID evidence дополнительно
сверяется сохранённый `/proc` start identity (`pid_start_time`); alive PID,
даже с совпавшей командой, но без совпавшего start identity не считается
independent proof. Marker-only запись не считается живым worker-ом. Terminal
marker другого поколения, либо marker, начавшийся до
admission текущего cycle, освобождает claim в месте, не переносит старый
`done` в новый cycle и не превращает старый terminal в успех текущего item.
При отсутствии evidence сохраняется bounded `active_meta.orphan_since`; окно
берётся из существующего profile-managed `segment_sql_orphan_after_sec` (с
безопасным fallback), а не из private host/instance настройки. После grace
item возвращается в тот же durable pending order. Следующий
current-generation worker проходит обычный `claim -> start -> terminal`, а
новый cycle создаётся только после честного terminal всего текущего cycle.
Для restart/reconciliation stale PID или mismatch не маскируются обычным
`done`: legacy `tasks.state` сохраняет совместимость (`lost`/`done`), но
`scheduler_reconciliation_last_receipt` обязан содержать explicit
`state=stale|recovered`, `reason`, `note`, `task_id`, age и независимое
`evidence`. Completion evidence получает `recovered_stale:*`, а отсутствие
процесса — `stale:pid_missing`; обе ветки атомарно освобождают только свой
root/task admission.
Для dependency closure live owner marker распространяется на все его covered
members: member не release-ится и не запускает duplicate, пока owner PID и
его start identity живы.
Ложный `cron_disallowed` при разрешённом route не допускается: receipt
различает `cron_disallowed`, `cycle_claim_or_terminal_reconcile` и
`no_projected_pending`. Это root-local reconciliation и не становится
host-wide barrier.

Для ordinary cycle есть два разных решения, которые нельзя смешивать:
`admission` фиксирует frozen `cycle.items` и отдельно добавляет поздние IDs в
durable `follow_up`, а `dispatch` запускает уже сохранённые pending IDs.
Follow-up FIFO получает следующий свободный ordinary worker раньше frozen
cursor; campaign/queue pressure блокирует только новую admission. При active
pressure каждый сохранённый pending cursor и follow-up продолжает dispatch.
Persisted ordinary work использует только effective regular worker budget;
ordinary priority участвует в shared regular accounting, а dedicated
super-priority capacity не потребляет и не расширяет этот budget. Если
throttled whitelist profile даёт regular budget `0`,
уже admitted cursor сохраняет ровно один shared slot.
Если обычный ring временно получил нулевой throttled limit, для такого cursor
сохраняется один shared slot при его фактической доступности.

Глобальный пятисекундный baseline cadence относится к новым baseline
admissions, но не к unfinished item текущего durable cycle: и newly planned,
и ранее сохранённый pending item атомарно проходит `claim -> start` на
следующем dispatch без ожидания чужого root/instance baseline. Этот запуск не
продвигает global baseline timestamp:
следующий новый admission не теряет свою собственную cadence. При этом
persisted cycle claim, dependency/conflict, host/instance capacity, TaskStore
duplicate, repeat interval и OS execution lock остаются обязательными.

Host fairness работает поверх этого правила: каждый root с persisted pending
работой получает свободный baseline/shared slot, а terminal callback закрывает
item до начала следующего cycle. Ни один active cycle с `pending > 0` и
`running = 0` не может оставаться только из-за порядка dispatch или pressure.

Import — отдельный singleton worker на каждый instance/root. На одном instance
одновременно допускается максимум один `task_type=import`; imports разных
instances могут идти параллельно в пределах общей host/profile capacity. Каждый
import занимает ровно один общий segment slot своего instance. Pending imports
одного instance наблюдаются в FIFO порядке: первый eligible получает первый
освободившийся slot, а другие instances используют свободную capacity. При
этом `scheduler_pause`, import route, DB capacity, duplicate/task lock и
instance guard остаются обязательными.

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

Hard ceiling относится только к штатным admissions. Ручной dispatch —
единственное исключение к обычному shared host/instance budget и round-robin
head: он добавляет максимум один extra manual slot на каждый запрошенный
instance, даже если root уже served или сейчас не его очередь. Например, при
normal host cap `11`, 11 обычных workers и по одному manual request на трёх
разных instances допустимы одновременно `14` workers; dedicated super lane в
этот regular budget не входит. Manual workers всё равно остаются в shared
occupancy, поэтому normal refill не выполняется при переходах `14→13→12→11` и
возвращается только при `10→11`. Manual override не меняет profile, config или
data и не отменяет same-entity, dependency/closure, import-singleton,
TaskStore duplicate, OS execution и self-update critical locks; он только
пропускает normal capacity/fairness gate.

## 5. Retry и terminal/error

`done` записывается только после успешного process exit. Для automatic segment
`runtime.segment_retry_count` — историческое имя внешней host-profile policy,
но значение является inclusive cap общего числа запусков одного item, а не
числом дополнительных повторов: `1` означает один запуск, `3` — ровно три
запуска (`attempts` 1..3). Значение `0` или отрицательное безопасно даёт один
эффективный запуск. Числовые значения существующих профилей не переписываются;
в текущем контракте к ним не применяется скрытое `+1`. До исчерпания bound
item остаётся claimed для respawn.

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
| static claim liveness, orphan grace и restart adoption | `mcd_agent/static_segment_lanes.py`, `daemon.py` | `tests/test_static_segment_lanes.py`, `tests/test_segment_liveness.py` |
| unavailable static graph fails closed only for static admission, preserving durable static state and ordinary/independent roots | `mcd_agent/daemon.py` caller projection `static_pending` | `tests/test_dispatcher_production_sandbox.py` full production dispatcher receipt |
| ordinary категории, FIFO и mid-cycle admission | `mcd_agent/segment_admission.py`, `daemon.py` | `tests/test_segment_admission.py` |
| dedupe и сохранение порядка ring | `mcd_agent/ring_utils.py`, `daemon.py` | `tests/test_segment_ring_reconcile.py` |
| dependency closure и terminal plan | `mcd_agent/segment_dependencies.py`, `daemon.py` | `tests/test_segment_dependencies.py` |
| persisted cycle, never-built admission и atomic claim | `mcd_agent/segment_admission.py`, `daemon.py` | `tests/test_segment_admission.py` |
| host/instance slots и rotation | `mcd_agent/daemon.py` | `tests/test_campaign_ring_dispatch.py` |
| external Mautic logical-task dedupe by root/type/entity and regular-slot reservation | `mcd_agent/daemon.py:list_external_runtime_task_summaries`, `_effective_segment_priority_slot_limit`, ordinary segment dispatch | `tests/test_external_task_admission.py`, `tests/test_segment_liveness.py` |
| adopted external segment PID/process-group progress watchdog with stale re-admission | `mcd_agent/daemon.py:_external_segment_process_liveness_signal`, `_external_segment_watchdog_keepalive` | `tests/test_external_task_admission.py` |
| normal hard caps, manual per-instance N+1 и dedicated super budget не смешиваются | `mcd_agent/daemon.py:_scheduler_host_slots_available`, `_scheduler_instance_slots_available`, `_submit_if_slot_uncoordinated` | `tests/test_campaign_ring_dispatch.py` hard-cap/3+ root rows; `tests/test_segment_admission.py` cap-4 и `11+3→14→...→10→11` receipts |
| важный root с заполненной capacity не ставит global remaining barrier | `mcd_agent/daemon.py:_scheduler_segment_entity_allowed`, `_scheduler_important_root_dispatchable` | `test_remaining_root_uses_free_host_slot_when_important_root_is_instance_full` |
| root-local continuous monitor/refill, live durable occupancy, eligibility-aware terminal hand-off/rotation and shared claim lifecycle | `mcd_agent/daemon.py:_iter_instances_with_root_dispatch_gate`, `_SchedulerRuntimeRegistry`, `_scheduler_store_running_rows`, `_scheduler_live_task_capacity_keys`, `_scheduler_notify_refill_priority`, `_ordinary_persisted_refill_worker` (`_refill_priority_root_ready`) | `tests/test_segment_liveness.py`; `tests/test_dispatcher_production_sandbox.py` full `run_loop` paths with unavailable static graph, dead TaskStore rows and terminal-root hand-off |
| host/instance lazy effective policy, mixed roots, toggle/restart/UID sync | `mcd_agent/config.py`, `mcd_agent/runtime_overrides.py`, `mcd_agent/daemon.py` ordinary planners/dispatch context | `tests/test_segment_liveness.py`, `tests/test_runtime_override_direction.py` mixed sandbox gate |
| lazy empty plan versus full-mode observed-ID fallback | `mcd_agent/daemon.py` ordinary snapshot planners | `tests/test_segment_liveness.py` lazy snapshot row; fresh M6/M7 full/lazy planning receipt |
| profile total-start budget (value `3` means exactly three starts) and bounded orphan expiry | `mcd_agent/config.py`, `mcd_agent/daemon.py` | `tests/test_segment_admission.py` |
| import admission baseline, partial-batch pending и terminal monitor distinction | `mcd_agent/config.py`, `mcd_agent/daemon.py` | `tests/test_campaign_due_sql.py`, `tests/test_campaign_ring_dispatch.py`, `tests/test_import_liveness.py` |
| monitor queued/running/done/error receipt | `mcd_agent/daemon.py` | `tests/test_scheduler_monitor_cycle.py`, `tests/test_signals_scheduler_history.py` |
| heavy-plan deferral and durable pending guard | `mcd_agent/daemon.py` | `tests/test_segment_liveness.py`, live M6/M7 acceptance receipt |
| self-update preserves ordinary application work and all durable queues | `mcd_agent/self_update.py`, `daemon.py`, systemd unit | `tests/test_self_update_version_reporting.py`, `tools/runtime_bridge_qa.py` (`mcd-runtime-bridge-qa-v1`) |
| legacy source-tree to runtime-only transition is authority-safe and accepts complete split TOML config | `tools/mcd_runtime_bootstrap.py`, `tools/mcd_profile_authority_probe.py` | `tests/test_profile_authority_probe.py`, `tests/test_runtime_bootstrap_capture.py`, live `mcd-runtime-bootstrap-receipt-v1` |
| same-version unlisted DEV runtime replacement is explicit, fully pinned, atomic and fail-closed | `tools/mcd_runtime_bootstrap.py` | `tests/test_runtime_bootstrap.py`, live `mcd-runtime-bootstrap-receipt-v1` with `runtime-to-runtime-pinned-rebuild` |
| activation-derived Portal material is imported atomically without new auth/job issuance; commercial API-key transport, host binding and installation IDs are validated and persisted while legacy agent/Portal signing-key state is scrubbed | `mcd_agent/commercial_registration.py`, `mcd_agent/commercial_portal.py` | `tests/test_commercial_registration.py`, `tests/test_commercial_outbound_boundary.py`, redacted `mcd-portal-registration-receipt-v1` |
| Portal worker restart forces one independently ACKed complete inventory per installation; a successful installation cannot clear another installation's pending startup publication, and transport failures retry through ordinary backoff without cursor/cutover mutation | `mcd_agent/commercial_self_hosted.py:SelfHostedPortalWorker._publish_inventory` | `tests/test_commercial_outbound_boundary.py` multi-install restart and partial-failure retry |
| successful Portal inventory rescan returns the canonical observed snapshot revision/hash in the existing terminal result; unchanged content does not bump revision and failed rescan returns no reference | `mcd_agent/daemon.py`, `mcd_agent/commercial_primitives.py`, `mcd_agent/commercial_self_hosted.py`, `mcd_agent/commercial_portal.py` | `tests/test_commercial_primitives.py`, `tests/test_daemon_commercial_guard.py`, Portal 1.7 terminal-receipt matching |
| Portal-only external-operation cache sync consumes a transient one-time grant before durable storage, verifies the complete closure/binding preimage, preserves unrelated cache rows and activates the manifest last | `mcd_agent/external_operation_cache.py`, `mcd_agent/commercial_self_hosted.py`, `mcd_agent/daemon.py` | `tests/test_connected_host_lifecycle.py`, `tests/test_commercial_generic.py` grant non-persistence/replay evidence; MCC 3.2.1 closure `sha256:ca8140086414c3cee58b5431dc652a7933b599f56c85a62a6514bcd934ac3e70` |
| connected-host lifecycle is an exact host-scoped external binding with preflight/create/receipt-owned cleanup/recreate; stable target UID forbids `@host`, exact binding/artifact/profile pins are checked before the existing offline-only runner, and only create/recreate ownership receipts authorize Portal projection | `mcd_agent/connected_host_lifecycle.py`, `mcd_agent/commercial_primitives.py`, `mcd_agent/commercial_generic.py` | `tests/test_connected_host_lifecycle.py`, `tests/test_commercial_generic.py`; binding `sha256:79d7ba83ba2bfab1acc01ab237bd6ad30276f3f3597fec26086b608ad784798f` |
| host-first Portal registration binds an activated MCD host before any Mautic instance exists; request contains only local DNS identity/display name, and the response must create a passive host installation with an active empty-inventory connection | `mcd_agent/commercial_portal.py`, `mcd_agent/commercial_host_registration.py`, `mcd_agent/cli.py` | `tests/test_commercial_portal.py`, redacted `portal-mcd-host-register-response-v1` |
| self-hosted v2.1 candidate jobs may omit stage metadata for a whole external composition; supplied `stage_selection` must structurally match `execution.stage`, without embedded stage-name/order recipes; existing receipts remain | `mcd_agent/commercial_portal.py`, `mcd_agent/commercial_self_hosted.py`, `mcd_agent/scenario_launcher.py` | `tests/test_commercial_self_hosted_v2.py`, redacted stage/terminal receipts |
| manual existing commercial-key fallback keeps pending/active/disconnected state idempotently | `mcd_agent/commercial_manual_activation.py`, `mcd_agent/commercial_portal.py`, `mcd_agent/daemon.py` | `tests/test_commercial_manual_activation.py`, redacted `mcd-commercial-manual-activation-receipt-v1` |
| active Portal state is reconciled after daemon startup; incomplete construction and exited workers retry without duplicates | `mcd_agent/daemon.py:_reconcile_commercial_worker` | `tests/test_daemon_commercial_guard.py` activation-material retry; `tests/test_commercial_self_hosted_v2.py` activate -> poll -> terminal v2 |
| Fail2ban startup safety is externalized to an MCC-owned scenario/recipe delivered through Portal and the obsolete MCD startup guard boundary is absent | MCC Debian-family scenario/recipe delivered through Portal; no `fail2ban_guard` runtime import/caller | `tests/test_daemon_commercial_guard.py`; `fail2ban-native-replacement-proof-20261101.json`; `fail2ban-external-lifecycle-receipt-20261101.json` |
| result-signing identity bootstraps from MCD-owned config and gates signed control execution on MCC ACK | `mcd_agent/config.py`, `mcd_agent/result_signing.py`, `mcd_agent/state_push.py`, `mcd_agent/mcd_control.py` | `tests/test_result_signing.py`, `tests/test_mcd_control.py`, live `mcd-state-v1` identity/key/ACK and terminal-receipt evidence |
| authority-safe desired runtime projection | `mcd_agent/config.py`, `mcd_agent/runtime_overrides.py` | `tests/test_runtime_override_direction.py`, `tests/test_runtime_stable_keys.py`, `tests/test_runtime_migration_adapter.py` |
| natural b2bss/Adonis evidence | runtime TaskStore/MCC receipts | rollout acceptance (после package/install) |

Любое расхождение между этой схемой, source, тестом и runtime receipt
блокирует acceptance; его нужно явно зафиксировать и исправить до handoff.

### Daily backup recovery

Daily instance backup scheduling is a durable scheduler concern. Before a scheduled `backup_run` the daemon persists one local-date claim and changes it to a `running`, `succeeded`, or `failed` receipt in `TaskStore`. The claim is reloaded after an MCD restart, so a restart cannot duplicate the same daily slot. A stale claim may be recovered only after its bounded grace expires and only when no independent backup lock is active. A missed slot is not launched outside the configured quiet window: it is caught up at the next eligible window, without stopping or replacing an already-running backup.

### Nginx deletion ownership and default stub

Instance deletion inventories exact requested-domain configs in sites-enabled,
sites-available and conf.d, including arbitrary enabled aliases and dangling
aliases to the exact target config. Parsed server names establish ownership;
shared files containing another server name are rejected. Root-path similarity
or a certificate reference never authorizes deleting another vhost.

Deletion snapshots only its owned configs, stages their removal, validates with
nginx -t, inspects the active nginx -T inventory and reloads only on success.
Failure restores only those owned configs, preserving recovery snapshots and
blocking certificate/database/files cleanup. Unknown active target configs
block cleanup. Certificates referenced by remaining vhosts or dynamic TLS
paths are preserved; references are checked again immediately before certbot.
An unchanged repeated deletion performs no unnecessary reload.

The default stub belongs exclusively to the nginx baseline generator. It has
server_name _, no instance domains, no certificate directives and no certificate
selection or generation. HTTPS uses ssl_reject_handshake on. A baseline apply
validates support through nginx -t and retains its existing rollback behavior.
Instance deletion never creates, repairs or rewrites this stub or another
vhost. A broken shared default therefore blocks deletion until its own normal
baseline operation repairs it; deletion must not hide that fault.

The supported narrow baseline-owner operation is
`mcd-cli nginx-baseline default-stub status|apply --json`. Status is read-only;
apply writes only the existing managed default stub (or creates a missing stub)
using its generator, validates nginx, then reloads. It never runs the full
baseline, apt postchecks, instance configuration or deletion. Unmanaged files
and symlinks are refused. Atomic replacement preserves file permissions/owner,
retains a backup, and restores only this file on validation/reload failure.
Concurrent edits are preserved rather than overwritten by rollback. An
unchanged repeat validates without reloading. The receipt distinguishes real
validation, reload, rollback and an inactive-service reload skip.
Evidence: `test_default_stub_operation` covers these boundaries, standalone CLI
routing, neighbors remaining byte/mtime-identical, and failures/replay.

Evidence: test_instance_delete_nginx_transaction covers aliases, quoted and
inline names, shared-config refusal, rollback, untouched defaults/other vhosts,
remaining certificate references and pre-mutation failure. Native Ubuntu nginx
QA additionally checks that the certificate-free stub validates, a broken
foreign default blocks deletion, and repeated scoped cleanup preserves the
default and neighboring vhost byte-for-byte.

### Backup profile synchronization contract

Backup profile synchronization is a separate typed contract and is never
projected into generic runtime overrides. MCD advertises and consumes
`mcd-backup-profile-v1` version 1 envelopes containing a profile,
`profile_updated_at` epoch, and exact transport `profile_sha256`. The effective
V1 projection matches MCC `canonical_mcd_profile`: remove `settings`, map its
backup enabled/schedule values, and preserve all other requested fields,
including `remote_root_dir`, authentication and declared `source`. MCD verifies
each requested field against actual candidate values without injecting unrelated
defaults or local-storage provenance. Schedule values affect the candidate
immediately; an older DB profile must not overwrite the new candidate during
verification. Unknown fields or mismatched actual values fail before profile
mutation. After persistence, MCD rereads the stored effective values and returns
masked `effective_profile`, `effective_sha256`, the epoch and `status`.
The transport hash covers the original envelope profile; the effective hash
covers the canonical projection before credential masking. `ok` requires the
effective hash to match that projection, including for disabled profiles and
idempotent same-epoch replay. Subsequent host pushes retain that same stored
canonical shape, without injecting defaults/provenance as a false same-epoch
conflict. Invalid/nonfinite epochs are rejected. Evidence:
`test_backup_profile_v1_readback` includes the exact published MCC hash fixture,
real temporary DB/config/crypto round trips, schedule adoption, old-state races,
and negative checks that never apply unknown or unverifiable values.
Schema, validation,
apply, or readback mismatch returns `unsynced` or `error` and does not claim a
successful sync. The legacy `backup_profile` telemetry field remains for older
MCC/MCD versions; it is not evidence of exact synchronization. Scheduler
semantics are unchanged.

### Bidirectional backup-profile synchronization

The backup-profile envelope is bidirectional. Both sides store the complete
profile, UTC `profile_updated_at`, and canonical `profile_sha256`. A newer
timestamp atomically replaces the older profile. Equal timestamp and equal
hash is a no-op; equal timestamp with a different hash returns `unsynced`
conflict without mutation. Every accepted apply and callback returns the full
masked effective profile, hash, and timestamp. No TOML file copying or rsync
utility participates.

### Split-config precedence

For split installations, the entrypoint config is the base and declared include
files are applied in order; the last existing local include is authoritative for
overrides such as `[backup]` and `[backup.schedule]`. Startup and reload must
therefore preserve `backup.enabled` and schedule enablement from the effective
local file when the entrypoint only supplies defaults or MCC bootstrap fields.

### Commercial Portal self-hosted scenario admission

Commercial MCD is an outbound-only Portal lane. A delivered
`portal-self-hosted-mcd-job-v2` is admitted only after exact account, commercial
key, agent, installation, host, scenario and applicability binding, local-facts
revalidation, expiry, nonce and idempotency checks. The commercial API key over
HTTPS is the only transport authentication; MCD does not require, persist or
validate a Portal/job signing key or `portal_signature`. The additive contract
version `2.1` is the registered-installation path: its
`execution.primitive` is `scenario.apply`, and `bootstrap_artifact` must be
absent. MCD accepts downloaded artifacts only when size and SHA-256 match the
immutable artifact pin/catalog entry. Portal sends the self-hosted job and
artifact pin without `portal_signature`, `signature`, public-key or trust-store
fields; MCD admission is based on the HTTPS API credential, exact binding,
expiry/idempotency, and the pinned size/SHA-256 values. MCD never imports MCC credentials or
scenario rules, and Portal remains only the connector/UI for this lane.

The commercial API credential and host binding described below apply only to
external OWH MCD hosts. The internal SalesSnap path is instance-bound; it does
not create a separate host/MCD key entity and does not introduce a replacement
token or signature identity.

The API-key-authenticated `portal-mcd-inventory-snapshot-v1` also carries the additive
`applicability_facts` coordinate (`mcd-portal-applicability-facts-v1`, contract
version `1.0`). Its `facts` mapping is the complete normalized value used by
the local execution guard: `os_id`, `os_version`, `architecture`,
`package_manager`, sorted unique `capabilities`, `target_kind`, and
`detector_schema`. It also carries MCD-owned `classification_reason` and
`classification_evidence` from the read-only
`mcd-host-classification-evidence-v1` probes. MCD defaults to
`existing_server`; it emits `new_server` only when no Mautic installation and
no installed or active web server, database, cache, or PHP runtime marker is
present. `mcd.service` is explicitly excluded. `facts_digest` hashes the same
mapping after removing only `detector_schema`. Inventory publication and
`scenario.apply` use the same reader and capability context, so the coordinate is stable across unchanged
polls, restart, replay, and Portal outage; its durable inventory hash and
revision include the field. Missing detector facts fail closed. Scenario
`required_capabilities` remain Portal-side applicability predicates and are
never synthesized into this facts mapping. Older Portal consumers may
ignore the additive field.
On every Portal worker start, each bound installation independently owes one
complete inventory publication and Portal ACK before unchanged-content hash
gating resumes for that installation. An ACK for one installation must not
suppress startup publication for another. A transport failure leaves only the
affected installation pending for the ordinary bounded retry; already ACKed
installations remain hash-gated. This restores every Portal projection after an
MCD restart without changing installation IDs, delivery cursors or cutover
state.
The producer reports truthful host/preflight capabilities such as
`package_manager:<name>`, `os_like:debian`, `dpkg`, `package:curl`,
`service_manager:systemd`, `root_sudo`, and the observed `network_egress`
capability; MCD feature labels are not substituted for these host facts.

The only MCD execution primitive is the contract's `scenario.apply` v1.0. MCD
passes the latest verified external scenario artifact to one allowlisted,
non-shell local runner process and owns no installer, OS, Mautic, scenario or
primitive recipe. Connection type (Portal self-host, Cloud, MCC direct or a
future integration) changes only delivery/authentication; it never selects a
different runner, frame, context or execution engine. No fixed onboarding
stage registry is constructed on this path. A legacy self-hosted artifact may
select `portal-self-hosted-executable-context-v1`; the frame validator accepts
both that schema and the current MCD context schema without changing the
primitive contract. Progress is checkpointed by
immutable `job_id`; replay of the same API-authenticated job returns the durable terminal
receipt without re-executing the runner only when the received command identity
is the same. A distinct Portal recovery command has its own command/cursor
execution identity and is executed exactly once, even though it retains the
same job idempotency key. The identity is derived again from the verified
`command_id` and `delivery_cursor` inside MCD, so a legacy caller cannot
silently downgrade a distinct recovery delivery to the old job-only key.
Recovery stage and terminal receipts continue from the prior durable terminal
sequence; they never restart at sequence 1 for a new command.

For the Mautic primitive context, the additive
`mcd-scenario-primitive-context-v1` contract is validated by the same launcher
(`mcd_agent/contracts/mcd-scenario-primitive-context-v1.json`). It binds the
job/request/command/idempotency tuple, scenario and artifact pins, host
fingerprint, and the normalized applicability facts digest. The application
coordinates are the already discovered absolute `root`, `console_path`,
`php_bin`, and `run_as_user`; MCD does not invent a path or default. Backup
evidence is the existing `mcd-mautic-patch-backup-attestation-v1` receipt over
the `mcc.backup.manifest.v1` marker. Migration policy carries only the existing
`list doctrine` discovery and the discovered migrate/up-to-date/version command
families with non-interactive apply; arbitrary SQL, down migrations, deletion
of migration history, and invented defaults are rejected. Rollback remains the
existing immutable patch-plan/backup restore path with drift-fail-closed
semantics and explicit `not_attempted|attempted|succeeded|failed|blocked`
state. The context validator checks binding and shape only; the launcher does
not execute PHP, choose versions, restore a backup, or synthesize a recipe.

Scenario delivery is a separate MCD-owned boundary described by
`mcd-scenario-artifact-delivery-v1` and implemented by
`mcd_agent/scenario_artifact_store.py`. On the trusted internal path MCD reads
active MCC catalog metadata first, downloads through the authenticated API into
an isolated staging file, verifies exact revision/media/size/SHA-256 and
catalog binding, fsyncs the object, atomically switches the active pointer,
and emits a verified delivery receipt before cleaning the previous local
object. This path has no artifact signer operation. A failed verification,
unknown/stale/mismatched catalog, partial download, disk-full condition,
outage, restart or concurrent delivery removes only the new staging material
and keeps the previous active pointer. Pending cleanup is replayed after the
durable receipt queue drains. The catalog is immutable and is never deleted by
MCD; rollback re-downloads a pinned old revision. No scenario runner starts
during this delivery sequence.

The executable scenario bridge follows the generic
`mcd-scenario-executable-context-v1` contract. The old
`portal-self-hosted-executable-context-v1` value is accepted only as a
compatibility data label and never creates a second execution path. The versioned
artifact must be `application/x-sh` or `application/x-elf`; the legacy
declarative JSON artifact remains audit-only and is never dispatched. MCD sends
the artifact and canonical context in the 88-byte `SSMCDX01` frame: exact
lengths, raw SHA-256 digests, and reserved flags are checked before execution.
The context contains only job/scenario identity, applicability digest, scalar
non-secret parameters, opaque secret references and the typed primitive
context; delivery account/provider identity remains outside the local frame
and receipt binding. It is exposed to the child only on inherited file
descriptor 3. The bundled `mcd-scenario-runner` uses a sterile environment,
fixed `--mcd-scenario-apply` argv, no shell/eval, and a bounded process group.
Child stdout is a length-prefixed canonical JSON stream with at most 256
progress frames and one terminal result; unknown fields, secret-shaped values,
non-contiguous progress, malformed frames, timeouts, signals, and oversized
output fail closed. MCD also captures only bounded stderr and persists a
redacted preview, output SHA-256 values, truncation flags, exit code/signal,
timeout state, admission stage, and any already-valid progress frames as typed
runner evidence. Raw stderr, artifact bytes, and executable context never enter
receipts or run state. MCC owns the executable artifact, active catalog
metadata and scenario policy; MCD owns authenticated download admission, local
staging/activation, execution admission, timeout, rollback state and durable
receipts. The legacy artifact-trust capability remains only for compatibility
and is not required by the trusted MCC SHA-only path.

If the bounded runner exits non-zero but emits a valid final result frame, MCD
decodes that frame before reducing the failure. The terminal receipt preserves
the artifact's typed `error_code`, `completed`, `total`, `rollback`, and bounded
runner result, so partial mutation and rollback evidence remain visible. Only a
missing or invalid final frame is reduced to the generic runner failure.

Stage receipts use `mcd-self-hosted-stage-receipt-v2` with only
`scenario_dispatch` and `execution`, statuses `queued|running|waiting|succeeded|failed`,
mandatory installation binding and unique idempotency keys. Terminal receipts
use `mcd-self-hosted-terminal-receipt-v2`, `succeeded|failed`, explicit
`rollback`, the same binding and a monotonic sequence. Failed terminals are
bounded and secret-free. Every unsent stage or terminal receipt is durable and
retried in order after Portal outage or MCD restart.

Any non-2xx response to a durable receipt, including the typed non-retryable
`receipt_sequence_regression`, is not an acknowledgement of that exact
idempotency key. MCD keeps the receipt and pending delivery cursor durable,
does not advance the cursor, and retries the same receipt in order after the
Portal-side state is reconciled. It never re-executes the runner or creates a
replacement job. Release publication is performed by the dedicated `Publish
MCD` process; MCC only registers/serves the catalog metadata used by MCD.

If a historical runtime already lost a pending receipt after a non-2xx
response, recovery may requeue only the exact terminal found in
`commercial_self_hosted_runs`. The worker requires the exact job id, command id,
delivery cursor, sequence, idempotency key, current installation binding and
canonical payload SHA-256; any mismatch is rejected. Recovery queues the saved
payload for normal worker delivery and never reconstructs it, starts the job, or
changes the host.

The commercial transport worker is a separate non-blocking Portal lane:
`pull_job` → exact v2 job admission → pinned artifact download(s) → local facts
guard → generic runner → ordered stage/terminal receipts. A 2.1 registered
scenario delivery has exactly one artifact download; only legacy 2.0 may have
the additional bootstrap download. Its cursor and pending receipt queue are
durable in the commercial state store. Network/Portal failure never pauses
corporate or standalone scheduler work. The commercial key is durable identity
metadata only; raw credentials and future quota, billing or tariff semantics
are not part of MCD.

Once Portal has leased and MCD has durably accepted a delivery, every local
outcome closes through a terminal receipt: expiry, malformed or failed
admission, revoked/disabled Portal state, artifact unavailability, and runner
exceptions are typed and bounded. A receipt that cannot be sent immediately is
queued durably before the delivery is considered complete; the runner is never
re-run just to deliver a receipt after restart. A poll/network failure before a
delivery exists remains a transport retry and has no command to acknowledge.
For Portal's same-job recovery command, the terminal continues at the next
monotonic sequence/idempotency key after any terminal from the first attempt.
A distinct recovery command executes once under its own command/cursor key;
retransmission of that same recovery command drains the durable terminal and
never re-runs the runner.

`daemon.run_loop` may start at most one Portal worker when the durable
Portal enrollment is valid: HTTPS Portal base, one existing commercial API
credential, commercial-key identity, host binding, and at least one
installation binding must all be present. No Portal job-signing keys are
required. Poll, adopt, inventory, settings, noop, receipt and job delivery use
only the existing commercial API key over HTTPS. Artifact acceptance is
SHA-256-only and does not load or require artifact signer keys; legacy signing
key values are ignored and scrubbed from local state.
Unknown/stale/mismatched SHA or an authenticated API failure rejects only that
operation with a durable failed terminal receipt. The live worker remains
available for later outbound polling; a restart reloads the same durable
catalog/receipt state.
The worker remains available for later outbound polling. The base
`passive`, `standalone`, or `corporate` profile is not an
eligibility switch; the Portal worker is an independent capability alongside
the existing scheduler. The corporate MCC URL/token and base profile are not
Portal eligibility switches. Missing or invalid Portal state, including an explicit
revoke/detach marker, fails closed. Single-cycle mode never starts the worker.
When activation changes durable state while the daemon is already running,
the same reconcile loop must start the worker without a daemon restart. A
transiently incomplete material set or an unexpectedly exited worker is
retryable with bounded backoff; it must not be hidden by a one-time
eligibility transition. Reconciliation starts at most one live worker and
stops only that worker on revoke/detach.
The worker owns only its Portal stop event and bounded thread; it does not
enter scheduler admission, locks, or task slots. On revoke, detach, or
shutdown MCD stops and joins only this worker, leaving corporate MCC and
standalone work running. A restart re-reads the same durable registration and
resumes without activation or token issuance; starting is idempotent and never
creates a duplicate Portal worker.

The existing activation-derived Portal credential is delivered through the
`mcd-portal-activation-material-v1` bootstrap envelope. MCD imports that
envelope locally and atomically into `CommercialState`; it never redeems the
activation code, creates an agent/key/job, or performs a second registration.
The importer validates the host fingerprint, commercial key and installation
IDs, and removes legacy agent/Portal signing-key material. It preserves the
cursor and pending receipts, and its redacted receipt contains no credential
or private key. A missing or mismatched binding fails closed before the
commercial worker can start.

#### Manual fallback for an existing commercial key

Fresh Portal bootstrap is an immutable external `mcd-bootstrap` component in
the shared scenario source. Its original exported bytes and existing live
bootstrap SHA remain unchanged; it is no longer an MCD tools recipe or config/
unit seed. The publisher copies only the approved external export after exact
SHA/size verification. Runtime archive and verified update bridge pins remain
separate and unchanged. Existing HTTPS/one-key transport, offline binding
import, exact completed-replay checks, passive/inactive state and receipts are
preserved by that external component. Its recipe tests/QA belong with the
external source. The runtime module entrypoint never installs dependencies;
missing modules fail normally and must be prepared by the verified external
bootstrap. MCD retains generic verified runtime activation/update plumbing.
See `docs/external-bootstrap-boundary.md` for the ownership and publication seam.

New one-key OWH enrollment uses `identity_kind="dns"` and plain `local_identity`
with server-issued agent/installation IDs. Material schema is
`mcd-portal-enrollment-material-v1`, contract 1.0. No hostname/DNS digest is
computed, stored or required for this branch; mixed DNS/fingerprint coordinates
are rejected. Import, eligibility, poll, inventory, connection/terminal receipts,
self-hosted jobs and executable context carry the same DNS coordinate. Existing
legacy/manual fingerprint data and routes retain their original behavior. The
new idempotency namespace is `:dns-enroll-v1` / `:dns-host-v1`, preserving history.
The executable context carries full DNS channel identity in top-level `binding`
and account_id in `job`; legacy job's four identifiers remain unchanged.
Evidence: tests/test_portal_dns_identity.py (host-hash function fails if called,
DNS import/replay, legacy preservation, poll/receipts, admitted/expired jobs,
primitive context); runtime 1.6.26 is required for the new material contract.

If the normal Own-host bootstrap is unavailable, the operator may use the
already-issued reusable commercial API key through the MCD-owned manual
fallback. One key may register unlimited independent external OWH MCD
connections; each registration has its own instance/activation state. The key
is entered through a hidden prompt, stdin, or an explicit
CLI argument for automation; it is never printed or included in receipts.
MCD stores it in the existing mode-0600 `CommercialState`, sends the existing
passive installation registration, and records
`commercial_portal_connection_state=pending_activation`. The Portal tile then
waits for explicit activation. Only an explicit `activate` changes the state
to `active`; only an explicit `disconnect` changes it to `detached`. Repeated
stage/activate/disconnect operations reuse the same key and registration
identity idempotently. This path has no SSH, DNS-token, global-settings-key,
or second authentication layer and does not create a job or activation code.
Subscription/entitlement expiry never changes this connection state; only the
explicit operator `disconnect` does so. Expiry may affect feature admission,
but it is not an implicit revoke or cancellation.

The Portal registration identity is durable installation metadata. The
registered `local_identity` is the DNS routing coordinate and remains separate
from the native MCD `instance_uid`; inventory rows never rename the native UID
to DNS. MCD carries the native UID as `source_instance_id`, resolves commands
through the exact installation-to-UID map, and keeps per-instance tenant
metadata in the managed carrier. For a host with multiple Mautic instances,
MCD does not guess a row from a hostname or first match. Restart, host rename,
and template clone therefore cannot silently change the Portal installation,
instance, or tenant binding.

### Managed-cloud versus self-hosted commercial boundary

### Opt-in MCC desired-runtime sync v3

#### Полное восстановление runtime-настроек после потери

MCC хранит полный разрешённый runtime-снимок хоста и его версионные метаданные.
Если локальный runtime-файл после обновления, перезапуска или неудачной записи
стал пустым, неполным или содержит reset-проекцию, это не считается изменением
настроек MCD. MCD сначала читает канонический снимок MCC, восстанавливает весь
`runtime_remote_allowed_keys` (включая host-поля, структурированные каталоги и
все instance-строки), сохраняет его в локальный mutable include и только затем
перезагружает конфигурацию. Восстановление не ограничивается stable- или
instance-ключами; false/zero/empty значения восстанавливаются как валидные
значения, если они присутствуют в каноническом снимке. MCD отдельно
распознаёт потерю полного локального снимка по отсутствующим или сброшенным
полям/instance-строкам и в этом случае всегда материализует весь разрешённый
MCC-снимок, включая замену оставшихся непустых reset-default значений, даже
если текущий ответ содержит только stable-поля.

Для каждого host/instance-поля применяется LWW по сохранённому `updated_at`,
затем `revision`, с детерминированным MCC tie-break. Более новое изменение MCD
отправляется в MCC и не затирается устаревшим ответом; более новое изменение
MCC применяется на хосте. Отсутствие поля в частичном ответе не является
удалением. Удаление допускается только явным MCC tombstone либо подтверждённым
локальным tombstone с последующей синхронизацией. После восстановления MCD
обновляет baseline без повторной отправки того же состояния как новой локальной
правки.

Embedded Forms reconciles the effective `form_embed_instance_settings` after
runtime adoption, independently of desired-state ACK delivery. A changed policy
or inventory applies immediately; `runtime-overrides trigger` also reconciles
an unchanged policy. Startup uses the same path. Failed/blocked application or
observed-status delivery retries at the existing runtime poll cadence, while a
new policy or explicit trigger bypasses that retry delay. Standalone MCD applies
locally with MCC disabled. Successful unchanged policy/inventory is cached;
an empty inventory or blocked vhost is never treated as completed application.
The reconciler uses the existing nginx backup/test/reload/rollback mechanism;
MCC stores desired policy and observed receipts, without editing nginx.
Evidence: `test_runtime_ack_daemon_integration` covers actual daemon adoption
with a canonical instance row, `test_runtime_stable_keys` covers triggers,
retry, inventory changes and failed delivery, and `test_form_embed` checks
rendering and rollback on a failed nginx validation.

MCD may negotiate the opt-in `desired_state_sync_protocol=3` endpoint without
changing the legacy v2 transport. The producer keeps the complete acknowledged
MCC snapshot and its revision as the common base in `runtime_sync`; a local
change is sent as only the changed host keys plus a durable idempotency key and
externally declared `semantic_conflict_groups`. A missing v3 endpoint records
an explicit capability miss and falls back to v2; an existing endpoint never
silently downgrades a rejected, queued, or conflicting v3 intent.

The v3 consumer uses the canonical desired snapshot returned through the
existing runtime-overrides/readback path. `applied` and `merged` results are
materialized atomically through the existing runtime override primitives and
then acknowledged by the normal chunked ACK. A `conflict` stores base,
central, host, group metadata, and both values durably; non-ambiguous fields
may be projected, but ambiguous fields remain held and are never resolved by
last-writer-wins. Restart and Portal/MCC outage retry the same idempotency key.

An operator resolution is accepted only after a newer canonical snapshot is
read back with `source=sync_v3_operator_resolution`; the conflict hold is then
cleared and the ordinary apply/ACK path verifies the actual local state. The
legacy v2, corporate, and standalone paths remain unchanged when v3 is not
negotiated.

For managed-cloud installations under `name.sales-snap.com`, the single
corporate MCD remains connected only to MCC. Portal reaches MCC through the
MCC server-to-server projection/intents boundary; MCC continues to dispatch
work and receive receipts through the corporate MCD. A second Portal key or
control plane must not be added to that host.

The Portal↔commercial-MCD lane is reserved for self-hosted installations. It
uses the outbound Portal contract and a host-level commercial agent, separate
from the corporate MCD identity. Passive commercial acceptance is held until
Portal completes the approved “Установки” flow; this hold authorizes no host,
source, credential, or tenant mutation.

### Commercial Portal network boundary and durable outbound cycle

The commercial worker is an outbound-only transport. MCD is the sole
connection initiator: it uses HTTPS poll/long-poll to consume queued Portal
commands and settings/resolutions, and HTTPS POST to deliver signed ACKs,
receipts, local intents, inventory and state. Portal has no callback, webhook,
SSH, reverse connection, inbound listener, bind, or public host reachability
through this lane. The corporate MCC transport remains a separate path.

Every commercial Portal HTTP request carries the stable product User-Agent
`mcd-agent/<version> portal-commercial/1.0`, including activation, registration,
adoption, polling, inventory, receipt, state and artifact requests. This is a
product identity header for normal WAF classification; it is not an
authentication credential and must not contain activation codes or secrets.
Transport failures are surfaced as redacted typed diagnostics with `stage`,
safe `code`, optional `http_status` and `retryable`. Response bodies, URLs
with query data, credentials and activation material are never included in
diagnostics.

Before processing a delivery MCD durably records the delivery, its prior
cursor, next cursor and installation binding. Stage2 uses the exact
installation-scoped poll, adopt/inventory and adopt-receipts routes. The
complete inventory is sent over HTTPS on adoption and on subsequent changes. Every
returned command is retained in order; the cursor advances only after the
terminal receipt for each command and all earlier receipts are accepted by
Portal. A daemon restart therefore resumes the saved batch or the durable
receipt queue; replay uses command/job idempotency keys and never re-runs a
completed scenario. A Portal/network failure leaves commands queued at Portal
and records an exponential, durable retry deadline. The next outbound cycle
drains receipts first, then resumes the saved batch or polls with the last
cursor; no scheduler or standalone work is blocked. Backoff is applied only
to transport failures, not to local validation/applicability rejection.

Poll cursors are durable per Portal installation, in
`commercial_portal_cursors_v1`, and are never shared between managed
installations on one physical MCD agent. The legacy scalar
`commercial_portal_cursor` remains only for the single-installation Own lane.
When a managed installation has no local cursor, MCD sends `cursor=null` and
accepts that installation's server cursor; it does not import a stale scalar
cursor from another connection. A saved delivery still retains its own
installation binding and cursor, so restart/replay cannot cross-ack another
tenant or installation. Poll request IDs and idempotency keys use the same
installation scope, so two connections on one provider agent cannot replay
one another's request state.

Inventory publication has its own durable revision ledger per installation.
MCD canonicalizes the complete inventory and removes only transport-volatility
fields `request_id` and `observed_at`; every other durable
field (MCD version, profile, settings, instances, capabilities, binding and
revision) participates in the hash. A changed durable hash increments
`snapshot_revision` exactly once under the state-store transaction; unchanged
content reuses the same revision across restart, outage, replay and concurrent
workers. The state stores the durable snapshot/hash plus last published and
acknowledged pins. A missing hash at legacy revision `0` performs one safe
baseline migration; corrupted or regressive state fails closed. Portal acceptance
pins the same hash/revision, and a failed publication retries that revision
without creating a new one.

After local state loss, an authenticated installation-scoped HTTP 409 may
report `inventory_snapshot_revision_conflict` or
`inventory_snapshot_revision_stale` using `portal-error-v1` / contract `1.0`,
with top-level integer `latest_snapshot_revision` in 0..9007199254740990.
Only this typed hint authorizes recovery. Under a SQLite transaction MCD
compares the rejected revision AND durable hash with the current ledger, then
sets the same observed snapshot's revision to `max(local, server) + 1` and
clears its publication/acknowledgement pins. Facts, settings, other installations,
pending work and Portal history remain intact. A concurrent local change wins;
an old conflict cannot rebase it. The next normal retry uses the new derived
inventory request/idempotency identity; adoption already uses fresh outer IDs.
Missing, opaque, boolean, fractional, negative or out-of-range heads, other
errors and incompatible envelopes do not authorize recovery. Normal transport
backoff remains active; no rejected request is acknowledged. Counter overflow
fails closed. `test_commercial_inventory_recovery` verifies both adopt and
inventory paths, durable restart, stale in-flight responses, metadata bounds,
unchanged settings/facts, receipt pins and isolation from other installations.

Commercial inventory prefers the full Mautic version actually recorded in
`vendor/composer/installed.json` (Composer v1 or v2 package list), including
pre-release suffixes. It never executes PHP or changes application files to
observe it. Conflicting, unavailable or non-version installed metadata does
not invent a full version; existing registration/observed-major fallback remains.
Installed evidence takes precedence over registration hints and desired lock
versions. The recovery tests cover both metadata formats and conflicting evidence.

### APT service-profile plan

APT policy remains the externally fetched MCC service profile. Dry-run lists
declared packages and enables optional actions only under the same policy
flags/defaults used by native apply; disabled Composer, package-service,
Zabbix, directory or repository actions are not presented as enabled work.
The obsolete `ensure_nodejs20=true` switch fails closed before any probe,
repository write or package operation, including in dry-run. Absent/false
does not install Node or select another version. Node runtime selection is
external policy, not a host/version default hidden in MCD. Tests assert the
no-side-effect rejection and enabled/disabled package/service plans. Live
acceptance uses the unchanged service-profile CLI and scoped server profile.

По APT для Portal 1.7 принят cache delivery: существующий `scenario.apply`
доставил на DEV135 точный артефакт и binding, но runtime cutover ещё не
завершён. Следующий обязательный этап — штатный MCD
`service-profile apply --component apt --dry-run` с исходным профилем и
доказательством вызова exact artifact; до этого нельзя объявлять 1.7
завершённым. MCC владеет сценарием, профилем и их authority; Portal доставляет
и координирует immutable artifact и lifecycle receipts. Старая builtin-
совместимость допускается только как временный путь до доказанного
переключения и не является финальным 1.7 решением.

Для offline service-profile через Portal 1.7 существующий custom-cache
manifest может объявить в operation binding единственный `profile_source`:
`{"type":"cached-json","file":"<relative>","sha256":"<64 hex>"}`.
MCD читает только объявленный относительный файл из своего существующего
cache, проверяет SHA-256 и JSON-object, отклоняет абсолютные пути, traversal,
symlink на любом компоненте пути, oversized/битый JSON и дубликаты binding.
MCD не выбирает имя файла, не создаёт default-профиль, не получает MCC
credentials и не подменяет повреждённый descriptor локальным профилем. Если
настроен MCC URL, authority остаётся за authenticated MCC response, а offline
descriptor не используется. Portal доставляет байты профиля, authority/source
record и digest; MCD выполняет только generic admission и execution.

Private external runtime input uses the existing synchronous framed stdin seam.
An active external handler may declare `runtime_input_schema` as the generic
`{"type":"string-map","fields":[...]}` descriptor; the field names belong to
the external scenario, not to MCD or an individual component. MCD validates
only the exact string-map shape, the global context-size bound, the required
`framed-runtime-input` capability and the descriptor's
`--runtime-input-stdin` command flag. Values remain in memory and the framed
capture path rejects private-value echoes; public fetches, progress and
receipts contain only the typed non-secret terminal result. The legacy Wazuh
registration-password contract remains compatible. The extracted Zabbix
monitor-user bootstrap must be an explicit operation; routine APT observation
must not invoke it implicitly.

### Corporate HTTP client identity

Corporate requests identify the actual client as
`mcd-agent/<installed-version> corporate-managed/1.0`; an explicitly supplied
User-Agent is preserved case-insensitively. The shared Request constructor
copies headers and does not change bearer credentials, URL, method, body,
TLS verification, routing or retry policy. This covers state/signals, service
profiles, config/runtime sync, authorization and artifact/download clients.
Portal keeps its existing independent transport. Tests exercise native JSON
callers and the actual state sender, proving payload/auth preservation. Live
acceptance requires standard unmodified CLI and genuine HTTPS heartbeat; a
diagnostic header probe alone is not an installed fix or registration receipt.

### DNS-bound Portal endpoint cutover

У уже зарегистрированного DNS-bound агента endpoint нельзя менять повторной
activation или replay enrollment material: activation создаёт новую identity,
а enrollment сохраняет immutable binding. Для переноса существующей связи MCD
использует только `mcd-commercial-endpoint-cutover` и контракт
`portal-mcd-dns-agent-cutover-request-v1`. В запрос входят существующие
`account_id`, `commercial_key_id`, `agent_id`, точные installation/domain,
ACK cursors, inventory revisions, pending receipt IDs и SHA-256 публичной
pre-state проекции; credential остаётся только Bearer transport secret и не
попадает в payload, receipt или журнал.

Новый endpoint обязан быть HTTPS DNS endpoint. Portal сначала аутентифицирует
существующий credential и подтверждает exact binding/cursor continuity без
replay или collision. MCD принимает только typed response с теми же IDs,
installations, ACK cursor, request/idempotency keys и `no_replay=true`.

Inventory revision допускает только semantic reconciliation. MCD и Portal
сравнивают SHA-256 canonical inventory без `request_id`, `observed_at` и
`snapshot_revision`, после published Portal projection: локальный top-level
`installation` descriptor и instance provenance `identity_source`,
`local_identity`, `source_instance_id` не входят в сохранённый Portal snapshot.
Richer local inventory при этом не переписывается. Если content hash одинаков,
локальный revision counter
может быть поднят до подтверждённого Portal floor; durable payload facts не
меняются, а receipt содержит pre/post counters и неизменный semantic hash.
Разный hash fail-closed требует отдельного authenticated inventory adopt и не
разрешает endpoint switch.

После первого подтверждения MCD одним compare-and-swap меняет
`portal_api_base` и, только при доказанном semantic equality, inventory revision
metadata. Затем он повторяет тот же идемпотентный authenticated probe и сверяет
continuity digest всей остальной commercial state, включая poll requests,
очереди и receipts. Ошибка postcheck атомарно возвращает старый endpoint и
inventory counters; mismatch, downgrade, concurrent state change или
невозможный rollback завершаются fail-closed. Повторный запуск выполняет те же
сетевые проверки и возвращает idempotent receipt. Transport/HTTP failure receipt
сохраняет только bounded typed Portal cause (`code`, HTTP status, retryable), не
URL, response body, credential или header values; это позволяет отличить local
prepare failure от edge/origin rejection без раскрытия секрета. Контракт и rollback проверяются
`tests/test_commercial_endpoint_cutover.py`.

### OWH-AUTO и generic command broker Portal↔MCD

Автоматическая первая установка OWH является отдельным self-hosted потоком.
MCD принимает только `portal-owh-auto-install-request-v1`, проверяет совпадение
`target.dns_name` и `target.local_identity`, HTTPS-политику и единственный
commercial API key. `agent_id` и `portal_api_base` не добавляются в payload:
MCD получает их из уже установленной runtime-конфигурации и до любого запроса
останавливается с `owh_auto_worker_config_missing`, если они отсутствуют.
Ответ регистрации должен быть `activated` с активным automatic state; ручного
Activate для этого пути нет. Manual enrollment остаётся отдельным pending-путём
с `install_scenarios=false`.

После активации MCD последовательно обрабатывает ровно четыре внешние стадии:
`base_packages`, `database_cache`, `web_stack`, `mautic`. На каждой стадии он
формирует запрос latest-compatible из наблюдаемых фактов текущего хоста,
получает внешний artifact, принимает только HTTPS URI, положительный размер и
`sha256:<64 lowercase hex>`, скачивает содержимое и сверяет размер/хеш. MCD не
содержит команды установки, версий Ubuntu, hostname/IP или Mautic-правил:
исполнение делегируется внешнему сценарию через фиксированный generic executor.
После каждой стадии сохраняются stage receipts, затем terminal receipt; API key
не попадает ни в receipt, ни в журнал, ни в UI. При сбое стадия закрывается
failed receipt и terminal failure, недостающий artifact не считается успешным.

Для уже подключённого MCD generic broker использует существующий outbound
HTTPS poll. MCD публикует через
`POST .../agents/{agentId}/installations/{installationId}/capabilities` только
валидированный capability inventory с self-digest SHA-256. Portal job принимается
только при exact match с последним опубликованным inventory: capability, command,
version, `execution=generic_primitive` и `mutates_state` должны совпасть. Shell,
script, argv, executable, sudo, пароль, ключ, token и подпись отклоняются до
исполнения. Generic primitive вызывается тем же внедрённым MCD handler, который
использует локальные typed primitives; arbitrary shell route отсутствует.

Каждая команда inventory также имеет `target_scope`: `host` для общих
хостовых операций и наблюдений и `instance` для операции конкретного
Mautic-инстанса. Публичный capabilities wire содержит в `installation` ровно
`installation_id` и `dns_name`; tenant coordinate туда не входит и не участвует
в self-digest. Instance-scoped managed job обязан отдельно содержать
`account_id`, совпадающий с admitted managed instance, и явный
`target_scope=instance`. Host-scoped job, включая cache sync, tenant field не
несёт: он связан agent/installation и не может вызвать instance semantics.
В целевом Portal 1.7 wire MCD публикует `target_scope` сразу в первом
capabilities payload и пересчитывает digest по этому полному payload; ACK
фиксирует доставленную ревизию, но не является переключателем формата.
Own legacy job без поля допускается только как single-owner compatibility path
и привязывается к scope уже advertised inventory; managed legacy job
fail-closed.

Inventory содержит безопасные read-only-примитивы:
`read_host_facts`, `check_prerequisites`, `check_web_php`,
`check_mysql` и `check_redis` (каждый `version=1.0`,
`execution=generic_primitive`, `mutates_state=false`, с собственным
`input_schema_ref`). Вход этих примитивов обязан быть пустым объектом.
С MCD 1.6.32 дополнительно объявлен `capability_id=inventory`,
`command_id=rescan_inventory`, `version=1.0`, `execution=generic_primitive`,
`mutates_state=true`, `input_schema_ref=portal://mcd/input/inventory-rescan-v1`.
Вход — только `{}`: пути, argv и политика discovery не принимаются от Portal.
Handler загружает текущую локальную конфигурацию без восстановления из MCC,
вызывает штатный `InstanceInventory.rescan(..., preserve_manual=True)` и
закрывает локальное соединение. Результат содержит только status и
`discovered_count` (число обнаруженных записей, не общее число с manual).
Ошибка discovery возвращает фиксированный код без текста исключения.
После принятого terminal следующий обычный outbound цикл публикует настоящий
inventory с новым revision/ACK; команда не создаёт регистрацию, не меняет
профиль, периодический scan, vhost или данные Mautic. Portal обязан дождаться
этого inventory, а не подменять его успешным receipt. Команда проходит те же
advertisement, binding, TTL, lease, durable started/terminal и replay gates.
Тесты `test_commercial_primitives`, `test_daemon_commercial_guard` и
`test_commercial_generic` проверяют пустой input, сохранение manual-записи,
текущую локальную discovery-конфигурацию и доставку terminal после рестарта
без повторного rescan. Live evidence требует authentic command receipt,
новый inventory revision/ACK, фактический root/версию и ровно один экземпляр
под прежней DNS-карточкой.
Для independently versioned external operations MCD публикует встроенную
host-scoped capability `external_operation_cache/sync_binding`. Её durable
input содержит только catalog revision, closure size/media/SHA-256 и ожидаемые
`binding_schema`/`binding_sha256`; URL, path на хосте и grant в durable job не
попадают. Portal добавляет ровно один `artifact_delivery` только в poll-ответе,
с одноразовым grant, привязанным к account/agent/control-installation/job и
активному MCC catalog artifact. Worker скачивает closure только через
authenticated `PortalClient.download_artifact`, проверяет размер и SHA-256,
сохраняет content-addressed ingress и удаляет весь transient delivery до записи
pending delivery/job digest. Рестарт поэтому никогда не хранит grant: он либо
повторит безопасную доставку до durable admission, либо использует уже
проверенный ingress object. Origin-relative `artifact_path` с ведущим `/`
заменяет path настроенного Portal base на том же HTTPS origin; он не дописывается
после `/api/v1/commercial`, а абсолютный URL другого origin отклоняется.

`sync_binding` не запускает внешний сценарий. Он проверяет gzip closure,
закрытый список members, digest каждого resource и canonical binding preimage,
затем append-only устанавливает executable bundle, operator profile и binding
identity. Existing manifest entries сохраняются, включая key-less group rows и
несколько entrypoint rows одного immutable bundle key; конфликт точного
key/entrypoint, component/operation,
resource или content-addressed path завершает job fail-closed. Manifest меняется
последним: незавершённый import может оставить только неактивные digest-файлы,
но не capability.

После полной проверки cache MCD добавляет host-scoped capability
`mautic_lifecycle/1.0` с четырьмя командами: read-only `preflight` и mutating
`create`, `cleanup_owned`, `recreate`. Каждая команда рекламирует точные
`binding_schema` и recomputed `binding_sha256`; generic job обязан повторить их
без изменений. Operation input является exact string-map внешнего контракта,
а artifact/profile/catalog pins обязаны совпасть с установленной binding
identity. `target_instance_uid` — стабильная сущность без `@host`; host остаётся
только transport/routing coordinate и отдельным диагностическим полем, но не
частью UID и не показывается как идентификатор в UI. Cleanup/recreate допускаются
только с canonical ownership receipt, а любая mutation — с canonical preflight
receipt и before-state hash. Исполнение идёт через существующий
`run_configured_external_operation(..., offline_only=True)`; OS/Mautic recipe,
пути и provider formulas в MCD отсутствуют. Durable started/terminal receipts,
ACK cursor и replay semantics остаются штатными generic broker semantics.

Portal может создать или заменить instance projection только после succeeded
terminal `create`/`recreate`, чей ownership receipt совпадает со stable UID,
DNS, root, DB/user, vhost paths, plan hash и artifact binding. `preflight` и
`cleanup_owned` сами по себе регистрацию не разрешают.

Instance-scoped managed generic jobs carry the tenant `account_id` inside the
job installation binding. MCD admits that value only when it exactly matches the
`managed_instances[instance_uid].account_id` material imported from the
server-issued carrier, the installation ID resolves to that same native UID,
and the advertised command is instance-scoped. The public capability inventory
always has the exact two-field installation identity and never carries tenant
metadata. This local-state check is performed before any instance handler or
typed Mautic command runs; host-scoped jobs carry no tenant field. Own jobs
remain backward-compatible with the existing host account binding. The
server-side Portal flow owns provider account references and injects the tenant
account only into instance jobs; the customer response never contains the
provider credential. Evidence: `tests/test_managed_tenant_contract.py`,
`tests/test_managed_portal_identity.py`, and the redacted live managed
`mautic_version` receipt.

Перед admission generic job MCD проверяет TTL также для сохранённого poll delivery.
Просроченный job получает durable typed terminal receipt
`portal_mcd_generic_command_expired` без вызова capability handler; после доставки
receipt курсор продвигается. Это проверяется cached host_facts regression.
Terminal sequence вычисляется из сохранённого last_sequence; для состояния
старых версий started_sent означает уже занятый stage sequence 1. Отказ после
started получает следующий sequence, без повторного вызова capability handler.

Заблокированный generic terminal outbox сверяется через authenticated GET
`agents/{agentId}/installations/{installationId}/commands/{commandId}/state`.
Только точное совпадение binding, job, command и delivery cursor, статусы
cancelled/failed, аудит job_ttl_expired и сохранённые receipt hash metadata
разрешают supersede. Исходный receipt и полный ответ Portal сохраняются в
`commercial_generic_superseded_receipts_v1`, затем закрывается этот cursor.
Ошибка HTTP, несовпадение или отсутствие доказательства сохраняют outbox.
Ни HTTP 409, ни локальный TTL сами по себе отмену не доказывают.
Generic result допускает глубину 8 для classification evidence host_facts;
входные команды сохраняют лимит глубины 5 и запрет произвольных команд/секретов.
Ошибка формирования terminal превращается в durable sequence 2 failure
commercial_generic_terminal_construction_failed без включения сырого результата.

Host classification ищет console только в approved roots /var/www, /srv/www,
/opt/mautic: непосредственно, в одном дочернем каталоге и его project/public_html.
Симлинки root/каталогов/console не обходятся; максимум 128 дочерних каталогов.
PHP-FPM версии обнаруживаются read-only list-unit-files с фильтром имени
php[major.minor]-fpm.service (также php-fpm.service); каждый допустимый unit
проверяется is-active. Evidence содержит все реально активные версии, а не
версию PHP CLI; схема evidence сохраняется, digest новых фактов пересчитывается.

Для generic job MCD сначала durable фиксирует `started` receipt, затем вызывает
handler и durable фиксирует terminal result. Receipt sequence — монотонный,
Portal idempotency — `{job_id}:{sequence}`. При недоступном Portal receipt
остаётся в общей durable очереди; после рестарта worker сначала дренирует её,
восстанавливает сохранённый result и не запускает primitive повторно. Cursor
продвигается только после принятого terminal receipt. Этот канал не является
MCC-каналом: публикация MCD-кандидата и продвижение в `test`/`approved` всегда
передаются треду **Publish MCD**; MCC лишь регистрирует каталог.

### Declared database repository verification

Repository setup errors and unrepaired APT refresh errors terminate profile
apply before package installation, purge, or upgrade. A skipped DB repository
is recorded as unapplied. Apply-once success is reusable only for successful
statuses with the same policy hash and an actually present repository. Fresh
hosts requesting DB packages need an explicit supported repository target if
installed-stack detection cannot select one; detection does not authorize an
implicit major upgrade. Unsupported explicit targets fail closed.

For an explicit MariaDB target, repository setup version must agree with its
major/minor family. Before installation, each requested MariaDB server/client
APT candidate must match that family; after installation, dpkg versions must
match independently. Missing candidates and distro fallback are errors, even
when apt update or apt install returns zero. Tests in
`tests/test_apt_db_fail_closed.py` cover cache repair, absent repositories,
empty-host detection, candidate and installed mismatches, version conflicts,
and prevention of package operations after repository errors. Host evidence
must include actual installed versions and a fresh native full-state receipt;
profile metadata alone is not readiness evidence.

The installed wheel's supported native CLI is
`/opt/mcd/venv/bin/mcd-cli`; it does not require a global PATH wrapper.
For example, native IPv6 policy operations are
`/opt/mcd/venv/bin/mcd-cli env ipv6 disable --config /opt/mcd/etc/mcd.toml`
and `env ipv6 status` with the same config. Optional `/var/www` preparation
remains controlled by the fetched profile's `ensure_var_www` flag.

### Image installation runtime acceptance

Image installation removes imported var/cache and var/logs after patching local
configuration and importing the database. It must then run the selected PHP's
`cache:warmup --env=prod --no-debug --no-interaction` as www-data before writing
and publishing the nginx vhost. Symfony's nonoptional Doctrine proxy warmer
creates the proxies required when production automatic proxy generation is off.
A nonzero ownership/prod warmup result aborts installation. Optional mail
configuration may invalidate cache, so it is followed by another prod warmup.
There are no cache mutations after the final warmup and verification.

Before inventory rescan and status=ok, probe `https://<domain>/s/login` through
127.0.0.1 with the actual hostname/SNI, standard CA/hostname validation, and no
proxy/redirect/HTTP fallback. Require HTTP 200 and a password form; capture safe
`runtime_warmup` and `runtime_verification` fields in the result. Missing/bad
certificates, HTTP 500, redirects and generic 200 stubs fail the gate and enter
the existing rollback transaction for this newly created installation. Even
when certificate issuance is skipped, the HTTPS gate requires valid existing
TLS; `--no-certbot` does not bypass readiness verification.

The existing source-operation lease prevents concurrent MCD replacement during
the operation. Inventory rescan is observational; no new discovery or scheduler
marker layer is introduced. No concurrent cache invalidation was demonstrated
in the failing vanila7 evidence. Tests cover warmup/publication/verification
ordering, mail-triggered invalidation, failure rollback and inventory exclusion,
and TLS/status/login validation. The live scenario is verified with a fresh
explicitly authorized disposable install through the normal deployment path;
existing failed instances are preserved as evidence rather than manually warmed.

### External installation scenario boundary

The external script owns installation algorithm, ordering, conditions,
algorithm-specific downloads and rollback. System-dependent package/repository,
service, path/owner and configuration-generation actions belong to external
blocks. MCD retains its own rings, scheduler, queues, task supervision and direct
Mautic management. All bin/console commands, including Doctrine migrations, are
performed by MCD through its existing execution mechanics; scripts request these
operations via mcd-cli and never duplicate console/runtime resolution logic. The legacy managed
image installer remains only during migration until its external replacement
has passed the normal-path scenario test; its removal is required to complete
the migration. Its previous release history is not rewritten.

Managed execution reuses `mcd-cli custom --config <path> --no-detach
--expected-sha256 <sha256> <immutable-key> -- <declared-script-arguments>`.
The existing custom manifest version 1 supplies key/file/sha256/run_mode and
interactive metadata. `run_mode=direct` and `--no-detach` preserve the command's
actual exit status and script progress/result stdout. An expected pin must be
64 hex characters and must equal both the selected manifest SHA and downloaded
bytes. Download into isolated staging and atomically publish only a verified,
content-addressed file; a failed fetch or integrity check cannot replace a
previous executable revision. CLI machine-output notices go to stderr.

Only nonsecret inputs and existing local configuration/secret references are
passed as arguments. External blocks own their progress/result JSON and
readiness/rollback semantics. MCC owns managed block artifacts, their immutable
manifest entries and dispatch; Operations executes/verifies the host task.
Portal owns its OWH external composition and existing framed FD3 protocol.
This does not add a DAG engine or convert custom stdout into the OWH protocol.
Both routes may consume shared external blocks through their declared inputs;
composition and protocol adaptation remain outside MCD installation code.

The existing approved external scenario folder architecture must be inspected
and reused for common/family reusable blocks plus external target descriptors.
Do not create per-distribution/version copies when only target names or package
and repository parameters differ. Overrides require demonstrated command or
behavior differences. No installation artifact tree, embedded script seed or
OS policy is added here.

### Workload and hardware profile independence

`[profile].name` is the MCD HOST workload choice; `[hardware].profile` and its
selection marker identify component/hardware settings. Hardware reconciliation
may update only the hardware coordinate and marker. It never clears workload
runtime overrides, sanitizes a workload profile, reconciles workload cron, or
emits a workload-profile change event. Startup and hardware-auto follow the
same boundary. Explicit workload selection does not write the hardware marker.
Hardware-auto seeds its marker from the current hardware coordinate, not from
the workload name. State pushes retain both existing independent fields.

This fix adds no hardware thresholds, provider rules, profile authority or
component matrix. One-time recommended HOST selection and external component
configuration must consume existing MCC data through the applicable approved
transport. Corporate managed hosts remain on their corporate lane; they are
not silently re-enrolled into the separate Portal OWH lane. Tests exercise real
runtime override preservation and CLI startup/auto/manual selection effects.


### 1.7.0 candidate: instance-scoped Mautic commands and registration

This section describes unshipped candidate behavior, not completed acceptance.
Before registration, an external non-Mautic installer prepares resources and
registers actual created facts through the existing inventory. After registration,
commands use stable instance identity: `mcd-cli exec --instance-uid <uid>
--command cache:warmup --console-arg=--env=prod --console-arg=--no-debug
--console-arg=--no-interaction --timeout 600`. No PHP/root/user arguments are
needed in that command. MCD resolves exactly one registered UID and complete
runtime metadata; missing/ambiguous/incomplete data fails before execution, never
selecting a first instance or global PHP. Literal arguments pass to the existing
executor. Direct version and doctrine migration operations are exposed without
changing workflow ordering or repairing known broken upgrade/migration flows.

`instances add` accepts explicit UID, runtime PHP/user and `--json --operation-id`
for actual created facts. The existing SQLite inventory transaction serializes
creation and emits created-versus-existing receipt. Existing replay preserves DB
metadata and never grants rollback ownership. Only newly created registrations
carry the existing job/request ID in `registering_operation_id`; there is no new
UUID credential/key layer. Owned removal matches exact UID/root/operation ID,
without broad name/root fallback. Rescan preserves stable identity, declared host
runtime and registration operation; complete explicit manual records survive a
pre-vhost scan. Tests: `test_registered_instance_runtime` and
`test_cli_console_arguments`; all ordinary CLI paths stay compatible.

Hardware and HOST workload profiles remain independent universal external MCC
data, shared by digest across families. External family/distro config generators
consume these values; only real path/owner/layout differences justify overrides.
First family sync includes its scenarios/generators and referenced universal
objects; later sync reuses valid SHA bytes. Complete selected-task cache remains
usable offline, including manual copies. Family catalog integration, commercial
archive launcher/prefetch and final source cleanup remain unfinished.

Evidence limits: local regression and fixture subprocess tests do not establish
native installation acceptance. A prior Docker-descriptor-injected Mautic version/
warmup proof covers real commands only. Actual installed R2 CLI without injection,
external block integration and 1.7.0 publication remain outstanding. The shipped R2 CLI preferred an obsolete /opt/mcd/src marker; the runtime-only
candidate removes that default read and reports the imported runtime version.
Acceptance verifies imported paths, installed file hashes and distribution
metadata. The updater requires a verified manifest/wheel, activates artifact
cache under /opt/mcd/runtime and cannot install a project checkout. The bridge
retains a legacy tree only during its guarded activation transaction, then
retires it after verification; a failure restores the original runtime/state.
Fresh bootstrap is owned externally and requires its own new revision removing
the legacy src copy. R2 native CLI version/warmup has passed without injection;
R3 clean Ubuntu clone and external consumer acceptance remain pending.


Native runtime-only acceptance checkpoint: the unlisted R4 artifact was activated
on the isolated Ubuntu clone with an explicitly SHA-pinned current bridge. The
first infrastructure attempt used an old release-checkout bridge and was rejected
by owner postchecks; its package install was not counted as a clean migration.
The corrected bridge moved the exact same pinned manifest from legacy src to
runtime (equal-version layout migration only; changed manifest is rejected).
Real-venv QA covers that admission, idempotent replay, running PID/task retention
and failure rollback. After successful activation no project src or transaction
source backup remains. A separate historical artifact directory was archived
outside installed runtime, content-fingerprint verified and retired only after
unit/process/cwd/pth/sys.path consumer checks.

Actual installed CLI postchecks without injection verify runtime manifest/payload,
site-packages import hashes against the wheel, matching running/installed/package
version with no source marker, health, instance-UID Mautic version, preserved TOML
hashes, inactive daemon and strict HTTPS 200/SSL verification 0. External block
1.3.1 had separately passed existing-instance replay and UID warmup with HTTPS 200
before/after. These are isolated checkpoints: fresh image creation/owned-new-row
rollback and fresh bootstrap native acceptance, remaining system-dependent
extraction and canonical 1.7.0 publication are still outstanding.


### PHP system-profile first working extraction checkpoint

At the extraction checkpoint, the existing MCD PHP writer and the external `php-system-profile` candidate passed
native normal and controlled validation-failure comparisons on separate serial
runs from the same canonical reset baseline. The actual universal MCC export is
passed intact as `resolved.profile`; no merge, capacity override or invented
profile data is used. Six rendered files, complete apply/noop return dictionaries,
real FPM validation, sysctl and HTTPS match. The identical invalid pool fixture
makes real FPM validation fail with code 78; both actors restore six managed
files, preserve kernel/master PID and keep HTTPS 200. Each fault is removed only
by canonical full VM reset, not a manual repair. Final reset is independently
verified healthy and fault-free. Evidence:
`docs/qa/mcd-php-system-profile-native-parity-20261031.json`.

This proves the transferred writer algorithm, not the completed legacy CLI
migration. The writer was retained at that checkpoint until the MCC catalog/task
descriptor selected the verified external composition and the legacy CLI
preserved its results through generic dispatch. The current cleanup checkpoint
below records its subsequent removal. Artifact versions/paths and tuning values
must not be embedded in the compatibility adapter.

The candidate decoder accepts optional object `result` inside the existing
terminal result frame. Existing framing/schema/version, counters, status,
secret-shape and size bounds remain in force; summary-only frames are unchanged.
The framed runner exposes it as `runner.result`. External composition owns the
explicit public payload; MCD does not choose PHP fields or workflow order.
Non-object payloads are rejected. Tests exercise an actual child result roundtrip
and guard rejection. Generic `custom --capture-result` runs synchronously,
collects stdout separately from stderr under the existing process output bounds,
and adapts raw JSONlines to the same validated terminal-frame decoder. It returns
the exact public result object only after a successful terminal matching the
child exit code. Missing/duplicate/invalid terminals and missing public results
fail closed; interactive, detached and fetch-only capture are rejected. Ordinary
custom execution is unchanged. Capture adds no implicit process lifetime limit.
Tests use actual child processes, including stderr impersonation and failure
cases. The legacy CLI seam still requires native catalog integration proof;
no all-entrypoint support or activation is claimed by this extension.


Runtime-only mismatch reporting compares installed distribution metadata with the
actually imported runtime even when no source tree exists. A stale metadata
version must set package/version mismatch flags; removing implicit source reads
must not hide that condition. Explicit development source diagnostics remain
separate. A regression test first reproduced the missing-source false-negative
and now verifies detection without reading a parallel project tree.

Runtime archive builder tests create an isolated Git fixture with canonical
refs rather than assuming the developer's current branch equals origin/main.
They exercise the real package payload, valid provenance, wrong-commit rejection
and feature-checkout rejection. The production canonical-origin gate is unchanged.


The custom CLI seam has additionally passed on a canonical disposable Ubuntu
lease with an unlisted capability-bearing wheel: cold execution used the actual
published candidate manifest and pinned archive, returning exactly the existing
writer's native apply dictionary. Warm `--capture-result` returned the exact noop
dictionary while an audit observer blocked any urllib request (zero attempts).
Six rendered files, FPM validation, kernel state and verified HTTPS matched the
native reference. This proves generic custom delivery, not activation, the
legacy PHP command adapter or a published runtime capability. Evidence:
`docs/qa/mcd-custom-cli-native-seam-20261031.json`. The canonical executor owns
full reset after the mutable test; no customer host is part of this proof.


### Service-profile descriptor migration (unreleased)

The existing PHP service-profile POST advertises actual
`external_handler_capabilities`. Runtime service-profile dispatch accepts only
`state=active, active=true` descriptors. The temporary candidate CLI flag/POST
opt-in used during isolated R7 proof has been removed. Version alone never grants
capability. Descriptor admission checks transport, component,
resolved plan, required capabilities and the canonical SHA-256 of the complete
`response.profile` (sorted compact UTF-8 JSON, no ASCII escaping, no newline,
finite JSON numbers only). This profile-data canonicalization is separate from
the existing framed protocol canonicalization. Artifact key/hash/entrypoint and execution arguments come
from the descriptor; MCD embeds no artifact version, plan tuning or recipe.

An explicitly selected external handler executes through the generic custom
cache and validated public-result capture. Descriptor admission, pin mismatch,
unsupported dry-run or child failure never falls back to the legacy writer.
Dry-run requires its own `dry_run_command` descriptor and never uses apply argv.
The old PHP writer and its temporary compatibility branch have now been removed
following native static/override/failure acceptance. Missing, inactive or invalid
handlers fail explicitly; there is no embedded fallback. All profile tuning and
file rendering belong to the external artifact, including exact operator
response profiles outside the catalog snapshots. The candidate probe has retired; the native cleaned-source recheck uses the
ordinary CLI and an active descriptor in the isolated QA API only. Production
activation remains a separate source-owner release gate.

The result collector has no lifetime deadline in synchronous custom mode,
including when a long-running child closes its output streams early. The framed
runner's default deadline remains unchanged. A real child sleeping beyond the
previous five-second post-output wait verifies this behavior.

The consumer independently admitted all 22 owner-exported live profile selections
and matched 110 pure rendering results against the existing writer, without
copying universal profile data into MCD. This is not native apply proof; see
`docs/qa/mcd-php-all-live-plans-consumer-20261031.json`. Legacy CLI native
HTTPS integration has passed using the isolated MCC API/DB and canonical
disposable lease; no fabricated production host or billing identity is permitted.


The legacy service-profile CLI normal path has passed through the real isolated
MCC HTTPS API (normal certificate verification with process-scoped CA trust),
public custom manifest and verified archive, generic dispatch and native PHP
writer. Dry-run, apply and noop dictionaries exactly match the old native
reference; dry-run preserves files/kernel, apply matches six rendered files,
FPM validation and verified HTTPS remain healthy. Evidence is staged in
`docs/qa/mcd-legacy-service-profile-cli-native-20261031.json`. The controlled failure case also passed from a full reset: real FPM validation
returned 78, the CLI propagated the validated failed external terminal and the
six managed files returned to their original absent state. Kernel state and
master PID were unchanged; verified HTTPS stayed healthy. The terminal retains
the existing `rollback=not_attempted` summary, while native file restoration is
independently observed; this transfer does not invent a new rollback claim.
Canonical full reset is mandatory after the retained invalid test fixture.
Operator-override native proof and reset remain required before old-writer
deletion, candidate activation or release.


PHP source cleanup removes 13 PHP-specific writer/render/detection functions and
three tests transferred to external-owner commit f8900b8 (seven PHP tests PASS).
Shared `_service_exists` and `_write_file` remain because MySQL still calls them;
MySQL and all direct Mautic/core paths retain their existing behavior. Runtime
boundary/audit records distinguish this completed component from remaining
recipes. The cleaned R8 package also passed the ordinary CLI recheck without a candidate
flag/POST opt-in: active descriptors were enabled only in the isolated QA API.
Installed module hashes match the cleaned commit, all 13 legacy PHP functions
are absent, shared MySQL helpers remain, and dry-run/apply/noop preserve the exact
operator profile and native file/service behavior. Final canonical reset after
R8 has also passed independently: managed/fault files and source checkout are
absent, kernel backlog is back to baseline, DB/FPM/nginx are active, MCD is
inactive and HTTPS remains certificate-verified 200. PHP component acceptance
does not imply completion/publication of the overall 1.7 release.


### MySQL working-generator batch (in progress)

The next extraction is the existing `apply_mysql_profile` closure: 19 function
bodies and three constants, with real MCC live profile export pins and 66 pure
render fixtures across MySQL/MariaDB/Percona. This is host DB tuning, not Mautic
code/schema repair. The old writer remains until native before/after and catalog
CLI proof. Existing dynamic partial/deferred behavior and no-surprise-restart
semantics are preserved; real Galera native execution is not claimed.

Generic descriptor admission now also verifies the requested component against
the response/handler. MySQL execution requires an explicit
`runtime_context.cluster_id` and compares its exact string/null value with the
current loaded MCD config before artifact execution. Missing is not null; null
is valid only when both sides actually contain null, and no blank default or
plan-derived cluster membership is invented. The producer pins that same context
inside the composition data, so changing context changes whole-archive identity
while the profile object/pin remains unchanged. The core maintenance guard and
its shared Galera detection remain authoritative. Context admission is tested before execution. With frozen native parity now
verified, PHP and MySQL public compatibility dispatch both use the generic
external handler without a builtin fallback. The old MySQL function remains
only for reference until typed public-CLI acceptance; it is no longer an
automatic caller path. Typed CLI acceptance and cleaned-runtime recheck are
still release gates.


The first old-writer stock CPX22 native probe exposed an existing MariaDB 11.4
constraint: startup `max_user_connections=0` rejects runtime assignment to 134
with error 1290. Old behavior is partial on a config change, deferred on repeat,
with earlier SQL assignments retained; this is not repaired or reclassified as
normal success during extraction. The first diagnostic lacks a checkpointed
initial return and is explicitly not acceptance evidence. Fresh reset captures
will compare that failure contract. A separate explicitly approved QA-only
operator profile changes just max_user_connections to zero for the working
normal case; this is test data, never a runtime fallback or live catalog change.
See `docs/qa/mcd-mysql-generator-native-20261031.json`.


A fresh checkpointed old stock reference now records the expected partial /
deferred operation failure, persisted first four assignments and unchanged
later variables, durable drop-in, unchanged master PID and healthy verified
HTTPS. Verification resumed the saved first-return checkpoint after a harness
field-name correction; it did not repeat the first mutation. External frozen
before/after comparison and the explicit operator-zero positive case remain
pending. Existing broken DB behavior is not repaired.

Failure outcome projection is external: a typed component exception carries its
exact public result through the existing failed terminal's optional result
field. The generic MCD adapter reuses the frame validator and carries that
validated object to the existing legacy service-profile outcome mapper. Partial
remains error/CLI 1, deferred or skipped remains skipped/CLI 1 on apply; a failed
terminal cannot become success even if its payload says applied. Missing/unsafe
payloads keep the normal failure path. No MySQL business-status mapping is added
to the generic decoder, and no status-map DSL or new protocol is introduced.


The frozen stock external actor matched the complete old expected-failure
reference: before/after files, twelve observed variables, planned/partial/deferred
public dictionaries, error text, earlier SQL side effects, PID and verified HTTPS.
This is failure equivalence, not successful tuning. The exact authority-owned
QA operator-zero profile has now passed the old native normal path with ten
dynamic values verified; its external comparison and typed CLI proof remain.

Standalone complete-cache execution does not require a configured repository or
MCC URL. URL construction happens only after a cache miss; cached catalog lookup
can also succeed without URL configuration. Reproduced tests failed before this
change and now execute real cached script/gzip entrypoints with zero network
calls. Missing/corrupt required artifacts still fail before execution.


Frozen external e64 positive parity has passed against the old QA operator-zero
reference: full planned/applied/repeat dictionaries, ten dynamic assignments,
before/after files and variables, unchanged master PID and healthy verified
HTTPS. Both frozen standalone MariaDB paths are accepted, including the stock
expected failure. Native MySQL/Percona/Galera execution is not claimed. The
ordinary typed CLI/HTTPS consumer remains the next gate before writer deletion.


The ordinary typed MySQL CLI positive path has passed through the real isolated
MCC HTTPS API and its owner-published catalog without a candidate opt-in. The
58b archive was actually downloaded/cached; planned, apply and repeat public
results match the old QA operator-zero reference. Config, twelve observed
variables, ten dynamic assignments, PID and verified HTTPS agree. Installed
consumer module hashes match its source and no automatic builtin MySQL caller
remains. Stock negative CLI and cleaned-runtime recheck are still required.


MySQL extraction checkpoint after typed stock negative acceptance: both ordinary
CLI paths now preserve exact old public results. Stock partial/deferred remain
error/skipped with exit 1; observed validated terminal frames are failed/exit 1.
Files, twelve variables, unchanged PID and verified HTTPS match the references.
Following this proof, 17 old recipe functions and two recipe constants were
removed from MCD, superseding the earlier retention checkpoints above.
Core cluster-mode/Galera detection and its marker constant remain unchanged.
Three transferred writer tests were retired; 40 focused tests pass and full
regression is 2001 passed, 61 exact baseline failures, 57 skipped. Cleaned R10
installed CLI proof and final canonical reset remain required. No partial 1.7
release or production activation is authorized by this component checkpoint.


Cleaned R10 has now passed the ordinary installed CLI positive path through
the real QA API and catalog: exact planned/apply/repeat results, files, twelve
variables, unchanged PID and verified HTTPS. Installed source hashes match,
legacy recipe symbols are absent, core cluster detection remains. Full receipt
fc2eb57bebd152a6f8ecfd14d1507a65c868d299c8b7de15e3210ce0f96aeef7.
Final full canonical reset and independent baseline verification remain pending.


The final MySQL full canonical reset is independently verified: exact baseline
files and twelve variables, active DB/FPM/nginx, inactive MCD, HTTPS200/TLS0,
no source checkout and no QA actor directory. Receipt SHA256
2fe97f9ca999c8dc3060b2a6b0c8ac61d7f337999749f63d55d8952ee3e97e5e.
The scoped MySQL transfer is closed; remaining system recipes and complete
1.7 publication remain pending. Production handler activation remains held.


The next scoped transfer is optional explicit Docker runtime installation,
limited to _docker_runtime_state/apply_docker_profile. The daemon advisory
allowlist continues to exclude Docker. Existing CLI fetch remains a dry-run
operation, including ready-runtime noop. Preserve exact apt update/install
docker.io and systemctl enable --now docker commands, timeout1800, failure
results and root guard order (ready noop, dry-run planned, then root check).
The frozen external block preserves both AST bodies and reads no profile
values; its empty pinned envelope is existing composition admission only.
Old native dry-run/install/repeat/ready-dry-run passed on a clean Ubuntu24.04
lease; receipt d0773125924bca552fbe2a574b303d33be92a8effe3465e6847e7c38a647d683.
External direct/CLI parity and source removal are pending; old bodies remain.
See docs/qa/mcd-docker-runtime-native-20261101.json.


Frozen Docker external direct native parity passed against the old baseline:
exact planned/applied/repeat-noop/ready-dry-run-noop dictionaries and healthy
DB/FPM/nginx/verified HTTPS, without importing MCD. Receipt SHA256
6b283c0f2f6bdd0ef2c120cbfb20a587b434ac9485a6039986ec4c905db57ab9.
Docker now uses the existing capability-aware service-profile fetch and active
external descriptor dispatcher; no builtin fallback. The legacy two bodies
remain reference-only until ordinary installed CLI acceptance and cleanup.
CLI fetch retains its existing dry-run behavior; daemon exclusion is unchanged.


Ordinary installed Docker CLI acceptance now passed through the real isolated
MCC resolver/catalog: fetch-absent, dry-run, apply, repeat-noop, ready-dry-run
and fetch-ready results exactly match the old native reference. Active binding
requires no candidate opt-in; actual archive803d cache and verified health pass.
Receipt SHA491cdbe9ef66c298574abbe1b3d41cfb3bffe2933671a9418836a57e9d36ce1f.
The two replaced Docker bodies and unused imports are now removed, superseding
the earlier reference-retention checkpoint. Two transferred writer tests were
retired; owner pure tests preserve their invariants. Generic dispatcher tests
prove success/failure/raw-result/no-fallback behavior; daemon exclusion and CLI
fetch compatibility remain. Cleaned R12 native proof and final reset pending.


Cleaned Docker R12 attempt1 is NOT accepted: fetch/dry-run passed but apt-get
update remained in select wait until the unchanged 1800-second command deadline
(checkpoint-to-failure1803.19s), yielding CLI1 and failed/external_install_failed
terminal without fake rollback. Docker remained absent and existing services
healthy. Bounded repository probes passed; cause of the APT wait is unknown.
The runner/decoder/CLI module hashes equal the successful R11 versions.
Failure receipt290da381a54e8a570cf4a3ec0624f490332403cc0eaf0ba8ccf110d197508602
is retained alongside diagnostic619fdf3c…; no algorithm/policy/repository change
was made. Full reset and a separately recorded same-bytes fresh proof are
pending; this failure is not relabeled as success or proven network outage.


Cleaned R12 fresh attempt2 passed after a complete canonical reset, with the
same wheel/archive/recipe/timeout: all six ordinary CLI public results equal
the old baseline, Docker ready and verified health. Binding receipt SHA256
aea391cbfb822f990285b7d22c2fd24e134bee5f57a69858e4586a4c8c52667c
separately binds current R12 module hashes, fresh baseline and nineteen safe
read-only metadata samples over287.73s. APT2.8.3 and26source/config fingerprints
remained unchanged. Raw result receipt491cdbe9… equals R11 byte-for-byte because
its dictionaries have no per-run timestamp; current producer proof is separate.
First failed attempt290da381… remains NOT_ACCEPTED, with unknown APT wait cause;
a successful fresh run does not establish network outage or erase that failure.
Final full reset and independent clean-baseline verification remain pending.


Docker final canonical reset is independently verified: Docker executable and
package absent, all QA actor paths and source checkout absent, MCD inactive,
DB/FPM/nginx active and HTTPS200/TLS0. Receipt SHA256
3868d9f1106a1668c79972c470d3861027328912560c691ddf8e25539add03d3.
The scoped Docker recipe transfer is closed; first failed R12 attempt remains
recorded with unknown APT wait cause. Local owned temporary credentials were
removed and MCC server QA cleanup requested after CLEANUP READY. Remaining
system recipes and complete1.7 Test/Approved publication remain pending.


MCC confirmed Docker QA service/unit, isolated SQLite/catalog/publish tree,
TLS/private credentials removed and port18444 closed; evidence commitc834abf8.
Production APIs are healthy and the exact803d/4885 catalog row remains
candidate/active=false. Local owned QA credential bundle is removed.
This closes scoped Docker cleanup; complete1.7 activation remains held.


Next whole working recipe: Wazuh apply_wazuh_profile, frozen source module
604d81a8ca7e4c91dc7b32286d878242ae9e81752d607a7f6b70eeb3e5df6f5b.
The25-function closure includes9helpers used by core inventory observation;
those remain in MCD, with16recipe-only functions removable only after proof.
Existing local seven-test baseline passes. Core host identity resolution stays
in MCD; the recipe consumes effective_mcc_host_name/configured_host_name/
local_hostname. Existing registration_password is a supported secret input:
real secrets must never enter public immutable profile artifacts. Current no-secret live profile omits empty registration_password and unused
api_* fields; no new encrypted refs/keys/transport are introduced. Nonempty
legacy private-input support remains an explicit existing-channel gap, not
an excuse to publish secrets or change old bodies. No enabled Wazuh host write
or production enrollment has occurred in this checkpoint, and old bodies remain.
See docs/qa/mcd-wazuh-system-profile-transfer-20261101.json.


Wazuh consumer source now advertises the existing two capabilities and validates
handler runtime_context as exactly three explicit flat identity fields against
fresh core resolve_agent_identity before any download/execute. First two fields
allow explicit null; local_hostname must be nonempty. Missing keys, stale facts
or a mixed response/handler context fail before execution. This changes no
existing agent-name selection policy: all25old recipe bodies and fallbacks stay.
Old read-only native defaults pass (planned/disabled, before==after), receipt
fb70b48a8d68a1eaf58b42fac52084913c950b004807a7fe7c28e47ba2bd7f8b.
82focused tests and release-regression pass; full2031PASS/61exactbaselinefail/
57skip. Portal source06d68… has25exact bodies/16pure tests; synthetic executable
9ad697… is authoring-only and MUST be recomposed with controlled loopback QA
profile and actual current identity before native execution. Original inactive
MCC draft remains rejected; a local active-copy mocked probe is not acceptance.


Wazuh capability-aware requests preserve actual nullable identity fields rather
than applying the legacy string envelope's null-to-empty conversion; requests
with unavailable core fields fail without inventing them. Other components keep
their legacy envelope. Existing generic downloader UA mcd-agent/1.6.38 returned
HTTP200/exact806d…/8860B and zero additional GET on cache hit; no artifact was
executed or Cloudflare policy changed (receipt7764bd20…). Controlled native OLD
phase uses MCC-owned QA profile968bab… differing only by loopback manager and
registration addresses plus start_service=false. Current core identity fbbc317e…
was collected from the actual loaded test config; source604d… remains old code.
This actor is running, not accepted yet; no production manager/enrollment.


Wazuh old native loopback path passed with actual agent4.14.9-1: planned,
firstapplied and repeatapplied, config/keys/repo files captured, service remains
inactive as explicit QAstart_service=false, Mautic health verified. Receipt
5a5fcb3e4d2b9fb2f7b230c25ce7b8f0092daea7bb115c6fe2fa2699e02aa630.
After full reset/newboot, current core identity was reproven from the same
loaded operator config; exactQAarchiveaf44…/8869B passed standalone execution
without MCD generator imports. Planned/first/repeat dictionaries, file hashes
and state exactly equal old native; receipt
306736e791af79f7914f93510ff9ca04c1bb1c4035db0d4877f76a9b50cff4c9.
Neither synthetic9ad nor production-target806d artifact was executed. A new
full reset and normal unlisted R14 install are requested for ordinary CLI QA.
The16recipe-only bodies remain until that acceptance;9inventory helpers stay.


Ordinary installed R14 Wazuh CLI passed real isolated MCC HTTPS/catalog with
active descriptor/no opt-in: fetch-only, planned, first and repeat raw results,
file hashes and observed state match old native; actualaf44cache and health
verified. Receipt6416b23fadd1cc985a6189034d78cb58f2a40250be0d50d1d59735984c994e67.
Following that proof,16recipe-only functions and unused imports/constant were
removed;9unchanged core inventory observers remain in wazuh_profile.py. Three
transferred writer tests are retired, core routing/inventory tests retained.
79focused checks/release-regression pass; cleaned R15 native proof/final reset
remain pending. Nonempty legacy private-input gap is still explicit; no secret
ref/key layer has been added and no full1.7 release/activation is claimed.

Cleaned installed R15 ordinary Wazuh CLI also passed fetch, planned, first and
repeat against the unchanged isolated MCC QA endpoint. Source b8619b99 and wheel
de74b83d are bound to the installed-module proof and normalized parity receipt
6416b23f by private producer receipt a05663d5. Normalized facts equal R14; this
is a separate execution of the cleaned installed runtime. Raw logs were checked
for token absence before saving. Final full reset is underway; independent
verification and MCC endpoint cleanup remain pending. No public channel changed,
no production enrollment occurred, and full 1.7 acceptance is not claimed.

The final R15 sandbox was fully recreated by its infrastructure owner. Independent
read-only verification passed (receipt066149fa): Wazuh/Docker not installed,
/var/ossec, /opt/mcd/src and all three actor directories absent; MCD inactive,
MariaDB/PHP-FPM/nginx active, canonical HTTPS200 with verified TLS. No mutable
actor followed this reset. MCC received CLEANUPREADY for its isolated18443 QA
endpoint and owned credential bundle; cleanup acknowledgment remains pending.

MCC cleanup ACK received: isolated18443 service/unit, server QA database/catalog/
TLS/private keys/token and its local credential bundle removed; port closed,
production QA host rows zero, production APIs healthy and candidate inactive.
The no-secret Wazuh transfer is scoped CLOSED. Nonempty legacy runtime-input
compatibility and complete cross-component 1.7 acceptance remain release gates.

Wazuh private-input compatibility uses the existing authenticated service-profile
response.runtime_input, exactly {registration_password:string}, held only in
memory. The external custom capture path sends the inner object over the existing
process stdin primitive as uint32BE length plus UTF-8 JSON and EOF, bounded by the
existing256KiB context limit. The producer emits deterministic compact JSON;
ordinary valid JSON remains acceptable to standalone consumers. Optional
--runtime-input-stdin and framed-runtime-input negotiate this support; existing
no-secret two-capability descriptors remain valid. Input shape/capability/flag
mismatches fail before artifact activation; a nonempty password in the public
profile is rejected. Public fetch and apply.fetch omit runtime_input. Capture
never streams raw child output; private errors use fixed messages and accidental
known-value/normalized-value/digest echoes are rejected before public decoding.
Only the external Wazuh script owns the unchanged child environment/authd.pass
normalization and0600 file behavior. No new secret refs/keys/store/runner exists.
MCD focused104checks pass; full snapshot2042pass with exact61baseline failures,
57skip and105subtests, release-regressionPASS. Cross-owner pinned-artifact and
standalone success/failure proof still pending; see private-runtime-input QA JSON.

Final private-input UTF8/256KiB source snapshot:2044PASS, same exact61baseline
failure IDs,57skip and105subtests; no new failures after final wire alignment.

Private-input integrated synthetic acceptance passed (receipt11a345a8): the MCC
owner's authenticated API factory produced the response for exact archive69273b52
(sourcee2211ba3/skeletonbcc9d651, unchanged25Wazuh bodies). The inactive descriptor
was rejected without starting; a separate MCC-owned isolated active fixture
passed real MCD admission, verified offline cache, stdin process execution and
terminal decoding. Success and controlled failure exactly match the same manual
standalone invocation; leaky failure produces no private public payload. Old
package-child environment receives the exact string; authd.pass normalization,
0600 and original repeat semantics are preserved. All host-command helpers were
mocked, authenticated response transport was fixture-injected, and artifact HTTP
requests were zero: this is synthetic integration, not native enrollment or a
live-HTTP claim. Public cache/task/argv/stdout/stderr/receipt contain neither
value nor digest. Independent Portal6standalone cases pass; MCD105focused pass.
The compatibility gap is scoped accepted; MCC synthetic private-fixture cleanup
ACK and complete whole1.7 acceptance remain pending. No channel was published.

MCC synthetic fixture cleanup ACK received: both inactive/active private fixture
roots (including mode0600 responses and temporary public/custom trees) removed.
Only non-secret factory receipts, integrated receipt11a345a8 and public exact
archive69273b52 remain in its framed-runtime-input-evidence directory. Production
Wazuh stays inactive. No further private fixture is used. Wazuh native no-secret
and scoped synthetic private-input compatibility are complete; whole1.7 remains
on hold for all other required recipes and their preservation acceptance.

### Nginx: внешний host baseline и совместимые входы

Генераторы host baseline, default-заглушки и Cloudflare real IP выполняются
только внешним проверенным артефактом. В MCD нет запасного встроенного writer.
Замороженный исполняемый архив30750556 и генераторabcd65aa сохраняют старый
алгоритм, кроме отдельно порученного оператором исправления default-заглушки:
только HTTP80 с server_name _ и return444; если80 уже занят другим
default_server, остаётся только управляемый маркер. Блока443, доменов инстансов
и сертификатов в этой заглушке нет. Чужие default/vhost и сертификаты не меняются.

Операция default_stub_operation читает/меняет только свой управляемый файл.
Чужой файл и symlink блокируются. Перед записью сохраняется snapshot,
выполняется атомарная замена, затем nginx-t и reload. Ошибка восстанавливает
свой файл; конкурентная правка не перезаписывается. Повтор без изменений
не делает reload. Удаление инстанса не исправляет общую заглушку и сохраняет
сертификат, на который ссылается соседний vhost. Инварианты удаления только
своего vhost остаются в MCD до отдельного принятого переноса OS-транзакции.

Пять совместимых операций: default_stub_operation с apply; ensure_nginx_baseline
и ensure_cloudflare_real_ip с reload_service; nginx_baseline_satisfied и
cloudflare_real_ip_state без флагов. CLI nginx-baseline default-stub status|apply
сохраняет прежние аргументы и успех0 для ok/drift. Входы host и
cloudflare-real-ip используют тот же мост; публичный CF-профиль можно передать
JSON-файлом. APT/readiness/migration также вызывают этот мост. Старые публичные
имена nginx_baseline импортируют nginx_external, а не дублируют генераторы.

Standalone CLI читает расположение кеша из --cache-dir, MCD_CUSTOM_CACHE_DIR
либо существующей необязательной custom-настройки. Регистрация, load_config,
восстановление конфига, URL MCC и Portal не требуются. С доступным cfg первичная
синхронизация использует существующий service-profile endpoint. Запрос содержит
operation, operation_input и измеренный core-факт ipv6_listen_forbidden.
Только cloudflare_real_ip_state передаёт profile_present: false связывает None,
true — текущую проекцию профиля. Неактивный кандидат можно просмотреть, но он
не сохраняется как исполняемый локальный выбор.

### Выбранная задача и точный offline cache

selected_tasks находятся рядом с items в существующем manifest.json; отдельного
реестра, runner, latest-указателя или файла выбора нет. Запись содержит только
публичные component/operation/operation_input и ответ существующего контракта.
Ключ связывает component, operation и SHA аргументов, профиля и runtime-контекста.
Разные apply/reload, IPv6-факты и None/{} не затирают друг друга. Пароли, токены,
credentials и runtime_input в этом публичном кеше запрещены.

Перед сохранением проверяются координаты, profile/context/input SHA и текущий
core-факт; затем существующий downloader получает/проверяет точный артефакт.
Перед исполнением текущий профиль проецируется только по именам полей,
объявленным MCC. Все семь имён CF-полей приходят как метаданные, отсутствующие
значения не дополняются. Несовпавшие профиль, факт или аргументы дают точную
ошибку; None не подменяется {} или актуальным профилем MCC.

Удалённый refresh не отменяет допустимый локальный выбор: сохраняются его
прежние точные key/SHA/entrypoint и строка items, даже если latest удалил строку
или переиспользовал key для другой ревизии. HTTP404 также проходит этот merge.
Удалённые selected_tasks не получают локальную власть. Обновление сериализовано
блокировкой существующего каталога и атомарной заменой manifest; постоянный
дополнительный lock-файл не создаётся. Плановая очистка не удаляет каталог
артефакта выбранной задачи по возрасту, лимиту или отсутствию в latest; очистка
невыбранных downloads и logs сохраняется. Изменение выбора должно быть явным.

Чтение выбора не создаёт каталог и не обращается к сети. При полном совпавшем
кеше существующий runner работает offline_only без GET/HEAD; вручную положенные
байты эквивалентны скачанным, TTL/provenance/online-pointer не являются допуском.
Missing/corrupt archive и mismatch входов отвергаются до внешнего процесса.
Полнота внутренних обязательных компонентов проверяется до recipe-мутации;
verifier внешнего скрипта может запуститься, но неполный bundle ничего не меняет.
Невыбранные необязательные компоненты не препятствуют запуску.

### Nginx: доказательства переноса и оставшаяся граница

Canonical evidence: docs/qa/mcd-nginx-host-baseline-transfer-20261101.json.
Исправленный OLD и standalone DIRECT прошли на отдельных полных сбросах QA.
R17 source91f515ae:17 обычных CLI вызовов,5 отрицательных сценариев до процесса,
HTTP0 (receipt4de85710). После этой приёмки sourceb45e9449 удалил48 заменённых
определений и частные пути/генераторы/CIDR defaults. На этом checkpoint были сохранены12 неизменённых
AST-определений; после принятого vhost-переноса осталось пять core/shared
определений; это не разрешение постоянно держать OS-рецепты в core.36 перенесённых
recipe-test definitions (37pytest cases) приняты в suite владельца Portal;
MCD сохраняет CLI/cache/маршрутизацию, scoped delete и pending Mautic transforms.

Очищенный R18 прошёл17 CLI сценариев и6 отрицательных, включая архив с корректным
внешним SHA и отсутствующим обязательным компонентом, HTTP0 (receiptfad3b5d4).
Проверены реальные child PID, равенство бизнес-результатов OLD/DIRECT,
реальный nginx-t failure/rollback, сохранение соседей/сертификатов и HTTPS200.
Нормализуется только generated backup_dir; исходные значения сохранены.

Source3adea1f3 дополнительно устранил потерю выбора при404 и плановой очистке.
R19 receipt0536be96 подтвердил на установленном runtime: реальный404 refresh
сохранил34 выбора; очистка удалила только невыбранного старого соседа; затем
пять обычных CLI наблюдений сделали0HTTP и сохранили bytes/mtime nginx/сертов.
Все проверки используют site-packages и кеш, без /opt/mcd/src.
Итог:47 focused PASS,2047 PASS в полной регрессии, те же61 baseline failure IDs,
57skip/105subtests; release-regression PASS. Финальный полный reset принят:
независимый receipt51615d34, новый boot, MCD inactive, DB/FPM/nginx active,
HTTPS200; Docker, Wazuh, /opt/mcd/src и actor-файлы отсутствуют.

Текущий host-baseline scope не означает готовность всей1.7: ещё обязательны
pending OS lifecycle, APT/security/local-mail и прочие принятые границы.
Частичная публикация1.7 не выполняется; отдельно разрешён prerequisite
maintenance1.6.39 с исправлением updater, без сценариев1.7. После их общей приёмки
публикация MCD проходит через Publish MCD по штатному процессу.

### Nginx: существующие vhost этапы migration и composer move

Замороженная граница d6856678/supplement4950f91f принята по OLD, DIRECT,
ordinary installed CLI/actual caller aliases и очищенному R22. Внешний артефакт
nginx-vhost e3143074 и все22 member pins проверены; baseline307 не меняется.
Migration принимает root/domains/php_version, требует непустые domains,
применяет baseline без reload, проверяет PHP-FPM socket и существующий текущий
SAN-сертификат, пишет vhost/form/assets/http2/site/alias, делает nginx-t/reload.
Публичный carrier {site:str} возвращается Python-caller как прежняя строка.
Composer принимает source_root/nginx_root/php_version/site_available/site_enabled,
сохраняет backup, меняет root/socket/assets/http2, пишет site/alias, выполняет
nginx-t и reload, возвращает {active,backup}. Ненулевой nginx-t поднимает прежний
RuntimeError; rc reload, как в legacy, не проверяется. Отдельный vhost rollback
или ожидание HTTP-готовности не добавлены. DNS и выпуск сертификатов не входят
в эти этапы. Окружающие stable UID, прямые Mautic-команды, очереди, leases,
receipts, form-setting authority и lifecycle остаются MCD.

Actual migration callers вызывают внешний мост дважды в соответствующих
ветках, composer — один целый внешний этап вместо прежнего дублированного
write/test/reload. После R20 удалены12 заменённых определений, migration
шаблон и семь перенесённых recipe-тестов; прежние private writer имена — aliases
без встроенных тел. В nginx_baseline остаются пять core/shared определений.
_nginx_web_root остаётся нужен прямой Mautic media логике, read-only cert
preflight и остальные lifecycle callers ожидают собственных границ.
Исторические неудачные OLD попытки и R21 package-preflight failure сохранены
в evidence; они не объявляются PASS. Каждый mutable actor отделён полным reset.

### Pending: универсальные workload-профили и authority настроек

MCC подтвердил22 hardware recommendation записи (объект30b3b568) и
опубликовал неактивный universal HOST-workload resource210ac920, catalog
d8bcdfde/final handoffc1b49715:14 literal presets, custom operator-owned,
11 hardware-boundary rows. Это data-only resource без entrypoint, лежащий
в resources существующего manifest, не в исполняемых items. Freeze текущего MCD
74df87d1 содержит13 named presets, legacy farm,31 реально назначаемое поле и
11 унаследованных runtime default значений; это эталон для внешнего authoring,
а не вторая authority. Custom остаётся операторскими настройками. Поля
ring_count/max_segment_retries/segment_priority_stale_age_sec из proposed schema
не приравниваются молча к другим AgentConfig-полям и не заполняются догадками.

Внешние workload-объекты универсальны для OS/provider; hardware metadata только
рекомендует профиль. Явный выбор оператора применяется буквально без clamp по
железу. Текущие profile formulas остаются pending до готового replacement и
приёмки consumer/standalone; источник значений при переносе не меняется.
Полная потеря/обнуление после обновления восстанавливает все durable настройки
из MCC. Reset/отсутствие не считаются пользовательской правкой. Для настоящих
правок updated_at определяет направление в обе стороны; явный новый tombstone
отличается от reset. Восемь текущих MCD-проверок этих правил прошли, live
межкомпонентная приёмка universal profiles ещё обязательна. Evidence:
docs/qa/mcd-universal-profile-authority-transfer-20261101.json.

Generic universal reader проверен отдельно от cfg integration: существующий
cache/downloader принимает data_only без entrypoint/execute/chmod-x. Весь
явно выбранный resource проверяется по archive SHA и21 member pins без
извлечения/запуска. Literal values14 профилей равны owner-freeze,204 CPU/RAM
boundary pairs normal/farm равны текущим core-функциям; custom не назначает
fixed preset. Inherited global defaults не превращаются в profile-owned edits.
Ровно совпавший manual cache работает при inactive distribution metadata,
без GET/HEAD/процессов/изменения bytes/mtime/mode. Механизм не выбирает latest
и не меняет settings clocks/reset engine. Семь unit tests и owner-data review
приняты; legacy tables/actual cfg callers ещё НЕ заменены. До удаления таблиц
обязателен согласованный existing first-sync intake явного
universal_workload_resource_ref в existing generic runtime settings map:
startup старого named-profile config не должен зависеть от недоставленного
ref или выбирать inactive candidate сам. Native cfg/LWW acceptance pending.

### Consumer двух полных vhost-операций

nginx-baseline migration-vhost apply и composer-vhost apply принимают явный
--operation-input JSON. Мост проверяет согласованные типы/форму, точные SHA
и текущий контекст, не содержит OS-путей/формул/fallback writer. site_enabled
может быть None; несуществующие в legacy status/dry-run/no-reload режимы
не добавляются. Failed {error:legacy text} сохраняет прежнее исключение.
Standalone использует тот же pinned manual cache без config/registration/URL.
Изменённые аргументы требуют соответствующей selection; unknown fields и
неподдерживаемые режимы отвергаются до процесса.

### Восстановление выбранного артефакта при доступном транспорте

При наличии cfg с настроенным существующим artifact transport и сохранённых
точных selection pins отсутствующий/повреждённый архив догружается через
существующий fetch-only downloader. Запрос latest/service-profile для ремонта
не выполняется: сохраняется прежний key/SHA/entrypoint. Затем тот же выбранный
recipe запускается offline_only. Полный совпавший кеш не делает GET/HEAD.
Без транспорта возвращается прежняя точная missing/corrupt ошибка. Повтор
после начавшейся recipe-мутации не выполняется; ремонт возможен только по
типизированному отказу до процесса. Две проверки missing/corrupt подтверждают
один exact artifact GET, затем0GET при повторе и сохранение local selection.

R22 очищенный installed runtime принят PASS (ef44e478, source proof f3168f40):
восемь результатов равны OLD/DIRECT, шесть отрицательных сценариев, HTTP0,
девять actual child PID, реальные caller aliases и отсутствие удалённого
шаблона. Соседи/сертификаты bytes/mtime сохранены, HTTPS200. Native scope —
именно vhost OS stages, не полный Mautic migration/composer lifecycle.
Финальный canonical full reset и независимый read-only proof459d6def приняты:
новый boot, MCD inactive, DB/FPM/nginx active, healthOK, HTTPS200, actor/fault,
Docker/Wazuh/src отсутствуют. Evidence docs/qa/mcd-nginx-vhost-transfer-20261101.json.

### Runtime wheel ownership and ordinary update migration

До pip force-reinstall сохраняется прежний ownership RECORD; после установки
остаётся dist-info целевого wheel. Удалённые owned ресурсы убирает обычный pip
uninstall, посторонние файлы пакета и настройки сохраняются. Source checkout
установка запрещена: требуются verified manifest и detached wheel.
Ordinary updater использует /opt/mcd/runtime и installed package identity.
Исторический immutable1.6.38 first hop может оставить ready artifact в src;
maintenance1.6.39 не использует его для imports/version/source installation.
Перед следующим обновлением legacy cache проверяется по существующему receipt,
manifest/wheel SHA и установленной версии. Только после проверки successor
package/import path он переносится в existing update rollback artifacts.
Чужой/непроверенный legacy каталог сохраняется с конкретным отказом до мутаций.
Ошибка восстанавливает прежний verified package и расположение кешей.
Package-phase proof и полный HTTPS channel/restart proof учитываются отдельно;
maintenance-релиз не снимает global1.7 HOLD.

R21 nginx actor не стартовал: preflight нашёл obsolete template вне нового
RECORD. Ручной зачистки не было, выполнен полный reset. Native package-phase
с исправленным lifecycle принят отдельно (b9675e87); R22 прошёл очищенный
installed CLI с прежним строгим template-absence gate и финальный reset. Текущая полная регрессия:
2067PASS/61 прежних failure IDs/57skip/105subtests.

Maintenance1.6.39 выпущен штатным Publish MCD в Test→Approved/latest
после actual installed38→Test39→unlisted cleaned40 ordinary update/restart,
повторного no-op и real post-pip verification failure41/import42→rollback40
(receipt58785e27). Прямые apply plans выбраны из pinned HTTPS Test catalog;
update/check enrollment/session roundtrip не заявляется. Две неудачные попытки
QA harness сохранены отдельно и отделены полным reset. Финальный full reset и
independent read-only proof98a02fd7 приняты. Настройки bytes/mtime и foreign
package file сохранены; никакого bridge substitution/manualrm/fleet prerequisite.
Это prerequisite updater maintenance, не выпуск nginx/universal/части1.7.
Evidence docs/qa/mcd-record-maintenance-release-20261101.json.

### Universal data: initial intake и существующий runtime sync

В existing runtime-package manifest явно выбранный MCC resource ref связан
с payload_sha256 и raw independent DATA archive по ref.file. Builder принимает
только пару explicit ref/local bytes, проверяет SHA/size/весь member closure
до staging; данных нет в core wheel. Transported bridge повторяет те же чистые
stdlib проверки до pip. Installed preseed helper работает до config/profile,
без network/registration/state DB; совпавший кеш не изменяется. Missing/corrupt
точная копия восстанавливается из того же verified payload. verify_only replay
после identity/key binding ничего не чинит и возвращает ограниченный публичный
CustomSelectedTaskError.code, без traceback/секретов. CommercialState read_only
открывает существующий DB в mode=ro, не выполняет init/scrub/chmod.

Первое применение cfg при отсутствии explicit runtime setting берёт только
ref из verified ready manifest рядом с managed venv и предварительно готовит
existing custom cache. Explicit durable ref всегда важнее initial package ref;
совпавший ручной кеш не требует manifest/provenance/TTL. При недостающих байтах
используется только тот же key/file/SHA и existing configured transport.
Первичное чтение не переписывает конфиг и не объявляет смену DATA version
пользовательской правкой updated_at. Existing generic host setting
universal_workload_resource_ref участвует в прежнем LWW/full-loss recovery.
Winning explicit tombstone сохраняет пустой local marker, подавляющий initial
ref после restart; marker проецируется в отсутствие на existing wire, без
второго clock/registry. 87 целевых cfg/settings проверок прошли. Встроенные
профильные таблицы пока сохранены только для незавершённого legacy-перехода;
их удаление требует fresh/upgrade/offline installed startup proof.

R25 fresh-prefix native принят как ограниченный MCD startup gate
(receipt71421a81): plain new venv/ready wheel/preseed до cfg,14 literal
профилей, два explicit inherited значения,0 profile GET/HEAD; ручной кеш
работает без parent manifest. Actual passive run-once завершился0; один
не связанный с profile-data CONNECT metadata-запрос сохранён отдельно.
Installed verify_only даёт типизированный missing/corrupt code без repair;
normal mode восстанавливает тот же SHA. Это не full Portal enrollment proof.
Actual MCC v3 fixture также выявил nullable cluster_node_index: clear после
числового значения теперь остаётся None вместо int(None). Прочие типы и
existing LWW clocks не меняются.88 целевых config/settings проверок PASS.

Nullable runtime values определяются существующей AgentConfig schema, а не
текущим Python-значением: Optional int/str после установленного значения
корректно очищаются в None. Existing TOML writer представляет null пустой
строкой; runtime projection восстанавливает declared None, чтобы roundtrip
не стал ложной пользовательской правкой. Защита рабочего local MySQL socket
сохранена.91 целевая проверка config/settings PASS; aliases не добавлялись.

R27 ordinary upgrade gate (receipt415b026e) PASS: exact installed Approved39
updater принимает unlisted QA44, реальный systemd PID меняется; первый startup
готовит DATA в existing custom cache до profile probe.14 literal профилей и
два explicit inherited значения совпали, profile artifact GET/HEAD=0.
Installed consumer через disposable loopback HTTP принимает неизменённые
MCC-owner handler exports:12 доставленных настроек восстановлены после
потери projection, revision31/33 ACK applied, unsupported negative ACK
rejected, более новая локальная timestamp сохраняет приоритет. Это не live
MCC session/enrollment proof. Config bytes/mtime и foreign file сохранены;
src и retired template отсутствуют. QA44 не публичный выпуск;39 unchanged.
Полный canonical reset выполнен Infra и независимо проверен read-only:
новый boot, MCD inactive, DB/FPM/nginx active, health ok, HTTPS200, src/Wazuh
absent. Reset receipt SHA bc9171d4366b12c043ad27e44c01b337fcc25568742766deff8d9321c5801c7a. Full suite:
2100 PASS,61 строго прежних baseline failures,57 skip,105 subtests PASS.
Legacy cleanup/cleaned native и full Portal fresh gate ещё pending;1.7 HOLD.

Universal numerical cleanup: встроенные named workload values и CPU/RAM
recommendation branches удалены после R25/R27 proofs. _apply_profile применяет
только exact selected DATA; custom сохраняет supplied значения. Именованный
profile без selection/cache теперь typed missing-or-corrupt, а не старый
встроенный fallback. Recommendation reader использует те же DATA bounds,
CLI/reconcile передают actual config path.204 boundary pairs сравниваются
с freeze actual pre-retirement functions;14 профилей — с owner values freeze.
Test-only frozen archives явно подключены семью legacy test modules, без
autouse и без замены reader/downloader/profile функций. Полная регрессия:
2111 PASS/61 exact baseline failures/57 skip/105 subtests; отдельно новая
204-pair проверка PASS; release-regression PASS. Numerical cleanup требует
новых cleaned installed fresh/upgrade/offline proofs. Static profile identity
allowlists и legacy compatibility guards здесь не удалены и остаются отдельным
audit пунктом; universal scope целиком пока не CLOSED,1.7 HOLD.

R28 numerical retirement accepted within its scope: fresh receipt f01317e5
(14 literal profiles,204 actual installed recommendation boundary pairs,0
profile GET/HEAD, manual cache without parent manifest, RO/typed negatives),
ordinary installed39 updater receipt295b22d6 (real PID restart/first-start
preseed/14profiles/frozen MCC consumer/full delivered-settings restore).
Оба actors завершены полным Infra reset и независимым read-only proof:
fresh9d81fe2e, upgrade977b3b6b. Один standalone metadata CONNECT в fresh
учтён отдельно; это не commercial all-network/no-MCC proof.

Следующий source checkpoint удаляет static profile ID списки из mode,
runtime response parser и CLI choices. Публичный список идёт из verified
catalog, legacy_compatibility_only исключён только из нового выбора; чтение
и применение уже записанного legacy остаётся. Remote newref+newname проходят
единое admission по НОВОМУ catalog до persist; startup bootstrap использует
тот же путь. Existing mutable config section renderer переиспользован,
profile+runtime updates/removals заменяют один файл атомарно, сохраняя
symlink target/mode; failure оставляет original и убирает temporary.
Core syntax/LWW/queues/rings/compatibility guards сохранены.

Literal null resource selection теперь отдельное значение None на existing
wire (TOML empty string), а absence/explicit tombstone — пустой marker{},
не проецируемый на wire. Оба подавляют initial package intake; повторный
null roundtrip не выглядит потерей настроек и не переписывает конфиг.
Custom supplied values и status работают без matrix; advisory auto admission
проверяет DATA до записи auto marker. Full2119PASS/61exactbaselineFAIL/57skip/
105subtestsPASS, required release-regression PASS. Identifier/null checkpoint
ещё требует installed proof; Portal1.4.1fullfresh/zeroMCC egress pending,
universal whole scope не CLOSED, общий1.7 HOLD.

R29 installed checkpoint теперь PASS (receipt34f55612): ordinaryApproved39→
unlisted46actualupdate/PIDrestart/preseed/14profiles/frozenMCCconsumer плюс
syntheticQAnewcatalog pair в одном actualHTTPsync/SQLite, newname отсутствует
в oldcatalog. Atomicpair/reload, unknownprofile refusal, legacy read/exclusion,
literalnull/tombstone/custom проверки пройдены. Полный canonical reset и
independentread-only proof d4cfc8ea завершены, originalQAlease освобождён.
Это не production MCC/Portal acceptance. Static profile numeric/name tables
удалены; tiny-specific directMautic/queue/fullscan/drift core guards KEEP.
Оставшийся mode_activate resume fallback literaltiny классифицирован как
profilepolicy DATA: exact owner freeze de2c69a4 передан Portal для минимального
additivecatalog.defaults.resume_profile в independentDATA1.0.1; saved nonempty
(включая legacy) сохраняет приоритет. Этот пункт ещё pending.
Portal1.4.1nativefresh не принят: ownerдоnativeвыявилnestedcachevarmkdirbug.
Исправленный immutableproducer1.4.2 f7993833/source04b72b77 проверен поpins;
actualfreshgatepending на explicitR29/data210 (не автоматическийnewDATA).
FixturefakePortalpeer ограниченregistration/archive, zeroMCC проверяет
отдельный networkobserver. Новых publicchannels/fleetactions нет;1.7HOLD.

Resume DATA source checkpoint: independentPortalDATA1.0.1 b9b65416 и explicit
MCCQAref a76fd4df доступны, старый210/d910 immutable. Единственнаяpolicydelta
в metadata — catalog.defaults.resume_profile=tiny; все14profile/hardware/
settings bytes unchanged. Core _activation_profile оставляет currentactive/
custom и savednonempty strip+lower (включаяlegacyfarm) как раньше; только
missing/blank savedname читает generic workload_resume_profile из selected
verifiedcache. Ссылочныйprofile валидируется, builtin fallback отсутствует.
Отсутствующий/invaliddefault останавливает mode_activate до profilewrite/cron.
Core tiny queues/Mautic algorithms не менялись.14 narrow resumecases PASS;
full2133PASS/61exactbaselineFAIL/57skip/105subtests,release-regressionPASS.
Новый checkpoint требует только narrow installed lookup/precedence proof,
без повторения незатронутых закрытых numerical/native gates.

Portalactualfreshattempt1 1.4.2/R29/210 остановлен на pip rc1 до helper0 и
registration0. Failedreceipt359413a3/tracesf42fd399 сохранены; полныйreset
ownedactorclone из stoppedfresh-MCD-onlybaseline выполнен. Причинаещёне
утверждается: test-onlyCA/proxy trust проверяется; production source неpatched.
Clone является fresh-MCD-only, НЕcleanOS; hostQA /var/www populated. STARTgate
должен использовать ordinarypopulatedview/passive0businessslots, preserving
protectedneighbors и0MCC attempts; namespaceempty не подменяет этот proof.
Текущие frozenPortalactors остаются R29/data210, не newDATA1.0.1.1.7HOLD.

Ready dependency delivery contract checkpoint: Portal1.4.2/R29actualattempt2
PIP_LOG подтвердил phpserialize>=1.3 sources skipped under --only-binary;
preseed0/registration0, receipt5cd42cb5/diagnosticsdcd7b853 сохранены.
Глобальногоразрешения hostsourcebuild/dependencydrop/preinstalledfakefresh
не делаем. На стадииразработки подготовлена полная Linuxx86_64CPython3.12
wheelclosure из6wheels (phpserialize readywheel включён), exactinputreceipt
ca655bf9; это ещё не nativefresh/offline acceptance.

Existing runtime manifest v1 получает optional dependency_wheelhouse=wheelhouse,
всеflatregularwhl входят в прежний payload_sha256. Agent/standalonebridge
проверяют directory closure/symlinks/digests до pip. Ownerbuilderoptional
--dependency-wheelhouse copiesexplicitpreparedfiles; canonicalmaingitgate
НЕменялся. NEWpayloadrequirements добавляет стандартныеpipoptions no-index/
find-links ./wheelhouse/only-binary, constraintsunchanged. Так старый39client
с прежним -r сможет употребить readyclosure без newparser/hostsourcebuild;
проверка actualold39 code/cwd=/ с emptytargetvenv ещё требуется. Newbridge
передаёт admittedabsolute wheelhousedir и those samepipflags. Никакого нового
resolver/registry/security/key/wireengine нет. Source2158PASS/61exactbaseline
failures/57skip/105subtests;26wheelhousecases inclcanonicalbuilderflag и
requiredrelease-regression PASS.

Nextfullfresh — явноНОВЫЙtargetR31 latestsource+DATA1.0.1b9/a76 и frozen
Portalproducer1.4.3 7aca8216, после build-stage targetemptyvenvofflineproof.
Старые39/R29/210/f799 bytes immutable; oldfailedattempts не переименованыPASS.
Actualtargetidentity Linuxx86_64CPython3.12.3 Ubuntu24.04 glibc2.39 фиксируется
в prepared/build proofreceipt; инаяархитектура не заявляется.1.7HOLD.


## Shared 1.6 / 1.7 hotfix invariants (2026-11-01)

An explicitly selected Dev policy stays Dev, including an empty-channel response;
no package is selected or downloaded through an Approved/latest fallback.
Nullable runtime settings clear to their declared optional type through apply,
TOML persistence, reload and runtime-wire projection. Empty remote local-MySQL
socket data preserves an existing working socket. Shared regression coverage is
`tests/test_shared_nullable_runtime.py` and `tests/test_dev_update_policy.py`.
The managed default deny vhost emits HTTP port 80 only, with no domain, TLS or
certificate dependency. Removal remains restricted to the instance-owned vhost;
neighbor vhosts and certificates remain protected. Dev uses the externally
verified baseline recipe; stable uses its minimal existing leaf generator fix.
A paired source/test fix does not authorize channel promotion or host rollout.
