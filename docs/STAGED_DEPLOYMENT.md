# Поэтапное развёртывание инфраструктуры и комиссии проекта

Этот runbook описывает [ops/deployment_stages.py](../ops/deployment_stages.py), [DeployFederation.s.sol](../web3/script/DeployFederation.s.sol) и [DeployDeploymentFeeRouter.s.sol](../web3/script/DeployDeploymentFeeRouter.s.sol). Полная платная последовательность с отдельной project DKG и legacy manifest/package-подписями ниже относится к Anvil (`31337`) и Sepolia (`11155111`):

1. `base`: deployer-граф, DKG registry, FROST coordinator и проектные кошельки.
2. Отдельная настоящая проектная DKG с purpose `EVM_AUTH`.
3. `fee_router` и `factories`; оба этапа должны завершиться до настройки плательщиков.
4. `prepare_payer` → внешняя FROST-сессия и подписи участников → `execute_payer` для первой фабрики.
5. Новый `prepare_payer` → новая FROST-сессия → `execute_payer` для второй фабрики.
6. `fees`: одноразовая настройка комиссии обеих фабрик.

`factories` также можно выполнить сразу после `base`: ему не нужны проектная DKG, токен комиссии или router. Подписи участников выполняются отдельными процессами. Runner не создаёт DKG, не подписывает операции за участников и не выполняет Bitcoin broadcast.

Проектные `projectAuth`, vault и router обслуживают deployment-комиссию. Используется отдельная проектная DKG, а не авторизация DAO пользовательской конфедерации. При `DEPLOY_STAKER_BPS=0` вся deployment-комиссия перечисляется в явно заданный `PROJECT_TREASURY`. Для этого проектного vault **не создаются settlement и активный source `DEPLOY_FEE`**.

Схема разделения проектной комиссии и доходов пользовательского DAO:
[FEE_FLOW.md](FEE_FLOW.md). Платный сценарий обеих фабрик проверен локально;
публичный платный деплой Sepolia пока не выполнен. Освобождение на Sepolia
выбирается явно, а не включается автоматически по chain ID.

## Ethereum mainnet: отличие от локального примера

Для нового полного запуска с друзьями используйте [V6 bootstrap](ETHEREUM_MAINNET_RUNBOOK.md)
и [независимый выбор сетей/flow](NETWORKS_AND_AUTOMATION.md). Существующие journals
продолжают по сохранённому deployment; смена runtime/плана требует отдельной миграции.
На chain `1` поддерживаются отдельные стадии инфраструктуры с уже завершёнными
и проверенными DKG results/authorization. Legacy `--manifest`/`--package` команды
из разделов 3.2 и 3.4 **не поддерживают** отдельную mainnet project ceremony:
они не предоставляют V6 enrollment/transaction policy. Полный поддерживаемый
mainnet bootstrap и подписи используют on-chain V6 и тот же завершённый
`evm_ceremony` result для отдельной project authorization. Это не четвёртая DKG.
Одних стадий `base`/`factories` для рабочего DAO/Bitcoin wallet недостаточно.
На Ethereum mainnet задайте `chain_id=1` и `confirmations>=12`;
runner дополнительно требует `finalized` RPC block и данные для provenance.
История блоков/receipts и историческое состояние — разные требования: известный
блок прямого CREATE позволяет обойтись без старого code/`eth_call`, тогда как
первое доказательство вложенных контрактов и поиск неизвестного блока могут
нуждаться в нём. См. [границы RPC](CONTRACT_PROVENANCE.md).
Неподтверждённые receipts не считаются завершённой стадией.

`base`/`factories` по-прежнему не требуют токена или финансовых значений. На
финансовых стадиях chain 1 обязательны:

- `PROJECT_REWARD_TOKEN`, его `PROJECT_REWARD_TOKEN_CODE_HASH` (`0x` + 64 hex),
  `PROJECT_REWARD_TOKEN_DECIMALS` (десятичная строка) и проверенная treasury;
- `INTEGRATION_FEE_EXEMPTION="false"` и положительный `DEPLOYMENT_FEE_AMOUNT`;
- `WRAP_STAKER_BPS="0"` и `DEPLOY_STAKER_BPS="0"`: весь project fee идёт treasury.

В локальном окружении перед mutating stages задайте
`SATOSHIFI_MAX_FEE_PER_GAS_WEI`, `SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI` и
`SATOSHIFI_MAX_TX_GAS`; ограничения и единицы приведены в V6 runbook. Runner
не подбирает эти бюджеты за оператора. `prepare_payer` только читает и не требует
fee caps или приватного ключа. Runner проверяет параметры **каждой фактически
подписанной** EIP-1559 транзакции через локальную RPC-границу до отправки, включая
resume. Он не полагается на `forge --gas-limit`: этот флаг ограничивает также
большой локальный script harness и не выражает нужный предел дочерней транзакции.

`DAOTestUSD`, `DAOMockFeed`, legacy-initiation, `exempt_all` и sample/demo scripts
на mainnet не разрешаются. Публично известные адреса из `.local.example.json`
не являются конфигурацией mainnet. Ниже они сохраняются только как локальный пример.

## 1. Подготовка и публичная конфигурация

Команды ниже выполняются из корня репозитория. Нужны установленный `signer-agent` с Python-зависимостями, Foundry в `PATH` и собранные ABI/контракты. Зафиксированная конфигурация проекта: Solidity 0.8.30, `evm_version=osaka`, `via_ir=true`, optimizer runs 1. Проверки размера выполняются без повышения лимитов EIP-170/EIP-3860:

```sh
forge build --root web3 --sizes
python ops/check_contract_sizes.py
python ops/generate_abis.py --check
```

Схема `--config` содержит ровно три верхнеуровневых поля:

| Поле | Формат и назначение |
|---|---|
| `chain_id` | Положительный десятичный integer или его строковое представление; ожидаемый EVM chain ID. |
| `confirmations` | Положительное десятичное число подтверждений **EVM receipt**, включая блок транзакции. Это не глубина Bitcoin SPV. В существующем manifest значение можно повышать, но нельзя уменьшать. |
| `parameters` | Объект только с разрешёнными публичными именами. **Каждое значение — строка JSON**, включая суммы, nonce, CSV и `"true"`/`"false"`. |

