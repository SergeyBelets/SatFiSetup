# Проверка режимов системы

`python -m ops.system_modes` выполняет проверки на изолированных локальных узлах.
Он не публикует контракты в публичных сетях, не использует рабочие ключи и не
изменяет production qualification. Запускайте из корня репозитория Python из
окружения с установленным `signer-agent[test,evm-test]`.

```sh
python ops/bootstrap_host.py build
python -m pip install --no-build-isolation -e './signer-agent[test,evm-test]'
python -m ops.system_modes preflight --mode all
python -m ops.system_modes run --mode local --output /absolute/new-test-run
```

Первый шаг ставит зависимости по `requirements.lock`. Второй добавляет
зависимости локального EVM-стенда в то же тестовое окружение.

`local` включает пять локальных developer-слоёв, включая исторические fixtures.
Они не входят в [поставку участникам](DISTRIBUTION.md); для неё используйте
portable `signer`/`relay` и собственный fresh prover для рабочих ZK-переходов.
`all` дополнительно проверяет условия
для нового ZK proving. Если что-либо отсутствует, итог будет `INCOMPLETE`, а
остальные доступные слои выполнятся. `--output` должен указывать на **новый**
каталог: прежние отчёты и журналы не перезаписываются. Не меняйте исходники во
время запуска: изменение считается ошибкой, даже если отдельные тесты прошли.

На Linux/Windows-WSL2 начните с `--mode relay`: portable `signer`, Solidity,
EVM и Core. `--mode signer` отдельно проверяет SDK без локального ZK host/prover.
Для ZK на x86-64 выполните [установку prover](ZK_PROVER_LINUX.md): platform gate
теперь допускает Linux/WSL2 x86-64, actual worker проверяет tools и изоляцию.
`zk-cached` использует developer fixtures вне participant package; свой рабочий
cache проверяйте повтором `zk_worker prove` до публикации.
Сборку новой машины и настройки других участников описывает
[полная инструкция](CROSS_PLATFORM_QUICKSTART.md). Установленный Foundry solc
также ищется в `$SVM_HOME/0.8.39/solc-0.8.39` (по умолчанию `~/.svm`).

[Начало тестов для коллег](PEER_TESTING.md) выделяет обязательный короткий
`signer` и последующий полный `relay`. Чистая Ubuntu 24.04 x86-64 сборка и
487 signer tests прошли в Linux-контейнере под CPU-эмуляцией; физический WSL2
и весь многочасовой relay lifecycle остаются отдельными прогонами.

| `--mode` | Что проверяется | Ограничение результата |
| --- | --- | --- |
| `python` | SDK, проверки подписантов, журналы, 30 комбинаций полного create/reuse-профиля | Сетевые границы в части тестов моделируются |
| `contracts` | Все обнаруженные Solidity-тесты; отдельные процессы для тестов с переменными окружения | Положительные сценарии квалифицированного derivation используют явные test fixtures |
| `evm` | Полный relay/regtest bootstrap, три независимых DKG, настоящее FROST-подписание, общий ULR/staking, второй независимый граф, approve/stake/unstake и повторы CLI | Без Bitcoin-платежей и без активации неквалифицированного verifier |
| `zk-testnet4` | Testnet4 native worker, bootstrap create/reuse и отказ real verifier на локальном Sepolia chain ID | Исторические Core-наблюдения; без ready receipts, fresh proof и public deployment |
| `zk-cached` | Developer-only. Отказ историческому mainnet receipt, граф EVM consumers и проверка происхождения proof | Для положительного прогона задайте `SATOHSI_ZK_ACCEPTANCE_RECEIPT_DIR` с локально рассчитанным 3.1.0 proof на соответствующем тестовом входе. Bitcoin-наблюдения заданы fixtures |
| `bitcoin` | Реальные локальные Core 30.3, FROST и EVM: существующий lifecycle-набор, mint, MP/S/BTC, вывод, комиссии, отмены, восстановление и сетевые профили | Bitcoin работает в regtest; mainnet/testnet4 в матрице проверяются на локальной EVM с явно заданными границами |
| `t02-relay` | Свежий bootstrap, восстановление CLTV, возврат S, ротация и подтверждение BTC-вывода с кошелька-преемника | Только локальные Anvil и Bitcoin Core regtest |
| `zk-fresh` | Preflight привязанного оператором `MAINNET_CANDIDATE` contour | Сам runner не запускает денежные действия и новое proving; используется отдельный durable contour |

