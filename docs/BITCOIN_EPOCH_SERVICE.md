# Old Bitcoin epochs: service and retention

`bitcoin epochs` implements bounded service orchestration for existing address-mode
wallets (§21.20). Each entry keeps its original BTC_SPEND context, address/fee
archives, registry/pool and signing board/batch graph. A new BTC epoch needs a
separate authorized group ceremony and deployment; transport keys and gas payees
do not move custody. This interface never rotates keys, signs payments, changes
canonical status, migrates assets or declares an address Ready.

The [standalone contour adapter](ZKSPV_CONTOUR.md#original-old-epoch-fleet-in-the-runner)
uses this same Fleet, original journals, enrolled challenge and external signed
retention claims. `--epoch-config` advances bounded observation without SPV
payments and requires a fresh full-fleet review before positive completion.
It cannot manufacture retained-key availability, renew claims or clear a pause.

Use a private copy of [bitcoin-epochs.example.json](../config/bitcoin-epochs.example.json).
Replace sample contexts with exact BTC_SPEND `Manifest.context` values and select
the existing configurations and original signing boards. Paths, credentials,
identity files and exported evidence are participant-local; retain all old
transport key files and original signer states/journals.

## Modes, scheduling and recovery

At most one entry is `ISSUING`. `SERVICING` retains an old epoch permanently and
cannot return to ISSUING locally. Changing this label does not close on-chain
issuance. A retirement review requires DRAINING with `issuanceClosed=true` at
both confirmed and latest snapshots. Close issuance through the existing
[incident/status quorum workflow](ADDRESS_DEPOSIT_REGISTRY.md#incident-and-status-client).

```bash
frost-signer bitcoin epochs run-once --config "$EPOCH_CONFIG"
# Explicit opt-in to the original admission/quote/payment policies:
frost-signer bitcoin epochs run-once --config "$EPOCH_CONFIG" --schedule-spv
```

Schedule `run-once` with the existing supervisor. A durable round-robin cursor
handles at most `max_epochs_per_poll` entries (1–8, up to 32 retained epochs).
Failure of one epoch does not skip the rest. A crash before cursor commit repeats
idempotent work. Import processes at most `max_bindings_per_poll` canonical
records (1–16); existing watch limits bound blocks, intents, outputs and reorg
repair. Archive witness reads are capped at `max_archive_records` (up to 65,536
records per archive category). Exhausted limits leave an explicit blocked result.

The service restores exact registered address bindings from the original registry
and runs the existing genesis rescan. Transport receipt, address scanning,
existing fee/refund scanning, canonical fee binding import and reserve
reconciliation have separate results. A delivery failure does not stop scanning.
Observation of original address and fee/refund scripts continues when a verifier
is quarantined: the observation client authenticates the graph without executing
the derivation verifier and cannot issue requests or send transactions. Existing
private fee archive scans are labeled `OBSERVATION_ONLY`; canonical binding and
qualification checks still govern money operations.

The membership/token, original transport public keys, descriptor hashes and known
ingress/spend observation IDs are retained in the operator identity's existing
`.transactions.sqlite`, outside `epochs.sqlite` and inbox backups. Removing an
entry, changing its original archive/board/graph, dropping old transport epochs
or losing the fleet journal fails closed. RPC/cookie locations may change and
transport keys may be appended. An older inbox cannot erase a known obligation.
A rescan can reconstruct canonical observations; missing intermediate/orphaned
history continues to require original archive recovery. No cursor reset, record
deletion or donation classification resolves it.

Late deposits, closed fee intents and pending refunds retain their owner, script,
intent and original accounting context. Discovered BTC is protected observation
data, never automatic mint, credit or free reserve. `--schedule-spv` uses the
separately configured original worker/transport and its existing budgets. No new
payment is sent by default. The ordinary address client continues to handle its
encrypted ACK/status outbox.

## Retained threshold before a planned departure

```bash
frost-signer bitcoin epochs review-start --config "$EPOCH_CONFIG" \
  --departing "$LEAVING_PARTICIPANT" --output "$PRIVATE_CHALLENGE"

# Each retained member, with the original BTC_SPEND manifest and signer state:
frost-signer bitcoin epochs attest --config "$EPOCH_CONFIG" \
  --challenge-file "$PRIVATE_CHALLENGE" --manifest "$BTC_MANIFEST" \
  --identity-file "$PARTICIPANT_IDENTITY" --state "$EXISTING_SIGNER_STATE" \
  --password-file "$PASSWORD_FILE" --output "$PRIVATE_ATTESTATION"

# Include distinct retained members for every original epoch.
frost-signer bitcoin epochs review --config "$EPOCH_CONFIG" \
  --challenge-file "$PRIVATE_CHALLENGE" \
  --attestation-file "$PARTICIPANT_ONE" --attestation-file "$PARTICIPANT_TWO"
```

Share the same private challenge with the participants. Their paths, passwords
and RPC credentials stay local. Each member uses its own enrolled service identity
and watch replica; no operator's private identity is shared. A member serving several epochs attests separately
for each original share. The client checks the actual local scalar with the native
public-share operation against the completed on-chain result/roster, the original
pinned graph/board and independent nonce journal. A financial pause blocks it.
It creates no Bitcoin nonce, signature share or payment authorization.

The separate EOA-signed claim binds the full challenge hash (including the leaving
members and exact epoch set), fleet/wallet context,
participant/public share, service fingerprint, both generations/sequences,
canonical anchor and bounded expiry (60–3,600 seconds). The reviewer counts the
original BTC_SPEND threshold separately for each epoch. Departing members,
duplicate signatures, foreign contexts, stale generations, expired claims and
reorged anchors cannot complete the check. Exact retries preserve the old expiry;
renew evidence with a new challenge.

`RETENTION_CHECKED` means fresh authenticated operational claims from a sufficient
retained set. Remote members can lie about retained data or become unavailable.
These claims are not a remote proof of secret possession, an executed FROST
ceremony, spending authority, independent qualification or future uptime guarantee.
Actual signing/restore availability remains part of the combined contour.

Fresh review also checks transport-key availability, complete original watch
coverage, applicable verifier/purpose qualification, canonical reconciliation and
BTC fee runway. EVM funding uses confirmed/latest/pending balances and the
original signed-envelope journal. Exact final source receipts recover lost global
completion marks without rebroadcast; unresolved signed obligations block review.
A shared payer must cover the sum of the configured epoch funding envelopes,
each at least one maximum-cost address transaction. This is a bounded funding
check, not unlimited future service. Confederation/operator funds are allowed;
project treasury funding is not required when the chosen payer and BTC reserve
actually satisfy the checks. Principal and unclassified deposits are protected.

## Issuance and signing enrollment

Running the fleet with its configuration path enrolls each validated address
service in its identity transaction journal. Later `address prepare`, `request`
and unregistered incoming requests retain the guard when the CLI option is
omitted. SERVICING cannot issue. Before an enrolled new epoch issues while old
epochs remain, the client revalidates the saved claims and current service/funding
for the fleet. Explicit initial enrollment uses
`address ... --epoch-service-config FILE`.

Successful `epochs attest` also enrolls the existing BTC signer state. Alternatively
use `dkg btc-commit/btc-sign --epoch-service-config FILE`. After enrollment,
omitting the option cannot bypass the original board/batch/registry, service and
generation checks before private work. The enrollment is also retained in the
independent spent journal, so restoring an older encrypted state cannot remove
the guard. The existing FROST nonce/spent namespace,
payment/replacement/principal and fee-consent checks remain in use. No command
clears a pause or recreates an unavailable share/journal.

## Qualification and supported scope

Current `QUALIFIED` and `FEE_QUALIFIED` remain false; production review remains
blocked. Focused tests use explicit chain/DKG/provenance/Core/qualification
fixtures with real native derivation/block/share operations, EOA signatures and
SQLite recovery. They establish no live value transfer, Groth16 proof or complete
Core/FROST/consumer contour.

The initial profile covers one owning pool per epoch. Another pool's registered
bindings yield `EPOCH_POOL_COVERAGE_REQUIRED`; partial coverage never passes
handover. Original canonical boards/batches, a retained threshold and accessible
data must exist. Incompatible immutable legacy graphs remain unsupported.

The [retained-graph handover](ZKSPV_CONTOUR.md#first-supported-handover-strategy)
adds an immutable local source/target plan, two operator references and optional
independent signer enrollment. New target registration rechecks original service
without transferring old assets or rewriting bindings. This does not implement
global on-chain family inheritance or live migration.

The local family ledger rejects re-enrollment of the same base BTC key under a
different context. It is not global on-chain cross-graph/key-family continuity.
The shared [continuity client](BITCOIN_CONTINUITY.md) now retains the complete
original accounting/profile identity and observed canonical child-key aliases
across the address, bootstrap and signer paths, with explicit transition review.
A corrected verifier/new registry still requires a separate qualified continuity
protocol with one consumed/backing domain before serving old backing. Automatic
live-asset migration, the unobserved-history limitation in §15.11 and unavoidable
on-chain halt during quorum/provider outages are not solved by this client.
