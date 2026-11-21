# Address UTXOs in the Bitcoin signer

Status: **CODE_IMPLEMENTED_QUALIFICATION_PENDING**. The explicit
`BTC_INPUT_KEYS_V1` client profile connects BASE/DERIVED inputs to the existing
Bitcoin plan, public signing board, native FROST operations and encrypted state.
It does not activate address issuance or qualify the derivation verifier.
The actual pinned verifier still has `QUALIFIED=false`; successful derived
signing tests use an explicit fixture for that qualification boundary.

The [old-epoch service](BITCOIN_EPOCH_SERVICE.md) adds durable optional
`--epoch-service-config FILE` enrollment for `btc-commit/btc-sign`. Once enrolled,
the original service/board/archive/generation checks persist when this option is
omitted. Participant retention claims create no Bitcoin nonce or spending
authority and do not replace the existing pinned trust and spent journals.

## Enrollment and normal commands

For a new candidate graph, inspect and explicitly pin the Bitcoin signing
profile using the enrolled BTC ceremony and the exact batch registry:

```sh
frost-signer dkg inspect-bitcoin-registry --address-signing \
  --batch-registry "$BATCHES" --board "$BOARD" --role btc_ceremony \
  --rpc "$RPC" --identity-file "$IDENTITY" \
  --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
  --password-file "$PARTICIPANT_DIR/password" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"

frost-signer dkg trust-bitcoin-registry --address-signing \
  --batch-registry "$BATCHES" --accept-fingerprint "$REVIEWED_FINGERPRINT" \
  --board "$BOARD" --role btc_ceremony --rpc "$RPC" \
  --identity-file "$IDENTITY" --state "$PARTICIPANT_DIR/btc_ceremony.enc" \
  --password-file "$PARTICIPANT_DIR/password" \
  --bitcoin-rpc "$BTC_RPC" --bitcoin-cookie "$BTC_COOKIE"
```

This uses the existing `btc-registry:<wallet-context>` trust namespace. An
existing different pin cannot be overwritten; live obligations require an
explicit continuity/migration procedure. The independent `--mint-only` pin
does not grant Bitcoin signing authority. The two enrollment flags cannot be
combined. Default inspection still refuses a status-enabled graph.

After enrollment, use the ordinary `btc-prepare --input TXID:VOUT ...`,
`btc-propose`, `btc-execute`, `btc-publish-plan`, `btc-commit`, `btc-sign`,
`btc-broadcast`, `btc-settle`, `btc-status` and `btc-recover` commands with the
existing participant/board/Core options. Preparation reads each UTXO script and
resolves its key from canonical registry state. There is no caller-provided
tweak, output key, key package, private descriptor or recipient override.
The [payment runbook](BITCOIN_ONCHAIN_RUNBOOK.md) describes this command sequence.
Plain bootstrap does not install this candidate graph. The explicit full
`--system-policy` [bootstrap](ULR_BOOTSTRAP.md) installs its configured address
components; it does not grant qualification or production Ready status.

## Public plan V3

V2 remains the ordinary base-wallet plan. V3 retains its fields and adds:

| Field | Meaning |
| --- | --- |
| `address_registry` | Exact immutable registry admitted by the signing pin |
| `custody_generation` | Canonical decimal uint256 snapshot, always positive |
| `derivation_generation` | Current generation for any DERIVED inputs; `0` for a BASE-only plan |
| `input_keys` | One canonical native BASE/DERIVED descriptor per input, in transaction order |
| `input_provenance` | One exact validation-provenance hash per input; zero for BASE |

The common board's existing hash binds the exact canonical plan bytes, version,
chain, board, wallet result and batch. Generations, descriptors and provenance
therefore affect reservation and EVM authorization hashes, without reinterpreting
an old V2 payload. Limits remain 16 inputs, 32 outputs/requests and 16,384 plan
bytes; a descriptor-heavy batch may reach the byte limit first.

Every input must use the same canonical DKG result/base and wallet context.
DERIVED descriptors retain the full immutable intent and binding commitment.
The signer independently recomputes derivation and registration provenance and
checks the actual registered script, pool/token/graph/key epoch and parent-block
anchor. BASE inputs use the post-DKG even-Y key without a second base tweak.
Native versioned operations derive a temporary per-input key/public package;
the persistent base package is never modified.

