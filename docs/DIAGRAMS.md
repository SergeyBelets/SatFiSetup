# Схемы текущей системы

Актуализация: 12 ноября 2026. [Холодный старт](COLD_START.md) ·
[Архитектура](ARCHITECTURE.md) · [NFT](NFT_CONFIGURATION.md).

Это единый индекс текущих Mermaid-схем в документации. Источник — блоки
Mermaid в соответствующих Markdown-файлах; SVG/PNG служат экспортами этих
блоков. Старые папки диаграмм и acceptance reports сохраняют исторические
snapshots, их нельзя считать текущей моделью по одному имени файла.

В participant archive входят Markdown/Mermaid-исходники. Экспорты SVG/PNG и
отчёты в `ops/artifacts` доступны в полном developer checkout и исключены из
поставки участникам. После распаковки source archive открывайте схемы на
исходных страницах; ссылки на developer-экспорты ниже требуют полного checkout.

[Текущий manifest с source/SVG hashes и PNG paths](../ops/artifacts/industrial-test-docs-20261112/current-diagrams.json) ·
[Холодный старт: проверка полного NFT-порядка](../ops/artifacts/cold-start-nft-20261105/README.md) ·
[Базовая сверка остальных схем с кодом](../ops/artifacts/docs-consistency-20261105/README.md).
Добавлена схема выбора USD по сетям. В схемах холодного старта и конфигуратора
показаны выпуск TUSD в regtest `create`, привязка внешнего USD в `backed`
и финансирование нового конвертера в `reuse`. Схема расширения общей factory
и схемы холодного старта, конфигуратора, архитектуры и установки показывают
закрепление ID федерации и конфедерации за их EVM_AUTH до adoption.
Схема платного развёртывания отделяет adoption от списания deployment fee.
Схема восстановления withdrawal CREATE показывает две отдельные проверки
оператором и возврат к прежнему DKG. Карта холодного старта ведёт к этой ветке
при revert. Схема диагностики показывает, какие данные брать из stdout и stderr.
Холодный старт и конфигуратор показывают три независимые высоты. Схема DKG
показывает проверку board и диапазона блоков из ответа RPC, а также сверку
высоты создания с проверенным блоком. Схема Bitcoin-watch показывает полный
разбор через архивный RPC, быстрый режим через loopback или явно доверенный
HTTPS, окно хранения хэшей и сверку курсора и хэша блока с текущей вершиной
перед `WATCH_COVERED`.
Конфигуратор и холодный старт показывают проверку Infura Sepolia и
архивного HTTPS Core testnet4 до сборки пакета удалённого запуска.
Схемы FROST и NFT показывают выбор более глубокой политики подтверждений
для сохранённой транзакции. В схемах холодного старта, FROST и Sepolia/testnet4
показаны привязка verifier 3.1.0, локальные proofs и публикация выбранным
участником. Схема prover показывает preflight и изоляцию Groth16 в pinned
linux/amd64 контейнере. Схема казначейства показывает сверку выпуска,
проверку calldata до FROST, публикацию digest и повтор нулевого расчёта
до конца UTC-окна. Пересобраны шесть схем. Исходники и хеши SVG всех 75
экспортов сверены с текущей документацией.

Для порядка действий читайте sequence diagrams, для структуры и условий —
flowcharts, для допустимых переходов — state diagrams. Это схемы реализации,
а не свидетельство production qualification. SVG сохраняет подписи при
увеличении; на узком экране откройте отдельную схему и увеличьте нужную область.

Проверенные границы:

- NFT create/mint — EVM_AUTH FROST и conf custodian minimum, issuer/admin/usage у manager.
- Quote не меняет usage; approved contract-pool расходует quota владельца при request,
  fee accrual происходит при settlement. Поздний cancel не возвращает quota.
- Pool связывается с manager один раз; старый manager нельзя заменить обычным setter.
- Bitcoin mainnet и testnet4 используют разные ZK guests/state/adapters; regtest — relay.
- Plain bootstrap и полный `--system-policy` имеют разный состав address graph;
  конфигурация кандидата не означает Ready или квалификацию.
- У каждого ZK-участника собственный рабочий proof; publisher выбран явно.

## Индекс экспортов

Всего 75 схем на 37 страницах.

