# Схемы текущей системы

Актуализация: 21 ноября 2026. [Холодный старт](COLD_START.md) ·
[Архитектура](ARCHITECTURE.md) · [NFT](NFT_CONFIGURATION.md).

Публичный архив ZK-перехода используется отдельно от поставки исходников.
Схемы показывают подготовку без proving, расчёт с экспортом и независимую
проверку ZIP через закреплённый EVM verifier. Обычная публикация использует
собственный результат worker. [Режимы запуска](ZK_SPV_WORKER.md#worker-modes-and-transition-archives) ·
[Манифест экспорта](../ops/artifacts/zk-worker-modes-20261121/diagrams/manifest.json).

| Схема режимов ZK | SVG | PNG |
| --- | --- | --- |
| Исходная поставка и подготовка хоста по роли | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_DISTRIBUTION_md_1.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_DISTRIBUTION_md_1.png) |
| Подготовка, расчёт и публичный ZIP перехода | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_DISTRIBUTION_md_3.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_DISTRIBUTION_md_3.png) |
| Собственный cache, origin и отдельная публикация | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_DISTRIBUTION_md_4.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_DISTRIBUTION_md_4.png) |
| Архитектурный маршрут ZK и проверка ZIP | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_ARCHITECTURE_md_2.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_ARCHITECTURE_md_2.png) |

Схема продажи комиссий показывает запас из собранных wrap/unwrap-комиссий
в S или MP, заявку с внесённым стейблкоином и точный расчёт после FROST.
Оплата NFT и прямые поступления S целиком учитываются в DAO.
DAO конфедерации учитывает выручку в сутки исполнения. Казна федерации
получает остаток MP и стейблкоин от продажи.
[Продажа комиссий S и MP](FEE_FLOW.md#продажа-комиссий-s-и-mp) ·
[Манифест экспорта](../ops/artifacts/fee-sale-20261121/diagrams/manifest.json).

| Схема продажи комиссий | SVG | PNG |
| --- | --- | --- |
| Доля комиссии, финансирование заявки, расчёт и возврат | [SVG](../ops/artifacts/fee-sale-20261121/diagrams/docs_FEE_FLOW_md_4.svg) | [PNG](../ops/artifacts/fee-sale-20261121/diagrams/png/docs_FEE_FLOW_md_4.png) |

[Восстановление SDK](RECOVERY.md) включает пять схем. Они показывают гонку
`finalizeSession`, проверку Bitcoin-платежа после settlement, повтор
исполненного intent, замену ожидающей EIP-1559 транзакции и обработку отставания RPC.
[Манифест первых экспортов](../ops/artifacts/sdk-recovery-20261121/diagrams/manifest.json) ·
[Манифест схем RPC](../ops/artifacts/rpc-lag-20261121/diagrams/manifest.json).

| Схема восстановления SDK | SVG | PNG |
| --- | --- | --- |
| Гонка финализации и привязка исторического nonce | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_1.svg) | [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_1.png) |
| Проверка и восстановление Bitcoin-платежа | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_RECOVERY_md_2.svg) | [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_RECOVERY_md_2.png) |
| Повтор исполненного intent, включая ULR | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_3.svg) | [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_3.png) |
| Замена EIP-1559 и повтор отправки | [SVG](../ops/artifacts/sdk-recovery-20261121/diagrams/docs_RECOVERY_md_4.svg) | [PNG](../ops/artifacts/sdk-recovery-20261121/diagrams/png/docs_RECOVERY_md_4.png) |
| Отставание EVM и Bitcoin RPC у участников | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_RECOVERY_md_5.svg) | [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_RECOVERY_md_5.png) |