The current output profile pays requested external P2WPKH/P2TR destinations and
returns change to the base wallet. Payments to registered wallet scripts are
rejected by this client profile. Per-address change/refund/advanced recipient
flows need their own qualified policy.

## Private-work and recovery boundaries

Custody ACTIVE/DRAINING permits ordinary service. SUSPENDED/COMPROMISED or a
changed custody generation blocks new private work and cached packet export.
DERIVED inputs additionally require the current QUALIFIED derivation generation,
the pinned verifier runtime/profile and its real qualification flag. A BASE-only
plan does not depend on derivation status. Resume never revives an old plan.

Checks run against confirmed and latest canonical EVM snapshots before nonce or
share work, after native work, before packet publication and before broadcast.
The EVM signer's V3 reserve/bind review independently checks the same public plan
and Core funding before private work and submission. Runtime/constructor trust
is established by the normal fresh graph inspection, not a self-reported flag.

Core preflight checks genesis, exact UTXO amount/script, confirmations,
reservations, pending deposits and principal conservation. A Bitcoin bestblock
change causes a retryable rejection instead of mixing observations. Mint remains
ordinary depth 6 in the current configuration and coinbase depth 100. Spending
retains the existing separate rule: at least one confirmation, or 101 for a
coinbase input, as specified in §20.8/§7.10.

A registered address output without issued backing or recorded controlled change
remains a protected pending deposit even without SMP1. It cannot fund unrelated
withdrawals or miner fees. Existing backing/change/reservation state is shared
with the common deposit registry; there is no second address balance ledger.

Nonce identity remains **`btc:<DKG-context>:<batch>:<input-index>`** in the same
encrypted participant state and independent `.spent.sqlite` journal. Plan and
message hashes bind each nonce; generations do not create a fresh namespace.
The journal is written before a share can be exported, and secret nonces are
removed after signing. Interrupted writes, missing/replaced journals and changed
transcripts fail closed. They do not regenerate a nonce or reset an attempt.
Nonce records also retain the observed Bitcoin bestblock and generation anchor.

Exact cached packets can be retransmitted without new secret operations or
requiring already-spent UTXOs. Their immutable plan/bindings, current generations,
batch membership and spent-journal evidence are still checked. A local monotonic
generation record retains the canonical EVM anchor; regression or an orphaned
anchor requires recovery and never deletes spent evidence. Existing wallet-wide
payment/reorg pauses apply to all BASE/DERIVED inputs through the same marker.

`btc-recover-inputs` explicitly re-establishes a canonical generation observation
for the selected published V3 plan. It verifies the current scope permissions,
nondecreasing retained generations and the common journal identity/entries,
then records an immutable recovery receipt and re-anchors only the public cursor.
It cannot clear the wallet's payment/reorg pause, resurrect an old generation,
rewrite a plan, repair a missing journal or regenerate any nonce/share.
Existing `btc-recover` remains the separate checked recovery of an actual paid
transaction. A generation that regressed after reorg remains blocked until
canonical authority/state safely re-establishes the retained restriction.

An EVM status change cannot revoke a Bitcoin signature already released. Exact
settlement/receipt recovery for a previously authorized Bitcoin payment does not
require a new issuance generation. This is signer enforcement, not a new
on-chain Bitcoin halt or an implemented incident sweep/refund protocol.

## Evidence and remaining work

[Focused evidence](../ops/artifacts/zkspv/worker-integration/address-signing-client-report.json)
records real local native DKG, mixed-input threshold signatures independently
checked by libsecp256k1, transaction attachment, client routing and common-journal
crash/restart tests. Core/EVM/provenance/qualification boundaries are explicit
fixtures. No actual Core acceptance, fresh ZK proof, public transfer or combined
Core/proving/transport/payment/FROST/consumer E2E was run in this increment.

The separate [request/watch component](ADDRESS_REQUESTS_WATCH.md) now provides
encrypted delivery, durable descriptors and bounded rescan/recovery code.
Still required: independent verifier/profile review, qualified operation of the
implemented issuance/watch, reconciliation, fee-reserve, recipient and old-epoch
clients, independent qualification of [canonical family fencing](BITCOIN_KEY_FAMILY.md)
and the combined operational contour.
No Ready or production qualification is granted by this component.
