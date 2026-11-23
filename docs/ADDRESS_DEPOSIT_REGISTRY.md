# Address registry, common accounting and issuance fences

[Registry and status diagrams](ZK_SPV_ARCHITECTURE.md) show this candidate next to
the selectable Bitcoin backends, the mint-only client and the remaining Bitcoin signer integration.

Status: **CODE_IMPLEMENTED_QUALIFICATION_PENDING; Ready disabled**.
This is a candidate contract contour for a **new** deployment. It is not a
qualified address service, a migration of existing obligations, or production
activation. Existing bootstrap configurations keep their legacy mode.

## Immutable graph and registration

`BitcoinAddressRegistry` pins the common `BitcoinDepositRegistry`, escrow,
batch registry, completed BTC_SPEND result/epoch/base, existing DKG-bound
EVM_AUTH authority, Bitcoin genesis, verifier/profile/runtime/source and
qualification commitments, and service policy. It verifies reciprocal graph
bindings and exact BTC/EVM DKG roster/threshold compatibility. Graph/scope IDs
are computed from that graph; callers cannot invent alternate scope labels.

The common registry accepts a one-time `setAddressRegistry` under its existing
threshold authority only at nonce zero, before any base registration and
therefore before issuance. It cannot install/replace a policy over an already
registered graph. There is no automatic import of balances or obligations.

`registerBatch(bytes[] intents, cg, dg, deadline, sequence, op)` accepts 1–8
canonical 382-byte intents. The operation binds the full ordered batch, graph,
derivation scope, both generations, deadline and registration nonce, inside
the usual chain/auth/target/selector FROST domain. Each intent must match the
exact chain, address registry, pool/token, graph, DKG result/epoch and policy.
The fixed verifier computes its descriptor; the caller supplies no output key.
Requester+request nonce cannot be rebound to a different intent.

The record retains the full public intent, requester/recipient, pool/token,
descriptor, script hash and validation provenance. Provenance binds the exact
verifier/profile/runtime/source/qualification, parent wallet, intent/binding,
registration block and parent-block anchor. It never overwrites the beneficiary
or prior validation. Registration reveals the recipient in this ECC profile.
The [request/watch transport](ADDRESS_REQUESTS_WATCH.md) and
[FROST registration client](ADDRESS_REGISTRATION.md) authenticate V2 requester
commitments and public typed intents before private signing; their operational
qualification remains pending.

Base and derived scripts share `scriptPools` in the common registry. Only the
fixed address registry may add derived scripts. Conflicting reuse, including a
base-script alias, fails atomically. A failed batch rolls back all records,
nonce and authorization consumption. An exact accepted operation returns its
historical receipt even after pause/expiry, without granting current permission.

## Recipient matrix

This additive profile applies to new immutable deployments. It does not retrofit
existing registries or change the low-level SMP1/coinbase monetary rules.

| Explicit profile | Acceptance and limits |
| --- | --- |
| `ordinary` | Nonzero checksummed 20-byte address with no code at the checked snapshot and outside the canonical system sinks. The requester may select their own address or a third party. This proves neither ownership nor future key/code availability. |
| `wallet-v1` | Deployed `RecipientWalletV1` from the canonical `RecipientPolicy` factory, exact runtime and recorded immutable EOA owner. Owner-only calls support actual MP transfer, escrow approval and direct withdrawal. ERC-1271 uses the pinned OpenZeppelin ECDSA implementation. |
| `advanced-contract` | Explicitly unverified third-party contract reception. Confirmation binds chain, namespace, policy, pool, token, requester, recipient and outer runtime hash, including the risk that MP can be permanently inaccessible. There is no verified exit/control guarantee. |
| Undeployed/counterfactual wallet, EIP-7702 delegated account, other claimed wallet/proxy profile | Unsupported as a verified wallet. Delegated accounts and undeployed contracts are rejected in both contract profiles. An ordinary no-code address makes no assertion about counterfactual deployment. A deployed unknown contract/proxy is eligible only for explicit unverified advanced reception. |

`RecipientWalletV1` is a direct, nonproxy wallet: no upgrades, modules, delegatecall
or owner replacement. This is the only supported smart-wallet implementation;
Safe and other wallets are not implicitly qualified. Public CREATE2 creation is
idempotent for the same owner/salt; another caller cannot take control of it.
The owner can call contracts with `execute` and receive ETH. No recipient
callback or new ERC-20 receiver interface is imposed on `PoolMpToken`.

