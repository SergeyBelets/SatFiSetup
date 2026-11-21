# Contract sizes and deployment verification — 4 November 2026

Current V6 verification status: [acceptance](PARALLEL_NETWORKS_ACCEPTANCE.md). The earlier eight-section [implementation report](UPGRADE_REVIEW_2026_10_18.md) and fresh-clone [acceptance report](ACCEPTANCE_REPORT.md) retain their historical scope. Relative compiler remappings are required for identical runtime bytes on different participant computers.

The project is built with Solidity 0.8.30, via-IR, optimizer enabled with one run,
and the explicitly pinned Osaka EVM target. Changing any of these settings requires
rebuilding and re-running the checks. No code-size override is used on the Anvil node.

## Current custody build: 4 November

The [current size inventory](../ops/artifacts/custody-completeness-20261104/contract-sizes.json)
checks **134 deployable production contracts/libraries**, portable metadata,
EIP-170 runtime and EIP-3860 creation-object limits. No limit override is used.
The [custody acceptance](../ops/artifacts/custody-transfer-20261104/README.md)
passed 908 ordinary Solidity tests and real local DKG/Core transfer/exit.

| Contract | Runtime bytes | Runtime headroom | Creation bytes before arguments |
| --- | ---: | ---: | ---: |
| MultiPoolDeployer | 24570 | 6 | 24822 |
| BitcoinWithdrawalEscrow | 24349 | 227 | 25199 |
| ConfederationMultiPool | 20961 | 3615 | 22286 |
| BitcoinDepositRegistry | 19604 | 4972 | 20244 |
| MiningPoolV1 | 15103 | 9473 | 26659 |
| BitcoinCustodyTransfer | 12967 | 11609 | 17296 |

The gateway's dynamic source tuples also count toward its complete initcode.
The existing pre-signing gate checks actual constructor arguments; this table
does not replace it. `MultiPoolDeployer` has only **6 bytes** of runtime margin.
Historical tables below retain their original artifact snapshots.

## Historical ordinary build: 2 November

The [2 November size report](../ops/artifacts/zkspv/acceptance-20261102/contract-sizes.json)
checks **129 deployable production contracts/libraries**, portable metadata,
EIP-170 runtime and EIP-3860 creation-object limits. The complete local Solidity
runs passed 834 ordinary and 25 state/adapter tests. This does not qualify a
production graph or replace constructor-argument checks before signing.

| Contract | Runtime bytes | Runtime headroom | Creation bytes before arguments |
|---|---:|---:|---:|
| MultiPoolDeployer | 24510 | 66 | 24762 |
| WithdrawalFeeCredits | 24392 | 184 | 26452 |
| BitcoinFeeReserve | 24021 | 555 | 38566 |
| BitcoinAddressRegistry | 17060 | 7516 | 27864 |
| WithdrawalFeeFunding | 14513 | 10063 | 15416 |
| BitcoinKeyFamilyRegistry | 10706 | 13870 | 11315 |
| SimpleMultiPoolDeployer | 4445 | 20131 | 29738 |

Fee verifier pins use the ordinary Foundry build, including its exact normalized
relative remappings and metadata. The solc-js helper uses the same remappings.
Metadata changes affect runtime hashes and require regenerated pins. No runtime
hash check or EVM size limit is bypassed. The initcode boundary test uses the
actual deployer's CREATE nonce after construction of its bytecode parts.
[Local checks, fixes and qualification boundary](ZKSPV_LOCAL_ACCEPTANCE.md).

## Historical BTC fee reserve increment: 1 November

All **115** ordinary deployable project contracts/libraries pass the static size
and portable metadata checks. `BitcoinFeeReserve` has **24,232 bytes** runtime
and **49,020 bytes** complete initcode (48,796 creation + 224 static constructor
arguments), leaving **132 bytes** under EIP-3860. Its stateless funding/plan
verifiers deploy separately. `MultiPoolDeployer` retains its 66-byte runtime margin.
No limit override or mainnet deployment is used. See the [size snapshot](../ops/artifacts/zkspv/worker-integration/btc-fee-reserve-sizes.json)
and [complete encoded constructor check](../ops/artifacts/zkspv/worker-integration/btc-fee-reserve-encoded-size.json).

## Historical ordinary-build snapshot: 30 October

The 2026-10-30 static artifact snapshot covers **111 deployable project contracts/libraries**
with portable metadata and no runtime/creation-object size override. It does not
run E2E, prove runtime identity at a deployed address, or include constructor
arguments in creation-object sizes. The isolated ZK state/adapter build is a
separate artifact set. [Machine-readable snapshot](../ops/artifacts/documentation-sizes-20261030.json).

