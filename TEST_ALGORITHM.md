# Segment scheduler liveness test algorithm

Этот документ описывает исполняемую матрицу M6/M7 для ordinary segment
cycles. Тесты не используют production host, не запускают реальный Mautic и
не меняют profile/config/data. Реальный MCC gate является отдельным
read-only/package/install шагом после unit matrix.

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
2. При campaign/admission pressure новый ID не добавляется в active cycle.
   Уже сохранённый pending ID всё равно claim-ится и стартует. После снятия
   pressure новый ID может быть добавлен по обычному FIFO/never-built правилу.
3. Несколько roots/instances с `pending>0` и `running=0` получают свободные
   slots по очереди dispatch. Один root не может навсегда удерживать cursor
   active только потому, что другой root первым использовал global baseline.
4. Повторить пункты 1–3 для Mautic 6 и Mautic 7. M6 сохраняет dependency
   closure order; M7 сохраняет native terminal/root plan и не создаёт
   искусственные internal items.

## Guard assertions

Для pending cycle bypass-ится только global new-admission baseline cadence.
Обязательны и проверяются отдельно: atomic cycle claim, `done/errors/claimed/
running`, dependency conflict, host/instance capacity, import reservation,
repeat interval, TaskStore duplicate key и OS execution lock. Pending запуск не
обновляет global baseline timestamp. New admission при том же baseline guard
остаётся заблокированным.

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
