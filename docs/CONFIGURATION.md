# Configuration and readiness

[Параметры федерации/конфедерации и новые группы](FEDERATION_CONFEDERATION_SETUP.md) ·
[NFT скидок: параметры, роли и требования](NFT_CONFIGURATION.md).

For the current complete Bitcoin + ULR deployment, start with the
[cross-platform runbook](CROSS_PLATFORM_QUICKSTART.md) and
[launch data/schema map](LAUNCH_READINESS.md). `--system-policy` selects the
common ULR/staking/treasury graph; the following ZK contour templates are
separate operational inputs and do not replace its invitation or bootstrap.

For ZK, use the [participant distribution and per-host setup](DISTRIBUTION.md).
Each participant needs its own Core/RPC, reviewed worker config, identity, private
0700 worker directory and locally built tools. Chain/state, Bitcoin policy and
guest image agree across participants; private paths, local proof keys, caches
and transaction journals stay with their owner. Ready receipts and working
inputs/journals are not deployment inputs supplied by the distributor.
`prepare` / `prove` and default worker `run` compute locally without transactions.
The designated publisher explicitly uses `publish` or `run --boundary all` for
the service loop. The local prover has macOS arm64 and Linux/WSL2 x86-64
backends. Follow the [Linux prover setup](ZK_PROVER_LINUX.md) for pinned tools,
Docker and isolation probes. Linux ARM64 is unsupported for full proving.
Fresh-proof acceptance and physical WSL testing remain pending; qualification
flags stay false.

Current local checks and commands: [local acceptance](ZKSPV_LOCAL_ACCEPTANCE.md).
The real Core/FROST/Groth16/EVM contour is deferred for device disk capacity;
example configurations do not grant production readiness. Test host overrides
apply to tests; production `host_binary`/`host_sha256` remain explicit reviewed inputs.

[Combined contour and retained-graph handover](ZKSPV_CONTOUR.md) describe the
private controller plan and original epoch fleet configuration. The
[example controller plan](../config/zkspv-contour.example.json) intentionally
contains null driver slots; it cannot run or claim full acceptance unchanged.
The [multi-request suite recipe](../config/zkspv-contour-suite.example.json) instead
contains 43 typed workflow recipes plus 195 pinned mandatory observation slots
(238 matrix entries, up to 512 jobs). See [lifecycle bindings](ZKSPV_SCENARIO_BINDINGS.md).
Supply private worker/identity inputs, compile once and explicitly bind later
jobs after their dependencies and reviewed quotes/claims are ready. The
[fee operation example](../config/zkspv-contour-fee.example.json) adds personal
funding/token refunds/named BTC-refund creation. See the suite runbook for the
read-only collector, binary proof/envelope evidence and recovery boundaries.
The optional private [contour service configuration](../config/zkspv-contour-service.example.json)
connects encrypted request/ACK/status and admission/quote/payment to the worker
pipeline. `PAID` requires matching requester payment and issuer automation;
`CONFEDERATION` requires an own-funding worker policy and disabled requester
payment. It can operate when project treasury funds are insufficient, within
publisher balance and existing durable cost caps. The builder pins requester,
peer and service configurations; see the runbook for recovery and scope limits.
Optional [bootstrap](../config/zkspv-contour-bootstrap.example.json) and
[funding OBSERVE](../config/zkspv-contour-funding.example.json) or
[SMP1 accepted-quote funding](../config/zkspv-contour-smp1-funding.example.json)
configurations connect setup to the same runner. Bootstrap pins the generated
profile, original journal/identity, expected worker addresses and explicit gas/
cost caps. It checks graph bindings with freshness explicitly deferred. SMP1
funding requires the original operation ID and accepted quote hash; the builder
pins its deposit configuration. No plan can infer owner acceptance from an ACK
or create a fresh funding quote during recovery.
Optional [SMP1](../config/zkspv-contour-mint.example.json) and
[address](../config/zkspv-contour-address-mint.example.json) mint configurations
pin original participant/bundle/identity journals, exact admission and deadline,
and gas/job/total/attempt caps. SMP1 requires the original accepted funding quote;
address mode uses the frozen registered intent. `--mint-config` includes header
materialization; `--consumer-only` selects a partial existing-state mint pipeline.
No configuration can bypass the actual qualification/generation/recipient guards.