| Contract | Runtime bytes | Runtime headroom | Creation bytes before arguments |
| --- | ---: | ---: | ---: |
| MultiPoolDeployer | 24510 | 66 | 24762 |
| SimpleMultiPoolDeployer | 23851 | 725 | 24192 |
| FROSTCoordinator | 23433 | 1143 | 24119 |
| BitcoinAddressRegistry | 13672 | 10904 | 18512 |
| BitcoinDepositRegistry | 13454 | 11122 | 14052 |
| MiningPoolV1 | 12976 | 11600 | 24476 |
| BitcoinDepositDerivation | 3761 | 20815 | 3787 |

`MultiPoolDeployer` has only **66 bytes** of runtime headroom. Candidate address
components fitting these limits does not qualify or activate the address service.
Complete encoded constructor inputs still need the existing pre-signing gate.

The 2026-11-01 [recipient continuation](../ops/artifacts/zkspv/worker-integration/recipient-sizes.json)
checks **127** deployable project contracts/libraries under the same limits.
The tightest runtime margin remains `MultiPoolDeployer` at 66 bytes. The new
recipient components and their [complete constructor inputs](../ops/artifacts/zkspv/worker-integration/recipient-encoded-sizes.json) are:

| Contract | Runtime bytes | Runtime headroom | Complete initcode bytes |
| --- | ---: | ---: | ---: |
| BitcoinAddressRegistry | 15124 | 9452 | 26114 |
| RecipientPolicy | 5795 | 18781 | 5834 |
| RecipientWalletV1 | 1156 | 23420 | 1424 |

The registry's constructor creates its immutable policy; wallets are created
separately through that policy's canonical CREATE2 factory. Fitting deployment
limits is separate from operational and verifier qualification.

```sh
python ops/check_contract_sizes.py --output NEW_CONTRACT_SIZES.json
```

## Limits and historical 22 October coverage

