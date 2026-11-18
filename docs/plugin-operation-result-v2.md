# MCD plugin operation result v2

`mcd-plugin-operation-result-v2` — единственный terminal execution result
установки плагина в MCD. Локальный executor принимает стабильную сущность
инстанса и желаемую операцию:

```bash
mcd-cli plugins --machine-result \
  --instance-uid INSTANCE_UID \
  --plugin-uid PLUGIN_UID \
  --package-uid PACKAGE_UID \
  --action auto \
  --operation-id OPERATION_ID
```

`INSTANCE_UID` обязан быть bare UID без `@host`. `--root` в machine mode
запрещён. MCD разрешает UID в локальный runtime самостоятельно. Допустимые
действия совпадают с локальным plugin CLI: `auto`, `install`, `update`,
`reinstall`, `remove`, `purge`.

`plugin_uid` обозначает логический плагин. Один логический плагин может иметь
несколько взаимоисключающих package-проекций с разными версиями и архивами.
`package_uid` обозначает одну такую проекцию, остаётся стабильным при обновлении
версии и не является bundle, путём, URL или именем хоста. Если для текущей
major-версии Mautic у `plugin_uid` существует несколько проекций,
`--package-uid` обязателен. MCD проверяет принадлежность проекции плагину и
выбирает ровно одну строку manifest. Для единственной проекции параметр можно
не передавать для обратной совместимости. Нулевая, чужая или неоднозначная
проекция завершает операцию validation error до файловой транзакции.

До окончания операции machine mode не публикует промежуточные filesystem,
cache или Mautic-фазы. После полного завершения stdout содержит ровно одну
машинную строку:

```text
MCD_PLUGIN_OPERATION_RESULT=<JSON>
```

Результат содержит:

- `schema=mcd-plugin-operation-result-v2`;
- caller-supplied `operation_id`;
- bare `instance_uid`;
- выбранные `plugin_uids`, `package_uids` и `action`;
- `terminal=true`, `status` и `rc`;
- `selected` с ожидаемыми package versions;
- подтверждённый Mautic `inventory`;
- `error_code` и `error` при отказе;
- `mcd_version` и `receipt_sha256`.

Выбранный `package_uid` возвращается в каждой записи `selected` и входит в
digest terminal receipt. Версия пакета остаётся отдельным ожидаемым значением:
разные версии одного логического плагина являются допустимыми данными, а не
конфликтом идентичности.

`root`, host identity и `instance@host` в receipt отсутствуют. Success означает,
что MCD закончил файловую транзакцию, native plugin registration/reload, cache
reset/warmup и точную inventory-проверку. Наличие файлов без terminal success не
является успешной установкой.

Обычный интерактивный CLI сохраняет старый `mcd-plugin-apply-result-v1` для
обратной совместимости. Новый MCC-путь не читает его progress-записи и не
строит собственную копию MCD state machine.

## Durable host queue

Удалённый оркестратор не держит SSH-процесс до конца установки. Он коротким
вызовом передаёт intent в локальную durable-очередь MCD:

```bash
mcd-cli plugin-job enqueue \
  --instance-uid INSTANCE_UID \
  --plugin-uid PLUGIN_UID \
  --package-uid PACKAGE_UID \
  --action auto \
  --operation-id OPERATION_ID
```

Для нескольких выбранных плагинов пары `--plugin-uid`/`--package-uid`
повторяются в одинаковом порядке. MCD валидирует bare UID, выбранные package
projections, текущую Mautic-совместимость и каталог уже на конечном хосте.
`operation_id` является idempotency key: повтор идентичного enqueue возвращает
существующий local job, а тот же ID с другим intent завершается конфликтом.

`mcd-cli plugin-job status --operation-id ID` — короткий readback. Он возвращает
`queued`, `running`, `succeeded`, `failed` или `cancelled`; terminal status
содержит исходный `mcd-plugin-operation-result-v2`. Отдельное поле
`notification_delivery_status` показывает доставку inventory/результата в MCC
и не меняет локальный execution success. На standalone-хосте оно равно
`disabled`.

Зарегистрированный агент включает последние bounded status receipts в обычный
`mcd-state-v1.plugin_jobs`. Это канал наблюдения, а не вторая очередь: MCC не
запускает работу повторно и не реконструирует terminal result. Локальный
`plugin-job status` читает тот же durable record и остаётся доступен независимо
от доставки state snapshot.

`mcd-cli plugin-job cancel --operation-id ID` отменяет только ещё не запущенную
работу. Запущенная транзакция остаётся под supervisor MCD и не убивается
удалённым timeout. Очередь и output receipt сохраняются локально; перезапуск
клиента MCC или потеря SSH-сессии не влияет на выполнение.
Повтор отмены уже отменённого задания идемпотентно возвращает `cancelled`;
running и terminal задания отменой не изменяются.

Очередь `plugins_apply` является host-local: задания для разных instance UID на
одном хосте принимаются сразу, но выполняются последовательно в одном MCD
daemon. Пока предыдущая транзакция не закрыта terminal receipt, следующая
остаётся `queued` и не превращается в `skipped`. На разных хостах очереди
независимы и могут выполняться параллельно. Каждый instance сохраняет свой
`operation_id`, durable status и terminal result.

Manifest читается MCD из `plugins.repo_base_url` и
`plugins.manifest_path_template`. Для этого не нужны MCC management token,
регистрация агента или UI-сессия. Terminal result включает digest фактически
прочитанного каталога. Регистрация добавляет только доставку observed inventory
и настроек; её отсутствие не превращает успешную локальную установку в ошибку.
