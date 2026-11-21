# Registered address deposits: SPV worker and service funding

Status, 1 November 2026: **CODE_IMPLEMENTED_QUALIFICATION_PENDING; Ready disabled**.
The actual derivation verifier remains `QUALIFIED=false`. Positive checks use
explicit Core/EVM/DKG/qualification fixtures. This increment does not claim a
fresh proof, live payment/deployment or combined long E2E.

## Explicit profile and immutable need

```json
{"kind":"address_deposit","consumer":"CHECKSUM_POOL","intent_id":"64_lowercase_hex","txid":"64_lowercase_Core_display_hex","vout":"0"}
```

The registered intent determines recipient, script and provenance. The worker
authenticates the canonical pool/common/escrow/address graph and native binding,
then checks actual transaction bytes, target output, UTXO, protected/reserved
inputs and independent Merkle inclusion. Its selected ZK adapter must be the
graph's backend. There is no SMP1/base-script fallback. Ordinary transactions
require at least six confirmations, coinbase requires 100. Immature funding waits
without admitting a paid state job. DRAINING custody can service an otherwise
valid existing deposit; closing issuance does not cancel its mint rights.

Worker **V5** and transport **V3** explicitly add:

```json
"address_deposits": {"version":"1","registries":{"CHECKSUM_POOL":"CHECKSUM_ADDRESS_REGISTRY"}}
```

Start from [worker V5](../config/zkspv-address-worker.example.json) and
[transport V3](../config/spv-address-transport.example.json). Worker consumers and
federation routing must admit the same pool. Both sides pin the same address
profile, Bitcoin mainnet policy, EVM finality and ZK state. Other versions retain
their existing scope and refuse address needs. The retail address board grants
no federation SPV requester role: the configured requester needs its existing
enrollment.

An existing V4 worker or V2 transport can add the profile in its **existing**
directory. It is pinned separately, preserving previous scope, need IDs, semantic
jobs, ciphertext and financial/transaction journals. Changing the pin or
downgrading fails closed. Do not replace directories to bypass a guard or budget.

## Consumer and watch hooks

`btc-address-mint-propose/retry/execute` now accept their existing
`--zk-worker-config` or `--zk-transport-config`. Remote requests also use
`--zk-request-federation` / `--zk-service-recipient`. Native funding/current
permission is checked before service. While state/header progress is pending the
consumer returns `AWAITING_ZK_STATE`; retry re-reads confirmed generations before
mint admission/signing. Exact completed mint recovery precedes the optional
worker/Core route. SPV readiness never replaces a mint proof or FROST approval.

```bash
python -m signer_agent.address_transport spv-sync \
  --config /private/address-service.json \
  --zk-transport-config /private/address-spv-transport.json \
  --zk-request-federation "$FEDERATION_ID" --zk-service-recipient "$SERVICE"

python -m signer_agent.address_transport scan \
  --config /private/address-service.json --zk-worker-config /private/address-worker.json
```

The same route flags on `serve-once` schedule already observed outputs after
receipt. Repeat these bounded commands in the existing operator loop; no scheduler
is installed. `spv-sync` validates saved native descriptors and canonical
registered bytes, rotates over at most `max_messages_per_poll` unspent outputs,
pins its routing identity/directory and preserves exact needs/results. Failed
qualification or missing registration never deletes protected watch obligations.
The worker independently re-reads Core; a saved observation cannot authorize payment.

## Admission, quote, payment and recovery

Existing issuer automation and encrypted ACK/QUOTE/STATUS handle the new need.
Enable issuer authority and the private payer policy as in
[the automation runbook](SPV_AUTOMATION_ARCHIVE.md). Exact pool/address-registry,
federation/service identity, treasury epoch, tariff, payer, deadline and gas/fee
caps remain binding. A quote alone grants no debit permission.

Fresh unsigned admission/payment revalidates the need at its gas reservation
boundary, including current generations and qualification. A permission change
observed there stops signing. Identical signed transaction recovery runs first, retaining
its original consent and operation. Existing total/per-job/attempt budgets remain.

Generations, mint/FROST sessions and proving attempts are excluded from the
economic need: retry keeps the same semantic service job. Compatible needs share
paid coverage. A completed primary mint does not make a waiting alias buy another
full contribution: its canonical link restores the original job/terms. Sufficient
Fresh state suppresses payment; header materialization is separate bounded
maintenance. `paid_or_confederation` retains voluntary own-funds operation under
durable caps; a short treasury is not an unconditional blocker. Own spending
creates no reimbursement claim or paid marker.

## Remaining qualification

Verifier/profile and production registration/watch/service operation, recipient
and old-epoch continuity, operational qualification of the new
[BTC fee reserve/reconciliation](BTC_FEE_RESERVE.md) and the combined
Core/prover/payment/FROST/consumer E2E remain unqualified. This worker supplies SPV
service; the BTC reserve is a separate canonical component. Per-owner
[withdrawal fee credits and owner workflow](WITHDRAWAL_FEE_SUBSIDY.md) are now
implemented, with actual purpose qualification and combined acceptance pending. Address/watch results
retain `ready=false`. No EOA, payer, issuer, FROST nonce/spent or financial recovery
journal is reset.

[Evidence](../ops/artifacts/zkspv/worker-integration/address-spv-worker-report.json)
separates native crypto/codec/SQLite checks from simulated network and financial
boundaries. Long combined tests remain deferred at the user's request.