`PRIVATE_KEY` и RPC URL не входят в публичный JSON или manifest. `PRIVATE_KEY` передаётся Foundry-скриптам через локальное окружение; это ключ EVM-отправителя, а не FROST-доля. Используйте выбранную локальную учётную запись без вывода её ключа. RPC передаётся только флагом `--rpc-url`; URL с данными доступа также хранится локально. Пароли, mnemonic, transport secrets, FROST shares и private participant state не добавляются в эти файлы.

Готовая публичная заготовка: [`config/deployment-stages.local.example.json`](../config/deployment-stages.local.example.json). Она достаточна для `base` на 31337. Owner и первый wallet в ней **сознательно** равны общеизвестному адресу первого локального аккаунта Anvil, `0xf39F…2266`; это примерные значения, которые надо проверить и заменить перед применением. Доля `10000` и label также выбраны явно. Ключа аккаунта в примере нет. Финансовые поля отсутствуют до выбора реального reward token и завершения проектной DKG; нулевые placeholders для них не нужны.

```sh
cp config/deployment-stages.local.example.json config/deployment-stages.local.json
export DEPLOY_RPC_URL='http://127.0.0.1:8545'
export DEPLOY_CONFIG='config/deployment-stages.local.json'
export DEPLOY_MANIFEST='ops/artifacts/project-local.json'
```

Сначала отредактируйте публичную копию и подготовьте локальный `PRIVATE_KEY` отправителя. `base` и `factories` используют владельца соответствующих deployer-контрактов. Назначение wallet/treasury не выводится из этого отправителя автоматически.

## 2. Точный CLI и смысл этапов

```sh
python ops/deployment_stages.py \
  --config "$DEPLOY_CONFIG" \
  --manifest "$DEPLOY_MANIFEST" \
  --stage base \
  --rpc-url "$DEPLOY_RPC_URL"
```

| Флаг | Обязательность и действие |
|---|---|
| `--config PATH` | Обязателен: публичные параметры. Новые параметры разрешено добавлять по мере прохождения этапов. |
| `--manifest PATH` | Обязателен: журнал именно этого графа и выбранной сети. |
| `--stage NAME` | Обязателен: `base`, `factories`, `fee_router`, `prepare_payer`, `execute_payer`, `fees` или `exempt_all`. |
| `--rpc-url URL` | Обязателен: выбранный EVM RPC; runner сверяет `eth_chainId`. |
| `--broadcast` | Явно разрешает отправку транзакций. Без него выполняется симуляция. Для `prepare_payer` этот флаг запрещён: этап только читает состояние. |
| `--resume-broadcast` | Только вместе с `--broadcast`, для продолжения текущего незавершённого запуска того же этапа. Точный журнал выбирается автоматически. |

Флага `--run-id` нет. Runner задаёт `EXPECTED_CHAIN_ID` из `chain_id`, `DEPLOYMENT_STAGE` из `--stage` и собственный путь Foundry broadcast. Не добавляйте эти служебные поля в `parameters`. `prepare_payer` не требует `PRIVATE_KEY`; остальные этапы, включая их симуляции, используют выбранного локального отправителя.

| Этап | Обязательные публичные входы сверх сети | Результат |
|---|---|---|
| `base` | Пять параметров проектных кошельков из следующей таблицы. | Базовые deployer-контракты, registry/coordinator/verifier, registry кошельков; связанный DKG registry и снятый у отправителя `ADMIN_ROLE`. |
| `factories` | `DKG_COORDINATOR`, `FROST_COORDINATOR`, `FEDERATION_DEPLOYER`, `CONFEDERATION_DEPLOYER`. | Обе фабрики и их разрешения в parent deployer-контрактах. Комиссии ещё не настроены. |
| `fee_router` | `DKG_COORDINATOR`, `FROST_COORDINATOR`, `FROST_AUTHORIZATION_DEPLOYER`, `PROJECT_DKG_ID`, токен, treasury и обе доли bps. | Сначала `projectAuth`, затем проектный income vault, затем router. |
| `prepare_payer` | Обе фабрики, router, `DKG_COORDINATOR`, `FROST_COORDINATOR`, `PROJECT_DKG_ID`, токен, treasury и обе доли bps. | Первый ещё не разрешённый payer, **текущий** nonce router и его operation hash. |
| `execute_payer` | Всё для `prepare_payer`, плюс `PAYER_OPERATION_NONCE`, `PAYER_OPERATION_HASH`, `FROST_SESSION_ID`. | Авторизация существующей завершённой FROST-сессии и `setPayer(payer,true,hash)`. |
| `fees` | Обе фабрики, `DEPLOYMENT_FEE_ROUTER`, `DEPLOYMENT_FEE_AMOUNT`, `INTEGRATION_FEE_EXEMPTION`; для платного режима также проверяемая проектная политика как у `prepare_payer`. | Одноразовый `configureDeploymentFee` каждой ещё не настроенной фабрики. |
| `exempt_all` | Параметры `base`, явное освобождение, нулевые router и amount; только 31337/11155111. | `base` → `factories` → нулевая exempt-настройка. |

После подтверждения `base` адреса из manifest автоматически передаются следующим этапам. Не копируйте адреса из вывода одной лишь симуляции.

### Проектные кошельки

Все пять значений обязательны для `base`, включая явный ноль для отсутствующего второго кошелька.

| Имя в `parameters` | Ограничения и единицы |
|---|---|
| `PROJECT_REGISTRY_OWNER` | Ненулевой owner `GlobalProjectWalletRegistry`. |
| `PROJECT_WALLET_1` | Ненулевой адрес первого проектного кошелька. |
| `PROJECT_WALLET_2` | Адрес второго кошелька либо `0x0000000000000000000000000000000000000000`. При наличии отличается от первого. |
| `PROJECT_WALLET_SHARES` | CSV десятичных bps в порядке кошельков. Ровно 1 либо 2 числа; каждое от 0 до 10000, сумма больше 0 и не больше 10000. `10000` bps = 100%. |
| `PROJECT_WALLET_LABELS` | CSV из 1 либо 2 непустых labels в том же порядке. Запятая служит разделителем и не входит в сам label. |

Ни отсутствующая доля, ни owner, ни label не заменяются скрытым default.

### Политика платного router

Добавляйте значения в тот же `parameters` после выбора и проверки соответствующих контрактов:

