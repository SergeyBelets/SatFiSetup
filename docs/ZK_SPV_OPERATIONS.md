> Порядок поставки обновлён: [каждый участник сам считает рабочие proofs](DISTRIBUTION.md).
> Архивы, receipts и cache в этом историческом runbook предназначены для локального
> developer review и не входят в participant distribution. Текущий worker требует
> локальную запись происхождения результата для публикации.

# Эксплуатация и границы локального ZK-SPV пакета

Актуальный исполняемый контур описан в [архитектуре и схемах](ZK_SPV_ARCHITECTURE.md),
[worker](ZK_SPV_WORKER.md), [transport](SPV_ENCRYPTED_TRANSPORT.md) и
[automation/archive](SPV_AUTOMATION_ARCHIVE.md). Он может запускать настоящий
prover и подписывать ограниченные EVM-транзакции под явно заданными policy.
Общий live E2E отложен пользователем из-за места на устройстве; production activation
остаётся закрыта. [Исторический локальный пакет от 2 ноября](ZKSPV_LOCAL_ACCEPTANCE.md) содержит
3269 Python, 834 ordinary Solidity и 25 state/adapter tests без ошибок/пропусков,
восстановленный native host и 195 локальных test-program bindings. Новые proving,
Core и Docker для отложенного контура не запускались. Проверка четырёх сохранённых
Groth16 receipt отдельно отмечена как повторная проверка исторических доказательств.

Локальные `ops.zkspv replay/tco/preflight` ниже — отдельные модели и проверки
пакета: они не отправляют транзакции и не загружают секреты. Исторические
proof-запуски далее сохраняют свои исходники, receipts и ограничения.
Не менять живой профиль только по наличию файлов или JSON-статуса.

## Календарь, журнал и восстановление

`ops/zkspv/scheduler.py` принимает уже проверенные наблюдения. Сам он не
проверяет PoW, proof, EVM provenance, finality или полномочия сообщений.
В этой самостоятельной модели факты должны поступать после таких проверок.
Исполняемый `zk_worker` описан отдельно; результаты модели не дают production activation. SQLite journal хранит policy
и scope, observed/published heads, scan cursor, needs, target, original deadline,
service job и attempts. Изменение scope/policy существующей DB отвергается.

Наблюдение диапазона и найденные needs записываются одной транзакцией;
ошибка admission не продвигает scan cursor. Пропуск диапазона не означает
отсутствия платежей. Bitcoin reorg переводит inclusion в revalidation.
EVM reorg восстанавливает anchor и повторно проверяет application-completion
receipts, сохраняя сами обязательства. Подтверждённый результат приложения
не отменяется на основании одного события scheduler.

Подготовка heartbeat начинается заранее, но сама по себе не разрешает раннюю
публикацию. Начальный скользящий интервал — 302400 s. Готовые monetary/security
needs обслуживаются раньше по собственному deadline. Coinbase maturity для mint
равно 100, обычная глубина берётся от соответствующего consumer. Разрыв высот
не используется как глубина reorg. Полезный finalized update может совместить
причины, но no-op, ACK, status и stale partial catch-up не сдвигают anchor.

Цель сохраняется при поступлении обычных новых блоков; если она устарела или
уже принята как partial progress, требуется новая attempt с тем же service job
и прежним сроком. Старый lease отзывается. Worker fence не является FROST nonce
или блокировкой EOA: существующие nonce journals и finality barriers сохраняются.

Fresh оценивается по двум ограничениям:

```text
!halted && now <= lastUsefulAcceptedAt + maxStateAge
        && now <= canonicalTipTimestamp + maxTipLag
```

Экономичный idle допускает Fresh=false до следующего heartbeat; это не даёт
права mint/settle. При готовой потребности нужен catch-up, даже если до heartbeat
далеко. Continuous Fresh требует отдельного выполнимого профиля, расчёта времени
и расходов. Параметры симуляции не становятся production defaults.

В `ops/zkspv/scheduler.py` status-slot/backoff/attempt limit остаются локальной
моделью. Исполняемый worker отдельно использует `SPVMessageBoard` с REQUEST, ACK
и STATUS, проверкой sender roles/key epochs/finality и повторным чтением inbox.
Сквозная эксплуатационная квалификация этого пути ещё не завершена. `NO_PROGRESS`, недоступность
наблюдения и `HALTED` различаются; статус не меняет Fresh или proof deadline.
Ожидание finality внутри grace само по себе не создаёт статус.

```bash
python3.11 -m ops.zkspv replay --hours 84 --days 30 --output /tmp/zkspv-idle.json
python3.11 -m ops.zkspv replay --hours 84 --days 180 --output /tmp/zkspv-idle-180.json
```

Это SIMULATION с постоянным шагом Bitcoin 600 s и нулевой latency принятия:
8 и 51 запланированных update соответственно. Она не измеряет gas, proving,
реальные block intervals, SLA или стоимость. `J_real`, `K_real`, `H_prove`
остаются null. Не интерпретировать simulation update как receipt.

## Экономика и admission

`economics.py` считает полную стоимость receipt один раз, в том числе failed tx;
комбинированные причины не умножают gas. L2 DA/operator fee явно учитываются,
неизвестное значение сохраняет UNKNOWN. Treasury principal, refundable cap и
перевод долей не добавляют второй копии resource cost. BTC miner fee учитывается
один раз; MP/S burn не считается ещё одной покупкой BTC. Эти правила калькулятора
применяются отдельно от реализованного [контрактного fee-credit ledger](WITHDRAWAL_FEE_SUBSIDY.md);
его settlement/refund receipts и invariant checks сохраняют собственную границу.

