# V6, parallel bootstrap and bounded batches: acceptance

Status: **PASSED — local acceptance** on 22 October 2026. All final suites
passed on the current V6 working tree. The [machine-readable summary](../ops/artifacts/parallel-networks-final-20261022.summary.json)
records the Git base, uncommitted state, frozen-source hashes and coverage limits.
The earlier [V5 acceptance](ETHEREUM_MAINNET_ACCEPTANCE.md) remains historical evidence.

Implemented scope:

- V6 selects Ethereum mainnet/Sepolia/local independently from Bitcoin
  mainnet/testnet4/regtest. Public Bitcoin requires checked Core/checkpoint policy;
  Ethereum mainnet retains finalized state, paid factories, external assets and
  explicit local transaction budgets.
- Bootstrap creates all three independent DKG ceremonies before waiting. Only
  explicitly independent groups of at most 8 deployment/call steps can share a
  confirmation barrier. Dependent outputs stay blocked until canonical receipts
  and postconditions pass. Signed bytes and nonce reservations are durable.
- DKG transport accepts at most 16 packets and 16384 total payload bytes per
  batch. Share hashes and receipts/public-key shares have separate bounded registry
  endpoints preserving the original caller. Persisted HPKE envelopes, full-round
  validation and all-N result attestation are unchanged.
- Every EVM network now applies the enrolled confirmation depth to DKG mailbox,
  registry, bootstrap observations and custodian approval reads. Chain 1
  additionally requires finalized state. Bootstrap records read block hashes;
  resume and cross-journal recovery reject orphaned observations and previously
  recorded recovery failures before rebroadcast. Historical mutable conditions
  need not remain true after their authorized operation completes.
- Canonical `DAOTestUSD`/`DAOMockFeed` are available on local/Sepolia with all
  three Bitcoin choices. Public Bitcoin requires explicitly pinned external
  component addresses and finite converter funding. Chain 1 rejects these
  packaged test components for every Bitcoin choice. Mintable test USD and
  mutable mock prices are not economic backing.
- Bitcoin signing publications and relay headers use bounded batches. Testnet4
  has its own BIP94 relay. `flow dkg/bootstrap/payment/relay` continue only their
  explicitly selected operations; financial intent and custodian approval remain
  manual reviewed actions.
- V6 payment authorization compares effective network policies, including the
  legacy regtest/one-confirmation defaults. This removes a false wallet mismatch
  without rewriting pinned graphs or accepting network, depth, public-policy or
  wallet-origin differences.
- A full-run Sepolia `unstake` exposed insufficient gas after the next block or
  UTC-day transition. Fresh bootstrap/DAO transactions now receive the existing
  mainnet gas margin on all supported EVMs; mainnet caps and exact signed retry
  bytes remain unchanged. Deterministic tests cover next-slot/midnight inclusion.

Focused evidence already completed:

| Check | Result | Artifact |
|---|---|---|
| DKG Solidity batching and prior protocol/board suites | 19 passed | [Foundry JSON](../ops/artifacts/dkg-batching-solidity-20261022.json) |
| DKG batching, provenance and Ethereum policy regression | 194 passed | [Pytest XML](../ops/artifacts/dkg-batching-python-20261022.xml) |
| V6 network matrix: real enrollment/three DKGs and selected relay on each EVM×Bitcoin pair | 9 passed, 334 seconds | [Matrix XML](../ops/artifacts/network-matrix-20261022.xml) |
| Real on-chain batched DKG plus 3/2 and 5/3 process signing scenarios | 3 passed, 146.60 seconds | [E2E XML](../ops/artifacts/dkg-batching-e2e-20261022.xml) |

The new on-chain DKG scenario uses actual Rust HPKE/FROST and EVM contracts.
It interrupts after a mined publication, recreates the participant and completes
the same 3/2 ceremony transcript with **18 transactions, 6 per participant**
(formerly 11 per participant). Each of the three bootstrap roles has a separate
ceremony; 18 is not a total for all three DKGs. Repeating ACTIVE
does not consume another nonce. Public calldata is checked for private values.
The worst 16-packet/16KiB nonzero-payload contract fixture used 12,535,823 gas
including calldata intrinsic gas; the current 30% margin remains below 2²⁴.
A smaller operator cap still rejects that transaction before signing.

Final full-run evidence:

