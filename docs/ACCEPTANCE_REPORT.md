# V1 lifecycle and first-use audit acceptance — 17 October 2026

Historical acceptance record for the revisions named below. V4 Bitcoin mainnet
setup is documented in the [mainnet runbook](BITCOIN_MAINNET_RUNBOOK.md). Current V3
regtest fields, upgraded contracts, test counts and deployment limits are
recorded in [the October 18 review](UPGRADE_REVIEW_2026_10_18.md) and
[current architecture](ARCHITECTURE.md); follow [first DKG](FIRST_DKG_ONCHAIN.md)
for a new setup. The earlier test results below have not been rewritten.

## Fresh GitHub clone verification — 17 October 2026

Verified executable source commit `232c44ff838d9f023f6d7c23a1c9ade4e5ca94d3` from a new clone of
`mrShizo/satoshify`, with a new Python venv and independent Solidity/Rust builds.
No `.env`, participant state or compiled artifacts were copied from the original checkout.
**4 Rust / 202 Foundry / 43 Python / 11 E2E passed**, zero failures/skips;
E2E case time total 757.01 seconds. The complete runner exited 0.
Evidence: [full output](../ops/artifacts/fresh-clone-acceptance.txt),
[JUnit](../ops/artifacts/fresh-clone-lifecycle.xml),
[structured validation](../ops/artifacts/fresh-clone-validation.json).

The clone review exposed and fixed two real first-use blockers:

- Foundry auto-remappings embedded absolute checkout paths in Solidity metadata,
  causing exact runtime checks to reject another participant's independent build.
  Explicit relative remappings now produce identical creation/runtime bytes for
  **all 91 deployable contracts** across two independently built directories.
  The size gate also rejects absolute paths in metadata. See
  [build comparison](../ops/artifacts/reproducible-builds.json).
- Invitations contained the operator RPC URL, potentially publishing provider
  credentials and inheriting a route unusable by friends. V2 invitations omit RPC;
  publish/assembly select local routes, and new publication of V1 is rejected.

The full 46-contract/146-step bootstrap now runs through the installed CLI from
three separate working directories, including discover/enroll, three DKGs,
FROST and custodian approvals, resume and verify. A further live first EVM DKG
used **two separate checkouts/venvs**: all three participants reached ACTIVE and
verified the same context using EVM HPKE transport, without sharing state files.
See [cross-clone result](../ops/artifacts/cross-clone-dkg.json).

The [first-use instructions](FIRST_DKG_ONCHAIN.md) now include tool/venv setup,
identity import, chain/gas setup, public status polling and final verification.
Tests ran on macOS/Anvil/Core regtest. Separate physical hosts, live Sepolia RPC
availability/funding/finality, and a production Bitcoin deployment were not
verified. Historical results below remain preserved with their original scope.

Subsequent verification: [contract sizes and deployment scripts](CONTRACT_SIZE_AND_DEPLOYMENT.md)
records the later successful full run: **4 Rust / 202 Foundry / 36 Python / 11 E2E**,
including actual script broadcasts and all 46 bootstrap runtimes. The evidence and
counts below preserve the earlier first-use audit run.

The four previously missing integration paths are implemented: resumable factory
bootstrap, proof-dependent MP/S escrow and burn, funded fees through DAO claim,
and the authenticated operational API/CLI. The supported executable environment
is **Anvil + Bitcoin Core regtest**, with explicitly labelled local USD/feed and
prefunded conversion fixtures. This is not a mainnet deployment or external audit.

The repeated isolated acceptance runner **exited successfully**: **4 Rust tests,
202 Foundry entries, 34 Python tests and all 9 E2E cases passed**, without failures
or skips. E2E took 696.98 seconds (11 minutes 37 seconds). Authoritative output:
[audit-full-acceptance.txt](../ops/artifacts/audit-full-acceptance.txt).
All temporary nodes stopped. The previous successful report is preserved as
[pre-audit-acceptance.md](../ops/artifacts/pre-audit-acceptance.md); the original
incomplete slice remains [phase1-acceptance.md](../ops/artifacts/phase1-acceptance.md).
An earlier audit run was intentionally interrupted to close an additional RBF
binding gap; the successful run above includes that fix and its regression test.