Each new registry creates its immutable `RecipientPolicy` at CREATE nonce 1.
Clients verify exact runtime and canonical direct policy creation, or the
registry's exact constructor and child address. A wallet's factory creation
record, runtime hash and owner must agree. Runtime or ERC-1271 success alone is
not proof of wallet provenance or a usable MP path.

The selection's context and typed choice hash become the existing `request_nonce`.
Consequently the 382-byte intent, its ID, derived BTC address and authenticated
request commitment bind that exact choice. No intent/guest encoding, registration
FROST ABI or requester commitment format changes. Legacy ordinary intents keep
their original nonce. No signature from every third-party recipient is required.
The packaged requester client retains its EOA transaction identity; supporting
this recipient wallet does not advertise a generic relayed wallet-requester flow.
The opening stays in the private journal and V3 encrypted REQUEST until the
existing public DKG registration-intent publication. Approving participants
independently validate that opening against the nonce. Only afterward does the
registration driver send/resume `selectFor` and execute the ordinary `registerBatch`.
Opening a choice grants no requester or quorum authority; its nonce is already
bound by the authenticated requester commitment and approved registration hash.
Recipient disclosure is not moved into the private `prepare`/request/ACK phase.

`requireRecipientNotSink` and `validateRecipient` run on-chain during registration
and address mint. The canonical graph's pool/token, registry/deposits, escrow,
authorization/coordinator/verifier, DKG, SPV, policy, fee/admission and callback
components remain excluded in every mode, including advanced. Raw calls, relayers
and quorum approval cannot bypass those gates.

Current code/profile checks are repeated for new mint. A changed recipient is
reported as `RECIPIENT_REVIEW_REQUIRED`; the immutable binding, accepted watch,
Bitcoin custody/signing and backup/recovery history remain intact. The operator
cannot reassign the BTC address, replace the beneficiary, claw back MP or remint.
An advanced runtime pin covers outer bytecode only: it cannot certify proxy
implementation/storage changes, future control or extractability.

### Client selection and wallet actions

Use the existing private address-service config and shared EOA transaction/cost
journal. `recipient-preview` and `prepare` keep the choice private. The latter
saves its reviewed opening in the existing private journal beside the intent draft.
Both steps display the recipient separately from the MP token and pool.
After moving a draft to a different requester directory, repeat `--recipient-file`
and `--recipient-accept` on `request` to restore that exact opening explicitly.

```bash
python -m signer_agent.address_transport wallet-create --config "$OWNER_CONFIG" \
  --wallet-owner "$OWNER" --wallet-salt "$CREATE_SALT_32_BYTES"

python -m signer_agent.address_transport recipient-preview --config "$REQUESTER_CONFIG" \
  --recipient "$WALLET_OR_CONTRACT" --recipient-mode wallet-v1 \
  --output /private/path/recipient-choice.json
# For unverified contract reception explicitly choose advanced-contract instead.
# Review the full selection and its limitations; copy its confirmation_hash.
python -m signer_agent.address_transport prepare --config "$REQUESTER_CONFIG" \
  --recipient-file /private/path/recipient-choice.json --recipient-accept "$CHOICE_HASH" \
  --intent-file /private/path/deposit-intent.json

python -m signer_agent.address_transport wallet-transfer --config "$OWNER_CONFIG" \
  --wallet "$WALLET" --recipient "$TO" --amount-units "$MP_UNITS" --wallet-salt "$OP_SALT"
python -m signer_agent.address_transport wallet-approve --config "$OWNER_CONFIG" \
  --wallet "$WALLET" --spender "$POOL_ESCROW" --amount-units "$MP_UNITS" --wallet-salt "$OP_SALT"
python -m signer_agent.address_transport wallet-withdraw --config "$OWNER_CONFIG" \
  --wallet "$WALLET" --withdrawal-id "$REQUEST_ID" --bitcoin-destination "$BTC_DESTINATION" \
  --amount-units "$MP_UNITS" --wallet-salt "$OP_SALT"
```