| Check | Result | Artifact |
|---|---|---|
| Rust cryptography and validation | 5 passed | [Rust report](../ops/artifacts/parallel-networks-final-20261022.rust.json) |
| All deployable Solidity suites | 460 passed, 46 suites | [Foundry report](../ops/artifacts/parallel-networks-final-20261022.solidity.json) |
| Complete Python suite | 1428 passed | [Python JUnit](../ops/artifacts/parallel-networks-final-20261022.python.xml) |
| Every collected E2E scenario | 47 passed, no failures or skips | [Combined JUnit](../ops/artifacts/parallel-networks-final-20261022.xml), [isolated execution and hashes](../ops/artifacts/parallel-networks-final-20261022.e2e-execution.json) |
| Canonical DAOTestUSD6 + DAOMockFeed8, full Sepolia bootstrap with mainnet and testnet4 BTC | 2 additional passed | [Test USD report](../ops/artifacts/testusd-full-bootstrap-20261022.json) |
| Runtime and initcode size limits | 97 project contracts pass | [Size report](../ops/artifacts/contract-sizes.json) |
| CLI documentation, links and diagrams | 182 CLI examples, 11 rendered diagrams pass | [Documentation QA](../ops/artifacts/parallel-networks-docs-20261022.json) |

ABI drift checks also pass. The narrowest runtime margin is 139 bytes
(`MultiPoolDeployer`, 24437 bytes). Each E2E scenario runs in a separate pytest
process with isolated local nodes/state; all 47 used one unchanged source snapshot.
Two test-only fixtures were then updated to use the canonical six-decimal test USD
and eight-decimal feed in Sepolia. Both affected public-Bitcoin bootstrap scenarios
were repeated from the exact updated test snapshot against unchanged production code;
the supplemental report records source hashes, pins, converter funding and DAO checks.
Earlier full-run failures exposed the gas-margin and payment-policy issues above;
the complete suites were repeated after those fixes and stale fixture corrections.

The final addresses-only CLI lifecycle completed both P2WPKH and P2TR payments,
including MP/share burn and settlement. Three separate participant directories
exchanged 18 HPKE envelopes and 24 public Bitcoin packets. Its public-output check
found none of the 39 extracted local test-secret values; nonce retries and
post-settlement reorg rejection also passed. This is specific regression coverage,
not proof that every possible information leak is absent.

No public funded deployment or Bitcoin mainnet/testnet4 payment is claimed.

The full test matrix distinguishes its evidence:

- Nine combinations use actual local EVM contracts, real Rust crypto, on-chain
  enrollment and three independent DKGs. All six test-EVM combinations deploy
  and mint canonical test USD; public-Bitcoin combinations also validate its
  explicit runtime/decimals pins. This matrix is not nine funded Bitcoin payouts.
- Four public-Bitcoin bootstrap variants exercise Ethereum/Sepolia chain IDs
  with mainnet/testnet4 historical Core adapters through the full generated plan.
- The funded Bitcoin payment lifecycle runs against actual Bitcoin Core regtest.
  Public-network scenarios use historical PoW data, not current public consensus.
- Separately, 4051 authentic testnet4 headers were replayed through Bitcoin Core
  30.3, including the 2016 and 4032 retarget boundaries; see the
  [Core consensus artifact](../ops/artifacts/testnet4-core-consensus-20261022.json).

The [network guide](NETWORKS_AND_AUTOMATION.md#расчётное-время-запуска-и-платежа)
contains latency estimates derived from sequential confirmation barriers, not
from accelerated local test runtimes. Mainnet bootstrap still takes roughly
38–40 hours before relay catch-up and human delays; a prepared mainnet payment
batch is roughly 2 hours 15 minutes with one relay group. Finality, Bitcoin depth,
manual financial approvals and independent participant checks remain required.

Deployment limits remain: trusted checkpoint history, RPC/consensus availability,
external token/oracle administrators, permissionless fixed-quote converter
inventory, local key/nonce-journal custody and on-chain metadata exposure.
These are internal tests and review, not independent audit sign-off. New V6
contracts require a new deployment; existing immutable contracts and journals
are not silently upgraded. Follow [networks and automation](NETWORKS_AND_AUTOMATION.md)
and the [mainnet runbook](ETHEREUM_MAINNET_RUNBOOK.md) for operation and migration boundaries.