| Связанные схемы RPC | SVG | PNG |
| --- | --- | --- |
| [Bitcoin-watch, отставание Core и сверка хеша](ADDRESS_REQUESTS_WATCH.md#working-without-a-local-bitcoin-node) | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_ADDRESS_REQUESTS_WATCH_md_1.svg) | [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_ADDRESS_REQUESTS_WATCH_md_1.png) |
| [Этапы ZK-контура и ожидание receipt](ZKSPV_CONTOUR.md#plan-and-execution) | [SVG](../ops/artifacts/rpc-lag-20261121/diagrams/docs_ZKSPV_CONTOUR_md_1.svg) | [PNG](../ops/artifacts/rpc-lag-20261121/diagrams/png/docs_ZKSPV_CONTOUR_md_1.png) |

Схемы локальных проверок показывают выбор операции по политике API, проверку
полноты дневного снимка ULR и передачу cookie после проверки TLS.
[Манифест экспортов](../ops/artifacts/api-ulr-tls-20261121/diagrams/manifest.json).

| Схема API, ULR и TLS | SVG | PNG |
| --- | --- | --- |
| [Рабочий API и восстановление операции](OPERATIONAL_API.md) | [SVG](../ops/artifacts/api-ulr-tls-20261121/diagrams/docs_OPERATIONAL_API_md_1.svg) | [PNG](../ops/artifacts/api-ulr-tls-20261121/diagrams/png/docs_OPERATIONAL_API_md_1.png) |
| [Дневной снимок ULR и исторический состав казн](ULR_TREASURIES.md#границы-расчёта-и-авторизации) | [SVG](../ops/artifacts/ulr-roster-20261121/diagrams/docs_ULR_TREASURIES_md_1.svg) | [PNG](../ops/artifacts/ulr-roster-20261121/diagrams/png/docs_ULR_TREASURIES_md_1.png) |
| [TLS удалённого Bitcoin RPC](PEER_TESTING.md#как-sdk-проверяет-https-для-bitcoin-rpc) | [SVG](../ops/artifacts/api-ulr-tls-20261121/diagrams/docs_PEER_TESTING_md_2.svg) | [PNG](../ops/artifacts/api-ulr-tls-20261121/diagrams/png/docs_PEER_TESTING_md_2.png) |

[Манифест схемы состава казн ULR](../ops/artifacts/ulr-roster-20261121/diagrams/manifest.json).

Схемы холодного старта показывают проверку ETH funding и ожидание на разных
RPC, `VERIFIED` для локального relay и отдельный переход от структурного ZK
графа к Fresh. В V3 покупатель получает NFT за S по утверждённому тарифу и
котировке оператора. Владелец NFT применяет скидку в пределах своего лимита.
DAO сначала резервирует возмещение газа, затем начисляет награду владельцам
стейка за полные UTC-сутки. Схема переноса в V3 показывает проверку группы
заявок до подписи и приём точного Bitcoin-выхода после погашения MP. На схеме
погашения выплата и комиссия майнеров уменьшают один BTC-резерв.
[Перенос MP → S → BTC](BITCOIN_CUSTODY_TRANSFER.md#профиль-v3-с-прямым-btc-резервом) ·
[Учёт DAO и погашение S](CONFEDERATION_DAO_IN_KIND.md).

| Схема V3 и ZK 3.1.0 | SVG | PNG |
| --- | --- | --- |
| Перенос MP, отдельный Bitcoin-выход и расчёт BTC | [SVG](../ops/artifacts/v3-safety-20261121/diagrams/docs_BITCOIN_CUSTODY_TRANSFER_md_1.svg) | [PNG](../ops/artifacts/v3-safety-20261121/diagrams/png/docs_BITCOIN_CUSTODY_TRANSFER_md_1.png) |
| Доход DAO, продажа части S, возмещение газа и claim | [SVG](../ops/artifacts/fee-sale-20261121/diagrams/docs_CONFEDERATION_DAO_IN_KIND_md_1.svg) | [PNG](../ops/artifacts/fee-sale-20261121/diagrams/png/docs_CONFEDERATION_DAO_IN_KIND_md_1.png) |
| V3 NFT: FROST-тариф, котировка оператора и квота владельца | [SVG](../ops/artifacts/cold-start-local-20261121/diagrams/docs_CONFEDERATION_DAO_IN_KIND_md_3.svg) | [PNG](../ops/artifacts/cold-start-local-20261121/diagrams/png/docs_CONFEDERATION_DAO_IN_KIND_md_3.png) |
| Погашение S и перерасчёт комиссии при RBF | [SVG](../ops/artifacts/mp-s-btc-20261121/diagrams/docs_CONFEDERATION_DAO_IN_KIND_md_2.svg) | [PNG](../ops/artifacts/mp-s-btc-20261121/diagrams/png/docs_CONFEDERATION_DAO_IN_KIND_md_2.png) |
| Маршрут проверки V3 на Sepolia и testnet4 | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_ZK_TESTNET4_SEPOLIA_md_1.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_ZK_TESTNET4_SEPOLIA_md_1.png) |
| Локальные инструменты ZK и проверка prover | [SVG](../ops/artifacts/mp-s-btc-20261121/diagrams/docs_ZK_PROVER_LINUX_md_1.svg) | [PNG](../ops/artifacts/mp-s-btc-20261121/diagrams/png/docs_ZK_PROVER_LINUX_md_1.png) |
| Подготовка нового стенда и режимы ZK | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_LAUNCH_READINESS_md_1.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_LAUNCH_READINESS_md_1.png) |
| FROST, независимая проверка ZIP и публикация | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_FROST_ZK_LIFECYCLE_md_3.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_3.png) |
| Bitcoin-платёж, ZK и подтверждение включения | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_FROST_ZK_LIFECYCLE_md_4.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_4.png) |
| Холодный старт: ETH funding, ULR и источники выплат | [SVG](../ops/artifacts/cold-start-local-20261121/diagrams/docs_COLD_START_md_1.svg) | [PNG](../ops/artifacts/cold-start-local-20261121/diagrams/png/docs_COLD_START_md_1.png) |
| Выпуск NFT прежнего профиля: отдельные create и mint | [SVG](../ops/artifacts/cold-start-local-20261121/diagrams/docs_COLD_START_md_2.svg) | [PNG](../ops/artifacts/cold-start-local-20261121/diagrams/png/docs_COLD_START_md_2.png) |
| Холодный старт V3, relay и ZK | [SVG](../ops/artifacts/zk-worker-modes-20261121/diagrams/docs_COLD_START_md_3.svg) | [PNG](../ops/artifacts/zk-worker-modes-20261121/diagrams/png/docs_COLD_START_md_3.png) |

Манифесты экспорта содержат хеши исходников и SVG.
[Холодный старт, NFT и claim](../ops/artifacts/cold-start-local-20261121/diagrams/manifest.json) ·
[Экспорт проверок безопасности V3](../ops/artifacts/v3-safety-20261121/diagrams/manifest.json) ·
[Предыдущий экспорт V3 и ZK](../ops/artifacts/mp-s-btc-20261121/diagrams/manifest.json).

Источники текущих схем — блоки
Mermaid в соответствующих Markdown-файлах; SVG/PNG служат экспортами этих
блоков. Старые папки диаграмм и acceptance reports сохраняют исторические
snapshots, их нельзя считать текущей моделью по одному имени файла.

В participant archive входят Markdown/Mermaid-исходники. Экспорты SVG/PNG и
отчёты в `ops/artifacts` доступны в полном developer checkout и исключены из
поставки участникам. После распаковки source archive открывайте схемы на
исходных страницах; ссылки на developer-экспорты ниже требуют полного checkout.

Экспорты ниже зафиксировали документацию на 12 ноября 2026 года. Индексы и хеши
схем относятся к этой версии. [Архивный manifest](../ops/artifacts/security-remediation-20261112/current-diagrams.json) ·
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
для сохранённой транзакции. Схема Linux/WSL2 отделяет CUDA-этапы от
Groth16 с закреплённым CPU R0VM и брокером вне sandbox. Схема казначейства
показывает повтор расчёта после пополнения в пределах UTC-окна.
Все экспорты сверены по SHA256.

Для порядка действий читайте sequence diagrams, для структуры и условий —
flowcharts, для допустимых переходов — state diagrams. Это схемы реализации,
а не свидетельство production qualification. SVG сохраняет подписи при
увеличении; на узком экране откройте отдельную схему и увеличьте нужную область.

Проверенные границы:

- В прежнем профиле NFT create/mint требуют EVM_AUTH FROST и conf custodian minimum. Роли issuer/admin/usage у manager.
- В V3 FROST и кастодианы утверждают тариф. Покупка по котировке оператора зачисляет S в DAO и выдаёт NFT в одной транзакции.
- Quote не меняет usage; approved contract-pool расходует quota владельца при request,
  fee accrual происходит при settlement. Поздний cancel не возвращает quota.
- Pool связывается с manager один раз; старый manager нельзя заменить обычным setter.
- Bitcoin mainnet и testnet4 используют разные ZK guests/state/adapters; regtest — relay.
- Plain bootstrap и полный `--system-policy` имеют разный состав address graph;
  конфигурация кандидата не означает Ready или квалификацию.
- У каждого ZK-участника собственный рабочий proof; publisher выбран явно.

## Индекс экспортов от 12 ноября 2026 года

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
| [COLD_START](COLD_START.md) | 3 | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/docs_COLD_START_md_1.svg), [2](../ops/artifacts/cold-start-nft-20261105/diagrams/docs_COLD_START_md_2.svg), [3](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/docs_COLD_START_md_3.svg) | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/png/docs_COLD_START_md_1.png), [2](../ops/artifacts/cold-start-nft-20261105/diagrams/png/docs_COLD_START_md_2.png), [3](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/png/docs_COLD_START_md_3.png) |
| [CROSS_PLATFORM_QUICKSTART](CROSS_PLATFORM_QUICKSTART.md) | 1 | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/docs_CROSS_PLATFORM_QUICKSTART_md_1.svg) | [1](../ops/artifacts/factory-id-docs-20261105/diagrams/png/docs_CROSS_PLATFORM_QUICKSTART_md_1.png) |
| [DISTRIBUTION](DISTRIBUTION.md) | 3 | [1](../ops/artifacts/release-security-docs-20261112/diagrams/docs_DISTRIBUTION_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_DISTRIBUTION_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_DISTRIBUTION_md_3.svg) | [1](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_DISTRIBUTION_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_DISTRIBUTION_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_DISTRIBUTION_md_3.png) |
| [ETHEREUM_MAINNET_RUNBOOK](ETHEREUM_MAINNET_RUNBOOK.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ETHEREUM_MAINNET_RUNBOOK_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ETHEREUM_MAINNET_RUNBOOK_md_1.png) |
| [FEDERATION_CONFEDERATION_SETUP](FEDERATION_CONFEDERATION_SETUP.md) | 1 | [1](../ops/artifacts/confederation-id-diagrams-20261105/diagrams/docs_FEDERATION_CONFEDERATION_SETUP_md_1.svg) | [1](../ops/artifacts/confederation-id-diagrams-20261105/diagrams/png/docs_FEDERATION_CONFEDERATION_SETUP_md_1.png) |
| [FEE_FLOW](FEE_FLOW.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FEE_FLOW_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FEE_FLOW_md_3.png) |
| [FIRST_DKG_ONCHAIN](FIRST_DKG_ONCHAIN.md) | 1 | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/docs_FIRST_DKG_ONCHAIN_md_1.svg) | [1](../ops/artifacts/watch-security-docs-20261111/diagrams/png/docs_FIRST_DKG_ONCHAIN_md_1.png) |
| [FROST_ZK_LIFECYCLE](FROST_ZK_LIFECYCLE.md) | 4 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FROST_ZK_LIFECYCLE_md_1.svg), [2](../ops/artifacts/release-security-docs-20261112/diagrams/docs_FROST_ZK_LIFECYCLE_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FROST_ZK_LIFECYCLE_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_FROST_ZK_LIFECYCLE_md_4.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_1.png), [2](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_FROST_ZK_LIFECYCLE_md_4.png) |
| [LAUNCH_CONFIGURATOR](LAUNCH_CONFIGURATOR.md) | 1 | [1](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/docs_LAUNCH_CONFIGURATOR_md_1.svg) | [1](../ops/artifacts/rpc-preflight-docs-20261112/diagrams/png/docs_LAUNCH_CONFIGURATOR_md_1.png) |
| [LAUNCH_MATHEMATICS](LAUNCH_MATHEMATICS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_MATHEMATICS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_MATHEMATICS_md_1.png) |
| [LAUNCH_READINESS](LAUNCH_READINESS.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_READINESS_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_READINESS_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_LAUNCH_READINESS_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_READINESS_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_READINESS_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_LAUNCH_READINESS_md_3.png) |
| [MAINNET_RELAY](MAINNET_RELAY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_MAINNET_RELAY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_MAINNET_RELAY_md_1.png) |
| [NETWORKS_AND_AUTOMATION](NETWORKS_AND_AUTOMATION.md) | 1 | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/docs_NETWORKS_AND_AUTOMATION_md_1.svg) | [1](../ops/artifacts/mock-usd-diagrams-20261105/diagrams/png/docs_NETWORKS_AND_AUTOMATION_md_1.png) |
| [NFT_CONFIGURATION](NFT_CONFIGURATION.md) | 2 | [1](../ops/artifacts/release-security-docs-20261112/diagrams/docs_NFT_CONFIGURATION_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_NFT_CONFIGURATION_md_2.svg) | [1](../ops/artifacts/release-security-docs-20261112/diagrams/png/docs_NFT_CONFIGURATION_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_NFT_CONFIGURATION_md_2.png) |
| [PEER_TESTING](PEER_TESTING.md) | 1 | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/docs_PEER_TESTING_md_1.svg) | [1](../ops/artifacts/bootstrap-recovery-docs-20261111/diagrams/png/docs_PEER_TESTING_md_1.png) |
| [RELAY_KEEPER_AND_GAS](RELAY_KEEPER_AND_GAS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_RELAY_KEEPER_AND_GAS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_RELAY_KEEPER_AND_GAS_md_1.png) |
| [TOKENS](TOKENS.md) | 3 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_TOKENS_md_3.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_TOKENS_md_3.png) |
| [ULR_BOOTSTRAP](ULR_BOOTSTRAP.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_BOOTSTRAP_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_BOOTSTRAP_md_1.png) |
| [ULR_CANDIDATES_AND_VOTING](ULR_CANDIDATES_AND_VOTING.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_CANDIDATES_AND_VOTING_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_CANDIDATES_AND_VOTING_md_1.png) |
| [ULR_STAKING](ULR_STAKING.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ULR_STAKING_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ULR_STAKING_md_1.png) |
| [ULR_TREASURIES](ULR_TREASURIES.md) | 1 | [1](../ops/artifacts/security-remediation-20261112/diagrams/docs_ULR_TREASURIES_md_1.svg) | [1](../ops/artifacts/security-remediation-20261112/diagrams/png/docs_ULR_TREASURIES_md_1.png) |
| [WITHDRAWAL_FEE_SUBSIDY](WITHDRAWAL_FEE_SUBSIDY.md) | 2 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_WITHDRAWAL_FEE_SUBSIDY_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_WITHDRAWAL_FEE_SUBSIDY_md_2.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_WITHDRAWAL_FEE_SUBSIDY_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_WITHDRAWAL_FEE_SUBSIDY_md_2.png) |
| [ZKSPV_CONTOUR](ZKSPV_CONTOUR.md) | 5 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_4.svg), [5](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_CONTOUR_md_5.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_4.png), [5](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_CONTOUR_md_5.png) |
| [ZKSPV_FUNCTIONAL_COVERAGE](ZKSPV_FUNCTIONAL_COVERAGE.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_FUNCTIONAL_COVERAGE_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_FUNCTIONAL_COVERAGE_md_1.png) |
| [ZKSPV_OBSERVATION_HISTORY](ZKSPV_OBSERVATION_HISTORY.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_OBSERVATION_HISTORY_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_OBSERVATION_HISTORY_md_1.png) |
| [ZKSPV_SCENARIO_BINDINGS](ZKSPV_SCENARIO_BINDINGS.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZKSPV_SCENARIO_BINDINGS_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZKSPV_SCENARIO_BINDINGS_md_1.png) |
| [ZK_PROVER_LINUX](ZK_PROVER_LINUX.md) | 1 | [1](../ops/artifacts/security-remediation-20261112/diagrams/docs_ZK_PROVER_LINUX_md_1.svg) | [1](../ops/artifacts/security-remediation-20261112/diagrams/png/docs_ZK_PROVER_LINUX_md_1.png) |
| [ZK_SPV_ARCHITECTURE](ZK_SPV_ARCHITECTURE.md) | 11 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_1.svg), [2](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_2.svg), [3](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_3.svg), [4](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_4.svg), [5](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_5.svg), [6](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_6.svg), [7](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_7.svg), [8](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_8.svg), [9](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_9.svg), [10](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_10.svg), [11](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_SPV_ARCHITECTURE_md_11.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_1.png), [2](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_2.png), [3](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_3.png), [4](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_4.png), [5](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_5.png), [6](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_6.png), [7](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_7.png), [8](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_8.png), [9](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_9.png), [10](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_10.png), [11](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_SPV_ARCHITECTURE_md_11.png) |
| [ZK_TESTNET4_SEPOLIA](ZK_TESTNET4_SEPOLIA.md) | 1 | [1](../ops/artifacts/docs-consistency-20261105/diagrams/docs_ZK_TESTNET4_SEPOLIA_md_1.svg) | [1](../ops/artifacts/docs-consistency-20261105/diagrams/png/docs_ZK_TESTNET4_SEPOLIA_md_1.png) |

Перегенерация выполняется локальным `ops/render_docs_diagrams.cjs` с закреплённым
в окружении Mermaid/Chrome. Manifest содержит источник каждого изображения;
проверяются SHA256, XML и отсутствие обрезанных подписей. Визуальная сверка
экспортов дополняет проверку смысла стрелок по контрактам и CLI.