Each distinct owner action needs its own operation salt; identical args/salt
resume the same transaction. Amounts are token base units. `wallet-approve`
accepts only this pool's exact escrow. The withdrawal is a direct MP primary
request from the wallet; fee consent/subsidy and S-share redemption through this
wallet are not advertised by these commands. Existing owner fee flows remain
separate. Transfer and withdrawal commands require the immutable wallet owner.
The requester, recipient owner and Bitcoin payer need not be the same person.

Focused [matrix evidence](../ops/artifacts/zkspv/worker-integration/recipient-matrix-report.json)
records real local wallet/policy/registry/token/escrow execution, including MP
transfer, approve and withdrawal. DKG, threshold approval, SPV and verifier
qualification remain explicit fixtures. This is component evidence; live
Core/proof/FROST/consumer/recovery acceptance and independent review are pending.

## Status model and authorization

CUSTODY starts ACTIVE at generation 1. Its states are ACTIVE, DRAINING,
SUSPENDED and terminal COMPROMISED. DRAINING permanently closes new issuance
while permitting old bindings to receive lawful mint. Temporary suspension can
resume only as permitted by the retained `issuanceClosed` flag.

DERIVATION starts SUSPENDED at generation 1. Its states are QUALIFIED,
SUSPENDED and terminal REVOKED. QUALIFIED requires the exact pinned runtime,
profile and verifier qualification flag. **The existing arithmetic candidate
has `QUALIFIED=false`; FROST approval cannot activate it.** Nonzero source or
qualification hashes are provenance commitments, not proof of an external audit.

`StatusChange` contains, in ABI order:

```text
scope:uint8 (0 CUSTODY / 1 DERIVATION), expectedState:uint8,
expectedGeneration:uint256, sequence:uint256, nextState:uint8,
closeIssuance:bool, deadline:uint64,
report:(bytes32 incidentId, bytes32 reason, bytes32 evidenceHash,
        bytes32 bitcoinBlock, bytes32 bitcoinTx, bytes32 evmAnchor)
```

Both scopes have independent status sequences, separate from registration and
pool mint nonce. `getStatusOperationHash` binds the fixed scope ID and entire
change under `SATOHSI_ISSUANCE_STATUS_V1`; the normal EVM_AUTH quorum consumes
the operation. Any relayer may deliver it. Incident ID, reason and evidence hash
are mandatory. A transition atomically increments generation/sequence using
checked arithmetic. An exact receipt is idempotent; stale expected generations
or preapproved unpause operations cannot override later incidents. Suspend and
revoke remain available if verifier execution is unavailable.

There is no replacement verifier, lower threshold, fund transfer, beneficiary
rewrite or SPV halt in this API. Cached bindings survive quarantine, but mint
checks current status and provenance each time. The
[canonical family root](BITCOIN_KEY_FAMILY.md) now gates registry enrollment and
status/known-alias inheritance. Independent custody-key migration and live
accounting transfer remain unsupported; a new label/deployment is not a recovery qualification. The
[continuity client](BITCOIN_CONTINUITY.md) enforces original-graph continuation,
retains family/status evidence and rejects unsupported transitions; it does not
implement a live migration protocol.

## Incident and status client

`frost-signer bitcoin incident` connects private reports to `setStatus`. New
opt-in common bootstrap exports a canonical `incident_board`, bound to the address
registry and its existing EVM_AUTH participants. Existing deployments require an
explicit canonical board/configuration; no live graph is migrated automatically.

`BitcoinIncidentMessageBoard` inherits the bounded request/reply wire format in a
separate deployment. Its `incidentDomain`, `SATOHSI_INCIDENT_HPKE_V1` context and
typed `INCIDENT_REPORT / INCIDENT_FORWARD / INCIDENT_ACK / INCIDENT_STATUS` bodies
separate it from address requests, SPV jobs and DKG slots. HPKE uses the existing
native library IPC. Details and a random salt stay in ciphertext/private SQLite;
the public status intent contains evidence commitments and anchors. Authentication
establishes the report's author and exact commitment, while its contents remain
a claim for quorum review. Delivery/ACK/status projection grants no authority or Ready.

Any registered transport identity can report to EVM_AUTH participants. A participant
can forward evidence with the original canonical delivery proof, preserving the
original author, incident ID and evidence. Each signer receives and reviews the
private evidence independently. No transport secret, private FROST share or secret
nonce is added to a public status intent. RPC failure never automatically causes
COMPROMISED/REVOKED; the existing quorum makes each explicit risk decision.