| Имя | Ограничения и единицы |
|---|---|
| `PROJECT_DKG_ID` | `0x` + 64 hex-символа: bytes32 завершённой **отдельной проектной** DKG с purpose `EVM_AUTH`. |
| `PROJECT_REWARD_TOKEN` | Адрес контракта ERC-20 с `decimals() <= 18`. |
| `PROJECT_REWARD_TOKEN_CODE_HASH`, `PROJECT_REWARD_TOKEN_DECIMALS` | Обязательны на chain 1: проверенные runtime hash и decimals внешнего токена. |
| `PROJECT_TREASURY` | Ненулевой получатель deployment-комиссии; не адрес reward token. |
| `WRAP_STAKER_BPS` | Явное десятичное целое от 0 до 10000. Это доля wrap-потока, а не deployment-потока. |
| `DEPLOY_STAKER_BPS` | Обязательно строка `"0"` для этого развёртывания. |
| `INTEGRATION_FEE_EXEMPTION` | Для платного пути строка `"false"`. |
| `DEPLOYMENT_FEE_AMOUNT` | Положительная сумма в **минимальных единицах** reward token, без десятичной точки. Например, `"50000000"` означает 50 токенов при `decimals=6`; это иллюстрация единиц, а не default комиссии. |
| `PAYER_OPERATION_NONCE` | Десятичный `uint256`, полученный последним `prepare_payer`. Это nonce router, а не nonce EVM-аккаунта. |
| `PAYER_OPERATION_HASH` | Bytes32 из того же `prepare_payer`. |
| `FROST_SESSION_ID` | Десятичный `uint256` завершённой signing-сессии FROSTCoordinator. Не путать с bytes32 `PROJECT_DKG_ID`. |

Групповой ключ, упорядоченный roster и threshold берутся из `getCompletedResult(PROJECT_DKG_ID)`. Отдельных полей для ручной подстановки этих значений нет. Перед отправкой проверяются код зависимостей, связанный registry, purpose и право отправителя вызвать `FROSTAuthorizationDeployer`.

### Адреса для повторного использования

Следующие необязательные публичные поля выбирают уже существующие компоненты. В создающем их этапе отсутствие или нулевой адрес означает «создать». После появления в подтверждённом manifest runner использует сохранённый адрес. До запуска Forge runner сверяет runtime и каноническую историю создания каждого явно выбранного компонента с локальным artifact, читает его зависимости через canonical getters и проверяет согласованность всего найденного графа. Проверка включает creation bytecode, каноническое ABI-кодирование аргументов, успешный CREATE/поддерживаемый deployer, receipt и блок создания. Наличие произвольного кода или общего интерфейса прав доступа не является достаточной проверкой типа контракта. Проверки владельца и готовности соответствующего этапа в Solidity-скрипте сохраняются.

| Компоненты | Имена параметров |
|---|---|
| Листовые deployer-контракты пулов | `FROST_AUTHORIZATION_DEPLOYER`, `MULTIPOOL_SHARE_TOKEN_DEPLOYER`, `SIMPLE_MULTIPOOL_DEPLOYER`, `CONFEDERATION_TOKEN_DEPLOYER`, `MULTIPOOL_DEPLOYER`, `FROST_CUSTODIAN_DEPLOYER` |
| Deployer-контракты fee-графа | `FEDERATION_FEE_MANAGER_DEPLOYER`, `FEE_DISTRIBUTION_CALCULATOR_DEPLOYER`, `FEDERATION_NFT_MODIFIER_DEPLOYER`, `PROJECT_WALLET_REGISTRY_DEPLOYER`, `FEE_SYSTEM_DEPLOYER` |
| Родительские deployer-контракты | `FEDERATION_DEPLOYER`, `CONFEDERATION_DEPLOYER` |
| DKG/FROST | `DKG_COORDINATOR`, `FROST_VERIFIER`, `FROST_COORDINATOR` |
| Проектные кошельки | `GLOBAL_PROJECT_WALLET_REGISTRY` |
| Фабрики | `FEDERATION_FACTORY`, `CONFEDERATION_FACTORY` |
| Проектная комиссия | `PROJECT_AUTHORIZATION`, `PROJECT_INCOME_VAULT`, `DEPLOYMENT_FEE_ROUTER` |

Существующий child-контракт должен ссылаться на правильные уже существующие зависимости. Например, vault связан с `PROJECT_AUTHORIZATION`, а router — с `PROJECT_INCOME_VAULT`. Runner может прочитать неуказанную зависимость из getter и закрепить её; явно переданный другой адрес вызывает отказ до новых транзакций. Для переиспользования компонентов используйте Python runner. Прямые Solidity-скрипты проверяют runtime и getters, но не историю конструктора; переданные им существующие адреса должны быть независимо проверены. Прямой Foundry-вызов не заменяет проверку происхождения. Проверка runtime учитывает только объявленные компилятором immutable-поля; повторяющиеся ссылки одного immutable должны содержать одинаковые значения. Полный фактический runtime hash и связи дополнительно проверяются при повторном запуске, включая `--resume-broadcast`.

У переиспользуемого контракта manifest сохраняет `provenance="observed_existing"`, `block_number`, `block_hash`, `runtime_hash` и отдельный `creation_proof`. Метка обозначает обнаружение существующего адреса; проверка канонического создания обязательна и повторяется при загрузке manifest. Старое наблюдение runtime без proof дополняется только после проверки исторических блоков и receipt. Недоступная история останавливает запуск. Проверяется наличие того же кода при требуемом количестве подтверждений; увеличение `confirmations`, смена canonical block, изменение кода или зависимостей не обходят проверку. Новый `projectAuth`, созданный внутренним вызовом закреплённого authorization deployer, определяется по его настоящему receipt/event. Сам deployer также проверяется по каноническому созданию. Для существующего coordinator проверяется ADMIN_ROLE его исходного создателя из creation proof: незавершённый base может продолжить этот создатель; завершённый граф не должен оставлять ему ADMIN_ROLE. Требования к историческому RPC и границы прямых скриптов описаны в [проверке происхождения](CONTRACT_PROVENANCE.md).

## 3. Платный путь: команды и точки ожидания

### 3.1. Base

Сначала симуляция, затем отдельный явный broadcast:

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage base --rpc-url "$DEPLOY_RPC_URL"

python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage base --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Продолжайте после достаточных подтверждений. Проверьте `FROSTCoordinator.dkgRegistry()` и отсутствие `ADMIN_ROLE` у исходного отправителя. Отказ от admin не лишает создателя права управлять **своей** signing-сессией; права менять чужие сессии он больше не имеет. Новый participant-only запрет на создание сессий не вводится.

### 3.2. Отдельная проектная DKG

Команды этого раздела предназначены только для Anvil/Sepolia. Не применяйте
legacy manifest/mailbox-процедуру к chain `1`; mainnet использует V6 выше.