[EIP-170](https://eips.ethereum.org/EIPS/eip-170) permits up to 24,576 bytes of
deployed runtime. [EIP-3860](https://eips.ethereum.org/EIPS/eip-3860) permits up to
49,152 bytes of complete initcode, **including ABI constructor arguments**, for
creation transactions and internal CREATE/CREATE2.

The archived 22 October inventory recorded 97 concrete contracts/libraries from
`contracts/src/` passing its artifact checks. Interfaces and abstract contracts have no deployable creation object.
Script and test harness bytecode is excluded: these harnesses execute locally;
the contracts they broadcast are checked separately.

| Contract | Runtime bytes | Runtime headroom | Creation bytes before arguments | Encoded initcode for documented constructor inputs |
|---|---:|---:|---:|---:|
| MultiPoolDeployer | 24,437 | 139 | 24,689 | 24,721 |
| SimpleMultiPoolDeployer | 23,789 | 787 | 24,130 | 24,194 |
| FROSTCoordinator | 23,433 | 1,143 | 24,119 | 24,151 |
| SimpleMiningPoolWithCalculator | 23,181 | 1,395 | 32,858 | 33,114 |
| BitcoinSigningBoard (plan + batched packet API) | 4,433 | 20,143 | 5,516 | 5,580 |
| MainnetHeaderRelay | 5,763 | 18,813 | 7,800 | 9,720 |
| Testnet4HeaderRelay | 6,143 | 18,433 | 8,232 | 10,152 |
| MainnetSPVGuard | 1,467 | 23,109 | 1,963 | 2,027 |

The Bitcoin signing-board row reflects the V6 batched CLI transport integration.
Its constructor remains `(address registry, uint256 maximumPacketBytes)`, so
64 bytes of arguments complete its creation input. That inventory is
[contract-sizes.json](../ops/artifacts/contract-sizes.json). The historical
[mainnet inventory](../ops/artifacts/mainnet-contract-sizes-20261018.json) predates V6 batching and testnet4.
The relay constructor contains ten fixed-length 80-byte predecessor headers and
an 80-byte checkpoint (1,920 encoded argument bytes); the guard has 64 argument bytes.

Plan publication is separately bounded by transaction gas. The board accepts at
most 16,384 plan bytes. A focused test with every storage word nonzero measures
11,698,716 gas for publication; the conservative call plus intrinsic/calldata
bound is 11,983,460, below Osaka's 16,777,216 transaction gas cap. This gas check
does not replace runtime/initcode checks. See the
[integrated board API and deployment guide](../web3/docs/bitcoin-signing-board.md).

The largest creation object is `SimpleMiningPoolWithCalculator`: it leaves 16,294
bytes for constructor arguments. The former script-only baseline reached 33,114
bytes. New maximum-argument deployments reach 49,151 bytes for a simple pool
with a 28,384-byte ID, and 49,138 bytes for a confederation pool with a 26,752-byte
ID. Joint token-name/symbol budgets and maximum 16-member DKG rosters are also
covered; see [the implementation report](UPGRADE_REVIEW_2026_10_18.md).
The artifact report is not a claim that arbitrary
unbounded strings/arrays will fit. Bootstrap checks the **encoded** constructor
transaction before gas estimation/signing. The on-chain factory creations are
subject to the node's normal EVM limits. That snapshot had 139 bytes of runtime margin on `MultiPoolDeployer`; the
current custody-build margin is 6 bytes as recorded above. Any further changes need the gate again.

The eight-section upgrade's machine-readable inventory is
[`upgrade-contract-sizes-20261018.json`](../ops/artifacts/upgrade-contract-sizes-20261018.json).
That earlier merged-version acceptance passed 4 Rust, 408 Foundry entries (405 tests and
three `testHelpersEnabled` getter checks), 557 Python and
13 E2E checks. Its bootstrap reached `VERIFIED` in 147 steps, with 46 runtimes and
38 top-level CREATE inputs checked. See the [current validation record](../ops/artifacts/upgrade-validation-20261018.json)
and [review](UPGRADE_REVIEW_2026_10_18.md) for exact coverage and remaining limits.
These counts describe the recorded upgrade run, not the subsequent Bitcoin CLI
transport changes. The extended board's focused suite passes 25 tests: 15
existing packet/deployment tests and 10 new plan/hash/gas tests.
`ops/check_contract_sizes.py` is part of `ops/local_acceptance.py`, which CI runs.
Boundary tests cover exactly-at-limit, one-byte-over, unresolved library bytecode,
and a constructor whose arguments push otherwise valid creation code over the limit.

## Deployment entry points and fixes

| Entry point | Purpose and checks |
|---|---|
| `frost-signer setup` + `bootstrap` | Resumable V6 graph for all nine independent EVM/Bitcoin combinations, with encrypted enrollment/DKG, participant approvals, escrow, fees and DAO. Follow [first DKG](FIRST_DKG_ONCHAIN.md), [networks and flows](NETWORKS_AND_AUTOMATION.md) or [Ethereum mainnet](ETHEREUM_MAINNET_RUNBOOK.md). |
| `DeployFederation.s.sol` | Explicit dispatcher for base, factories, separate project fee infrastructure, sequential payer permissions and one-time fee setup. Base binds DKG then renounces ADMIN_ROLE; it needs explicit wallets and no financial inputs. See STAGED_DEPLOYMENT.md for each stage. |
| `DeployDeploymentFeeRouter.s.sol` | Creates separate project authorization/vault/router from completed EVM_AUTH. Explicit deployStakerBps=0 sends the full deployment fee to treasury. |
| `DeployMainnetHeaderRelay.s.sol`, `DeployBitcoinSigningBoard.s.sol` | Explicit checkpoint/reorg/freshness policy; relay script deploys both raw relay and `MainnetSPVGuard`, which consumers must use. Board has immutable wallet/packet limits and approved public plans. Old packet-only boards require new deployment. |
| `frost-signer dkg btc-deploy-board` | Resumable canonical deployment against the participant's trusted registry; requires explicit `--max-packet-bytes` from 512 to 4096. Existing DKG keys are reused; new board-bound plans require new batch IDs. |
| `SeedDAOTestFeed.s.sol` | Validates explicit closed-day round arrays and skips matching existing values; allowed test networks only. |
| `DeployFederationGovernance.s.sol` | Deploys a governance wrapper, then separately registers it using a target-bound FROST operation. Registration discovers an identical existing entry without another transaction. |
| `DeployCustodianDAO.s.sol` | Deploys and wires the seven DAO contracts for an active confederation. Separate registration requires FROST and custodian approvals; repeat registration does not deploy a second DAO. |
| `DeployCustodianDAOIncomeAdapter.s.sol` | Deploys the requested source adapter and prints its pending configuration operation. Fixed rejection of `UnwrapFee = 3`; validates the `UNWRAP_FEE` source/stream pair. Source activation still requires the printed FROST operation. |
| `DeployDemo.s.sol`, `DeployDemoWithCalculator.s.sol` | Legacy local demos, now explicitly restricted to chain 31337. Corrected malformed example payout script to a structurally valid P2PKH script. These examples do not establish a real spendable Bitcoin custody wallet. |
| `ImportDistributionTable.s.sol` | Sample data importer, not a deployment script. Restricted to 31337 because it still uses sample entries and placeholder address decoding. |

The base infrastructure has no dependency on a router or deployment fee. Before
paid entity creation, finish the project authorization/router, authorize both
factories with consecutive FROST operations, configure each factory once, and
approve token allowance to the factory. This project deployment-fee policy
creates no settlement or DEPLOY_FEE vault source. Integration exemption is
explicit and limited to chains 31337 and 11155111 with zero router and fee.
An independent project DKG through the legacy staged CLI is supported on
Anvil/Sepolia. Ethereum mainnet V6 uses its completed federation EVM result;
standalone chain-1 infrastructure scripts do not provide a fourth ceremony.

For existing component addresses, use `ops/deployment_stages.py`: it validates
canonical creation inputs and constructor arguments as well as runtime and
bindings. Direct `forge script` reuse performs runtime/state checks but does not
prove canonical CREATE history; see [contract provenance](CONTRACT_PROVENANCE.md).
Chain-1 staged deployments require finalized receipts, minimum twelve confirmations,
explicit local gas caps and exact external-token identity. DAO/adapter leaf scripts
also require `EXPECTED_CHAIN_ID`; chain-1 DAO creation additionally pins token/feed
hashes and token decimals. See [staged deployment](STAGED_DEPLOYMENT.md).
Public Sepolia deployment with the exemption disabled has not been executed.
The paid deployments in the acceptance evidence ran on isolated local nodes.

## Reproduce

From the repository root, in the installed signer-agent Python environment:

```sh
forge build --root web3 --sizes
python ops/check_contract_sizes.py
python ops/local_acceptance.py
```

The full runner needs the pinned Rust/Foundry tools and Bitcoin Core 30.3 on PATH.
It starts isolated loopback Anvil/regtest nodes and stops them after the tests.
Foundry script E2E uses a temporary project copy, artificial identity 1 and separate
broadcast logs; it does not load the operator's `.env` or broadcast to a public network.

The script tests inspect transaction receipts, actual runtime and constructor input
sizes, factory permissions, completed DKG adoption, pool creation, governance/DAO
registration and repeat behavior. The complete bootstrap test inspects every runtime
in its 46-contract manifest and actual top-level creation inputs. This is local
deployment evidence; it is not an assertion that an existing public-network instance
has the same configuration.

## Historical deployment acceptance before this specification

The following counts and linked logs describe the earlier deployment work retained
from the input archive. They are not the results of the current eight-section
implementation; its results and before/after sizes are in the
[implementation report](UPGRADE_REVIEW_2026_10_18.md).

The full isolated runner exited **0**: **4 Rust tests, 202 Foundry entries,
36 Python tests and 11 E2E scenarios passed**, with zero failures or skips.
E2E duration: **737.32 seconds (12 minutes 17 seconds)**. The 202 Foundry entries
include two inherited getter checks, as explained in the earlier acceptance report.
Warnings are existing compiler/linter notices, a Python dependency deprecation and
ABI event-filter notices; they are not omitted failing tests.

The full bootstrap reached `VERIFIED` after **146 steps**, with **46 contracts**.
All 46 runtimes and **38 top-level CREATE inputs** in its journal passed the limits.
The separate script scenarios verified actual broadcasts, including governance and
DAO registration, idempotent repeat registration, both local demos and the unwrap
adapter. Wrong-chain and missing-fee preflight failures consumed no deployer nonce.
The three targeted script scenarios also passed separately before the full run.

Evidence:

- [Full runner output](../ops/artifacts/deployment-full-acceptance.txt)
- [Full E2E JUnit](../ops/artifacts/deployment-lifecycle.xml)
- [Deployment validation, sizes and source hashes](../ops/artifacts/deployment-validation.json)
- [Targeted script JUnit](../ops/artifacts/deployment-scripts.xml)

ABI drift and `git diff --check` passed. Temporary test nodes were stopped by the
runner. No public-network deployment was performed.

## Withdrawal fee profile

The immutable reserve creates `BitcoinFeeReserveUser` as its first child (CREATE nonce 1); native provenance proves the exact parent constructor. Fixed delegation shares `BitcoinFeeReserveStorage`; there is no setter or arbitrary target. `ops/generate_fee_runtime_hashes.py` pins stateless verifier runtimes: build, generate, rebuild, then `--check`. `SimpleMultiPoolDeployer` uses two immutable STOP-prefixed `CreationCodePart` blobs and verifies the joined creation hash before canonical CREATE. Its first pool nonce is 3. Check actual returned blob lengths (half of SimpleMultiPool creation bytes + STOP), as the empty compiled blob runtime is not the deployed runtime. Encoded constructors, ordinary runtime/initcode caps and measured isolated monetary/publication gas are recorded in the withdrawal subsidy evidence. No size/gas limit override qualifies this profile.
