# Portal↔MCD OWH-AUTO v1

## Граница ответственности

`Publish MCD` публикует MCD-кандидат и выполняет стандартный release loop.
MCC не выкатывает MCD: он только регистрирует каталог. Portal создаёт
automatic install request и доставляет его MCD через согласованную boundary.

Automatic payload содержит только account/target/commercial access и policy.
`agent_id` и `portal_api_base` не добавляются в него: MCD берёт эти значения из
своей runtime-конфигурации и fail-closed проверяет их до сетевого запроса.
Commercial API key используется только для HTTPS; он не попадает в receipts,
логи или UI. Legacy signing, Ed25519 agent keys, encryption и дополнительные
tokens для OWH-AUTO не используются. Manual enrollment остаётся отдельным
pending activation flow и не запускает сценарии.

## Автоматическая установка

1. MCD валидирует `portal-owh-auto-install-request-v1` и совпадение DNS/local
   identity.
2. Через текущую HTTPS registration boundary принимает active automatic
   response и durable сохраняет полную installation binding.
3. Последовательно запрашивает latest-compatible scenario для
   `base_packages`, `database_cache`, `web_stack`, `mautic`.
4. Для каждого artifact проверяет HTTPS, размер и SHA-256, вызывает только
   внедрённый generic executor и отправляет stage receipt.
5. После всех стадий отправляет один terminal receipt. Любой сбой закрывает
   текущую стадию и terminal со статусом `failed`.

MCD не зашивает в исходники сценарии установки, версии ОС, hostname/IP или
правила Mautic. Это данные внешнего Portal scenario artifact; MCD выполняет
только общий fetch/hash/execute/receipt boundary.

## Generic command broker

Для уже подключённого MCD используется существующий outbound HTTPS poll:

- capability inventory: `POST /api/v1/commercial/agents/{agentId}/installations/{installationId}/capabilities`;
- jobs: текущий `POST /api/v1/commercial/agents/{agentId}/installations/{installationId}/poll`;
- receipts: текущий `POST /api/v1/commercial/agents/{agentId}/receipts`;
- generic artifact: текущий HTTPS artifact route с `X-Content-Digest`.

MCD принимает только job, объявленный последним inventory, и только
`execution=generic_primitive`. Поля shell/script/argv/executable/sudo/password/
key/token/signature отвергаются. Stage и terminal receipts durable; sequence
идёт от 1, idempotency равна `{job_id}:{sequence}`, cursor продвигается только
после terminal receipt. При рестарте или потере Portal сначала дренируется
очередь receipts, затем возобновляется job без повторного вызова завершённого
primitive.