Solidity проверяет UTC, claim, кандидатов, делегирование, казначейства,
снимки до 4096 адресов, минимумы 294/330, резервирование, bind и ограничения
комиссий. SDK повторяет проверки происхождения контрактов и точных подписываемых
intent. Положительный тест с квалифицированным fixture не квалифицирует обычный
`BitcoinDepositDerivation` или fee derivation.

Для gas/SPV E2E слой `bitcoin` сам задаёт RPC своего временного Anvil и копирует
собранные `SPVServicePayment.t.sol` / `GasCallerFixture.sol` artifacts в отдельный
`gas-fixtures` внутри run-каталога. Production `out` не изменяется. Отсутствие
этих artifacts блокирует запуск; пустые переменные окружения больше не приводят
к пропуску сценариев. Два gas-теста используют synthetic authority/state,
третий — настоящий конфедерационный DKG/FROST и synthetic SPV state. Они
проверяют оплату, claims и управление gas treasury, но не новое SPV-доказательство.

Поддерживаемая матрица развёртывания: EVM `1`, `11155111`, `31337`; для relay —
Bitcoin `mainnet`, `testnet4`, `regtest`; для ZK — Bitcoin `mainnet` и `testnet4`
с отдельными pinned guests. Это 15 пар backend/сетей, каждая в `create` и `reuse`. Проверка
профилей не является подтверждением развёртывания или операций в публичных сетях.

## Полное восстановление Т02 через relay

Запустите из корня репозитория после сборки SDK и контрактов. Нужны Anvil,
Bitcoin Core 30.3, FROST и 128 МиБ свободного места в каталоге результатов.
Runner создаёт изолированные Anvil и Core с `txindex=1` на loopback и запускает
полный сценарий Т‑02. Для этого прогона достаточно обычной сборки контрактов.

```sh
python -m ops.system_modes preflight --mode t02-relay
python -m ops.system_modes run --mode t02-relay --output /absolute/new-t02-relay-run
```

Время работы pytest по умолчанию ограничено 1 800 секундами (30 минут).
Параметр `--timeout-seconds` позволяет увеличить его для медленной машины.
Сроки FROST-сессий, CLTV и отдельных операций определяются сценарием и протоколом.
Например, `--timeout-seconds 2700` отведёт прогону 45 минут.

Сводный статус и таймаут записываются в `report.json`. В подкаталоге `t02-relay`
хранятся `command.log`, журналы узлов и `pytest.xml` завершённого теста. Для
успешного вывода в JUnit записываются Bitcoin txid, число подтверждений, сумма
выплаты, комиссия майнерам и остаток обеспечения. При
истечении лимита в отчёте фиксируется `COMMAND_TIMED_OUT`, а журналы остаются
в каталоге прогона. Bitcoin Core и Anvil останавливаются при выходе runner.

## Инструменты и ресурсы

Runner находит инструменты через PATH, перечисленные переменные или существующие
репозиторные пути `ops/.local-tools/acceptance-20261102`, `frost-crypto/target/debug`
и `ops/.local-tools/zk-worker`.

```sh
export SATOHSI_BITCOIND=/absolute/bitcoin-30.3/bin/bitcoind
export SATOHSI_ANVIL=/absolute/anvil
export SATOHSI_FORGE=/absolute/forge
export SATOHSI_SOLC=/absolute/solc-0.8.39
export SATOHSI_CRYPTO_BIN=/absolute/satohsi-frost-crypto
export SATOHSI_ZK_WORKER_HOST=/absolute/satoshifi-zk-worker-host
export SATOHSI_ZK_TESTNET4_WORKER_HOST=/absolute/satoshifi-testnet4-zk-worker-host
python -m ops.system_modes preflight --mode bitcoin
python -m ops.system_modes run --mode evm --output /absolute/new-evm-run
```

