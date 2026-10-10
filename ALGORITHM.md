# Алгоритм MCD: admission и dispatch сегментов

Этот документ является рабочим каноном для MCD scheduler. Он описывает
фактическую схему после исправления admission и должен обновляться вместе с
кодом, тестами и runtime receipt. Документ не является разрешением на
ручной `segments:update`, изменение профиля, capacity или данных на хосте.

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
58` в super и `59` в priority на Mautic ≤6. Для Mautic 7 sibling terminal
commands не должны добавлять 57 второй раз; source/CLI и TaskStore receipts
обязаны подтвердить, покрывает ли один native command union closure.

## 4. Claim, worker и slots

Перед spawn worker атомарно claim-ит следующий eligible item из persisted
cycle или статической queue. Claim проверяет `done/errors/claimed/running`,
глобальную dependency closure, failure cooldown, host slots, instance lane
slots, import reservation и launch guard. `super-priority` выбирается раньше
`priority`, затем ordinary; приоритет не вытесняет уже running работу.
При отказе spawn claim освобождается.

После terminal одного item worker/dispatcher немедленно получает сигнал и
claim-ит следующий eligible item текущей очереди; ожидание минутного tick не
является частью алгоритма и tick не является per-item dispatcher. При
свободной capacity и отсутствии block-gate claim выполняется в том же
dispatch pass.

Каждый отказ admission сохраняется в `TaskStore.runtime_sync` с exact gate
(`host_capacity`, `instance_capacity`, `launch_guard`,
`dependency_conflict`, `cycle_not_eligible` и т.п.), lane, счётчиком и
`updated_at`. Поэтому `Processing=0` не является немым состоянием: receipt
показывает, почему item не был claimed.

TaskStore task key и OS execution lock остаются последней защитой от duplicate
claim, включая restart и concurrent workers.

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

`done` записывается только после успешного process exit. Non-zero exit,
timeout, lost PID и lock-busy становятся terminal error после исчерпания
обычного retry policy; для неуспешного Mautic command outcome фиксируется как
`mautic_failed`. Такой item остаётся в persisted cycle и в UI Finished с
красным error outcome, не исчезает из projection и переоценивается в
следующем ordinary cycle; permanent dead-letter не создаётся.

Один только потерянный TaskStore PID не доказывает падение Mautic. До
terminal error MCD сначала пытается rebind/adopt совпадающий Mautic process,
затем проверяет read-only DB evidence и собственный runtime marker. Пока
есть process/DB/runtime evidence или не истёк bounded orphan grace, claim и
UI остаются `running`/Building. Цикл terminal, когда каждый его item
находится в `done` или terminal error и нет running/claimed работы.

## 6. Наблюдаемость и acceptance

Каждый цикл виден в scheduler monitor receipt (`queued`, `running`, `done`,
`errors`, `total`, `cycle_id`, `updated_at`). Для segment cycle дополнительно
публикуются `admission_receipt` и persisted `admission_rejections`; receipt
валидирует source cycle, lane state, error outcome и exact gate.
Для acceptance нужны source,
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
| `mautic_failed` остаётся Finished/error и retry-ится новым cycle | `daemon.py`, `segment_admission.py` | `tests/test_segment_admission.py`, `tests/test_scheduler_monitor_cycle.py` |
| PID loss не уничтожает ongoing build | `daemon.py`, TaskStore runtime marker | `tests/test_segment_admission.py` |
| exact admission gate и immediate claim | `daemon.py`, `segment_admission_receipt.py` | `tests/test_segment_admission.py`, `tests/test_scheduler_monitor_cycle.py` |
| host/instance slots и rotation | `mcd_agent/daemon.py` | `tests/test_campaign_ring_dispatch.py` |
| monitor queued/running/done/error/admission receipt | `mcd_agent/daemon.py`, `mcd_agent/signals.py` | `tests/test_scheduler_monitor_cycle.py`, `tests/test_signals_scheduler_history.py` |
| natural b2bss/Adonis evidence | runtime TaskStore/MCC receipts | rollout acceptance (после package/install) |

Любое расхождение между этой схемой, source, тестом и runtime receipt
блокирует acceptance; его нужно явно зафиксировать и исправить до handoff.