[Reviewed withdrawal](../config/zkspv-contour-withdrawal.example.json) configuration
pins the original owner/operator fee configurations, accepted owner quote and
exact V4 `terms_hash`, reviewed request/plan, separate BTC_SPEND and EVM_AUTH bundles,
and original Bitcoin participant state. V2 explicitly pins the previous reviewed
configuration for RBF; V3 additionally pins each owner/quote/request in a batch.
Neither layout renews consent or chooses a replacement implicitly. `--withdrawal-config` resumes preparation
and selects one BTC/MP/S settlement; `--settlement-only` selects a partial existing
family settlement. The reviewed version requires the SDK's existing request,
consent, UTXOs, fresh graph and funding/report prerequisites. It cannot be mixed
with bootstrap/mint/funding options. Optional
[reconciliation](../config/zkspv-contour-reconciliation.example.json) configuration
pins the original fee/EVM_AUTH context, Bitcoin signer state, current deadline
and explicit report ID. Both adapters preserve pauses and exact signed recovery;
they never create private FROST shares or renew consent/report windows implicitly.

[Incident contour](../config/zkspv-contour-incident.example.json) configuration
pins the original reporter/operator incident clients, EVM_AUTH bundle, private
report and accepted exact `status-preview` decision hash. `--incident-config`
creates a standalone partial incident/recovery plan, without monetary/Fresh
predecessors. The MiningPool must be in the worker allowlist. Original transport
keys, inboxes, identity nonce/cost journals and signer approvals remain required;
recovery service/fee configurations are pinned transitively. New generations,
deadlines, attempts or terminal-state recovery cannot be inferred by the runner.

[Old-epoch contour](../config/zkspv-contour-epoch.example.json) configuration pins
the original fleet, enrolled challenge and external retention attestation files.
`--epoch-config` creates a standalone partial observation/review plan. It pins
every address/fee policy and retained transport file hash, checks the challenge
in the original identity lane and never signs claims or renews expiry. At least
one SERVICING epoch and a worker-allowlisted matching MiningPool are required.
Fleet round-robin/watch/archive limits apply. SPV scheduling, payments, issuance
and rotation are not enabled by this adapter.

[Bitcoin continuity](BITCOIN_CONTINUITY.md) uses the original address service
configurations and existing identity/signing journals. Its baseline profile is
`ORIGINAL_BTC_ACCOUNTING_GRAPH_V1`; a new label/configuration does not import old
obligations or permit a different registry/verifier. The common bootstrap manifest
declares the required canonical inventory and unsupported cross-graph migration.

The private [Bitcoin epoch service configuration](../config/bitcoin-epochs.example.json)
selects up to 32 original wallet contexts, address/fee configurations and signing
boards, bounded scheduling/archive limits, retention-claim expiry and per-epoch
funding envelopes. `SERVICING` is irreversible locally and requires canonical
DRAINING/closed issuance before handover. See the [runbook](BITCOIN_EPOCH_SERVICE.md)
for enrollment and recovery without resetting signing journals.

`config/local.json` and `config/sepolia.json` are nonsecret configuration examples, not
successful deployment manifests. Empty contract maps are intentional. No fabricated
contract address or transaction receipt is supplied.

Optional ZK configurations are described in [bootstrap](ZK_SPV_BOOTSTRAP.md)
and the [current architecture](ZK_SPV_ARCHITECTURE.md): policy V2 supports
Bitcoin mainnet and testnet4 with separate pinned programs and state/adapter contracts,
worker template V4 adds permanent archive and optional issuer automation, and
transport template V2 adds an explicitly opt-in payer policy. The primary worker
and transport templates leave admission/payment automation disabled. Separate
opt-in policy fragments still require reviewed values and explicit enabling.
Confederation self-funding is
permitted within its own configured budgets. These settings do not enable the
candidate address registry, qualify its verifier or issue Ready addresses.

```
frost-signer doctor --profile local --json
frost-signer doctor --profile sepolia --json
```