The follow-up closes first-use gaps: participants now discover a public invitation
from a known friend's EVM address, enroll their own transport keys and perform all
three ceremonies through `DKGOnchainBoard`. Round-two packets are HPKE encrypted.
No shared mailbox or off-chain file exchange is needed for this bootstrap.
The complete graph has **46 contracts and 146 journal steps**. Typed signing
intents and actual custodian approvals also use the on-chain/CLI path.
See the [point-by-point audit](TZ_AUDIT_2026_10_17.md) and
[first-use runbook](FIRST_DKG_ONCHAIN.md).

Further fixes bind the independent spent journal to encrypted nonce state,
reject missing/replaced journals, persist participant EVM transactions before
broadcast, pin DKG branch observations, diagnose terminal waits, validate ABI
ranges/types before queueing and require private file permissions. Authorized
Bitcoin fee replacement has fresh batch IDs/nonces, inherited obligations and
shared input reservations. The Core test replaces a 1,000-satoshi fee transaction
with a separately approved 1,500-satoshi version; proof settlement burns once.

## Input provenance and changes

The input checkout was already dirty: 32 modified tracked files and 8 untracked
files at HEAD `885a0a4ea6327befbcd7c40c3337a5f9e2d2aa53`. The original path/hash
inventory is [input-tree.json](../ops/artifacts/input-tree.json). Existing pool,
fee, TWAP, DAO and factory remediation belongs to that input. No reset, old-HEAD
replacement, deletion of user untracked files, commit, history rewrite, mainnet
transaction or Sepolia deployment was performed. No live `.env` was read.

The final per-file inventory is
[implementation-files.json](../ops/artifacts/implementation-files.json). It
separates modified user-provided files, other tracked files and new implementation
files. Test identities are artificial or freshly generated for ephemeral local
nodes; FROST shares are generated inside separate participant processes.

Changes preserve the existing coordinator/factory/pool/DAO stack:

- `DKGCoordinator` and `FROSTCoordinator` provide the completed result registry,
  canonical registry binding, enforced shares and automatic public-share loading.
  Rust uses pinned ZF FROST 2.2.0 and HPKE; EVM retains the existing transcript
  through an explicit scalar adapter. Bitcoin uses a separate BTC_SPEND ceremony.
- Both factories adopt completed EVM_AUTH ceremonies and deploy result-bound
  `DKGAuthorization` through their existing controlled deployer graph. Registry
  substitution is rejected. Governance and the complete DAO graph have actual
  registration workflows, with custodian approval where required.
- `bootstrap.py` and `bootstrap_plan.py` journal the full graph, roles, constructor
  bindings, hashes, receipts and external approval waits. Signed EVM bytes are
  persisted before broadcast; restart reconciles those bytes. Factory-produced
  contracts have pinned runtime and build-artifact hashes as well.
- `RegtestHeaderRelay`, `BitcoinDepositRegistry` and `MiningPoolV1` check real
  headers, proof of work, canonical inclusion/depth, vout/script/amount and
  coinbase maturity before issuance. Common outpoint consumption rejects reuse,
  reserved inputs and wallet change. A discovered byte-order bug in
  `BitcoinTxParser.flipBytes32` was corrected and given golden/fuzz coverage.
- Existing multipools now escrow S and net MP. `processWithdrawal` prepares the
  request. `BitcoinWithdrawalEscrow.settle` verifies the exact reserved payment,
  aggregates obligations sharing a script, preserves remaining Bitcoin principal,
  burns MP, and calls the pool to burn S and accrue unwrap fees atomically.
  Direct MP requests use the same registry. Only the registered mining pool's
  canonical token is accepted; reporting another poolDAO is insufficient.