```bash
python3.11 -m ops.zkspv tco --inputs ops/artifacts/zkspv/cost-inputs.json
python3.11 -m ops.zkspv.preflight --config config/zkspv.example.json \
  --decision ops/artifacts/zkspv/decision.json
```

Последняя команда ожидаемо возвращает exit 2. `package_checks_pass` отражает
только структуру/целостность пакета; `production_ready` остаётся false без
квалифицированного canonical runtime reader. JSON PASS не является обходом
проверки происхождения. Никакого deployment или secret loading команда не делает.

В TCO сравниваются baseline keeper, common ordinary с тем же расписанием и ZK.
Отдельно нужны fixed observation/history/storage, proving/wrapping, доставка
сообщений и неудачные attempts, heartbeat/security reserve и latency. Начальная
gas-ликвидность sender и production тариф/резерв ещё не определены. Локальные
native treasury, payment registry, FROST governance и ограниченный claimFor caller
описаны в [SPV_GAS_TREASURY.md](SPV_GAS_TREASURY.md). Код admission/quote/opt-in
payment и worker publication связан через canonical registry/state view;
полный live proving/payment/consumer E2E ещё отложен. Нельзя считать BTC treasury или имеющийся keeper wallet этой кассой.

## Повтор локального proof с увеличенным тайм-аутом

Для уже собранного guest доступен отдельный launcher. Он не меняет guest,
исходный build-report или прежний runner; сверяет хеши и использует тот же
`sandbox-exec` с `deny network*`, отключённым dev-mode и проверкой receipt.

```bash
python3.11 -m ops.zkspv.prove_retry \
  --r0vm /path/to/r0vm-3.0.3 \
  --verifier /path/to/verify_receipt \
  --height 32257 --timeout 7200 --output /new/proof-attempt-directory
```

`--timeout` задаёт секунды только для генерации proof: по умолчанию 7200,
допустимо 1–14400. Каталог вывода должен быть новым. До старта нужны хотя бы
8 ГиБ свободного места; подготовка зависимостей выполняется заранее. Launcher
сохраняет точную команду, время начала, timeout, хеши runtime/guest/input и
собственного исходника. После тайм-аута группа процессов prover завершается.
Статус `LOCAL_STARK_PASS` появляется только после отдельной криптографической
проверки receipt, image ID и точного журнала. Запуск или тайм-аут не дают PASS.
Groth16 wrapping и положительная EVM-проверка остаются отдельными стадиями.

29 октября запуск с лимитом 7200 секунд дал `LOCAL_STARK_PASS`: генерация заняла
3408,052 секунды, отдельная проверка — 27,336 секунды. Receipt и логи сохранены
в `rust/zkspv/guest-integration/evidence/local-proof`; сводка с хешами —
[`guest-proof-extended-report.json`](../ops/artifacts/zkspv/guest-proof-extended-report.json).
Это один mainnet append, а не полный benchmark или квалификация всех сценариев.

Для этого же receipt далее выполнены succinct-сжатие, настоящая Groth16-обёртка
и положительная проверка в локальной EVM. Команды, границы привязки state address
и правила Docker-изоляции: [Groth16 bridge](../rust/zkspv/groth16-integration/README.md).
Используется закреплённый ARM64-образ; ранее записанное ограничение x86 не
применяется к проверенному образу. `submitTransition` и production-приёмка этим
запуском не закрыты.

Готовый [пакет для macOS ARM64](../ops/artifacts/zkspv/launch-packages/groth16-macos-arm64.tar.gz)
содержит receipt, нативные инструменты и pinned sources; [инструкция](../ops/artifacts/zkspv/launch-packages/README.md)
описывает проверку готового proof и повторную обёртку. После отдельной распаковки
все 334 хеша и положительная EVM-проверка были повторно проверены. Docker-образ
скачивается отдельно в подготовке; никакой proof в сеть не отправляется.

Отдельный [state-bound запуск](../rust/zkspv/state-proof-integration/README.md)
теперь завершён: настоящий новый Groth16 принят `submitTransition` фактически
развёрнутого state, проверены replay/domain rejection, материализация заголовка
и истечение Fresh. Подготовка держит локальную Anvil до принятия proof; `accept`
закрывает только этот узел. `report.json` связывает исходники, все три receipt-стадии
и EVM-квитанции. Существующий launch package выше сохраняет прежний standalone
verifier-сценарий; новый результат находится в `state-proof-integration`.
Исторические часы Anvil не подтверждают live freshness/SLA или production-приёмку.

## Неустранимые этим snapshot ограничения

Шифрование SPV requests/ACK/status реализовано отдельно от DKG transport;
полный zero-balance SPV bootstrap, live E2E и relay-role/DKG certificate profile
пока не квалифицированы. Funded-caller claims и локальная FROST-ротация кассы
проверены отдельно; production handover/readiness ещё не квалифицированы.
Адресные requests/Ready, все старые epochs и поздние ingress требуют полного
watch set, проверенной derivation и реального per-input spend. Не показывать
адрес пользователю как Ready. Funding extractor доступен независимо; monetary
envelope текущего контрактного пути не расширяется автоматически.

SMP1 recipient/txid и существующие финансовые intents публичны. Архивирование
DB и прежних scripts не заменяет nonce anti-rollback: клонированные signer
states одновременно не используются. Потеря данных/branch context оставляет
RECOVERY_REQUIRED; нельзя очищать journal и продолжать signing. Отказ worker
не означает on-chain halt. Для §15.11 выбрана [локальная блокировка и восстановление журнала](ZKSPV_OBSERVATION_HISTORY.md)
с неизменённым mainnet guest; полнота неопубликованной истории и общий on-chain halt
не гарантируются. Old-threshold availability и post-mint Bitcoin reorg остаются
отдельными рисками.