The profile doctor does not read .env or private keys. It checks protocol, implemented
Bitcoin network, tool availability, configured EVM chain and optional runtime code hashes.
It returns NOT_READY when checks fail and READY_INTEGRATION only when the configured
integration checks pass. Public Bitcoin returns `READY_MAINNET_PROTOCOL` only after
its enrolled policy, own Core, full graph, DKG and current relay/guard checks pass;
this status is not production custody approval. For a step-based profile it verifies the bootstrap journal
and declared role/binding checks. Healthy RPC alone is insufficient. Price-feed
history and funded daily reports are validated separately at settlement.
For ULR, the doctor checks staking, snapshots, treasury, voting and the fee
pipeline instead of the legacy settlement. For a complete `system_bootstrap`,
it also checks the current system bindings and reports
`SYSTEM_QUALIFICATION_PENDING` while the verified manifest keeps `ready=false`.
It does not turn a configured graph into production readiness.

Public contract entries, when provided, are `{"address":"...","runtime_hash":"..."}`.
chain_id is a decimal string. Monetary values in Bitcoin plans are satoshi strings.
Outgoing Bitcoin signing plans fix N<=16, inputs<=16 and outputs<=32; external
funding uses a separate [100 kB stripped profile](BITCOIN_FUNDING_PROFILE.md),
without a blanket 256-input/output cap. For outgoing plans, exhaustive maximum-gas/load tests are
not complete. The supplied `local`/`sepolia` examples do not enable mainnet.
Generated V6 bootstrap selects Ethereum mainnet/Sepolia/local independently
from Bitcoin mainnet/testnet4/regtest. The [network matrix](NETWORKS_AND_AUTOMATION.md)
explains external assets, paid factory policy and consensus requirements.
[Release preparation](PRODUCTION_PREPARATION.md) selects Ethereum, Bitcoin and
backend independently, generates pair-specific templates and an offline review
package. Mainnet and testnet4 support relay or their distinct `zk-state-v2`
profiles; regtest supports relay. This gives 15 network/backend combinations.
The selected new-deployment intent does not replace empty-inventory review. Public
Bitcoin requires pinned checkpoint policy and each participant's synchronized,
unpruned Core with txindex. Ethereum mainnet always requires finalized receipts
and paid factories, including when paired with test Bitcoin.

The Sepolia reward-token and feed addresses are copied from the supplied specification.
They have **not** been checked against a selected RPC, code, decimals or feed description
in this work. Sepolia deployment was not attempted. Explicit economic policy choices,
funding, independent participants and production recovery remain external prerequisites.

Optional [managed NFT configuration](NFT_CONFIGURATION.md) is post-bootstrap:
`nft init` writes each signer's independent public chain/board/role/factory/manager
pin; create/mint terms are separate public intents. These are not `nft_*` keys
in the signed launch JSON. Mint authority stays with the governed fee manager.

Runtime secrets belong in participant-local mode-0600 files/volumes. The high-entropy
storage key is separate from the EVM identity. FROST shares are stored in encrypted state
indexed by the manifest context, which commits chain/registry/session/epoch/purpose/roster;
participant/index are checked metadata. Never share state/journal volumes between participants.

## Operations and lifecycle profiles

V6 `mode` is `integration` only for regtest on local/Sepolia; it is `backed`
when chain ID is 1 or Bitcoin is public mainnet/testnet4. Ethereum chain 1 always
requires positive paid deployment fees, an external reward token and no integration
exemption. A BTC/USD feed is additionally required by the legacy DAO profile;
the ULR/system route has no feed, TWAP, rate controller or legacy settlement. Relay selection follows the explicitly chosen Bitcoin network; it
never silently changes network. See [bootstrap](BOOTSTRAP.md) and the
[Ethereum runbook](ETHEREUM_MAINNET_RUNBOOK.md) for local gas/fee budgets.
API policy examples contain no usable bearer credential;
add SHA-256 hashes of independently generated high-entropy tokens and grant the
minimum role. Keep operator identities, participant state and API process access
separate. Monetary/ABI integer arguments are decimal strings.

The chain confirmation policy is `evm_confirmations`; manifests record canonical
receipt hashes. Anvil tests use one confirmation and deliberately exercise
recovery separately. New V6 invitations default to one confirmation on local/Sepolia; the
step-by-step Sepolia example explicitly chooses twelve. Choose the enrolled
depth deliberately and use your own RPC and verified deployment. No remote deployment is created
by the local acceptance suite. Ethereum mainnet requires at least twelve
confirmations **and** the `finalized` RPC block; missing finality fails closed.

