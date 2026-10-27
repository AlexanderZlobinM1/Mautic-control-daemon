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
Актуальная редакция этого канона — MCD 1.5.247 (follow-up после immutable
1.5.88). В этой редакции startup/profile rebase не считается локальной
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
| result-signing identity bootstraps from MCD-owned config and gates signed control execution on MCC ACK | `mcd_agent/config.py`, `mcd_agent/result_signing.py`, `mcd_agent/state_push.py`, `mcd_agent/mcd_control.py` | `tests/test_result_signing.py`, `tests/test_mcd_control.py`, live `mcd-state-v1` identity/key/ACK and terminal-receipt evidence |
| authority-safe desired runtime projection | `mcd_agent/config.py`, `mcd_agent/runtime_overrides.py` | `tests/test_runtime_override_direction.py`, `tests/test_runtime_stable_keys.py`, `tests/test_runtime_migration_adapter.py` |
| natural b2bss/Adonis evidence | runtime TaskStore/MCC receipts | rollout acceptance (после package/install) |

Любое расхождение между этой схемой, source, тестом и runtime receipt
блокирует acceptance; его нужно явно зафиксировать и исправить до handoff.

### Daily backup recovery

Daily instance backup scheduling is a durable scheduler concern. Before a scheduled `backup_run` the daemon persists one local-date claim and changes it to a `running`, `succeeded`, or `failed` receipt in `TaskStore`. The claim is reloaded after an MCD restart, so a restart cannot duplicate the same daily slot. A stale claim may be recovered only after its bounded grace expires and only when no independent backup lock is active. A missed slot is not launched outside the configured quiet window: it is caught up at the next eligible window, without stopping or replacing an already-running backup.

### Backup profile synchronization contract

Backup profile synchronization is a separate typed contract and is never
projected into generic runtime overrides. MCD advertises and consumes
`mcd-backup-profile-v1` version 1 envelopes containing the complete normalized
profile, `generation`, and canonical JSON SHA-256. MCD validates schema, version,
and desired hash before applying the profile; the apply/readback result contains
masked `effective_profile`, `effective_sha256`, generation, and `status`.
`ok` is valid only when desired and effective hashes match. Schema, validation,
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
revalidation, expiry, nonce, idempotency and Portal signature validation. The
additive contract version `2.1` is the registered-installation path: its
`execution.primitive` is `scenario.apply`, it requires the signed scenario
artifact, and `bootstrap_artifact` must be absent. MCD downloads, size/digest
checks, and verifies only that scenario artifact against the Portal-owned
artifact trust set. Legacy contract `2.0` remains accepted with its required
bootstrap and scenario pins for compatibility; bootstrap is not part of the
registered scenario operation. MCD never fetches MCC directly, never imports
MCC credentials or scenario rules, and Portal is the self-hosted scenario
authority.

The signed `portal-mcd-inventory-snapshot-v1` also carries the additive
`applicability_facts` coordinate (`mcd-portal-applicability-facts-v1`, contract
version `1.0`). Its `facts` mapping is the complete normalized value used by
the local execution guard: `os_id`, `os_version`, `architecture`,
`package_manager`, sorted unique `capabilities`, `target_kind`, and
`detector_schema`. `facts_digest` hashes the same mapping after removing only
`detector_schema`. Inventory publication and `scenario.apply` use the same
reader and capability context, so the coordinate is stable across unchanged
polls, restart, replay, and Portal outage; its durable inventory hash and
revision include the field. Missing detector facts fail closed. Scenario
`required_capabilities` remain Portal-side applicability predicates and are
never synthesized into this signed facts mapping. Older Portal consumers may
ignore the additive field.

The only MCD execution primitive is the contract's `scenario.apply` v1.0. MCD
passes the verified external scenario artifact to an allowlisted, non-shell
runner process and owns no installer, OS, Mautic, scenario or primitive recipe.
No fixed onboarding stage registry is constructed on this path. Progress is
checkpointed by immutable `job_id`; replay of the same signed job returns the
durable terminal receipt without re-executing the runner.

The executable scenario bridge follows Portal's additive
`portal-self-hosted-executable-scenario-v1` contract. The signed artifact must
be `application/x-sh` or `application/x-elf`; the legacy declarative JSON
artifact remains audit-only and is never dispatched. MCD sends the artifact and
canonical context in the 88-byte `SSMCDX01` frame: exact lengths, raw SHA-256
digests, and reserved flags are checked before execution. The context contains
only job/scenario identity, applicability digest, scalar non-secret parameters,
and opaque secret references; it is also exposed to the child only on inherited
file descriptor 3. The bundled `mcd-scenario-runner` uses a sterile environment,
fixed `--mcd-scenario-apply` argv, no shell/eval, and a bounded process group.
Child stdout is a length-prefixed canonical JSON stream with at most 256
progress frames and one terminal result; unknown fields, secret-shaped values,
non-contiguous progress, malformed frames, timeouts, signals, and oversized
output fail closed. MCD also captures only bounded stderr and persists a
redacted preview, output SHA-256 values, truncation flags, exit code/signal,
timeout state, admission stage, and any already-valid progress frames as typed
runner evidence. Raw stderr, artifact bytes, and executable context never enter
receipts or run state. Portal owns the executable artifact and scenario policy;
MCD owns only this transport, admission, timeout, and durable receipt boundary.

