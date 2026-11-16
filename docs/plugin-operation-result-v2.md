# MCD plugin operation result v2

`mcd-plugin-operation-result-v2` — единственный машинный контракт установки
плагина между внешним оркестратором и MCD. Оркестратор выбирает текущий хост,
но передаёт MCD только стабильную сущность инстанса и желаемую операцию:

```bash
mcd-cli plugins --machine-result \
  --instance-uid INSTANCE_UID \
  --plugin-uid PLUGIN_UID \
  --action auto \
  --operation-id OPERATION_ID
```

`INSTANCE_UID` обязан быть bare UID без `@host`. `--root` в machine mode
запрещён. MCD разрешает UID в локальный runtime самостоятельно. Допустимые
действия совпадают с локальным plugin CLI: `auto`, `install`, `update`,
`reinstall`, `remove`, `purge`.

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
- выбранные `plugin_uids` и `action`;
- `terminal=true`, `status` и `rc`;
- `selected` с ожидаемыми package versions;
- подтверждённый Mautic `inventory`;
- `error_code` и `error` при отказе;
- `mcd_version` и `receipt_sha256`.

`root`, host identity и `instance@host` в receipt отсутствуют. Success означает,
что MCD закончил файловую транзакцию, native plugin registration/reload, cache
reset/warmup и точную inventory-проверку. Наличие файлов без terminal success не
является успешной установкой.

Обычный интерактивный CLI сохраняет старый `mcd-plugin-apply-result-v1` для
обратной совместимости. Новый MCC-путь не читает его progress-записи и не
строит собственную копию MCD state machine.