- `PoolFeePipeline` claims real MP, converts against prefunded inventory and uses
  explicit WRAP/UNWRAP/DEPLOY streams. Nonexempt factories collect a real DEPLOY
  fee atomically with successful creation. Initial integration bootstrap chooses
  a visible, one-time zero-fee exemption to resolve the DAO creation dependency.
- The DAO report worker pins its EVM block and exact evidence bytes, checks funded
  source totals and historical TWAP, and prepares the actual settlement helper
  hash. Existing staking, rate, reserve, eligibility and rounding rules remain.
- The loopback API has bearer-hash roles, action/view allowlists, idempotency,
  base-unit string arguments, public manifests/status/events and financial reads.
  CLI preparation, eth_call simulation, separate execution and canonical reads
  share the same journal. It has no participant-key export/signing endpoint.
  The indexer retains orphaned logs and rewinds checkpoints without money actions.

The old unit pool tests now use an explicitly named `UnitEscrowFixture` to deliver
a payment callback. Assertions distinguish preparation from final processing.
Those fixtures do not stand in for Bitcoin acceptance: the separate regtest test
uses real relay, deposit registry, escrow, proof, token burn and fee/DAO contracts.

## Reproduction and observed verification

Tool versions: Foundry/Anvil 1.8.5, solc 0.8.30, cargo/rustc 1.89.0, Python 3.9.12,
Bitcoin Core 30.3. See [tool-versions.txt](../ops/artifacts/tool-versions.txt).
Python dependencies and Cargo dependencies are locked. Bitcoin release checksums
were checked; release signatures were not independently verified.

From the repository root, with those binaries on PATH and signer-agent installed:

```sh
python -m pip install -r signer-agent/requirements.lock
python -m pip install --no-deps -e signer-agent
python ops/local_acceptance.py
```

The runner executes locked Rust build/tests, `forge build --root web3 --sizes`,
ABI drift verification, all Foundry tests, Python tests and the complete
multi-process/regtest suite. It creates isolated loopback nodes and stops them on
exit. CI configuration in `.github/workflows/local-acceptance.yml` runs the same
command; that hosted workflow has not been dispatched by this task.

| Verification | Result / evidence |
|---|---|
| Original Foundry/Python baseline | 178 / 5 passed; [baseline Foundry](../ops/artifacts/baseline-forge.txt), [baseline Python](../ops/artifacts/baseline-python.txt) |
| Current Rust tests | 4 passed, including common EVM vectors and independent k256 BIP340 verification; [Rust](../ops/artifacts/rust-tests.txt) |
| New monetary boundary tests | 9 passed; three conservation/version fuzz cases each ran 256 inputs, plus common Solidity signature vectors; 3 on-chain board boundary tests also passed |
| Current full Python tests | 34 passed; [Python](../ops/artifacts/python-lifecycle.txt) |
| Final Foundry/size/ABI and nine E2E cases | 202 / size gate / ABI check passed; **9 E2E passed**, no skips; final runner output above |
| API preparation/execution/retry | Real HTTP → separate executor → real balance change, no duplicate transaction; [API execution](../ops/artifacts/api-execution.txt) |
| Public deployment/payment evidence | [lifecycle.xml](../ops/artifacts/lifecycle.xml), [public manifests, certificates and payments](../ops/artifacts/lifecycle-public-results.json) |
| CLI and public-artifact checks | [14 CLI smoke checks](../ops/artifacts/cli-smoke.json), [secret-field scan](../ops/artifacts/public-artifact-scan.json), `git diff --check` passed |

Foundry discovers two ABI functions named `testHelpersEnabled` as test entries
(the relay and a unit proof fixture). They are not counted as independent security
assertions. Current full Foundry output has 202 passing entries, including those
two getter calls. Runtime sizes are in [lifecycle-sizes.json](../ops/artifacts/lifecycle-sizes.json);
the closest EIP-170 margin is MultiPoolDeployer: 24,347 bytes, 229 bytes remaining.
Compiler lint and Python dependency deprecation warnings remain. Web3 also warns
when unrelated receipt events are discarded while extracting one named event;
the expected event count and resulting bindings are checked. The final E2E run
reported 91 warnings, with no failed test or hidden skip.

