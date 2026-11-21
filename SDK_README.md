# Satohsi MultiPool DAO

[Запуск с нуля: Linux, Windows/WSL2, macOS и разные участники](docs/CROSS_PLATFORM_QUICKSTART.md)
[Готовность к запуску: схемы, форматы и данные оператора](docs/LAUNCH_READINESS.md).
· [Режимы тестирования](docs/SYSTEM_TEST_MODES.md).
· [Поставка без готовых proofs и собственный расчёт у каждого](docs/DISTRIBUTION.md).
· [Коллегам: проверка хоста и начало тестов Linux/Windows-WSL2](docs/PEER_TESTING.md)
· [ZK prover: установка tools и раннер Linux/WSL2 x86-64](docs/ZK_PROVER_LINUX.md).

For Sepolia + Bitcoin testnet4, each participant can use their own Sepolia RPC
and an archival Bitcoin Core endpoint over HTTPS. Run `configure check-rpc`
against the agreed invitation before enrollment. [Remote RPC setup](docs/LAUNCH_CONFIGURATOR.md#sepolia-и-bitcoin-testnet4-без-локальных-нод).
The [12 November security review](docs/SECURITY_LOGIC_REVIEW_2026_11_03.md#повторная-проверка-12-ноября-2026)
covers FROST confirmation retries, deployment provenance and ZK prover isolation.

4 November custody update: **912 Solidity / 3372 SDK+ops tests**;
real Core/FROST transfer, independent cBTC exit and custodian treasury payout,
plus full create/reuse. [Current results and test commands](docs/SYSTEM_TEST_MODES.md#проверка-custody-от-4-ноября-2026)
· [Evidence and remaining limits](ops/artifacts/custody-docs-tests-20261104/README.md).
The fresh working ZK proof, physical Linux/WSL2 run for this revision and
address/fee production qualification remain pending. New deployments use the
pinned coordinator and session-owner checks described in the [custody runbook](docs/BITCOIN_CUSTODY_TRANSFER.md).

Historical 2 November acceptance snapshot: **3269 Python, 834 ordinary Solidity and 25 state/adapter tests**, with no failures or skips.
[Evidence and reproduction](docs/ZKSPV_LOCAL_ACCEPTANCE.md) · [Functional coverage and remaining work](docs/ZKSPV_FUNCTIONAL_COVERAGE.md).
The real Core/FROST/Groth16/EVM contour is deferred for device disk capacity;
independent qualification and production activation remain pending.

3 November architecture update: [ZK-SPV, automation, archive and address-deposit diagrams](docs/ZK_SPV_ARCHITECTURE.md),
[proof-free distribution and independent computation](docs/DISTRIBUTION.md).
Relay/ZK configurations remain selectable. The address registry/mint/status
contracts are a candidate; production activation and Ready addresses remain
blocked pending the documented qualification and client/watch/recovery work.
The [address registration client](docs/ADDRESS_REGISTRATION.md) connects durable
V2 requests to public FROST sessions with explicit generation-aware recovery.
The [address SPV worker](docs/ADDRESS_SPV_WORKER.md) connects registered intents
to bounded consumer/watch requests and opt-in admission/quote/payment automation.

Current [architecture diagrams and SVG/PNG index](docs/DIAGRAMS.md) include
NFT issuance, quota use and network-specific ZK bindings.

5 November NFT update: [managed FROST create/mint and quota protection](docs/NFT_CONFIGURATION.md).
Each signer independently pins the entity graph; public quotes do not consume
NFT volume. Only approved pool contracts can consume quota for their caller.

Operational fixes from 26 October: [review and validation](docs/OPERATIONS_REVIEW_2026_10_26.md),
[relay keeper and actual gas](docs/RELAY_KEEPER_AND_GAS.md),
[MP mint and treasury withdrawals](docs/BITCOIN_MINT_AND_TREASURY.md).
Historical EVM RPC work is reduced by persistent authenticated provenance caches,
direct creation proofs at known blocks and a default 8192-block invitation search.
See [RPC requirements and remaining history](docs/CONTRACT_PROVENANCE.md) and
[keeper restart setup](docs/RELAY_KEEPER_AND_GAS.md). These changes preserve finality
and reorg checks; first verification of internal deployments can still need old state.

The enabled SMP1 deposit path requires a Bitcoin OP_RETURN containing `SMP1`, the explicit
EVM recipient and an allocation hash. Generate it with `bitcoin deposit-commitment`
before sending BTC; the [mint runbook](docs/BITCOIN_MINT_AND_TREASURY.md) preserves
the wallet output index during Bitcoin Core funding.
Coinbase rewards can include the same recipient data; when SMP1 is present,
the contract enforces it instead of allowing an unrelated reward allocation.

Current code: [architecture and independent proving](docs/ZK_SPV_ARCHITECTURE.md).
[Worker launch modes](docs/ZK_SPV_WORKER.md#worker-modes-and-transition-archives)
cover prepare-only service, local proving and separate public transition archives.
Earlier operational verification: [26 October operations/RPC review](docs/OPERATIONS_REVIEW_2026_10_26.md).
Network-matrix baseline: [V6 acceptance](docs/PARALLEL_NETWORKS_ACCEPTANCE.md).
Earlier specification implementation: [18 October review](docs/UPGRADE_REVIEW_2026_10_18.md),
[eight-section specification](docs/UPGRADE_SPEC_2026_10_18.md),
[staged project deployment](docs/STAGED_DEPLOYMENT.md), [Bitcoin mainnet relay](docs/MAINNET_RELAY.md)
and [Bitcoin signing board](web3/docs/bitcoin-signing-board.md).

New integration deployments: [first DKG using on-chain transport](docs/FIRST_DKG_ONCHAIN.md).
Bitcoin payments after DKG: [on-chain CLI runbook](docs/BITCOIN_ONCHAIN_RUNBOOK.md),
[Bitcoin mainnet setup](docs/BITCOIN_MAINNET_RUNBOOK.md),
[Ethereum mainnet bootstrap](docs/ETHEREUM_MAINNET_RUNBOOK.md) and
[cryptographic review](docs/BITCOIN_ONCHAIN_SECURITY_REVIEW.md).
All new invitations use **V6**, selecting Ethereum mainnet/Sepolia/local and
Bitcoin mainnet/testnet4/regtest independently. The [network matrix and flow guide](docs/NETWORKS_AND_AUTOMATION.md)
explains required assets, checkpoint policy, finality and local budgets. Every
friend reviews the explicit financial policy and signs their enrollment on chain.
`flow dkg` advances three independent ceremonies together, `flow bootstrap`
continues approved deployment steps, and `flow payment`/`flow relay` continue an
explicit Bitcoin batch and header synchronization. Financial FROST intents and
custodian approvals still require explicit participant review.
Bounded batches retain HPKE, context/sender checks, finalized round barriers and
durable exact-byte retries. The [complete invitation example](docs/FIRST_DKG_ONCHAIN.md#1-оператор-публикует-приглашение)
and [field reference](docs/BOOTSTRAP.md) specify every financial value and unit.
The [original 17 October audit](docs/TZ_AUDIT_2026_10_17.md) describes the supplied baseline.

User DAO CLI: [пошаговая настройка и работа с друзьями](docs/DAO_CLI.md).
`frost-signer dao --help` covers wallet balances, staking, rewards, signer selection
and governance; writes simulate unless explicitly sent with your own identity.

Common [ULR staking](docs/ULR_STAKING.md) has an opt-in
[confederation treasury route](docs/ULR_TREASURIES.md): one shared wallet+stake
snapshot, 90% of each USD-token treasury's excess over $1000 weighted by its
custodians' ULR, and one payment per completed UTC day. `frost-signer ulr-treasury`
prepares, proposes and executes reviewed connections/snapshots and daily payouts.
The [signed ULR bootstrap profile](docs/ULR_BOOTSTRAP.md) creates the single fixed
10m issue and shared graph, or attaches another confederation to its existing roots,
with relay/ZK selected before deployment through `setup invite --ulr-policy FILE`.
For the full Bitcoin graph use `setup invite --system-policy FILE`, prepared by
`frost-signer system-profile`: it also includes canonical key family, address/request
and signing boards, fee reserve and withdrawal fees. Sepolia + Bitcoin testnet4
offers relay and a separate ZK test profile. [ZK testnet4 runbook and verification scope](docs/ZK_TESTNET4_SEPOLIA.md).
Choose the four issued ERC20 names/symbols before enrollment with
`system-profile --token-metadata FILE` or `setup invite --token-metadata FILE`;
[schema and example](docs/TOKENS.md#имена-и-тикеры-до-запуска).

New `system-profile` runs also require `--bitcoin-custody-policy FILE` (start
with `config/bitcoin-custody.example.json`). Confederation Bitcoin and EVM
monetary keys then belong only to custodians. Foreign MP uses a confirmed
Bitcoin custody transfer before shares are issued; see
[the custody transfer runbook](docs/BITCOIN_CUSTODY_TRANSFER.md).
The [4 November diagram gallery](ops/artifacts/custody-completeness-20261104/diagrams/README.md)
includes separate DKG roles, the BTC handover, request states and updated launch
and token flows as source-backed SVG/PNG.

[ULR custodian candidates and delegated voting](docs/ULR_CANDIDATES_AND_VOTING.md)
use wallet plus personal stake for the 200,000 ULR candidacy threshold. Active
admission still needs FROST/custodian approval; delegated votes leave stake and
rewards under their original owner's control.

Ethereum/L2 contracts for Bitcoin SPV verification, wrapped MP tokens, federation S-token pools, confederations, fees and threshold authorization.

## Authorization model

`FederationFactory.deployFederation(...)` and `ConfederationFactory.deployConfederation(...)`
are the final deployment entry points. The current V1 path first adopts a completed
`EVM_AUTH` DKG result and deploys `DKGAuthorization`, a `FROSTAuthorization` subclass, bound to:

- the canonical `FROSTCoordinator` selected when the factory is deployed;
- that coordinator's immutable `FROSTVerifier`;
- the DKG-approved 32-byte even-Y group public key;
- the exact ordered participant set and threshold;
- the canonical DKG ID and completed result hash.

Legacy initiation/deployment remains restricted to EVM 31337/11155111. New
deployments follow [the V1 architecture and diagrams](docs/ARCHITECTURE.md).

There is no address-counter approval mode, verifier bootstrap or caller-selected verifier. The former compatibility adapter and its deployer were removed.

Privileged calls derive target-bound hashes from chain ID, authorization contract, target, selector, parameters and nonce. A finalized enforced coordinator session is checked again by the authorization contract, then only the bound target may consume it once.

## Components

| Area | Main components |
|---|---|
| Bitcoin proof lifecycle | `RegtestHeaderRelay`, `MainnetHeaderRelay`, `Testnet4HeaderRelay`, `MainnetSPVGuard`, `BitcoinDepositRegistry`, `BitcoinBatchRegistry`, `BitcoinSigningBoard`, `BitcoinWithdrawalEscrow` |
| Bitcoin issuance and examples | `RegisteredMiningPool`, `PoolMpToken`; separate legacy `SimpleSPVContract` / `SimpleMiningPool*` examples and transaction helpers |
| Federation pools | `SimpleMultiPool`, `ConfederationMultiPool`, S-token contracts |
| DKG and FROST | `DKGOnchainBoard`, `DKGCoordinator`, `FROSTCoordinator`, `FROSTVerifier`, `DKGAuthorization`, `signer-agent/` |
| Deployment | Staged runner, canonical creation provenance, factories, controlled orchestrators and leaf deployers |
| Fees | `FederationFeeManager`, distribution and wallet registries, NFT modifier |
| Custodian DAO | Fixed-supply ERC-20, UTC-day staking, staker decisions, custodian-count governance, Chainlink BTC/USD TWAP, income vault, fee router, income adapters and daily settlement |
| Clients | First-DKG/bootstrap CLI, on-chain Bitcoin payment CLI, DAO CLI, authenticated operations API and explicitly selected regtest/testnet4/mainnet Bitcoin worker |

The paid factory-deployment router has separate authorization/vault contracts and
sends 100% of its fee to project treasury. The Anvil/Sepolia staged CLI uses a
separate project ceremony; Ethereum mainnet V6 reuses its federation EVM DKG result.
The legacy manifest/package path does not support a fourth mainnet DKG. Pool-fee conversion into DAO income is a
separate flow; see [the fee diagrams](docs/FEE_FLOW.md).

## Verification

```bash
forge build --root web3 --sizes
python -c 'from ops.local_acceptance import run_solidity_suites; run_solidity_suites("ops/artifacts/solidity-suites.json")'
PYTHONPATH=signer-agent python -m pytest signer-agent/tests -q
```

Solidity tests that use process-global environment variables (including local
fixture/script imports) run in separate Forge processes; other suites run once
per suite. The runner checks executed names and counts against discovery;
skips and failures stop acceptance. Compiler settings and EVM limits are unchanged.
Deployment requires the explicit parameters
and stages in [STAGED_DEPLOYMENT.md](docs/STAGED_DEPLOYMENT.md); a bare script
invocation is intentionally insufficient. `python ops/local_acceptance.py`
also starts Bitcoin Core and Anvil and runs `tests/e2e`; that full contour is
currently deferred because of disk capacity. For the completed local-only
checks and their reproduction commands, see
[ZK-SPV local acceptance](docs/ZKSPV_LOCAL_ACCEPTANCE.md).

The [new V6/parallel acceptance](docs/PARALLEL_NETWORKS_ACCEPTANCE.md) tracks the
current implementation and its full verification status. The earlier
**22 October 2026 Ethereum V5 acceptance** passed **5 Rust, 437 Solidity,
1218 Python and 21 E2E checks**, with all 96 contracts within size limits.
It includes addresses-only V5 bootstrap and real local Forge/DAO transactions;
no public mainnet transaction was sent. See the [historical V5 acceptance report](docs/ETHEREUM_MAINNET_ACCEPTANCE.md)
for scope, artifacts and remaining risks.

The recorded **18 October 2026 regtest Bitcoin CLI acceptance**, before the mainnet extension, passed 5 Rust tests,
415 Solidity tests (418 Foundry entries including three getters), 685 Python
tests and 14 E2E scenarios, with no failures or skips. All 95 compiled project
artifacts passed size gates. The [acceptance report](docs/BITCOIN_ONCHAIN_ACCEPTANCE.md)
links the machine-readable results and checked source hashes. The
[earlier specification review](docs/UPGRADE_REVIEW_2026_10_18.md) retains its
original baseline counts.

The updated `MultiPoolDeployer` runtime is 24,437 bytes, with 139 bytes
remaining under EIP-170. Keep both runtime and complete constructor-payload
checks as deployment gates.

## Documentation

Start at [docs/README.md](./docs/README.md). It is the index for every maintained document.
See [the token map](docs/TOKENS.md) for MP, federation FS, confederation CS,
DAO governance/reward tokens, their issuance and the actual bootstrap symbols.

The [V6 acceptance](docs/PARALLEL_NETWORKS_ACCEPTANCE.md) records current validation
status. The [18 October review](docs/UPGRADE_REVIEW_2026_10_18.md) and
[5 October review](docs/STRICT_REVIEW_2026_10_05.md) retain their historical scope.
These internal checks are not external audit sign-off.
The subsequent [Bitcoin CLI acceptance](docs/BITCOIN_ONCHAIN_ACCEPTANCE.md) covers
on-chain payment transport and a separate cryptographic review.

HSM integration, production operational monitoring and deployment key custody require separate review.
Mainnet profile support is not an external audit or production-custody approval.

## V1 integration lifecycle

The new integration path joins completed DKG results to both factories, proof-only
MP issuance, real S/MP escrow, Bitcoin payment-proof burn, funded fee conversion,
DAO income/claim, and an authenticated operations API. See
[bootstrap](docs/BOOTSTRAP.md), [Bitcoin lifecycle](docs/BITCOIN_WITHDRAWALS.md),
[fee flow](docs/FEE_FLOW.md), and the [current verification status](docs/PARALLEL_NETWORKS_ACCEPTANCE.md).
The operator independently selects Ethereum mainnet/Sepolia/local and Bitcoin
mainnet/testnet4/regtest in V6. Ethereum mainnet requires paid factory fees,
external assets, finalized state and explicit local budgets. Public Bitcoin
requires a synchronized Core, pinned checkpoint, canonical work, bounded relay
lag, freshness and the enrolled EVM receipt depth.
`MainnetSPVGuard` enforces relay freshness and halt at the proof-consumer boundary.
`dkg btc-network-status` and `dkg btc-relay` operate from the pinned on-chain graph.
See the [mainnet runbook](docs/BITCOIN_MAINNET_RUNBOOK.md) for setup and trust limits.
The [mainnet acceptance report](docs/BITCOIN_MAINNET_ACCEPTANCE.md) records the
Bitcoin mainnet extension run before V5: 5 Rust / 426 Solidity / 954 Python / 15 E2E passed. All 96
contracts pass size checks. The report separates real historical Core replay,
V4 DKG with simulated Core, and new regtest payments; no funded mainnet payment
or production EVM deployment is claimed.
The [Bitcoin CLI](docs/BITCOIN_ONCHAIN_RUNBOOK.md) now carries plans and signing
packets through `BitcoinSigningBoard` without participant file exchange.
Public Sepolia paid-deployment
acceptance has not been run.

## License

MIT

## Reproduce the local integration

Run `python ops/local_acceptance.py` to reproduce the local DKG → factories → proven MP issuance → Bitcoin payment → MP/S burn → fee conversion → DAO claim scenario. The runner starts isolated Anvil and Bitcoin Core nodes, runs participant processes, and stops its nodes on exit. Current observed results are in [V6 acceptance](docs/PARALLEL_NETWORKS_ACCEPTANCE.md); the [earlier acceptance report](docs/ACCEPTANCE_REPORT.md) is retained for historical comparison. Participant setup and tool requirements are in [first DKG](docs/FIRST_DKG_ONCHAIN.md).

First DKG with friends: [on-chain onboarding](docs/FIRST_DKG_ONCHAIN.md).
Specification follow-up: [17 October audit](docs/TZ_AUDIT_2026_10_17.md).

BTC fee reserve and address reconciliation now have an explicit canonical profile,
principal-preserving settlement and typed FROST/Core guards. See the
[runbook](docs/BTC_FEE_RESERVE.md). Ready/production qualification and the long
combined contour remain pending.
