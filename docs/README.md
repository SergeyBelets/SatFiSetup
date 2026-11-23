# Documentation Index

В холодном старте и конфигураторе описаны источники USD для `create` и `reuse`.
Отдельная схема показывает, где bootstrap выпускает TUSD и где оператор
финансирует конвертер и APY переводом. Актуальные Mermaid, SVG и PNG перечислены в индексе.
[Холодный старт](COLD_START.md#2-что-подготовить-и-кому) ·
[Подготовка TUSD](NETWORKS_AND_AUTOMATION.md#тестовый-usd-какие-режимы-и-как-настроить) ·
[Индекс схем](DIAGRAMS.md).

[Права в контрактах](CONTRACT_PERMISSIONS.md) описывают проверенные ограничения
в графе ULR, NFT и Bitcoin и полномочия, остающиеся у оператора после запуска.

## Текущий аудит безопасности и экономики

| Язык | Отчёт |
| --- | --- |
| Русский | [Markdown](audit/SatoshiFi_security_audit_2026-11-23.md) · [PDF SmartState](../output/pdf/SatoshiFi_security_audit_2026-11-23.pdf) |
| English | [Markdown](audit/SatoshiFi_security_audit_2026-11-23_EN.md) · [SmartState PDF](../output/pdf/SatoshiFi_security_audit_2026-11-23_EN.pdf) |

PDF и экспорты схем публикуются в полном Git-репозитории. Для тестового запуска коллеги используют [исходную поставку](DISTRIBUTION.md).

## Руководства и предыдущие проверки

[Проверка границ подписи, proving и provenance от 12 ноября](SECURITY_LOGIC_REVIEW_2026_11_03.md#повторная-проверка-12-ноября-2026)
описывает усиление confirmations без смены журнала, ограничения CUDA и
оставшиеся LA-01/02/03. [Удалённый холодный старт](LAUNCH_CONFIGURATOR.md#sepolia-и-bitcoin-testnet4-без-локальных-нод)
проверяет возможности Infura Sepolia и Bitcoin testnet4 до enrollment.

Предыдущий [отчёт согласованности](../ops/artifacts/docs-consistency-20261105/README.md)
отделяет проверку документации от исполнения протокола. Предыдущая контрактная
[NFT-приёмка](../ops/artifacts/nft-governance-20261105/README.md): 938 Solidity,
3465 SDK/ops и локальный native DKG/FROST create/mint E2E.

В [холодном старте](COLD_START.md#nft-конфедерации-после-bootstrap-полный-порядок)
теперь есть полный post-bootstrap NFT-порядок: адреса из manifest, свой pin,
create/mint, FROST и custodian approvals, owner/metadata/quote. Команды
bootstrap для публичного Bitcoin и regtest разделены через `BTC_ARGS`.

[Параметры федерации/конфедерации и новые группы](FEDERATION_CONFEDERATION_SETUP.md) ·
[NFT скидок: параметры, роли и требования](NFT_CONFIGURATION.md).

[Холодный старт с нуля](COLD_START.md): единый порядок подготовки, ролей,
финансирования, DKG, relay/ZK, ручной рассылки и первого smoke.

[Сквозной сценарий FROST и ZK](FROST_ZK_LIFECYCLE.md): отдельные DKG,
уравнения подписи и proof, nonce recovery, гонки публикаций и Bitcoin settlement.

Обновление 5 ноября: [точные формулы и математические обоснования](LAUNCH_MATHEMATICS.md),
[новые SVG/PNG и source hashes](../ops/artifacts/launch-docs-math-20261105/diagrams/manifest.json),
[отчёт проверки документации](../ops/artifacts/launch-docs-math-20261105/README.md).
Формулы учитывают конфигурируемые H, m, резерв, bps и prefunded APY.
Предыдущая [функциональная приёмка конфигуратора](../ops/artifacts/launch-configurator-20261105/README.md)
выполнена отдельно: 921 Solidity, 3436 SDK/ops и create/reuse E2E.


[Математическое описание](LAUNCH_MATHEMATICS.md): единицы, точное округление,
границы выплат, UTC-учёт, APY и предпосылки безопасности.

[Единый конфигуратор запуска](LAUNCH_CONFIGURATOR.md): `frost-signer configure wizard`
выбирает сети, create/reuse, confirmations, имена токенов, пороги, комиссии и
дополнительный стартовый APY из собственного USD-бюджета.

Для ZK с тестовыми BTC используйте новый [порядок Bitcoin testnet4 + Sepolia](ZK_TESTNET4_SEPOLIA.md):
отдельные pinned guest/host, policy V2 и проверка рабочего proof для нового state.

[Проверка логики и разграничения прав, 3 ноября 2026](SECURITY_LOGIC_REVIEW_2026_11_03.md):
открытые находки Docker/API, риск времени казначейских выплат и локальные воспроизведения.

[Запуск с нуля на Linux, Windows/WSL2 и macOS](CROSS_PLATFORM_QUICKSTART.md):
сборка, личные настройки, другой состав участников, три DKG, полный ULR/Bitcoin
bootstrap, общий root и новая конфедерация. [Общий runner тестов](SYSTEM_TEST_MODES.md).
[Карта готовности, схем и входных данных](LAUNCH_READINESS.md) отличает готовые
шаблоны от реальных bindings и личных данных будущего запуска.
[Поставка участникам](DISTRIBUTION.md) исключает готовые ZK proofs. Рабочий
переход рассчитывает выбранный prover для развёрнутого state. Остальные
участники проверяют публичный ZIP через собственные RPC или повторяют расчёт.
[Linux/WSL2 x86-64 prover](ZK_PROVER_LINUX.md)
использует отдельные pinned tools и bubblewrap/seccomp; macOS arm64 сохраняет
прежний backend. Полный fresh-proof прогон и физический WSL2 пока не подтверждены.
[Коллегам: начало тестов](PEER_TESTING.md) — короткий smoke без правки кода,
проверенный Ubuntu container baseline и отдельная граница Windows/WSL2.

[Общий стейкинг ULR](ULR_STAKING.md): один payout-стейблкоин, посуточный учёт,
несколько источников и поздние доплаты. [Казначейский маршрут](ULR_TREASURIES.md)
добавляет общий снимок wallet+stake и дневную выплату с долей по ULR (defaults: 90% сверх $1000),
явное подключение конфедераций и CLI. [Единый ULR bootstrap](ULR_BOOTSTRAP.md)
объединяет выпуск 10 млн, общий стейкинг, казны, кандидатов и делегирование;
выбор relay/ZK и создание/повторное использование общих адресов подписываются в V6.
Полный `--system-policy` добавляет Bitcoin family, адресный/request/signing graph
и fee reserve в тот же bootstrap; Sepolia + Bitcoin testnet4 поддерживает relay и отдельный ZK guest.

[Кандидаты в кастодианы и делегирование ULR](ULR_CANDIDATES_AND_VOTING.md):
порог по кошельку + личному стейку, отдельное одобрение действующего состава,
представитель для голосования и сохранение всех наград у владельца.

**Historical architecture/acceptance snapshot, 4 November 2026:**
[custodian-only DKG, actual BTC handover and independent cBTC exit](BITCOIN_CUSTODY_TRANSFER.md),
[system architecture](ARCHITECTURE.md) and [launch order](LAUNCH_READINESS.md).
The [new SVG/PNG exports and source hashes](../ops/artifacts/custody-completeness-20261104/diagrams/)
retain the Mermaid sources of that snapshot. The
[current custody acceptance](../ops/artifacts/custody-docs-tests-20261104/README.md)
records 912 Solidity tests and 3372 SDK/ops cases. The preceding
[completeness review](../ops/artifacts/custody-completeness-20261104/README.md)
also verified native Core/FROST transfer, independent cBTC exit, a custodian
treasury payout and full create/reuse. [Commands and regression coverage](SYSTEM_TEST_MODES.md#проверка-custody-от-4-ноября-2026)
describe how to repeat them. Fresh ZK and physical Linux/WSL2 remain unqualified
for this increment. The earlier 908/3344 acceptance retains its historical snapshot.

The preceding 3 November update covers [ZK-SPV, suite and address-deposit diagrams](ZK_SPV_ARCHITECTURE.md)
and [distribution, independent proofs and cache recovery](DISTRIBUTION.md).
Its [historical SVG/PNG exports and source hashes](../ops/artifacts/documentation-independent-proving-20261103/)
retain that snapshot. The independent-proving code passed
[3123 SDK/packaging tests](../ops/artifacts/independent-proving-20261103/validation.json);
the Linux/WSL2 x86-64 prover port is now implemented with
[component evidence](../ops/artifacts/zk-linux-port-20261103/validation.json).
Fresh proving and multi-host/physical WSL qualification remain pending.
The 2 November [local acceptance package](ZKSPV_LOCAL_ACCEPTANCE.md) records **3269 Python tests,
834 ordinary Solidity tests and 25 state/adapter tests**, without failures or skips.
All three formerly excluded native checks are restored. The [functional coverage audit](ZKSPV_FUNCTIONAL_COVERAGE.md)
distinguishes 43 workflow recipes, 195 local row bindings across 55 modules and
actual qualification. Local fixture/model checks do not certify complete real-contour coverage.
The actual Core/FROST/Groth16/EVM contour is **deferred by the user for device disk capacity**.
Independent qualification remains pending; `QUALIFIED=false`, `FEE_QUALIFIED=false` and
`production_ready=false`. Earlier C1–C3/1776-test reports retain their own snapshots.

The [release preparation and independent review package](PRODUCTION_PREPARATION.md)
supports independent Ethereum/Bitcoin network selection and 15 explicit backend
combinations. The user selected a new deployment; empty-inventory acceptance is
still required. The participant package contains source and pins without ready
proofs. Historical evidence packages are developer review material and are not
participant distributions.

The [canonical family root](BITCOIN_KEY_FAMILY.md), [recoverable observation history](ZKSPV_OBSERVATION_HISTORY.md)
and [expanded lifecycle/assertion bindings](ZKSPV_SCENARIO_BINDINGS.md) document the new code and its limits.

ZK-SPV specification 2.16: [implemented local changes and blockers](ZK_SPV.md),
[operations](ZK_SPV_OPERATIONS.md), [migration](ZK_SPV_MIGRATION.md),
[consumer worker](ZK_SPV_WORKER.md), [encrypted transport](SPV_ENCRYPTED_TRANSPORT.md),
[admission/payment and permanent archive](SPV_AUTOMATION_ARCHIVE.md),
[funding transaction profile and EVM parser](BITCOIN_FUNDING_PROFILE.md),
[canonical mint admission](BITCOIN_MINT_ADMISSION.md),
[передача обеспечения федерация → конфедерация и отдельные ключи кастодианов](BITCOIN_CUSTODY_TRANSFER.md),
[address derivation and per-input crypto](DEPOSIT_DERIVATION_INPUTS.md),
[address registry and issuance fences](ADDRESS_DEPOSIT_REGISTRY.md).
The [address Bitcoin signer](BITCOIN_ADDRESS_SIGNING.md) documents explicit V3
enrollment, per-input generation checks and shared nonce recovery.
The [address request/watch service](ADDRESS_REQUESTS_WATCH.md) covers encrypted
delivery, durable ACK and bounded canonical rescan/recovery; Ready remains disabled.
The [FROST address registration workflow](ADDRESS_REGISTRATION.md) binds V2
requester commitments to typed public sessions, generation fences and explicit recovery.
The [registered-address SPV worker](ADDRESS_SPV_WORKER.md) adds explicit V5/V3
profiles, bounded watch scheduling and opt-in admission/quote/payment automation.
The [old Bitcoin epoch service](BITCOIN_EPOCH_SERVICE.md) retains original
watch/signing graphs, late deposits and refund archives, and checks fresh
participant retention claims before a planned departure. Production qualification
remains open; admitted graphs now share the independently pinned canonical family root.
The [combined contour controller and retained-graph handover](ZKSPV_CONTOUR.md)
add durable staged execution, encrypted request/ACK/status, canonical service
admission/quote/payment, explicit own funding and local original-graph service prerequisites.
Optional structural bootstrap and exact Bitcoin funding adapters reuse original
deployment/nonce/PSBT journals, with freshness deferred and no Ready claim.
Current-root header materialization and SMP1/address mint adapters now reuse
original admission/FROST authorization/receipt engines; real combined acceptance
and production derivation qualification remain pending.
Reviewed V4 withdrawal preparation, selected BTC/MP/S settlement and full-inventory
reconciliation adapters retain owner consent, principal and original actor/
Bitcoin journals. External FROST authorization remains required; incident pauses
and expired windows need explicit recovery.
The standalone incident adapter now carries a reviewed exact transition through
encrypted request/ACK/status and original EVM_AUTH receipts, independently of
monetary/Fresh prerequisites. Private signer approval stays external; terminal
observations, watch/reserve recovery checks and financial pauses are preserved.
The old-epoch adapter advances bounded original Fleet observations and rechecks
retained threshold, archives/watch, qualification and summed funding. All 25
capability types are available across configurations. The multi-request suite now
joins them with dependency commitments, explicit late input binding, 43 workflow
recipes, 195 mandatory observation slots and a source/input/proof/receipt evidence
bundle. Actual per-case qualified execution and live asset migration remain open.

The [original-graph continuity guard](BITCOIN_CONTINUITY.md) adds canonical
inventory, persistent family/status checks and explicit refusal of unsupported
registry/verifier/accounting transitions. It does not qualify live migration.
This is a partial implementation with production activation blocked; its new
evidence is separate from the historical acceptance counts below.

Operational fixes from 26 October: [review and validation](OPERATIONS_REVIEW_2026_10_26.md),
[relay keeper and actual gas](RELAY_KEEPER_AND_GAS.md),
[MP mint and treasury withdrawals](BITCOIN_MINT_AND_TREASURY.md).

Start with [the architecture and diagrams](ARCHITECTURE.md) for the current
component, DKG, deployment and Bitcoin flows. The implementation baseline is
**18 October 2026**: [review, fixes and acceptance evidence](UPGRADE_REVIEW_2026_10_18.md),
checked against [the eight-section specification](UPGRADE_SPEC_2026_10_18.md).
The subsequent CLI integration is recorded in [Bitcoin on-chain acceptance](BITCOIN_ONCHAIN_ACCEPTANCE.md).
New **V6** invitations independently select Ethereum mainnet/Sepolia/local and
Bitcoin mainnet/testnet4/regtest. See [the network matrix and flow commands](NETWORKS_AND_AUTOMATION.md)
for parallel DKG, bounded packet/header batches and durable continuation. The
[network-matrix acceptance](PARALLEL_NETWORKS_ACCEPTANCE.md) tracks that revision;
the [26 October review](OPERATIONS_REVIEW_2026_10_26.md) records subsequent operations
fixes and reduced historical RPC requirements.
The [Ethereum mainnet runbook](ETHEREUM_MAINNET_RUNBOOK.md) covers chain 1,
finalized receipts, local gas budgets and addresses-only onboarding.
The earlier [Ethereum V5 acceptance](ETHEREUM_MAINNET_ACCEPTANCE.md) records
5 Rust, 437 Solidity, 1218 Python and 21 E2E passes on its earlier source snapshot,
with no public mainnet transactions. The
[mainnet setup guide](BITCOIN_MAINNET_RUNBOOK.md) covers checkpoint policy, your own
synchronized Core, external token/feed identities, bounded relay maintenance and
remaining custody risks. The [mainnet implementation and acceptance](BITCOIN_MAINNET_ACCEPTANCE.md)
records the pre-V5 run: 5 Rust, 426 Solidity, 954 Python and 15 E2E passes, with all 96 contracts
within size limits. Public Sepolia paid-deployment acceptance has not been run.

## Set up and operate the system

| Task | Current guide |
|---|---|
| Token types, bootstrap symbols, issuance, backing and redemption | [Token map: MP, FS, CS, DAO and rewards](TOKENS.md) |
| First ceremony with only code and friends' addresses | [First DKG using encrypted on-chain transport](FIRST_DKG_ONCHAIN.md) |
| Independent network choice, testnet4 and automatic DKG/payment progress | [Networks and automation](NETWORKS_AND_AUTOMATION.md) |
| Invitation fields, complete integration plan and durable resume | [Bootstrap](BOOTSTRAP.md), [configuration](CONFIGURATION.md) |
| Recover a reverted withdrawal CREATE group and collect safe SDK diagnostics | [Withdrawal deployment and recovery](BOOTSTRAP.md#withdrawal-deployment-and-recovery), [diagnostics for participants](PEER_TESTING.md#как-собрать-диагностику-sdk) |
| Paid factory infrastructure and project treasury | [Staged deployment](STAGED_DEPLOYMENT.md) |
| Contract identity, reduced historical RPC use and reused dependencies | [Creation provenance](CONTRACT_PROVENANCE.md) |
| Keeper installation, persistent cache, restart and actual deployment gas | [Relay keeper and gas](RELAY_KEEPER_AND_GAS.md) |
| Compiler, runtime/initcode limits and deployment entry points | [Contract sizes and deployment](CONTRACT_SIZE_AND_DEPLOYMENT.md), [Web3 README](../web3/README.md) |
| Stake, claim, choose a DAO signer and vote | [DAO CLI](DAO_CLI.md), [Custodian DAO](CUSTODIAN_DAO.md) |
| Deposits, escrow, Bitcoin payout proof and MP/S burn | [Bitcoin lifecycle](BITCOIN_WITHDRAWALS.md), [signer trust](BITCOIN_SIGNER_TRUST.md) |
| Ethereum mainnet bootstrap, fees and finality | [Ethereum mainnet runbook](ETHEREUM_MAINNET_RUNBOOK.md) |
| Bitcoin mainnet configuration and relay readiness | [Mainnet CLI runbook](BITCOIN_MAINNET_RUNBOOK.md), [relay contract](MAINNET_RELAY.md) |
| Bitcoin payment without participant file exchange | [Step-by-step CLI runbook](BITCOIN_ONCHAIN_RUNBOOK.md), [separate cryptographic review](BITCOIN_ONCHAIN_SECURITY_REVIEW.md) |
| Pool fees, conversion and separate project deployment fees | [Fee flow and diagram](FEE_FLOW.md), [fee accounting](FEES.md) |
| API authentication, roles and durable operations | [Operational API](OPERATIONAL_API.md), [OpenAPI](openapi.json) |
| DKG, signing, nonce persistence and local tools | [DKG protocol](DKG_PROTOCOL_V1.md), [DKG runbook](DKG_RUNBOOK.md), [FROST](FROST.md), [signing protocol](SIGNING_PROTOCOL.md), [signer-agent README](../signer-agent/README.md) |
| Contract and CLI reference | [API reference](API.md) |
| Mainnet relay and Bitcoin packet board reference | [Mainnet relay](MAINNET_RELAY.md), [Bitcoin signing board](../web3/docs/bitcoin-signing-board.md) |
| Existing deployments and immutable epochs | [Migration](MIGRATION.md), [ZK migration boundaries](ZK_SPV_MIGRATION.md) |
| Current ZK/address architecture, status diagrams and remaining integration | [Code map and diagrams](ZK_SPV_ARCHITECTURE.md), [address registry](ADDRESS_DEPOSIT_REGISTRY.md) |
| Self-hosted consumer updates, encrypted requests and optional funding | [Worker](ZK_SPV_WORKER.md), [transport](SPV_ENCRYPTED_TRANSPORT.md), [automation/archive](SPV_AUTOMATION_ARCHIVE.md) |

## Current validation and limits

The latest [historical-RPC optimization check](../ops/artifacts/history-optimization-20261026.summary.json)
records **1659 Python tests and 14 targeted E2E scenarios**, all passing. It follows
the [complete operations run](../ops/artifacts/operations-final-20261026.summary.json)
of 1608 Python, 482 Solidity, 5 Rust and 48 E2E tests. Solidity/Rust code did not
change in the subsequent RPC optimization; the targeted run is not a full rerun
of the now 49 E2E scenarios. All 97 measured contracts fit size limits.
These checks use local Anvil/Core and explicitly scoped historical adapters,
not funded public mainnet transactions. The [operations review](OPERATIONS_REVIEW_2026_10_26.md)
lists the migration, RPC and custody limitations.

The recorded pre-mainnet [regtest Bitcoin CLI validation record](../ops/artifacts/bitcoin-onchain-validation-20261018.json)
records 5 Rust, 415 Solidity tests (418 Foundry entries including three getters),
685 Python and 14 E2E scenarios, with no failures or skips. All 95 compiled
project artifacts passed size gates. The [acceptance report](BITCOIN_ONCHAIN_ACCEPTANCE.md)
describes the three-friend DKG, on-chain payments, nonce recovery and secret-boundary
checks. It also links verified CLI examples, rendered diagrams and checked source hashes.
The [earlier specification validation](../ops/artifacts/upgrade-validation-20261018.json)
retains its original counts and the 46-contract / 147-step bootstrap evidence.
The separate [mainnet Core/policy replay](../ops/artifacts/mainnet-policy-core-20261018.json)
checks historical mainnet consensus and proves that a real unsynchronized header-only
Core is rejected by live policy. It does not claim a funded mainnet transaction.

Read the [current deployment limits](OPERATIONS_REVIEW_2026_10_26.md#границы-исправлений),
the [earlier upgrade limits](UPGRADE_REVIEW_2026_10_18.md#что-остаётся-перед-реальным-развёртыванием)
and [security report](../security_best_practices_report.md) before funded use.
Canonical creation provenance does not establish the safety of an external
oracle, converter or market. Integration regtest uses test USD/feed fixtures; backed V6 uses
explicit existing token/feed dependencies and a prefunded fixed-quote converter.
Ethereum mainnet additionally funds both paid factory creations; it retains the same external
asset and converter trust assumptions.
External issuer, proxy upgrade and oracle trust remain visible deployment assumptions. Private DKG payloads are encrypted, while on-chain metadata remains
public. Signing-state/nonce-journal rollback must not be treated as recovery.

For deployment, the account that deploys each factory reserves its entity ID
for an EVM_AUTH session and commits the formation and share-token metadata.
Adoption checks the formation against the reservation and the canonical DKG
result. Deployment checks the reserved token name and symbol. Subsequent approvals use exact target
operation hashes, enforced signing sessions and independently verified bindings.
[Factory deployment workflow](FEDERATION_CONFEDERATION_SETUP.md).
The current three-friend custody profile requires only Bob/Carol for monetary
confederation EVM/BTC signatures; Alice remains in federation EVM. Paid deployment
uses a separate project authorization and two sequential payer operations;
explicit integration exemption is a different configuration. Follow the linked
runbooks for the actual commands and required parameters.

## Historical evidence

These reports retain their original counts, assumptions and finding status.
Where an older report says a component was absent, use the current guides and
18 October review to determine whether that gap has since been closed.

- [17 October fresh-clone and first-use acceptance](ACCEPTANCE_REPORT.md)
- [17 October specification audit](TZ_AUDIT_2026_10_17.md)
- [17 October dependency review](DEPENDENCY_SECURITY_2026_10_17.md)
- [Original federation/confederation security audit](FEDERATION_SECURITY_AUDIT.md)
- [25 September specification review matrix](CUSTODIAN_DAO_REVIEW.md)
- [5 October strict review](STRICT_REVIEW_2026_10_05.md)
- [Earlier deployment-readiness analysis](DEPLOYMENT_READINESS.md)

These are internal reviews, not independent external audit sign-off.

- [SPV native gas treasury and stable payment registry](SPV_GAS_TREASURY.md) — local component; production funding gate remains UNKNOWN.

## BTC fee reserve and reconciliation

See [the reserve runbook](BTC_FEE_RESERVE.md) for the graph-scoped canonical allocations,
principal-preserving settlement, independent Core inventory and typed FROST commands.
Use `bitcoin fee-reserve` and the private `--fee-reserve-config` on participants.
Address service V2 pins the same policy without resetting watch or nonce journals.
Ready and operational qualification remain pending; long combined E2E is deferred.

- [Personal BTC/MP/S withdrawal fees](WITHDRAWAL_FEE_SUBSIDY.md): exact owner consent, principal conservation, named BTC carry/refunds, per-MP S restoration, V4 signer and bounded public transport; qualification pending.