Core должен быть версии 30.3; [официальные архивы и SHA256SUMS](https://bitcoincore.org/bin/bitcoin-core-30.3/).
Нужны собранные production artifacts в `web3/contracts/out`, а для ZK —
также локально собранные state-adapter artifacts. Исторический receipt нужен
только developer-слою `zk-cached`; он не передаётся участникам. Применяйте инструкции
сборки из [bootstrap](BOOTSTRAP.md) и [ZK worker](ZK_SPV_WORKER.md).
Runner ничего не скачивает и не перестраивает native FROST/prover автоматически.
Solidity собирается во временный отдельный `out/cache/build-info`, чтобы не
изменять байткод, проверяемый SDK. Foundry scripts в E2E также используют отдельную
копию проекта и искусственные локальные ключи.

Ресурсные минимумы runner: Python 256 MiB, EVM/cached ZK 512 MiB, Core regtest
1 GiB, Solidity 2 GiB. Для свежего proof нижний порог 8 GiB соответствует
минимальному допустимому `minimum_free_bytes` worker; фактические требования
prover могут быть существенно выше. Успешный preflight не гарантирует достаточную
RAM, длительность или вместимость конкретного proof. Локальный regtest не
синхронизирует mainnet и не подключает пользовательский Bitcoin datadir.

## Отдельный локальный прогон V3

Соберите Solidity-артефакты и native FROST до запуска. Установите
`frost-signer` в venv из той же копии репозитория, из которой запускаете
pytest. Для набора V3 используйте отдельные Anvil с chain ID `31337` и
Core 30.3 regtest с `txindex` и отдельным datadir. Укажите адреса этих узлов
и путь к cookie.

```sh
export SATOHSI_TEST_RPC=http://127.0.0.1:18545
export SATOHSI_BTC_RPC=http://127.0.0.1:18443
export SATOHSI_BTC_COOKIE=/absolute/private/regtest/.cookie
python -m pytest -q tests/e2e/test_in_kind_bootstrap.py
python -m pytest -q tests/e2e/test_custody_transfer.py -k native-s-pass
```

Набор bootstrap проверяет `create/reuse` с разными участниками и расчёт
дневного дохода. `test_stakers_claim_net_confirmed_income` подтверждает
возмещение газа по receipt, перенос дохода на следующие UTC-сутки, claim
двух стейкеров в пропорции 1:3 и повтор команды без второй выплаты.
Сценарий `native-s-pass` проверяет погашение MP, приём BTC-резерва, покупку
NFT за обеспеченные S и вывод со скидкой. Дождитесь окончания теста перед
пересборкой артефактов, чтобы bootstrap мог завершиться по тем же хешам.
[Локальные результаты и границы проверки](COLD_START.md#10-схема-холодного-старта-и-границы-проверки).

`zk-testnet4` проверяет native witness и verifier 3.1.0 на локальном Anvil.
Опциональный `SATOHSI_ZK_ACCEPTANCE_RECEIPT_DIR` для `zk-cached` содержит
`state.journal` и `groth16/verified/seal.bin` для фиксированного локального
mainnet-сценария на Anvil `31337`. Раннер сверяет адрес state из journal
и сам proof. Этот сценарий использует уже вычисленный на своём хосте proof.

## Новый proof и публичные сети

Сначала заполните приватные worker/service/owner bindings и соберите план по
[инструкции полного contour](ZKSPV_CONTOUR.md). Исходный шаблон с deferred jobs
не считается готовым планом. Сохраняйте исходные identity, FROST-state и журналы
для каждой EOA; новое имя запуска не разрешает сбрасывать nonce или spent state.

```sh
python -m ops.system_modes preflight --mode zk-fresh --suite-plan /absolute/private/suite-plan.json
python -m ops.zkspv.contour_suite inspect --config /absolute/private/suite-plan.json
python -m ops.zkspv.contour_suite step --config /absolute/private/suite-plan.json
python -m ops.zkspv.contour_suite collect --config /absolute/private/suite-plan.json
```

`step` продвигает только следующий разрешённый шаг. Продолжайте по состоянию
durable contour и его бюджету. Локальные тесты, cached receipt и preflight не
заменяют новые Core observations, proof, signing, settlement и reconciliation.
Для публичного Sepolia/testnet4 нужны свои RPC, gas, test BTC, заранее выбранный
USD-токен, его runtime pin и финансирование конвертера. Текущий ZK guest нельзя
переключить на testnet4 параметром сети.

## Отчёт

В `report.json` сохраняются слой, его scope, SHA256 исходников и инструментов,
реальное число тестов, ошибки и блокирующие условия. Каждый слой содержит лог и
JUnit либо отчёт Solidity. Пустой набор, failure, error и skip считаются ошибкой.
`qualified` всегда `false`: квалификация и разрешение production-активации
требуют отдельного проверенного evidence bundle. Для ежедневной ULR-работы
используйте [снимок и выплату](ULR_TREASURIES.md); owner-команды доступны ниже.

```sh
frost-signer ulr-treasury wallet --rpc "$EVM_RPC" --config "$ULR_PIN" --owner "$OWNER"
frost-signer ulr-treasury approve --rpc "$EVM_RPC" --config "$ULR_PIN" --amount 200000000000000000000000 --identity-file "$IDENTITY" --state "$OWNER_STATE" --id approve-ulr-1 --send
frost-signer ulr-treasury stake --rpc "$EVM_RPC" --config "$ULR_PIN" --amount 200000000000000000000000 --identity-file "$IDENTITY" --state "$OWNER_STATE" --id stake-ulr-1 --send
frost-signer ulr-treasury unstake --rpc "$EVM_RPC" --config "$ULR_PIN" --amount 200000000000000000000000 --identity-file "$IDENTITY" --state "$OWNER_STATE" --id unstake-ulr-1 --send
```

Суммы указаны в base units (ULR: 18 decimals). Без `--send` выполняется
симуляция; для новой операции нужен новый `--id`, для точного повтора — прежний.
Approve допускает ноль для отзыва allowance. Остальные owner-команды и claim
не требуют работоспособности управления конфедерации, чтобы вернуть свой stake.

## Проверка custody от 4 ноября 2026

Исторический срез 4 ноября: **912 Solidity и 3372 SDK/ops-теста** без ошибок
и пропусков. [Результаты](../ops/artifacts/custody-docs-tests-20261104/README.md)
отделены от [предыдущих сквозных прогонов](../ops/artifacts/custody-completeness-20261104/README.md).
Протокольные исходники в этих двух срезах одинаковы; обновлены инструкции и тесты.
Исторические числа ниже не являются результатами текущего checkout.

| Регрессия | Где проверяется |
| --- | --- |
| Только оператор может первоначально закрепить coordinator; нужен уже связанный DKG registry | `web3/test/DKGOnchainBoard.t.sol` |
| Нулевой адрес, EOA и coordinator другого DKG отвергаются; после отказа корректная привязка и точный повтор работают | `web3/test/DKGOnchainBoard.t.sol` |
| Оператор, другой кастодиан и посторонний не занимают чужую сессию, chunks или будущий ID | `web3/test/CustodianMonetaryEpoch.t.sol` с настоящим FROSTCoordinator; verifier здесь unit fixture |
| Pin coordinator предшествует DKG, signing sessions и делегированию публикации во всех create/reuse backend/network profiles | `signer-agent/tests/test_bootstrap_custody.py` |
| Казна, gas, registration/status и BTC/withdrawal fee принимают разрешённого кастодиана и отвергают постороннего до отправки/подписи | `test_bitcoin_funding.py`, `test_gas_governance.py`, `test_address_registration.py`, `test_incident_status.py`, `test_btc_fee_reserve.py`, `test_withdrawal_fee.py` в `signer-agent/tests` |
| Представитель и чужой threshold не попадают в отдельный адресный Bitcoin graph | `signer-agent/tests/test_address_mint_trust.py` |

После обычной сборки хоста полный SDK/ops и контракты проверяются из корня:

```sh
python -m pytest signer-agent/tests tests/ops -q
python -m ops.system_modes run --mode contracts --output /absolute/new-contracts-run
python ops/generate_abis.py --check --zk
python ops/generate_fee_runtime_hashes.py --check
```

SDK/ops выше — полный developer-набор с локальными fixtures. Для поставки
без готовых proofs на новом хосте сначала используйте portable `signer`,
как описано в [peer testing](PEER_TESTING.md). Полный набор и короткий subset
имеют разное число тестов. Полный Solidity runner изолирует тесты, использующие
process environment, и создаёт отдельные out/cache/build-info. Обычный
параллельный `forge test` не является заменой этой изоляции: deployment-script
fixtures меняют переменные окружения процесса и могут влиять друг на друга.
Для одного board-набора допустим целевой запуск:

```sh
forge test --root web3 --match-contract '^DKGOnchainBoardTest$' --threads 1
```

Прямой `forge test` не запускают параллельно с bootstrap:
production artifacts и transaction journals должны оставаться неизменными.

В предыдущем настоящем Core/FROST прогоне `tests/e2e/test_custody_transfer.py`
перенёс 1 000 000 сатоши, затем без исходных участников и представителя
назначения вывел 996 003 сатоши и выплатил 400 сатоши из казны, сохранив backing.
`tests/e2e/test_system_bootstrap.py` подтвердил полный create/reuse с другим
составом, настраиваемыми именами, общей эмиссией 10 млн и pause/confirmation
рассылки. Для повтора используйте описанные выше `evm` и `bitcoin` режимы:
каждый сам создаёт локальные узлы и требует новый output-каталог. Отдельный
успешный E2E не означает, что весь многочасовой `bitcoin`-слой был повторён.

Свежий рабочий ZK proof нового переноса, физические Linux/WSL2 для текущего
revision и production qualification адресного/fee режима ещё не подтверждены.
`QUALIFIED`, `FEE_QUALIFIED`, `ready` и `production_ready` не повышались.
Текущая DKG-эпоха фиксирована: кандидатство и делегирование не меняют доли
Bitcoin-ключа. Для смены его состава нужна отдельная DKG и перенос средств;
автоматическая миграция всех действующих cBTC-позиций не реализована.

## Сохранённая локальная проверка

Эти отчёты предшествуют требованию собственного расчёта каждым участником.
Их прежние результаты сохраняются; они не подтверждают новый порядок публикации
и не являются комплектом для распространения.

[Отчёт от 3 ноября 2026](../ops/artifacts/portable-system-v1/validation.json)
содержит результаты на macOS arm64: 3014 SDK-тестов, 888 Solidity-тестов,
полный create/reuse bootstrap с двумя непересекающимися составами участников
и два E2E со сохранённым настоящим Groth16 receipt. Native FROST: 24 теста прошли;
отдельный генератор публичных qualification vectors помечен ignored.
Все 133 production-контракта укладываются в ограничения размера; минимальный
запас runtime — 66 байт.

После исправления автоматической подготовки fixtures отдельно прошли все три
gas/SPV E2E без пропусков. Другие Bitcoin/EVM E2E выполнялись частями, включая
настоящие Core regtest платежи и четыре полных Ethereum/Sepolia ×
mainnet/testnet4 bootstrap. Полного чистого прогона всего 53-case слоя `bitcoin`
после последних изменений harness пока нет; исходные незавершённые отчёты
сохранены вместе со списками завершённых именованных сценариев.

Отчёты сохраняют исходные статусы и SHA256. Родительский Solidity-прогон отметил
изменения SDK во время выполнения; отдельно проверено, что Solidity, его тесты,
параметры компиляции, harness и forge/solc оставались неизменными. Успех этого
слоя не переименовывает весь родительский прогон в успешный. Аналогично,
прерванный дублирующий E2E-прогон подтверждает только перечисленные завершённые
сценарии, а не весь запрошенный набор.

Linux/macOS CI подготовлен, но не запускался в этой сессии; отдельного запуска
на Windows/WSL2 также не было. Новый proof и публичный monetary contour не
выполнялись. Эти локальные результаты не изменяют `qualified=false`.