| Исходная страница | Схем | SVG | PNG |
| --- | --- | --- | --- |
| [ADDRESS_REQUESTS_WATCH](ADDRESS_REQUESTS_WATCH.md) | 1 | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/docs_ADDRESS_REQUESTS_WATCH_md_1.svg) | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/png/docs_ADDRESS_REQUESTS_WATCH_md_1.png) |
| [ARCHITECTURE](ARCHITECTURE.md) | 8 | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/docs_ARCHITECTURE_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_2.svg), [3](../ops/artifacts/factory-id-docs-20261105/diagrams/docs_ARCHITECTURE_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_4.svg), [5](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_5.svg), [6](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_6.svg), [7](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_7.svg), [8](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ARCHITECTURE_md_8.svg) | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/png/docs_ARCHITECTURE_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_2.png), [3](../ops/artifacts/factory-id-docs-20261105/diagrams/png/docs_ARCHITECTURE_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_4.png), [5](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_5.png), [6](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_6.png), [7](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_7.png), [8](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ARCHITECTURE_md_8.png) |
| [BITCOIN_CUSTODY_TRANSFER](BITCOIN_CUSTODY_TRANSFER.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_CUSTODY_TRANSFER_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_CUSTODY_TRANSFER_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_CUSTODY_TRANSFER_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_CUSTODY_TRANSFER_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_CUSTODY_TRANSFER_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_CUSTODY_TRANSFER_md_3.png) |
| [BITCOIN_KEY_FAMILY](BITCOIN_KEY_FAMILY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_KEY_FAMILY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_KEY_FAMILY_md_1.png) |
| [BITCOIN_MAINNET_RUNBOOK](BITCOIN_MAINNET_RUNBOOK.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_MAINNET_RUNBOOK_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_MAINNET_RUNBOOK_md_1.png) |
| [BITCOIN_MINT_AND_TREASURY](BITCOIN_MINT_AND_TREASURY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_MINT_AND_TREASURY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_MINT_AND_TREASURY_md_1.png) |
| [BITCOIN_WITHDRAWALS](BITCOIN_WITHDRAWALS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_BITCOIN_WITHDRAWALS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_BITCOIN_WITHDRAWALS_md_1.png) |
| [BOOTSTRAP](BOOTSTRAP.md) | 1 | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/docs_BOOTSTRAP_md_1.svg) | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/png/docs_BOOTSTRAP_md_1.png) |
| [COLD_START](COLD_START.md) | 3 | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/docs_COLD_START_md_1.svg), [2](../ops/artifacts/cold-start-nft-20261105/diagrams/docs_COLD_START_md_2.svg), [3](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_COLD_START_md_3.svg) | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/png/docs_COLD_START_md_1.png), [2](../ops/artifacts/cold-start-nft-20261105/diagrams/png/docs_COLD_START_md_2.png), [3](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_COLD_START_md_3.png) |
| [CROSS_PLATFORM_QUICKSTART](CROSS_PLATFORM_QUICKSTART.md) | 1 | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/docs_CROSS_PLATFORM_QUICKSTART_md_1.svg) | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/png/docs_CROSS_PLATFORM_QUICKSTART_md_1.png) |
| [DISTRIBUTION](DISTRIBUTION.md) | 3 | [1](../ops/artifacts/release-security-docs-20261112/diagrams/docs_DISTRIBUTION_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_DISTRIBUTION_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_DISTRIBUTION_md_3.svg) | [1](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_DISTRIBUTION_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_DISTRIBUTION_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_DISTRIBUTION_md_3.png) |
| [ETHEREUM_MAINNET_RUNBOOK](ETHEREUM_MAINNET_RUNBOOK.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ETHEREUM_MAINNET_RUNBOOK_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ETHEREUM_MAINNET_RUNBOOK_md_1.png) |
| [FEDERATION_CONFEDERATION_SETUP](FEDERATION_CONFEDERATION_SETUP.md) | 1 | [1](../ops/artifacts/confederation-id-diagrams-20261105/diagrams/docs_FEDERATION_CONFEDERATION_SETUP_md_1.svg) | [1](../ops/artifacts/confederation-id-diagrams-20261105/diagrams/png/docs_FEDERATION_CONFEDERATION_SETUP_md_1.png) |
| [FEE_FLOW](FEE_FLOW.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_3.png) |
| [FIRST_DKG_ONCHAIN](FIRST_DKG_ONCHAIN.md) | 1 | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/docs_FIRST_DKG_ONCHAIN_md_1.svg) | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/png/docs_FIRST_DKG_ONCHAIN_md_1.png) |
| [FROST_ZK_LIFECYCLE](FROST_ZK_LIFECYCLE.md) | 4 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FROST_ZK_LIFECYCLE_md_1.svg), [2](../ops/artifacts/release-security-docs-20261112/diagrams/docs_FROST_ZK_LIFECYCLE_md_2.svg), [3](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_FROST_ZK_LIFECYCLE_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FROST_ZK_LIFECYCLE_md_4.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_1.png), [2](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_2.png), [3](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_4.png) |
| [LAUNCH_CONFIGURATOR](LAUNCH_CONFIGURATOR.md) | 1 | [1](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/docs_LAUNCH_CONFIGURATOR_md_1.svg) | [1](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/png/docs_LAUNCH_CONFIGURATOR_md_1.png) |
| [LAUNCH_MATHEMATICS](LAUNCH_MATHEMATICS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_MATHEMATICS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_MATHEMATICS_md_1.png) |
| [LAUNCH_READINESS](LAUNCH_READINESS.md) | 3 | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_LAUNCH_READINESS_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_READINESS_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_READINESS_md_3.svg) | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_LAUNCH_READINESS_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_READINESS_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_READINESS_md_3.png) |
| [MAINNET_RELAY](MAINNET_RELAY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_MAINNET_RELAY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_MAINNET_RELAY_md_1.png) |
| [NETWORKS_AND_AUTOMATION](NETWORKS_AND_AUTOMATION.md) | 1 | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/docs_NETWORKS_AND_AUTOMATION_md_1.svg) | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/png/docs_NETWORKS_AND_AUTOMATION_md_1.png) |
| [NFT_CONFIGURATION](NFT_CONFIGURATION.md) | 2 | [1](../ops/artifacts/release-security-docs-20261112/diagrams/docs_NFT_CONFIGURATION_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_NFT_CONFIGURATION_md_2.svg) | [1](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_NFT_CONFIGURATION_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_NFT_CONFIGURATION_md_2.png) |
| [PEER_TESTING](PEER_TESTING.md) | 1 | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/docs_PEER_TESTING_md_1.svg) | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/png/docs_PEER_TESTING_md_1.png) |
| [RELAY_KEEPER_AND_GAS](RELAY_KEEPER_AND_GAS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_RELAY_KEEPER_AND_GAS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_RELAY_KEEPER_AND_GAS_md_1.png) |
| [TOKENS](TOKENS.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_3.png) |
| [ULR_BOOTSTRAP](ULR_BOOTSTRAP.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_BOOTSTRAP_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_BOOTSTRAP_md_1.png) |
| [ULR_CANDIDATES_AND_VOTING](ULR_CANDIDATES_AND_VOTING.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_CANDIDATES_AND_VOTING_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_CANDIDATES_AND_VOTING_md_1.png) |
| [ULR_STAKING](ULR_STAKING.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_STAKING_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_STAKING_md_1.png) |
| [ULR_TREASURIES](ULR_TREASURIES.md) | 1 | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_ULR_TREASURIES_md_1.svg) | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_ULR_TREASURIES_md_1.png) |
| [WITHDRAWAL_FEE_SUBSIDY](WITHDRAWAL_FEE_SUBSIDY.md) | 2 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_WITHDRAWAL_FEE_SUBSIDY_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_WITHDRAWAL_FEE_SUBSIDY_md_2.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_WITHDRAWAL_FEE_SUBSIDY_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_WITHDRAWAL_FEE_SUBSIDY_md_2.png) |
| [ZKSPV_CONTOUR](ZKSPV_CONTOUR.md) | 5 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_4.svg), [5](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_5.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_4.png), [5](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_5.png) |
| [ZKSPV_FUNCTIONAL_COVERAGE](ZKSPV_FUNCTIONAL_COVERAGE.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_FUNCTIONAL_COVERAGE_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_FUNCTIONAL_COVERAGE_md_1.png) |
| [ZKSPV_OBSERVATION_HISTORY](ZKSPV_OBSERVATION_HISTORY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_OBSERVATION_HISTORY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_OBSERVATION_HISTORY_md_1.png) |
| [ZKSPV_SCENARIO_BINDINGS](ZKSPV_SCENARIO_BINDINGS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_SCENARIO_BINDINGS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_SCENARIO_BINDINGS_md_1.png) |
| [ZK_PROVER_LINUX](ZK_PROVER_LINUX.md) | 1 | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_ZK_PROVER_LINUX_md_1.svg) | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_ZK_PROVER_LINUX_md_1.png) |
| [ZK_SPV_ARCHITECTURE](ZK_SPV_ARCHITECTURE.md) | 11 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_4.svg), [5](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_5.svg), [6](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_6.svg), [7](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_7.svg), [8](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_8.svg), [9](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_9.svg), [10](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_10.svg), [11](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_11.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_4.png), [5](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_5.png), [6](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_6.png), [7](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_7.png), [8](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_8.png), [9](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_9.png), [10](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_10.png), [11](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_11.png) |
| [ZK_TESTNET4_SEPOLIA](ZK_TESTNET4_SEPOLIA.md) | 1 | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/docs_ZK_TESTNET4_SEPOLIA_md_1.svg) | [1](../ops/artifacts/industrial-test-docs-20261112/diagrams/png/docs_ZK_TESTNET4_SEPOLIA_md_1.png) |

Перегенерация выполняется локальным `ops/render_docs_diagrams.cjs` с закреплённым
в окружении Mermaid/Chrome. Manifest содержит источник каждого изображения;
проверяются SHA256, XML и отсутствие обрезанных подписей. Визуальная сверка
экспортов дополняет проверку смысла стрелок по контрактам и CLI.