The end-to-end monetary scenario uses real proof-backed MP and distinct wallet
principal/treasury inputs. It pays 600,000 satoshis to P2WPKH, then 700,000 satoshis
to P2TR (including a direct request sharing the multipool request's script).
Each payment receives six confirmations before burn. Actual wrap/unwrap fees are
claimed, converted and split; the funded DAO day pays 50,000 local reward-token
base units to the staker. These numbers describe test economics, not a market
quote. A deliberate final Bitcoin reorg exercises RECOVERY_REQUIRED and broadcast
pause; it never remints tokens or permits a timeout refund.

## Requirement → implementation → verification

| Requirement | Implementation | Verification |
|---|---|---|
| DKG-01, DKG-02 | Library part1/2/3, encrypted local state, all-N result certificate | `test_dkg_processes.py`: real 2-of-3 and every one of ten 3-of-5 signing subsets |
| DKG-03, DKG-04 | One group-wide normalization; original participant indexes/public-share parity retained | Separate-process signatures; explicit library 16-of-16/parity/public-share test; common signing vectors cover both nonce parities and noncontiguous indexes |
| DKG-05–DKG-07 | Immutable on-chain broadcast/envelopes, receipts, phase deadlines, terminal abort and pinned branch observations | `DKGV1.t.sol`, `DKGOnchainBoard.t.sol`, `test_dkg_protocol.py`, `test_dkg_recovery.py`; malformed package, sender substitution, retry, timeout and reorg |
| SIGN-01–SIGN-03 | Canonical verifier, enforced shares, result/epoch/purpose/target binding | Real authorization tests, inherited critical tests, `CanonicalRegistryBinding.t.sol` |
| SIGN-04 | Shared public `signing-v1.json` fixture; Python transcript/rho/challenge, Rust scalar adapter, actual Solidity coordinator/verifier | `SigningGolden.t.sol`, `test_frost_math.py`, Rust shared-vector test, independent coincurve and k256 BIP340 |
| SIGN-05 | Persist-before-publish nonce state, ID-bound spent journal, exclusive lock | `test_nonce_recovery.py`, exact retry and reorg tests; cross-host cloning boundary below |
| DEPLOY-01 | Durable bootstrap and per-identity EVM nonce journal | `test_bootstrap_resume.py`; complete generated bootstrap with real external approvals in `test_full_bootstrap.py` |
| MP-01 | Legacy demo paths remain explicitly separate from V1 proof-only issuance | Inherited demo/PoolMpToken tests; V1 bootstrap never uses owner-mint MP |
| MP-02, MP-03 | Proof/depth/maturity, exact allocation, shared consumption and backing registry | Real regtest mint/replay rejection; `DepositConservation.t.sol` |
| BTC-01, BTC-02 | Independent Taproot signing, broadcast, canonical SPV settlement | `test_bitcoin_processes.py`: actual P2WPKH and P2TR payment plus MP/S burn |
| BTC-03 | Exact plan, prevouts, amounts/scripts, fee bound, change/backing policy | Altered plan rejection in regtest; Rust input/balance checks; conservation fuzz tests |
| BTC-04 | Aggregate all obligations for each script | Real two-request P2TR payment; insufficient shared-output rejection in `EscrowBoundary.t.sol` |
| BTC-05, BTC-06 | Irreversible Signing boundary, exact rebroadcast, proof replay guard, recovery pause | Real Core RBF/reorg/retry tests; no-refund-before-recovery and cross-version double-burn fuzz tests |
| FEE-01, FEE-02 | Actual token counters by wrap/unwrap; proof-time unwrap accrual; retryable manager reporting | Monetary lifecycle and inherited `PoolDKGRemediation.t.sol` |
| FEE-03 | Atomic claim → funded conversion → router → income vault | Real lifecycle, `FundedConverter.t.sol`, inherited funded DAO tests |
| FEE-04 | Nonexempt deployment fee in the same transaction as product creation | `test_confederation_bootstrap.py`: missing funding/allowance revert, successful single fee, duplicate rejection |
| GOV-01 | Canonical governance registry; signalling kept distinct from executable target approvals | Complete generated bootstrap; inherited governance execution tests |
| DAO-01–DAO-03 | Existing custodian snapshots, reserve voting and signer delegation rules preserved | `CustodianDAO.t.sol`: 8-of-11, future day, reserve shortfall, ownership and claim rights |
| DAO-04 | Historical TWAP with explicit rounds and pinned evidence | Existing TWAP/phase/stale-history regressions plus actual report worker in lifecycle |
| DAO-05 | Ordered funded settlement and real claim | Real token receipt in lifecycle; inherited eligibility/rounding/conservation tests |
| API/CLI and recovery | Authenticated preparation, separate executor, canonical reads/indexing, durable idempotency | `test_operations.py`, `test_operational_api.py`, `test_bootstrap_resume.py` |
| SEC-01 | Participant-local encrypted packages, sanitized errors, public API/journal boundary | Secret-field/API denial tests and public-artifact scan; local co-location is not independent-operator isolation |
| REG-01 | Full regressions, sizes, ABI generation and CI gate | Final isolated acceptance runner |

## Deployment and recovery boundaries

- No Sepolia RPC deployment was attempted. Example reward/feed addresses were not
  verified on an external RPC. The new operational engine permits only Anvil or
  Sepolia EVM IDs with regtest Bitcoin and rejects `backed` mode before submission.
- The regtest relay verifies its pinned consensus profile; it is not a mainnet
  difficulty implementation. Production markets, audited conversion/liquidity
  policy, actual feed history and independent participants are external setup.
- Replacement transactions require new FROST approvals and fresh Bitcoin nonce
  commitments. All versions spend the same input set and inherit the same requests;
  an ordinary bind cannot assign different obligations to a replacement. Any
  approved conflicting version may prove the payment, once. Invalidation followed
  by token refund is not implemented. A deep post-settlement reorg is an incident,
  not automatic reminting. See [Bitcoin recovery](BITCOIN_WITHDRAWALS.md).
- Restoring both encrypted signer state and its local spent journal from an old
  snapshot, or cloning both to another host, is outside the local anti-rollback
  guarantee. Missing or substituted journals now fail closed. Use independently current storage or reviewed key rotation. Full
  network-fault, maximum-load and independent custody audits are not claimed.
- DAO evidence hashes bind exact bytes. BTC TVL and provider weights remain
  explicit threshold-approved accounting inputs; a hash does not prove their
  economic truth. Local USD/feed and converter quote are test fixtures.
- [MIGRATION](MIGRATION.md) describes closing old obligations, preserving old keys,
  confirmed UTXO transfer, fee/day reconciliation and new proof-based deposits.
  No privileged balance-copy or fabricated legacy-to-V1 DKG migration was added.

[Bootstrap](BOOTSTRAP.md), [fee flow](FEE_FLOW.md), [API/OpenAPI](OPERATIONAL_API.md)
and [configuration](CONFIGURATION.md) document the operational interfaces.


## Additional first-use evidence

The full-bootstrap case recorded 36 on-chain DKG packets, including 18 HPKE
envelopes. Its test compared those payloads against 27 actual local test-secret
values and found none published. This checks the implemented carrier; it is not
a proof that a compromised participant cannot intentionally disclose its own key.
Public summaries are in [lifecycle-public-results.json](../ops/artifacts/lifecycle-public-results.json).

The persistent node launcher passed stop/restart with preserved Anvil block hash:
[local-nodes-smoke.json](../ops/artifacts/local-nodes-smoke.json).
During an incomplete real bootstrap, doctor correctly returned NOT_READY for the
incomplete graph and absent Bitcoin RPC configuration:
[doctor-incomplete.json](../ops/artifacts/doctor-incomplete.json).