## Legacy browser configuration

`js/config.js` does not ship an RPC provider credential. Set
`globalThis.SATOSHIFY_SEPOLIA_RPC_URL` before loading that file to supply your
browser RPC endpoint; the default is empty. Any credential delivered to a browser
is visible to its users. This setting is separate from the signer-agent profiles.

## BTC fee reserve and reconciliation

See [the reserve runbook](BTC_FEE_RESERVE.md) for the graph-scoped canonical allocations,
principal-preserving settlement, independent Core inventory and typed FROST commands.
Use `bitcoin fee-reserve` and the private `--fee-reserve-config` on participants.
Address service V2 pins the same policy without resetting watch or nonce journals.
Ready and operational qualification remain pending; long combined E2E is deferred.

## Personal BTC/MP/S withdrawal fees

[withdrawal-fee.example.json](../config/withdrawal-fee.example.json) is fee observation V2 with separate `fee_watch_databases`; preserve existing identity/transaction/spent journals. SPV worker V6 and transport V4 add `withdrawal_fees: {version:"1", registries:{MINING_POOL:FEE_CREDITS}}`. Their allowlist still authenticates the canonical MiningPool graph, and qualification of its distinct WITHDRAWAL_FEE purpose is mandatory. See [the owner/operation flow](WITHDRAWAL_FEE_SUBSIDY.md). Templates contain public placeholders and are not deployment authorization.
## Personal withdrawal bootstrap policy

`config/withdrawal-fee-bootstrap.example.json` is an opt-in public policy for
the common bootstrap. Supply its reviewed provenance/service commitments and
pass it to `setup invite --withdrawal-fee-policy`; all V6 enrollments commit it.
The example zero hashes intentionally fail validation. Without this policy the
existing bootstrap profile is preserved. Deployment exports fee bindings and
worker/transport route maps in the manifest, without credentials or local paths.
See [WITHDRAWAL_FEE_SUBSIDY.md](WITHDRAWAL_FEE_SUBSIDY.md#common-bootstrap).
## SMP1 wallet funding

`config/smp1-deposit.example.json` configures the owner's Core 30.x wallet RPC,
EVM pool/network, local journal and fee/input caps. Set its zero pool placeholder
to a proven deployment and keep the config/cookie private. It uses the existing
explicit Bitcoin checkpoint/finality policy on public networks. Credentials and
wallet routes are not public enrollment inputs. See
[automatic SMP1 preparation](BITCOIN_MINT_AND_TREASURY.md#automatic-smp1-wallet-preparation).

The [recipient matrix](ADDRESS_DEPOSIT_REGISTRY.md#recipient-matrix) uses the
canonical address registry's immutable `recipientPolicy`; address-service config
and the 382-byte intent schema do not change. Common opt-in bootstrap exports
`recipient_policy` in its manifest/bindings. No mutable URL/allowlist supplies
wallet authority. If an SMP1 pool has no address registry, use SMP1 config version
`"2"` with the otherwise identical V1 fields plus `"recipient_policy": "CHECKSUM_ADDRESS"`
for a policy deployed with the exact canonical constructor. V1 still supports
ordinary recipients and contract selections through an available registry's
policy. Contract selections require a separately reviewed local choice file;
never replace an existing transaction/cost/watch journal to retry selection or
wallet actions. The private owner config must use the wallet's immutable owner
identity for transfer/approve/direct-withdrawal commands.

## Incident reporting and status decisions

Use [incident-service.example.json](../config/incident-service.example.json) with
the canonical manifest's `incident_board`, the existing identity/transaction/cost
journals and a separate private inbox directory. The public DKG board is supplied
separately as `--registration-board`. Transport keys use the existing private key
file format, retaining old epochs. Each signing participant must provide its own
`dkg sign-evm --incident-config FILE`, receive the report and explicitly approve
its exact review hash. A public hash alone does not permit private signing.

`recovery_service_config` defaults to null: restriction/reporting remains available,
while resume requires this graph's existing address-service config, Core access,
complete registered watch coverage and applicable reserve reconciliation. The
incident client does not reset existing recovery pauses or qualification flags.
[Commands and recovery](ADDRESS_DEPOSIT_REGISTRY.md#incident-and-status-client).
