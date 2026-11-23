# FROST Signer Agent

After bootstrap, use [`frost-signer nft`](../docs/NFT_CONFIGURATION.md) for
managed collection creation and mint through typed FROST approval. Each signer
uses `dkg sign-evm --nft-config FILE` with its independent pin. NFT fee previews
are read-only; only approved pool contracts consume quota for their caller.

For a fresh Linux/macOS/Windows-WSL2 host, use the [cross-platform runbook](../docs/CROSS_PLATFORM_QUICKSTART.md)
and `python ops/bootstrap_host.py build/check` from the repository root.
Native Windows is rejected before importing POSIX signer state; use WSL2.

Use `setup invite --ulr-policy FILE` for the [shared ULR bootstrap](../docs/ULR_BOOTSTRAP.md):
create the fixed 10m ULR/common staking graph once, then reuse its pinned roots for
additional confederations. The invitation commits relay/ZK selection. Bootstrap enrolls
each treasury, binds candidates and creates delegated voting and its funded fee route.
For the full Bitcoin graph, prepare `frost-signer system-profile` and pass
`setup invite --system-policy FILE`. It requires the family-aware Bitcoin profile
and adds the address request board, signing/incident boards and fee reserve to
the same journal. Bitcoin mainnet and testnet4 each support relay or their own
pinned ZK guest/state/adapter; regtest supports relay. See the
[testnet4 + Sepolia runbook](../docs/ZK_TESTNET4_SEPOLIA.md).
Choose the four issued ERC20 names/symbols before enrollment with
`system-profile --token-metadata FILE` or `setup invite --token-metadata FILE`;
[schema and example](../docs/TOKENS.md#имена-и-тикеры-до-запуска).

Supply `--bitcoin-custody-policy FILE` to new `system-profile` runs. This
selects custodian-only confederation Bitcoin/EVM DKG and, when source graphs
are enrolled, the `custody-transfer` owner workflow. See the
[complete runbook](../docs/BITCOIN_CUSTODY_TRANSFER.md). Old signed profiles
retain their old roster and are not upgraded by installing this SDK.

`frost-signer ulr-treasury` connects the [common ULR staking and treasury route](../docs/ULR_TREASURIES.md):
prepare/propose/execute shared historical wallet+stake snapshots, explicitly
enroll confederation treasuries, redirect their fee routers and pay the fixed
daily formula. Participant `dkg sign-evm --ulr-config FILE` uses the reviewed
shared graph and rechecks the typed intent before private work. Writes simulate
unless explicitly sent with the existing local identity and durable EOA journal.

Current local acceptance (2 November): [results, native host and reproduction](../docs/ZKSPV_LOCAL_ACCEPTANCE.md).
All 3269 local Python tests passed without exclusions or skips. The real combined
Core/FROST/Groth16/EVM contour is deferred for disk capacity. Address and fee-purpose
qualification gates remain disabled. Historical reports retain their own source snapshots.

New deployments: [first DKG using on-chain transport](../docs/FIRST_DKG_ONCHAIN.md), [Bitcoin mainnet setup](../docs/BITCOIN_MAINNET_RUNBOOK.md), [earlier upgrade validation](../docs/UPGRADE_REVIEW_2026_10_18.md). Start here when participants have only code and each other’s addresses. Legacy import/demo instructions below are separate paths.

The agent uses `FROSTCoordinator` as the shared on-chain registry. It needs no separate WebSocket coordinator or bulletin-board service.

## ZK-SPV and address candidates (1 November 2026)

See the [current architecture and diagrams](../docs/ZK_SPV_ARCHITECTURE.md) for
configurable relay/ZK backends, encrypted SPV requests, V4 funding/archive code
and the unactivated address registry. [Worker](../docs/ZK_SPV_WORKER.md) and
[automation/archive](../docs/SPV_AUTOMATION_ARCHIVE.md) cover the running code
paths; their combined live E2E remains pending. Explicit address mint commands
now use generation-aware admission and a separate `--mint-only` graph pin.
The [explicit address Bitcoin signer](../docs/BITCOIN_ADDRESS_SIGNING.md) connects
V3 BASE/DERIVED inputs, current generation/provenance checks and common journal
recovery. The [address request/watch service](../docs/ADDRESS_REQUESTS_WATCH.md)
adds encrypted delivery, durable ACK, raw-block rescan and public-binding recovery.
The [address registration workflow](../docs/ADDRESS_REGISTRATION.md) prepares
typed public batches, verifies requester commitments at every signing boundary
and recovers exact execution through the existing EVM_AUTH quorum.
The [address SPV worker](../docs/ADDRESS_SPV_WORKER.md) connects registered
intents to explicit V5/V3 consumer/watch routes and automatic admission/quote/payment.
Full operational qualification remains pending. Default legacy enrollment still refuses the new graph.

The [integration status and remaining gates](../docs/ZK_SPV.md) describe this
partial implementation; production ZK-SPV activation remains blocked.

Ordinary mint requires 6 confirmations in the current configuration; coinbase
mint requires 100. The client reads the enrolled ordinary depth from the common
registry and preserves the fixed coinbase rule. All existing
`gettxout`, canonicality, inclusion, exact allocation and backing checks remain.

New bootstrap graphs deploy and threshold-bind `BitcoinMintAdmission`. Its
canonical pool-nonce slot is claimed atomically with session creation. Another
participant can discover the same immutable public request and resume it without
the original operator's transport permissions. Explicit retries preserve the
original deadline and all previous sessions, receipts and spent-nonce journals;
each claim permits at most eight attempts. Existing approvals remain executable
after that deadline. A threshold operator transfer lets a replacement operator
open new requests. Unbound pools from the current reviewed build retain the
separate local retry path. Older immutable bytecode requires its qualified
client and migration assessment; exact artifact checks do not accept it merely
because its selectors match.

Use the normal `dkg btc-mint-propose` and `btc-mint-execute` funding arguments.
`btc-mint-retry` explicitly requests a new bounded attempt using the same terms.
The public admission address selects the signing and maintenance source:

```bash
frost-signer dkg sign-evm --mint-admission ADMISSION --session SESSION [participant options]
frost-signer dkg btc-mint-status --mint-admission ADMISSION [participant options]
frost-signer dkg btc-mint-transfer-propose --mint-admission ADMISSION --new-operator ADDRESS --deadline UNIX_SECONDS [participant options]
frost-signer dkg btc-mint-transfer-execute --mint-admission ADMISSION --session SESSION [participant options]
```

For the candidate address graph, use `btc-address-mint-propose`,
`btc-address-mint-execute` and explicit `btc-address-mint-retry` with
`--intent-id`, `--txid`, `--vout`, the original `--deadline` and `--mint-admission`.
There is no recipient/amount override. See [enrollment and signer requirements](../docs/ADDRESS_DEPOSIT_REGISTRY.md#mint-client-and-admission).
The candidate verifier is still unqualified; these commands do not issue Ready addresses.

The [admission scope and recovery rules](../docs/BITCOIN_MINT_ADMISSION.md)
describe explicit retirement, permitted external legacy races and remaining
address-deposit queue/Ready gates. This is a local SPV integration, not production
qualification of the address-deposit or ZK-SPV backend.

New bootstrap graphs deploy and threshold-bind `CallbackPoolRegistry` before
withdrawal use. Operators and Bitcoin signers check its canonical constructor,
deployer origins, exact pool/share-token code and bindings, and exact withdrawal
state before monetary authorization or a Bitcoin nonce. A pool's own getters are
insufficient. Existing immutable deployments and live signed obligations require
an explicit migration assessment; rebuilding the client does not upgrade them.

The public funding inspector uses the existing pinned rust-bitcoin 0.32.7 codec:

```bash
frost-signer bitcoin inspect-funding --raw-file funding.hex --txid DISPLAY_TXID
```

It preserves full raw and canonical stripped bytes, validates the expected txid,
distinguishes wtxid, and accepts `FUNDING_STRIPPED_100K_V1` transactions up to
100,000 non-witness bytes, including more than 256 inputs/outputs. Its full-raw
resource bound is 4,000,000 bytes; the 400,000-WU relay policy is not imposed on
already included funding. This inspection is neither SPV nor UTXO validation.
It does not relax the legacy SMP1 32-output limit or the payment-plan limits.
Native derivation/per-input crypto and the candidate address registry/mint/status
contracts are implemented with separate focused evidence. Their full EVM gas,
live Bitcoin signer/watch/recovery and independent profile qualification remain
pending; see [the registry boundaries](../docs/ADDRESS_DEPOSIT_REGISTRY.md). No address is
issued as Ready by this command.

## DKG-bound signing flow

```text
any configured participant
  -> createSessionFromDKG(completed EVM_AUTH ID, exact target helper hash, target, deadline)
  -> coordinator loads the original roster, threshold and registered public-share points

threshold participants
  -> submitNonceCommitments(D, E)
  -> submitVerifiableSignatureShare(z_i)

any session participant
  -> aggregate verified shares
  -> finalizeSession(Rx || z, operationHash)

any relayer
  -> DKGAuthorization.authorizeOperation(sessionId, operationHash, target)

bound target
  -> consumeOperation(operationHash) as part of the privileged call
```

The completed DKG result is immutable and the session enforces the canonical verifier and a contract target. `DKGAuthorization` also checks the exact registry/session/result. No participant is required to act as a permanent creator or aggregator. The legacy `createSession` + explicit share-registration flow remains separate; it cannot authorize new DKG-bound deployments.

`FROSTAuthorization` independently checks the finalized session's coordinator, canonical verifier, group key, ordered participants, threshold, verified-share count, operation hash, target and aggregate signature.

## Legacy scalar-import installation

For the recommended distributed DKG/V6 invitation path, follow the repository-root installation in [FIRST_DKG_ONCHAIN.md](../docs/FIRST_DKG_ONCHAIN.md). The commands below instead run from `signer-agent/` and configure the retained legacy scalar importer.

Python 3.11 or newer is required. Use a current security patch release.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary :all: -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e '.[test]'
cp .env.example .env
```

Required values:

```dotenv
EVM_RPC_URL=...
EVM_SENDER=0x...
EVM_PRIVATE_KEY=...
FROST_COORDINATOR_ADDRESS=0x...
FROST_ABI_PATH=./contracts/FROSTCoordinator.json
FROST_DEPLOYMENT_BLOCK=...
FROST_SIGNING_SHARE_HEX=...
SIGNER_STATE_KEY=...
SIGNER_STATE_PATH=./.state/signer-state.enc
```

The signing share is the raw 32-byte Shamir scalar. Its public point must equal the share registered for `EVM_SENDER` in the session.

The legacy scalar importer does not normalize group-key parity. The new `dkg` path uses library DKG, records its Taproot transform and normalizes the complete result consistently; see [DKG_PROTOCOL_V1](../docs/DKG_PROTOCOL_V1.md). The dated [strict review](../docs/STRICT_REVIEW_2026_10_05.md) describes the earlier scope and is not an audit of this adapter.

## One-Shot Operation

The legacy approval commands below are for local Anvil. Public network signers
use typed `dkg sign-evm` after enrollment. See the
[signing protocol](../docs/SIGNING_PROTOCOL.md).

```bash
frost-signer doctor
frost-signer pending --json
frost-signer run-once
frost-signer run-once --approve-all
frost-signer run-once --reject-all --reason "policy mismatch"
frost-signer decide 42 approve
frost-signer decide 42 reject --reason "unexpected payout"
frost-signer finalize 42
frost-signer relay 42 0xFROSTAuthorization 0xTargetContract
```

FROST has two rounds. Re-run one-shot mode after the threshold commitments or shares arrive. `AUTO_FINALIZE=true` lets this participant finalize when all required verified shares are available.

`relay` refuses non-finalized or non-enforced sessions, verifier overrides, target mismatches, coordinator mismatches and group-key mismatches. It does not accept or submit an arbitrary aggregate signature; the authorization contract reads the finalized result directly from the configured coordinator.

Continuous polling is optional:

```bash
frost-signer watch --interval 30
```

## Nonce Safety

- Secret nonce pairs are encrypted and fsynced before D/E points are published.
- Restarts recover the original nonce for an existing commitment.
- Accepted shares replace nonce secrets with a consumed tombstone.
- Missing local nonce state blocks signing; the agent never regenerates a nonce for an existing commitment.
- Chain ID, coordinator, session, group key, operation hash, participant order and commitments are transcript-bound.
- Odd aggregate nonce Y is normalized deterministically to `-R` by every participant and the contract.

## Verification

```bash
pytest -q
python -m py_compile signer_agent/*.py
```

Current distributed DKG, contract, Python and EVM/Bitcoin integration status is recorded in [V6 acceptance](../docs/PARALLEL_NETWORKS_ACCEPTANCE.md). The [18 October upgrade review](../docs/UPGRADE_REVIEW_2026_10_18.md) and [earlier acceptance report](../docs/ACCEPTANCE_REPORT.md) are historical snapshots. Run the complete suite from the repository root with `python ops/local_acceptance.py`. Registry point validation remains distinct from local library VSS verification. The V1 bootstrap, token proof settlement, fee routing and operations API are described below. Production custody/recovery and independent audit remain separate release requirements.

## Distributed DKG participant (experimental)

Invitation discovery searches the most recent 8192 blocks by default. Use
`setup discover --recent-blocks` or `--from-block` for older invitations;
`--from-block 0` requests the full history. A `NOT_FOUND_IN_RANGE` result is scoped
to the returned block range. Pass the selected result's `block` as
`--board-creation-block` to `setup join-chain`, `status`, `assemble-chain`,
`manifest-chain` or `pending` to avoid repeating creation discovery. This locator
does not replace constructor, canonical receipt, runtime or finality checks.
See [historical RPC requirements](../docs/CONTRACT_PROVENANCE.md).

The agent provides `frost-signer dkg transport-init/create/run-once/status/export/import/verify/sign-evm/btc-commit/btc-sign`. Recommended onboarding uses `setup invite/publish/discover/join-chain/assemble-chain` and required explicit V6 economic/Bitcoin policy. Public DKG packages and HPKE envelopes travel through `DKGOnchainBoard`; each participant retains its own secrets. Build the pinned `frost-crypto` crate first. See [DKG_RUNBOOK](../docs/DKG_RUNBOOK.md), [SIGNING_PROTOCOL](../docs/SIGNING_PROTOCOL.md), and [current acceptance evidence](../docs/PARALLEL_NETWORKS_ACCEPTANCE.md). The new commands use local credential files, not imported secrets in a shared .env. The legacy signing CLI remains for compatibility and is not the new bootstrap profile.

### V1 operational commands

`frost-signer bootstrap plan|apply|resume|verify` runs the durable public deployment
journal. `frost-signer api` starts the authenticated loopback service;
`frost-signer ops prepare|execute|status|view` operates prepared tasks with a separate local
EVM identity. `frost-signer bitcoin checkpoint|proof|relay|settle|broadcast|confirm` connects
Core proofs and the EVM settlement boundary. Full flags and state separation are
in [Bootstrap](../docs/BOOTSTRAP.md), [Operational API](../docs/OPERATIONAL_API.md) and [Bitcoin withdrawals](../docs/BITCOIN_WITHDRAWALS.md).
FROST shares remain exclusively in participant CLI processes.

For payment coordination without files, use `frost-signer dkg btc-deploy-board`,
`btc-prepare`, `btc-propose`, `btc-execute`, `btc-publish-plan`, `btc-pending`,
`btc-status`, `btc-commit`, `btc-sign`, `btc-broadcast`, and `btc-settle`.
For mainnet readiness and bounded relay synchronization, use `btc-network-status`
and `btc-relay`; they obtain network, policy, raw relay and guard from the pinned
on-chain registry graph, without a shared policy file. The standalone
[`frost-relay-keeper`](../docs/RELAY_KEEPER_AND_GAS.md) uses a separate gas identity
and persists authenticated origin evidence in `<identity>.relay-provenance.enc`.
Its optional `--relay-creation-block` avoids old-state discovery on first start;
restarts keep runtime, canonical-anchor and network-readiness checks.
The [payment runbook](../docs/BITCOIN_ONCHAIN_RUNBOOK.md) covers both approvals and
each participant's commands. Only public nonce commitments and signature shares
are published; long-term signing shares and secret nonce scalars stay local.

## User DAO commands

`frost-signer dao --help` provides typed wallet/staking/reward/governance commands.
Follow [the Russian step-by-step DAO runbook](../docs/DAO_CLI.md) after bootstrap.
It discovers registered contracts from the on-chain board or a verified factory,
uses each user's own EVM identity, and simulates writes unless `--send` is supplied.

V6 supports all nine Ethereum mainnet/Sepolia/local × Bitcoin
mainnet/testnet4/regtest combinations. See [network selection and flow commands](../docs/NETWORKS_AND_AUTOMATION.md).
`flow dkg` advances enrolled roles the address belongs to; custody representatives
skip conf-EVM/Bitcoin with `NOT_A_PARTICIPANT`. Custodians hold independent
conf-EVM and Bitcoin key shares; [role map](../docs/BITCOIN_CUSTODY_TRANSFER.md).
`flow bootstrap` continues
approved deployment steps. `flow payment` is restricted to an explicitly chosen
signing board/batch, and `flow relay` periodically synchronizes headers. Financial
FROST intents and custodian approvals remain explicit. Bounded packet/header
batches preserve the durable nonce and exact-byte retry rules.
Ethereum mainnet always requires paid factories, finalized state and local
budgets; public Bitcoin requires its reviewed checkpoint policy and own synced
Core/txindex. The [on-chain Bitcoin CLI](../docs/BITCOIN_ONCHAIN_RUNBOOK.md)
uses `BitcoinSigningBoard` for plans and public signing packets, then broadcasts
and settles payment proofs. This does not claim production custody readiness.
For project factories and the separate deployment-fee authorization graph, use
[staged deployment](../docs/STAGED_DEPLOYMENT.md).

### Native SPV gas treasury component

`python -m signer_agent.gas_treasury` prepares explicit native funding/claim requests and submits them through the shared EOA nonce journal. New funding is restricted to local qualification chain 31337 until the SPV state profile is qualified. See [bindings, commands and limitations](../docs/SPV_GAS_TREASURY.md).

Gas configuration now has enrolled `dkg gas-prepare-configure`, `gas-prepare-pause`, `gas-propose`, `gas-execute`, and `gas-status` commands. Participants approve the public on-chain intent with `dkg sign-evm`; configuration is restricted to local qualification chain 31337. The shared signer guard checks the configuration nonce and financial terms before private work. See [the governance workflow](../docs/SPV_GAS_TREASURY.md#пороговая-конфигурация).

`python -m signer_agent.gas_caller` polls reviewed canonical treasuries with explicit fee/gas caps, a durable global period budget and per-payee/epoch attempt limits. It checks actual balances, preserves signed transactions across restarts, and accounts for failed or zero-payout claims. See [caller configuration and recovery](../docs/SPV_GAS_TREASURY.md#ограниченный-автоматический-claim-caller) and `config/spv-gas-caller.example.json`. The complete production gas-funding gate remains UNKNOWN.

## BTC fee reserve and reconciliation

See [the reserve runbook](../docs/BTC_FEE_RESERVE.md) for the graph-scoped canonical allocations,
principal-preserving settlement, independent Core inventory and typed FROST commands.
Use `bitcoin fee-reserve` and the private `--fee-reserve-config` on participants.
Address service V2 pins the same policy without resetting watch or nonce journals.
Ready and operational qualification remain pending; long combined E2E is deferred.

Personal withdrawal fees: [BTC/MP/S consent, credit/watch, V4 plans, refunds and CLI](../docs/WITHDRAWAL_FEE_SUBSIDY.md). The purpose has its own qualification fence and uses the existing private nonce/spent/recovery namespace. Local component checks do not activate Ready.