Stage receipts use `mcd-self-hosted-stage-receipt-v2` with only
`scenario_dispatch` and `execution`, statuses `queued|running|waiting|succeeded|failed`,
mandatory installation binding and unique idempotency keys. Terminal receipts
use `mcd-self-hosted-terminal-receipt-v2`, `succeeded|failed`, explicit
`rollback`, the same binding and a monotonic sequence. Failed terminals are
bounded and secret-free. Every unsent stage or terminal receipt is durable and
retried in order after Portal outage or MCD restart.

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
outcome closes through a signed terminal receipt: expiry, malformed or failed
admission, revoked/disabled Portal state, artifact unavailability, and runner
exceptions are typed and bounded. A receipt that cannot be sent immediately is
queued durably before the delivery is considered complete; the runner is never
re-run just to deliver a receipt after restart. A poll/network failure before a
delivery exists remains a transport retry and has no command to acknowledge.
For Portal's same-job recovery command, the terminal continues at the next
monotonic sequence/idempotency key after any terminal from the first attempt;
draining that durable terminal completes the delivery and never re-runs the
runner.

`daemon.run_loop` may start at most one Portal worker when the durable
Portal enrollment is valid: HTTPS Portal base, credential, agent and
commercial-key identity, host binding, at least one installation binding, and
Portal job-signing keys must all be present. Portal artifact trust is not
activation material and is not a worker-start gate: poll, adopt, inventory,
settings, noop, and receipt operations use Portal job-signing keys only. The
canonical `portal_artifact_signing_keys` set is seeded by activation and can be
replaced only by a bound, time-valid, monotonically increasing
`portal-mcd-artifact-trust-update-v1` command delivered in the signed poll
response. A legacy `portal_mcc_publisher_keys` map is read only while canonical
Portal trust is absent and is deprecated. Artifact trust is passed as an
operation capability; missing or unknown/revoked key fails only that operation
with typed `self_hosted_mcd_artifact_signer_unknown` or
`self_hosted_mcd_artifact_signer_revoked` and a durable failed terminal
receipt. The live worker refreshes this capability immediately after a
successful durable trust update; a restart reloads the same canonical state.
The worker remains available for later outbound polling. The base
`passive`, `standalone`, or `corporate` profile is not an
eligibility switch; the Portal worker is an independent capability alongside
the existing scheduler. The corporate MCC URL/token and base profile are not
Portal eligibility switches. Missing or invalid Portal state, including an explicit
revoke/detach marker, fails closed. Single-cycle mode never starts the worker.
The worker owns only its Portal stop event and bounded thread; it does not
enter scheduler admission, locks, or task slots. On revoke, detach, or
shutdown MCD stops and joins only this worker, leaving corporate MCC and
standalone work running. A restart re-reads the same durable registration and
resumes without activation or token issuance; starting is idempotent and never
creates a duplicate Portal worker.

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
MCC-снимок, даже если текущий ответ содержит только stable-поля.

Для каждого host/instance-поля применяется LWW по сохранённому `updated_at`,
затем `revision`, с детерминированным MCC tie-break. Более новое изменение MCD
отправляется в MCC и не затирается устаревшим ответом; более новое изменение
MCC применяется на хосте. Отсутствие поля в частичном ответе не является
удалением. Удаление допускается только явным MCC tombstone либо подтверждённым
локальным tombstone с последующей синхронизацией. После восстановления MCD
обновляет baseline без повторной отправки того же состояния как новой локальной
правки.

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
complete signed inventory is sent on adoption and on subsequent changes. Every
returned command is retained in order; the cursor advances only after the
terminal receipt for each command and all earlier receipts are accepted by
Portal. A daemon restart therefore resumes the saved batch or the durable
receipt queue; replay uses command/job idempotency keys and never re-runs a
completed scenario. A Portal/network failure leaves commands queued at Portal
and records an exponential, durable retry deadline. The next outbound cycle
drains receipts first, then resumes the saved batch or polls with the last
cursor; no scheduler or standalone work is blocked. Backoff is applied only
to transport failures, not to local signature/applicability rejection.

Inventory publication has its own durable revision ledger per installation.
MCD canonicalizes the complete inventory and removes only transport-volatility
fields `request_id`, `observed_at`, and `agent_signature`; every other durable
field (MCD version, profile, settings, instances, capabilities, binding and
revision) participates in the hash. A changed durable hash increments
`snapshot_revision` exactly once under the state-store transaction; unchanged
content reuses the same revision across restart, outage, replay and concurrent
workers. The state stores the durable snapshot/hash plus last published and
acknowledged pins. A missing hash at legacy revision `0` performs one safe
baseline migration; corrupted or regressive state fails closed. Portal acceptance
pins the same hash/revision, and a failed publication retries that revision
without creating a new one.