Use a private copy of [incident-service.example.json](../config/incident-service.example.json).
`board` is `contracts.incident_board` from the manifest; `--registration-board` is
the separate public DKG board. Keep the existing identity transaction/cost journals
and a separate incident inbox directory. The private details file has exactly
`{"summary":"bounded operator observations","references":[]}`; references are
at most 16 lowercase 32-byte hashes. Do not put secret shares, nonces, keys or RPC
credentials in reports. Report export writes a private file; ordinary output is redacted.

```bash
# Existing key generator; retain old epochs for delayed ciphertext.
python -m signer_agent.address_transport keygen --epoch 1 --key-file "$TRANSPORT_KEY_FILE"
frost-signer bitcoin incident register-key --config "$INCIDENT_CONFIG" --epoch 1
frost-signer bitcoin incident report-prepare --config "$REPORTER_CONFIG" \
  --scope custody --reason-code OBSERVATION_UNAVAILABLE --details-file "$PRIVATE_NOTES" \
  --output "$PRIVATE_REPORT"
frost-signer bitcoin incident report-send --config "$REPORTER_CONFIG" \
  --file "$PRIVATE_REPORT" --recipient "$QUORUM_PARTICIPANT"
frost-signer bitcoin incident flush --config "$REPORTER_CONFIG"
frost-signer bitcoin incident receive-once --config "$PARTICIPANT_CONFIG"
# An authorized recipient can forward it to another participant using report-send --report-id.

frost-signer bitcoin incident status-prepare --config "$OPERATOR_CONFIG" \
  --registration-board "$DKG_BOARD" --report-id "$REPORT_ID" \
  --next-state SUSPENDED --deadline "$DEADLINE"
frost-signer bitcoin incident status-propose --config "$OPERATOR_CONFIG" \
  --registration-board "$DKG_BOARD" --operation-id "$OPERATION_ID"

# Each signer exports/reads the private evidence, adopts and explicitly approves.
frost-signer bitcoin incident report-export --config "$PARTICIPANT_CONFIG" \
  --report-id "$REPORT_ID" --output "$PRIVATE_REVIEW_FILE"
frost-signer bitcoin incident status-adopt --config "$PARTICIPANT_CONFIG" \
  --registration-board "$DKG_BOARD" --session "$SESSION"
frost-signer bitcoin incident status-review --config "$PARTICIPANT_CONFIG" \
  --registration-board "$DKG_BOARD" --operation-id "$OPERATION_ID"
frost-signer bitcoin incident status-approve --config "$PARTICIPANT_CONFIG" \
  --registration-board "$DKG_BOARD" --operation-id "$OPERATION_ID" --accept "$REVIEW_HASH"
frost-signer dkg sign-evm --rpc "$EVM_RPC" --board "$DKG_BOARD" --role conf_ceremony \
  --session "$SESSION" --identity-file "$IDENTITY_FILE" --state "$EXISTING_SIGNER_STATE" \
  --password-file "$PASSWORD_FILE" --incident-config "$PARTICIPANT_CONFIG"

frost-signer bitcoin incident status-execute --config "$RELAYER_CONFIG" \
  --registration-board "$DKG_BOARD" --operation-id "$OPERATION_ID"
```