Создайте новую церемонию `EVM_AUTH` на registry из manifest с согласованными участниками, их публичными transport keys и явным threshold. Каждый участник хранит собственное приватное состояние. Порядок roster фиксируется до начала DKG.

Если `PARTICIPANT_IDENTITY` отличается от `ceremonyRegistrar()` нового реестра,
регистратор сначала назначает этот адрес создателем для ID из manifest.
[Порядок назначения](DKG_PROTOCOL_V1.md#ceremony-creation).

Детальная процедура и команды приведены в [DKG runbook](DKG_RUNBOOK.md) и [первом on-chain запуске](FIRST_DKG_ONCHAIN.md). Для manifest-пути отдельной проектной церемонии используются существующие команды:

```sh
frost-signer dkg create --manifest "$PROJECT_DKG_MANIFEST" --rpc "$DEPLOY_RPC_URL" \
  --identity-file "$PARTICIPANT_IDENTITY"

frost-signer dkg run-once --manifest "$PROJECT_DKG_MANIFEST" --rpc "$DEPLOY_RPC_URL" \
  --identity-file "$PARTICIPANT_IDENTITY" --state "$PARTICIPANT_STATE" \
  --password-file "$PARTICIPANT_PASSWORD_FILE" --mailbox "$PROJECT_PUBLIC_MAILBOX"

frost-signer dkg status --manifest "$PROJECT_DKG_MANIFEST" --rpc "$DEPLOY_RPC_URL"
```

`create` выполняется один раз; `run-once` выполняет каждый участник до завершения церемонии. `WAITING` требует участия остальных, а не подстановки искусственного результата. Проектный public manifest DKG и локальный mailbox здесь являются входами существующего участнического CLI; `ops/deployment_stages.py` ими не управляет.

После завершения добавьте в публичный config `PROJECT_DKG_ID`, проверенные token/treasury и обе доли bps. Manifest runner сохраняет подтверждённый `project_dkg_result`; отсутствие завершённой DKG не превращается в успешный результат.

### 3.3. Router и фабрики

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage fee_router --rpc-url "$DEPLOY_RPC_URL" --broadcast

python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage factories --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

В этих командах тоже можно сначала опустить `--broadcast` для симуляции. `fee_router` создаёт только отсутствующие `projectAuth`, vault и router. У существующей авторизации сверяются registry, coordinator, DKG id/result, ключ, полный roster и threshold. У существующего vault проверяются reward token, authorization, отсутствие settlement и активного deployment source. У router проверяются treasury, token, доли и привязка к проектной авторизации.

### 3.4. Первый payer: подготовить, подписать, исполнить

Legacy manifest/package-подписи и прямой `cast send` ниже относятся только к
Anvil/Sepolia. На chain `1` используйте опубликованные V6 intents и
`dkg sign-evm --board ...` из mainnet runbook; подмена chain ID в package
не делает этот legacy путь поддерживаемым mainnet-сценарием.

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage prepare_payer --rpc-url "$DEPLOY_RPC_URL"
```

Получите `Next payer`, `Current nonce`, `Operation hash`. В новом графе первым будет `FederationFactory`. Сохраните nonce/hash в одноимённых `PAYER_OPERATION_*` полях config. Если payer равен нулевому адресу, обе фабрики уже разрешены; новую signing-сессию создавать не требуется.

Оператор создаёт настоящую сессию:

```solidity
FROSTCoordinator.createSessionFromDKG(
    projectDkgId,
    operationHash,
    deploymentFeeRouter,
    explicitFutureDeadline
);
```

`explicitFutureDeadline` — выбранный абсолютный EVM timestamp в секундах, помещающийся в `uint64`. Для отправки через заранее подготовленный локальный keystore, без ключа в командной строке:

```sh
cast send "$FROST_COORDINATOR" \
  'createSessionFromDKG(bytes32,bytes32,address,uint64)' \
  "$PROJECT_DKG_ID" "$PAYER_OPERATION_HASH" "$DEPLOYMENT_FEE_ROUTER" "$SIGNING_DEADLINE" \
  --rpc-url "$DEPLOY_RPC_URL" --account "$SESSION_CREATOR_ACCOUNT"
```

В этой команде адреса, id и hash — публичные значения, извлечённые из подтверждённого графа/подготовленной операции; `SESSION_CREATOR_ACCOUNT` — имя уже настроенного локального keystore. Возьмите session ID из подтверждённого события `SessionCreated`, а не из ожидаемого следующего номера.

Участникам передаётся проверяемый public intent следующей формы; placeholders заменяются точными значениями подготовленной операции. В примере `chain_id="31337"`; для Sepolia укажите `"11155111"`:

```json
{
  "chain_id": "31337",
  "target": "<deployment fee router address>",
  "operation_hash": "<prepared bytes32 hash>",
  "artifact": "CustodianDAOFeeRouter",
  "hash_method": "getSetPayerOperationHash",
  "hash_args": ["<prepared factory address>", true]
}
```

Каждый подписывающий участник явно запускает:

```sh
frost-signer dkg sign-evm --manifest "$PROJECT_DKG_MANIFEST" --rpc "$DEPLOY_RPC_URL" \
  --coordinator "$FROST_COORDINATOR" --session "$FROST_SESSION_ID" \
  --package "$PAYER_INTENT_FILE" --identity-file "$PARTICIPANT_IDENTITY" \
  --state "$PARTICIPANT_STATE" --password-file "$PARTICIPANT_PASSWORD_FILE"
```

Агент независимо вызывает helper target-контракта, проверяет результат и DKG binding. Участники повторяют команду для commitments и shares согласованного набора; после threshold происходит настоящая агрегация/проверка подписи. Точный механизм и сохранение nonce описаны в [SIGNING_PROTOCOL.md](SIGNING_PROTOCOL.md). Состояние ожидания или один session ID не означают FROST-одобрение.

После завершения сессии запишите её десятичный `FROST_SESSION_ID` в config и исполните:

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage execute_payer --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Скрипт ещё раз читает текущие nonce/hash и отклоняет устаревшие входы. Если `projectAuth.isOperationApproved(hash,router)` уже истинно после ранее подтверждённой части запуска, повторная авторизация пропускается и выполняется оставшийся `setPayer`. После успеха проверяются `authorizedPayers(payer)==true` и увеличение router nonce ровно на 1.

### 3.5. Второй payer и fees

После подтверждения первой операции **повторите** `prepare_payer`. Он прочитает новый nonce и выберет `ConfederationFactory`. Создайте отдельную FROST-сессию для нового hash, получите реальные подписи и повторите `execute_payer`. Два hash нельзя заранее получать через current-nonce getter: исполнение первой операции изменяет nonce.

После завершённых запусков `PAYER_OPERATION_NONCE`, `PAYER_OPERATION_HASH` и `FROST_SESSION_ID` разрешено обновлять для следующей операции. Во время продолжения незавершённого broadcast они должны оставаться неизменными.

Добавьте `INTEGRATION_FEE_EXEMPTION="false"` и положительный `DEPLOYMENT_FEE_AMOUNT`, затем выполните:

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage fees --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Перед первым изменением `fees` проверяет обе фабрики, разрешения обоих payers, router policy и полномочия отправителя для ещё не настроенных фабрик. Совпадающая готовая настройка пропускается. Другая сумма, router или exemption вызывает ошибку: настройки фабрики одноразовые.

Для создания сущности на готовой фабрике её `feeSetupAuthority()` сначала
резервирует ID за конкретной EVM_AUTH DKG. После завершения DKG допустимый
инициатор выполняет V1 adoption с этой парой. Это отдельные транзакции после
развёртывания фабрик. [Команды резервирования федерации](FEDERATION_CONFEDERATION_SETUP.md#6-дополнительная-федерация-на-существующей-factory) ·
[Команды резервирования конфедерации](FEDERATION_CONFEDERATION_SETUP.md#7-дополнительная-конфедерация-на-существующей-factory).

Вызывающий финальный deploy выдаёт ERC-20 allowance **адресу фабрики**.
Фабрика списывает ровно `DEPLOYMENT_FEE_AMOUNT`, а router перечисляет его
в `PROJECT_TREASURY`. При недостаточном балансе или allowance финальный
deploy откатывается целиком. Подтверждённые резервирование и adoption
позволяют повторить deploy после пополнения.

### Разные `feeSetupAuthority`

Общий `fees` удобен, когда один отправитель уполномочен настроить обе ещё не настроенные фабрики. Для разных authority используются две отдельные операции `configureFactory(address)` из `DeployDeploymentFeeRouter.s.sol`.

Перед каждым вызовом передайте в локальное окружение все публичные fee-параметры и адреса из таблиц выше, включая `EXPECTED_CHAIN_ID`. Для соответствующего вызова `PRIVATE_KEY` должен принадлежать именно `feeSetupAuthority()` выбранной фабрики:

```sh
forge script --root web3 script/DeployDeploymentFeeRouter.s.sol:DeployDeploymentFeeRouter \
  --sig 'configureFactory(address)' "$FEDERATION_FACTORY" \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast

# Локально переключите PRIVATE_KEY на authority второй фабрики.
forge script --root web3 script/DeployDeploymentFeeRouter.s.sol:DeployDeploymentFeeRouter \
  --sig 'configureFactory(address)' "$CONFEDERATION_FACTORY" \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Оба payer permissions всё равно должны быть подтверждены до этих вызовов. Прямой запуск Foundry не импортируется автоматически в журнал Python runner: сохраните receipts отдельно. Для возвращения к runner восстановите локально исходного отправителя manifest. Последующий этап `fees` сверит готовую настройку и пропустит уже совпадающие записи. Изменять authority или повторно конфигурировать фабрику этот путь не позволяет.

## 4. Явное освобождение на 31337/11155111

Для локального/Sepolia exempt-развёртывания используйте отдельный config/manifest или ещё не настроенный граф. Помимо пяти wallet-параметров явно добавьте:

```json
{
  "INTEGRATION_FEE_EXEMPTION": "true",
  "DEPLOYMENT_FEE_ROUTER": "0x0000000000000000000000000000000000000000",
  "DEPLOYMENT_FEE_AMOUNT": "0"
}
```

Это фрагмент `parameters`, а не полный config. Затем:

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage exempt_all --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Router, проектная DKG и финансовый токен для этого этапа не требуются. Фабрики получают ровно `(address(0),0,true)`. На других chain ID освобождение запрещено. Для платного теста Sepolia создайте новые фабрики с `exemption=false`; уже настроенные exempt-фабрики нельзя превратить в платные.

Legacy-initiation, legacy deployment и legacy inter-federation commitments
допускаются только на 31337/11155111. Инициировать legacy DKG через новую
фабрику может её `feeSetupAuthority()` для свободного ID и
незарезервированной сессии. V1 adoption требует подтверждённого резерва,
финальный deploy требует настройки комиссии или допустимого exemption.

## 5. Подтверждения, повтор и частичный запуск

Runner использует manifest schema `satoshifi-deployment-v2`. Помимо публичных параметров и адресов он хранит отправителя, политику confirmations, транзакции, результаты этапов и отдельные `runs[run_id]`. Один manifest закреплён за одним EVM-отправителем: смена `PRIVATE_KEY` на другой адрес отклоняется. Старый schema-v1 manifest автоматически не мигрируется. Каждый новый broadcast получает отдельный каталог:

```text
<manifest-stem>-broadcast/<run_id>/DeployFederation.s.sol/<chain_id>/run-latest.json
```

Продолжение выбирает сохранённый `inflight_run`; новый запуск не читает случайный `run-latest.json` от прежнего этапа. Для возобновления незавершённого `execute_payer`, например:

```sh
python ops/deployment_stages.py --config "$DEPLOY_CONFIG" --manifest "$DEPLOY_MANIFEST" \
  --stage execute_payer --rpc-url "$DEPLOY_RPC_URL" --broadcast --resume-broadcast
```

Используйте прежние manifest, сеть, отправителя, stage и объединённые публичные параметры текущего запуска, включая payer nonce/hash/session. Runner передаёт Foundry `--resume` для **того же** run-specific журнала. Частично отправленный запуск остаётся незавершённым даже тогда, когда уже отправленный префикс полностью подтверждён: ещё не отправленные транзакции не следует терять.

| Ситуация | Действие |
|---|---|
| Транзакция отправлена, receipt отсутствует или confirmations ещё мало | Дождаться результата; другой этап блокируется. Для продолжения отправки — тот же stage с `--broadcast --resume-broadcast`. |
| Подтверждён только префикс broadcast | Продолжить сохранённый запуск с прежними параметрами и его журналом. |
| Этап целиком завершён | Обычный повтор использует подтверждённые адреса и пропускает совпадающее состояние. Для следующего payer заново читается current nonce. |
| Preflight отклонён до отправки | Runner разрешает новый исправленный запуск только после доказательства отсутствия отправки: журнал не содержит submitted tx, `latest` и `pending` nonce отправителя не изменились. Параметры неуспешной попытки тогда не фиксируются. |
| Была только симуляция | Предложенные параметры и предсказанные адреса не считаются подтверждённым deployment. |
| Receipt со status 0 | Транзакция не считается успешно выполненным шагом. Исправление причины не превращает старый receipt в успех. |
| Изменились chain, привязанный runtime/receipt, готовая immutable-конфигурация | Остановка и разбор несоответствия; не редактируйте журнал для объявления неподтверждённого успеха. |

Если у всех запланированных транзакций текущего запуска уже есть окончательные receipts, runner допускает новый обычный broadcast **того же этапа**: новая симуляция сверит готовую часть и повторит оставшиеся необходимые действия после устранения причины revert. Для неполного плана с ещё не отправленными транзакциями используется прежний `--resume-broadcast`. Новый этап не подменяет незавершённый запуск.

Новый адрес добавляется по успешному каноническому receipt после выбранного количества подтверждений. Проверяются сеть, отправитель, nonce, target/calldata/value транзакции, код, фактический runtime hash и размеры deployment; внутреннее создание `projectAuth` определяется по событию закреплённого authorization deployer. Для существующих адресов используется отдельная provenance, описанная в разделе 2. При доказанном preflight-отказе без транзакций откатываются предложенные параметры и вновь закреплённые адреса; dry run не сохраняет предложенные anchors. Manifest не является восстановлением потерянных FROST-секретов: сохранение signing state и nonce journal каждого участника остаётся отдельной обязанностью.

Подтверждённые нетранзиентные параметры нельзя менять в том же графе. Можно добавлять ранее отсутствовавшие поля, а три transient payer-поля менять между завершёнными запусками. Смена RPC endpoint той же сети не заменяет проверку сохранённых канонических receipts и runtime. При восстановлении храните manifest вместе с его run-specific broadcast-каталогами и локальным `<manifest-stem>-private/<run_id>/cache`. Последний содержит приватный Foundry resume cache и не предназначен для публикации. Временный keystore для `--resume` runner создаёт локально, не помещая ключ в argv.

Каталог manifest должен принадлежать текущему пользователю и не быть доступным
на запись группе/остальным. Приватные каталоги и их run/cache уровни получают
0700; подставленные symlink и чужой владелец приводят к отказу. Генерируемые
Foundry cache/broadcast исключены из обычного Git добавления, но это не заменяет
проверку перед публикацией резервной копии. Внешние ошибки RPC выводятся без
произвольного текста, чтобы не раскрывать URL credentials; собственные ошибки
runner имеют стабильные коды.

Нулевой process exit сам по себе не означает достаточные подтверждения: если receipts ещё не готовы, runner возвращает код `2` с `DEPLOYMENT_NOT_CONFIRMED` и сохраняет run. Между этапами также дождитесь завершения любых внешних транзакций того же EVM-отправителя: новый broadcast отклоняется, пока `latest` и `pending` nonce этого адреса различаются.

## 6. Дополнительные deployment-входы

Эти параметры относятся к отдельным контрактам/скриптам и **не входят** в allowlist `parameters` для `ops/deployment_stages.py`.

### Отдельные Forge leaf scripts на Ethereum mainnet

Ниже сохранены прямые Forge-примеры для интеграционных сетей. Для mainnet
запускайте leaf script через [ethereum_rpc_budget.py](../ops/ethereum_rpc_budget.py),
после задания `EXPECTED_CHAIN_ID=1`, своего локального `PRIVATE_KEY`, трёх
`SATOSHIFI_…` лимитов и публичных env-параметров конкретного скрипта.
`DEPLOYER_ADDRESS` должен совпадать с адресом этого identity. Пример relay:

```sh
python ops/ethereum_rpc_budget.py --rpc-url "$DEPLOY_RPC_URL" \
  --sender "$DEPLOYER_ADDRESS" --confirmations "$EVM_CONFIRMATIONS" -- \
  forge script --root web3 script/DeployMainnetHeaderRelay.s.sol:DeployMainnetHeaderRelay \
  --slow --non-interactive --broadcast
```

Для симуляции сначала выполните ту же команду без `--broadcast`. Для DAO,
adapter или governance замените только script target и задайте его обязательные
входы. Не передавайте `--rpc-url` в дочернюю Forge-команду: wrapper добавляет
локальный endpoint, max fee, priority fee и estimate multiplier сам.
Staged runner и wrapper используют 130% оценки газа Forge, чтобы оставить запас
между симуляцией и исполнением. Этот запас не повышает `SATOSHIFI_MAX_TX_GAS`:
если gas limit подписанной транзакции превышает локальный предел, отправка запрещена.
Он проверяет chain 1 и доступность finalized state, затем сверяет chain, sender,
gas и обе комиссии **до** передачи каждой подписанной транзакции upstream.
Проверяются также cached transactions при Forge resume; сырые подписи не сохраняются
wrapper. Локальные Forge cache/broadcast всё равно являются чувствительными файлами.

Wrapper не заменяет durable manifest или canonical-creation проверку staged runner.
После успешного broadcast дождитесь требуемой глубины **и** финальности всех receipts
и проверьте полученные addresses/bindings, прежде чем использовать их в следующем
процессе. Для полного friends bootstrap используйте V6 CLI, а для управляемых
проектных стадий — `deployment_stages.py` с его manifest и проверками resume.

### Withdrawal escrow: минимумы в сатоши

`BitcoinWithdrawalEscrow` принимает пять обязательных аргументов конструктора:

| Аргумент | Единицы и ограничения |
|---|---|
| `registry` | Адрес `BitcoinBatchRegistry` с кодом. |
| `relay` | Адрес SPV relay с кодом и `testHelpersEnabled()==false`. |
| `confirmations` | `uint32`, глубина **Bitcoin** подтверждений, не меньше 6. |
| `minP2WPKHSats` | Положительное `uint256`, минимум одной P2WPKH-заявки в сатоши. Выбранное значение ТЗ — **294**, передаётся явно. |
| `minP2TRSats` | Положительное `uint256`, минимум одной P2TR-заявки в сатоши. Выбранное значение ТЗ — **330**, передаётся явно. |

Отдельного escrow Foundry-скрипта с `MIN_*` env-полями нет. В интеграционном bootstrap используются явные строки `min_p2wpkh_sats` и `min_p2tr_sats`, см. [BOOTSTRAP.md](BOOTSTRAP.md). При прямом deployment аргументы передаются, например, так; `ESCROW_CONFIRMATIONS` — явно выбранное локальное значение:

```sh
forge create --root web3 contracts/src/federation/BitcoinWithdrawalEscrow.sol:BitcoinWithdrawalEscrow \
  --constructor-args "$BITCOIN_BATCH_REGISTRY" "$BITCOIN_HEADER_RELAY" "$ESCROW_CONFIRMATIONS" 294 330 \
  --rpc-url "$DEPLOY_RPC_URL" --account "$DEPLOY_KEYSTORE_ACCOUNT" --broadcast
```

`1 сат = 10^10` минимальных MP-единиц. Пулы сначала округляют чистую выплату вниз до целых сатоши, а разницу включают в итоговую комиссию. Escrow проверяет минимум каждой отдельной заявки; `requestDirect` дробную MP-сумму отклоняет и сам её не округляет. Эти минимумы не выводятся из future batch aggregation.

### DAO settlement и treasury пользовательской конфедерации

Скрипт `web3/script/DeployCustodianDAO.s.sol` читает:

| Env | Значение |
|---|---|
| `EXPECTED_CHAIN_ID` | Явный EVM chain ID; также обязателен для `registerExisting` и adapter script. |
| `PRIVATE_KEY` | Локальный EVM-отправитель. |
| `CONFEDERATION_FACTORY`, `CONFEDERATION_ID` | Активная конфедерация и её фабрика; ещё без зарегистрированного DAO. |
| `DAO_TOKEN_NAME`, `DAO_TOKEN_SYMBOL` | Явные непустые метаданные нового DAO-токена. |
| `DAO_TOKEN_RECIPIENT` | Явный ненулевой получатель DAO-токена. |
| `DAO_REWARD_TOKEN` | ERC-20 контракт с `decimals() <= 18`. |
| `DAO_REWARD_TOKEN_CODE_HASH`, `DAO_REWARD_TOKEN_DECIMALS` | На chain 1: проверенные runtime hash и decimals reward token. |
| `BTC_USD_CHAINLINK_FEED_CODE_HASH` | На chain 1: проверенный runtime hash feed. |
| `BTC_USD_CHAINLINK_FEED` | Контракт historical feed с `decimals()==8`. |
| `DAO_TREASURY` | Явный ненулевой treasury пользовательского DAO, не адрес самого settlement; передаётся immutable-аргументом settlement. Не подставляется из `msg.sender` или `PROJECT_TREASURY`. |
| `DAO_USE_RESERVE` | Явное boolean-значение начальной политики reserve. |

`DeployCustodianDAO.s.sol` сверяет обязательный `EXPECTED_CHAIN_ID` до broadcast.
На chain 1 он проверяет перечисленные hashes/decimals и отвергает известные test USD/feed.
Перед запуском дополнительно проверьте свой endpoint; для mainnet передайте
явные Forge gas/fee caps из локального бюджета:

```sh
cast chain-id --rpc-url "$DEPLOY_RPC_URL"
forge script --root web3 script/DeployCustodianDAO.s.sol:DeployCustodianDAO \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Этот скрипт создаёт отдельный DAO-граф и выводит pending registration hash. Регистрация требует действующей FROST-авторизации конфедерации и custodian approvals; она не происходит от одного факта deployment. Для существующего графа есть `registerExisting`, описанный в [BOOTSTRAP.md](BOOTSTRAP.md).

У `CustodianDAODailySettlement` новый седьмой аргумент конструктора — `treasury`. В bootstrap это поле `treasury`. `settleDay` принимает отчёт с пустым `report.providers`: оставшаяся LP-часть перечисляется в treasury, `lpIncome` записывается нулём, а перевод отражается в `treasuryIncome`. При отсутствии eligible stake доля стейкеров сначала прибавляется к этой части; при пустом LP-списке reserve не тратится на покрытие недополученной базы. Это отдельный механизм от проектной deployment-комиссии без settlement.

### BitcoinSigningBoard

| Env | Единицы и ограничения |
|---|---|
| `EXPECTED_CHAIN_ID` | Явный EVM chain ID. |
| `BITCOIN_BATCH_REGISTRY` | Адрес реально развёрнутого batch registry; его завершённый wallet epoch имеет purpose `BTC_SPEND`. |
| `MAX_PACKET_BYTES` | Положительный максимум **байтов одного packet**, immutable. |
| `PRIVATE_KEY` | Локальный EVM-отправитель. |

```sh
forge script --root web3 script/DeployBitcoinSigningBoard.s.sol:DeployBitcoinSigningBoard \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Запишите выведенные registry, walletId, walletResult и maxPacketBytes. Board хранит
публичные планы, commitments и signature shares; CLI подключён к нему и собирает
проверенный witness. Один deployment не выполняет платёж: дальнейшие шаги —
[on-chain payment runbook](BITCOIN_ONCHAIN_RUNBOOK.md). API и retry описаны в
[bitcoin-signing-board.md](../web3/docs/bitcoin-signing-board.md).

### Bitcoin mainnet header relay

| Env | Единицы и ограничения |
|---|---|
| `EXPECTED_CHAIN_ID` | Сеть **EVM**, где размещается контракт. Она не выбирает Bitcoin-сеть. |
| `BTC_CHECKPOINT_HEADER` | `0x`-encoded raw header ровно 80 байт. |
| `BTC_CHECKPOINT_HEIGHT` | `uint64`, высота не меньше 10 и кратна 2016. |
| `BTC_CHECKPOINT_CHAINWORK` | `uint256`, накопленная Bitcoin mainnet work **включая** checkpoint, не меньше work самого checkpoint-блока; передать десятичное либо `0x`-число без изменения разрядности. |
| `BTC_CHECKPOINT_PREDECESSORS` | CSV ровно из десяти `0x`-encoded headers по 80 байт: от `H-10` до `H-1`, от старого к новому. |
| `MAX_REORG_DEPTH` | Явное число 1–144: предел удаляемых блоков старой цепи. |
| `MAX_TIP_AGE_SECONDS` | Явное число 1–86400 секунд: on-chain freshness policy guard. |
| `PRIVATE_KEY` | Локальный EVM-отправитель. |

```sh
forge script --root web3 script/DeployMainnetHeaderRelay.s.sol:DeployMainnetHeaderRelay \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

До broadcast оператор независимо выбирает и проверяет checkpoint, предшественников и chainwork. Скрипт не подставляет доверенную точку автоматически. Checkpoint 32256 из тестовых fixtures исторический: его нельзя использовать для системы с реальными средствами. Нужны независимо проверенная недавняя граница периода и синхронизация relay до
принятой актуальной высоты до подключения потребителей. Скрипт создаёт raw relay
**и** `MainnetSPVGuard`; в deposit registry/escrow передаётся адрес **guard**,
а заголовки отправляются в raw relay. Guard проверяет свежесть и halt; keeper
всё равно должен регулярно добавлять заголовки. Развёрнутый на Sepolia `MainnetHeaderRelay` продолжает проверять Bitcoin **mainnet**. Байтовый порядок, trust anchor и поведение постоянного halt при превышении лимита изложены в [MAINNET_RELAY.md](MAINNET_RELAY.md).

### Тестовые USD/feed и явная история дня

`DAOTestUSD` и `DAOMockFeed` развёртываются только на 31337/11155111. У тестового USD 6 decimals и открытый mint; у mock feed 8 decimals и изменяемые rounds. Это явно тестовые компоненты, без fallback на них в рабочем settlement.

```sh
forge create --root web3 contracts/src/demo/DAOTestUSD.sol:DAOTestUSD \
  --rpc-url "$DEPLOY_RPC_URL" --account "$DEPLOY_KEYSTORE_ACCOUNT" --broadcast

forge create --root web3 contracts/src/demo/DAOMockFeed.sol:DAOMockFeed \
  --rpc-url "$DEPLOY_RPC_URL" --account "$DEPLOY_KEYSTORE_ACCOUNT" --broadcast
```

Для `web3/script/SeedDAOTestFeed.s.sol` обязательны:

| Env | Формат и единицы |
|---|---|
| `EXPECTED_CHAIN_ID` | 31337 либо 11155111; должен совпасть с RPC. |
| `DAO_TEST_FEED` | Адрес `DAOMockFeed` с кодом и 8 decimals. |
| `DAO_TEST_DAY` | Десятичный UTC day index: `floor(unixTimestamp / 86400)`. Это не дата вида `YYYY-MM-DD`. День должен быть закрыт по EVM времени. |
| `ROUND_IDS` | CSV последовательных положительных `uint80` без пропуска, не меньше двух, в пределах одной Chainlink phase (`id >> 64`). Высокие `uint80` допустимы; переход через границу phase отклоняется до записи истории. |
| `ROUND_PRICES_8` | CSV положительных `int256` в единицах USD × `10^8`; например, 60 000 USD = `6000000000000`. |
| `ROUND_TIMESTAMPS` | CSV Unix timestamps в секундах, строго возрастающих, без будущего времени. |
| `PRIVATE_KEY` | Локальный EVM-отправитель. |

Все три массива равной длины. Первый round находится в начале дня или раньше максимум на 24 часа; последний — на границе конца дня либо позже. Между соседними rounds не больше 24 часов, и все rounds кроме последнего предшествуют концу дня. При дополнительном заполнении feed включайте уже существующее перекрытие: совпадающий `(id,price,timestamp)` пропускается, конфликтующая запись отклоняется до отправки.

Оператор обязан поддерживать тестовую историю и публиковать следующие rounds с запасом до предельного интервала 24 часа, чтобы задержка запуска или транзакции не оставила разрыв. Перед расчётом дня нужна также закрывающая точка на его конце либо позже. Seed-скрипт сам не планирует следующие публикации; будущие timestamps он не принимает. Поздний запуск расчёта допустим, если оператор обеспечил полную подходящую историю.

```sh
forge script --root web3 script/SeedDAOTestFeed.s.sol:SeedDAOTestFeed \
  --rpc-url "$DEPLOY_RPC_URL" --broadcast
```

Заполнение feed не финализирует TWAP. После подтверждённых rounds `calculate(day,roundIds)` рассчитывает цену по явно переданному полному списку. Для сохранения результата вызовите `append`; только после финализации checkpoint допустим `calculate(day,[])`:

```sh
cast call "$DAO_TWAP" 'calculate(uint256,uint80[])(uint256)' \
  "$DAO_TEST_DAY" "[$ROUND_IDS]" --rpc-url "$DEPLOY_RPC_URL"

cast send "$DAO_TWAP" 'append(uint256,uint80[])' \
  "$DAO_TEST_DAY" "[$ROUND_IDS]" --rpc-url "$DEPLOY_RPC_URL" --account "$DEPLOY_KEYSTORE_ACCOUNT"

cast call "$DAO_TWAP" 'calculate(uint256,uint80[])(uint256)' \
  "$DAO_TEST_DAY" '[]' --rpc-url "$DEPLOY_RPC_URL"
```

Здесь `DAO_TWAP` — адрес `ChainlinkBTCDailyTWAP`, связанного с заполненным feed. Для большой истории страницы `append` содержат максимум 512 **новых** последовательных rounds; уже добавленные в checkpoint IDs не повторяются. Первая страница доказывает начало дня и достигает round строго после его начала, последняя заканчивается первым round на границе конца дня либо после неё. Это отличается от перекрытия, разрешённого при повторном заполнении самого mock feed.

Пропущенный запуск можно выполнить позднее, если сохранены все нужные rounds. Пропущенный round, gap более 24 часов, неположительная цена или некорректное время вызывают отказ без подстановки цены или смены immutable feed.

## 7. Воспроизводимые проверки

Скриптовые тесты меняют process-global env через `vm.setEnv`; Foundry snapshot не восстанавливает эти значения. При совместном запуске обнаружено пересечение значений между test cases даже внутри одной suite с одним worker. Полный acceptance поэтому запускает env-зависимые test cases в отдельных Forge-процессах с `--threads 1`; зависимость определяется и по локальным imports. Остальные suites выполняются по одному процессу на suite. Discovery автоматически включает все исполняемые suites; имена и количество фактически прошедших тестов должны точно совпасть с discovery. Failures и skips останавливают проверку:

```sh
python -c 'from ops.local_acceptance import run_solidity_suites; run_solidity_suites("ops/artifacts/solidity-suites.json")'
python ops/local_acceptance.py
```

Подробное покрытие 24 прежних script-проверок и 12 новых runtime/reuse regression: [deployment-script-tests.md](../web3/docs/deployment-script-tests.md). Итоговые результаты общей приёмки, исправления при объединении и актуальные размеры: [проверка архива 18 октября](UPGRADE_REVIEW_2026_10_18.md). Отчёты внутри исходного архива не заменяют прогон объединённой версии.