The existing FROST rounds and independent nonce/spent journals remain in force.
For a [standalone contour](ZKSPV_CONTOUR.md#standalone-reviewed-incidentrecovery),
`status-preview --output FILE` exports a read-only exact decision before report
delivery/session creation. Its confirmation hash pins the runner's selected
transition, without creating a local signer approval. The adapter uses the
original encrypted report/ACK/status and nonce/cost journals, waits for external
participant review/FROST, and requires the exact canonical status receipt. It
cannot renew/retry a decision or bypass the recovery guards below.
Before private work, status signing checks the participant identity, enrolled
EVM_AUTH, private report, exact public intent, explicit local approval and both
confirmed/latest generations. The contract serializes transitions using each
scope's own status sequence. Mint/registration sequences and thresholds are unchanged.
A relayer delivering an already authorized decision needs its public operation,
without the private evidence or a new local approval.

Restriction/revocation inspection authenticates the canonical graph without
executing/admitting the derivation verifier. Resume additionally requires the
applicable qualification and a `recovery_service_config` pointing to this graph's
existing address service. It rechecks Core network/tip, coverage of every registered
intent within the configured bound and healthy existing reserve reconciliation.
It does not clear financial/signing pauses or fund an empty reserve. Missing
observations block resume; terminal COMPROMISED/REVOKED and issuanceClosed persist.

`status`, `status-recover` and explicit `status-retry` preserve evidence and attempt
history, with at most eight attempts per decision. A changed generation/sequence
requires a new hash, quorum authorization and local review. Lost session/result
writes resume the same durable EOA envelope. Canonical `statusReceipts` recover exact
completed execution after expiry or later status changes without report plaintext.
`STATUS_CONFIRMING` exposes a latest-only receipt separately from confirmed state;
it is not a confirmed pause. Orphaned anchors require explicit revalidation and
their evidence is retained. No command resets EOA, FROST nonce or spent journals.

Observed generations, terminal decisions and closed issuance are also retained in
the identity transaction journal, outside inbox backups. Restoring an inbox or
reorging its cursor cannot erase that evidence. Lower/orphaned observations block
fresh permissive decisions. A newly reviewed restrictive decision can reassert a
hold against canonical current state without discarding the retained witness.
Explicit recovery reanchors only with adequate generations and preserved terminal/
closed constraints; it never clears a known terminal incident to permit resume.
Unchanged polling deduplicates witnesses. This local protection is separate from
canonical contract status and does not promise an inevitable on-chain halt during
quorum/provider outage. Cross-graph family admission/status fencing is now handled
by the independently pinned canonical root.

Delivery retries use explicit `--delivery 1..31`, preserving report/evidence and old
signed/queued bytes. `notify` queues bounded encrypted observations. `backup`
contains private reports and local workflow records; identity transaction/cost and
signer journals remain separate. `report-import --message-id ID --file FILE`
authenticates retained evidence against canonical delivery; include `--delivery`
for a nonzero attempt. Neither a resend nor an ACK creates a status decision.

[Incident component evidence](../ops/artifacts/zkspv/worker-integration/incident-status-report.json)
records real HPKE, the native EVM FROST adapter, libsecp256k1 signature verification,
SQLite and local Solidity. Chain DKG/quorum, provider and qualification boundaries
are explicit fixtures. The long Core/proof/FROST/consumer contour remains pending.

## Mint and common backing

Address mint uses the explicit `MintClaim` ABI tuple:

```text
intentId:bytes32, bindingCommitment:bytes32, txid:bytes32 (wire digest),
vout:uint32, recipient:address, satoshis:uint64,
custodyGeneration:uint256, derivationGeneration:uint256,
validationProvenance:bytes32
```

`getAddressMintOperationHash` binds this tuple, registry and custody scope under
`SATOHSI_ADDRESS_MINT_V1`, the `mintFromAddressProof` selector and shared pool
nonce. Execution requires exact current generations, binding, recipient and
provenance. The common registry parses **FUNDING_STRIPPED_100K_V1**, checks all
inputs against consumed/wallet-output/reservation state, exact output/script and
amount, target reservations, canonical inclusion and depth: ordinary transactions
require 6 confirmations in the current configuration; coinbase requires 100.
The same `consumed`, `walletOutput`, `backingSats` and reservations serve both
modes. MP issuance is exactly sats × 10^10. Allocation and intent receipts are
stored only on success; any error rolls back authorization, nonce and money.

The script selects the mode. A genuine/conflicting SMP1 marker does not redirect
or reject an otherwise valid address output; that output cannot mint through
SMP1. Controlled spend recording recognizes registered derived scripts as wallet
change, carries principal and prevents later deposit mint. Funding inputs already
reserved for wallet spend are rejected in **both** deposit modes.

For status-enabled graphs, old `mintFromProof` is closed. Explicit
`mintFromProofV2` binds `SATOHSI_CUSTODY_MINT_V2`, registry/scope and custody
generation, with unchanged SMP1 allocation/coinbase rules and the same pool
nonce. DERIVATION suspension does not block independent SMP1; CUSTODY suspension
does. Old approvals cannot revive after resume. `mintProofVersion()` reports
1 for ordinary graphs, 2 for the new graph. Old deployed immutable contracts do
not acquire this behavior from a source change.

## Mint client and admission

The explicit `btc-address-mint-propose`, `btc-address-mint-execute` and
`btc-address-mint-retry` commands use canonical `BitcoinMintAdmission` records.
They take `--intent-id`, display-order `--txid`, `--vout`, the current attempt's
`--deadline` and `--mint-admission`. Recipient and amount have no CLI overrides:
the recipient comes from the immutable binding, and sats from the parsed output.
The client independently recomputes the native descriptor, binding and validation
provenance, checks Core genesis/UTXO, every funding input, canonical inclusion
and confirmation depth, then rechecks the current generations before execution.

Enrollment uses `inspect-bitcoin-registry` then `trust-bitcoin-registry` with
`--mint-only --batch-registry BATCHES` and the usual enrolled participant options.
This stores a separate `btc-mint-registry` pin. It grants no BTC signing trust;
the legacy Bitcoin signer continues to refuse the new graph. The separate explicit
[`--address-signing` profile](BITCOIN_ADDRESS_SIGNING.md) supports V3 BASE/DERIVED
plans with current generation checks and the common nonce/spent journal.
Status/retire and
operator-transfer commands select this pin with `--mint-only`. For threshold
EVM signing, use `sign-evm --mint-admission ADMISSION --session SESSION` with the
enrolled EVM role and local `--bitcoin-rpc` / `--bitcoin-cookie`. Each signer
independently reviews the funding proof before private signing work. The EVM
signer builds its public BTC graph from the enrolled invitation/completed DKG;
it does not import a BTC signing share.

All mint modes share one pool-nonce admission slot. An address claim retains its
intent, binding, outpoint, beneficiary, sats, provenance, deadline and original
generation snapshot. Each attempt separately binds current custody/derivation
generations. A monotonic generation change permanently fences the old attempt,
including approved authorizations after resume. Explicit retry preserves the
economic claim, current deadline and eight-attempt budget. On the updated graph,
the operator can explicitly renew an expired and retired attempt with
`btc-mint-renew`. See the [conditions for a new deadline](BITCOIN_MINT_ADMISSION.md).
The client checks again after authorization before sending mint.
Temporary RPC failure does not establish permanent retirement.

Exact completion recovery checks allocation, intent, immutable `mintedAddressSats`
and consumed state before accessing Core or checking current permission. It works
after the backing output has been spent and after a later suspension/revocation.
`consumed` alone never proves success. The client currently rejects new
`--zk-worker-config` / `--zk-transport-config` requests for this address route:
its automatic paid observer/watch profile remains pending. It can use an already
prepared common SPV state. Plain bootstrap does not install this candidate;
the explicit full `--system-policy` [bootstrap](ULR_BOOTSTRAP.md) configures its
address graph with `ready=false`. Installation does not qualify the verifier
or grant production activation.

Focused Forge tests run real curve computation, target-bound one-use
authorization consumption, registry/status logic, token issuance and backing.
**Completed DKG records, signature approval, SPV inclusion and verifier
qualification are explicit fixtures.** The test-only qualification wrapper
delegates arithmetic to the actual candidate; it is not production code or
independent review. Tests include atomic batches, exact retry, terminal states,
stale generation fencing, runtime quarantine, mode separation, change and
reservations, and funding with 257 inputs/outputs. No real proof, Core acceptance
or combined E2E was added. Source snapshots and test receipts are recorded in
`ops/artifacts/zkspv/worker-integration/address-registry-code-report.json` (historical)
and [current mint client evidence](../ops/artifacts/zkspv/worker-integration/address-mint-client-report.json).

Remaining before Ready: independent verifier/profile/integration qualification;
qualified operation of [encrypted address requests and watch/rescan/recovery](ADDRESS_REQUESTS_WATCH.md);
address SPV worker profile and full live Bitcoin/Core signing qualification;
recipient profile operational qualification, UTXO reconciliation and fee-reserve handling;
qualified operation of the [old-epoch service](BITCOIN_EPOCH_SERVICE.md) and
cross-graph continuity rules; target resource
limits and combined Core/proving/consumer E2E. No address is issued as Ready.
