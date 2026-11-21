# Bitcoin continuity: original accounting graph only

The client profile `ORIGINAL_BTC_ACCOUNTING_GRAPH_V1` admits continuation of an
already authenticated graph. It does not implement cross-graph asset migration,
verifier replacement or custody-key migration. The newer
[canonical family root](BITCOIN_KEY_FAMILY.md) adds on-chain admission/status/alias
fencing for independently pinned roots without turning a second accounting graph
into continuation of the first.
Unsupported transitions remain BLOCKED; production qualification remains open.

The additional [retained-graph handover](ZKSPV_CONTOUR.md#first-supported-handover-strategy)
coordinates an old draining graph and a separate target with a distinct known
parent. It adds durable operator/signer issuance holds and fresh retention review.
Every asset stays in its original graph; this is not cross-graph asset migration
; canonical status inheritance is enforced separately by the family root.

## Retained identity and restrictions

The family key commits to Bitcoin genesis and the normalized base public-key X
coordinate, independently of EVM chain, epoch labels and registry address. The
baseline also rejects reuse of a known parent/child public key under a different
Bitcoin genesis/network, rather than treating it as independent custody. Separate
network deployments remain available with independent BTC keys. The
catalog binds it to the original EVM chain, registry, common deposits/consumed
domain, escrow, batches, SPV, authorization, DKG wallet/result/epoch and status
scopes. It also pins the exact derivation semantics/profile, verifier address,
runtime/source hashes, qualification commitment and service policy. Pool issuers
may share that original common graph; a second accounting registry is not a
continuation of its consumed/backing storage.

All facts come from freshly authenticated canonical contract configurations,
completed DKG and existing CREATE/runtime/provenance checks. Configuration files
locate contracts; an exported inventory is never trusted as migration authority.
No root import, editable beneficiary, verifier pointer or lower quorum is added.

Records live outside inbox backups in the existing identity
`.transactions.sqlite` and, for enrolled signer trust, the independent
`.spent.sqlite`. Both retain the original identity and monotonic status evidence.
COMPROMISED, REVOKED and closed issuance cannot be forgotten by a different label,
new DKG context, stale encrypted state or a higher generation with an incompatible
terminal decision. An orphaned observation requires explicit safe reanchoring;
it is not silently overwritten by a newer active-looking graph.

Canonical registered address and fee-purpose bindings also record their derived
public output keys as aliases of the original family. A known child key cannot
be reenrolled as an independent parent. Discovery of an alias after an earlier
enrollment also fences subsequent monetary work. This covers observed canonical
derivations, not every mathematically possible undisclosed public tweak. The
catalog does not prove the independence of an arbitrary unknown key or discover
all deployments on every network. Its operator/signer scope and the DKG trust
boundary are explicit; the global cross-graph qualification gate stays open.

## Client integration

The address client records canonical observations and checks graph continuity
before requests and uncompleted registration. Registration uses current issuance
permission; address mint checks continuity before paid SPV/admission work and
again before authorization/execution. The retained SMP1 route checks custody
without borrowing address-derivation qualification. Exact historical completion
receipts are inspected before new monetary permission where the existing path
supports completion recovery.

Explicit Bitcoin registry pinning and subsequent fresh trust checks retain the
same family in the independent signing journal. The normal generation-aware
Bitcoin private boundary checks this retained identity before nonce/share work.
FROST EVM registration, mint and fee policies reconstruct the current context
before private work; mint policy is rechecked at each such boundary. A typed
bootstrap `setAddressRegistry` operation checks the same catalog **before** an
EVM_AUTH nonce/share, including the original one-time unbound/nonce-zero phase.
An empty new registry cannot erase a known family's original graph.

Incident reports and old-epoch scanning continue to record restrictions without
granting monetary permission. DERIVATION revocation fences its address/fee
profile; custody restrictions apply to the family's base and address issuance.
Previously authorized Bitcoin-paid settlement remains subject to its existing
exact receipt/context rules. The catalog does not reset EOA/FROST nonce journals,
financial pauses, reservations, owner consent, principal or old signed versions.

## Canonical inventory and transition review

Use each original private address-service configuration:

```bash
frost-signer bitcoin continuity inspect --config "$OLD_ADDRESS_CONFIG" \
  --request-id "$KNOWN_REQUEST" --batch-id "$KNOWN_BATCH" \
  --output "$PRIVATE_INVENTORY"

frost-signer bitcoin continuity check --config "$OLD_ADDRESS_CONFIG" \
  --candidate-config "$CANDIDATE_ADDRESS_CONFIG" --output "$PRIVATE_REVIEW"

# Same immutable graph, adequate current generations and preserved terminal/closed state:
frost-signer bitcoin continuity recover --config "$OLD_ADDRESS_CONFIG"
```

Inventory includes exact registered intents/scripts/recipients and validation
provenance, enumerable live backing, known reservations, named pending requests,
payment families/replacement counts, available personal BTC claims/budgets and
fee-reserve allocations/reconciliation. Current verifier failure is an explicit
unavailable fee component, not an empty ledger. Enumeration is bounded by
`--max-bindings` (1–256), `--max-entries` (1–4,096), 32 named requests and 16 named
batches. Limits stop collection; they do not discard records to manufacture PASS.

The contracts do not enumerate every consumed/walletOutput key, historical
request/batch, outstanding approval or unregistered/orphaned ingress. Named
request/batch inputs are locators, never claims of completeness. Consequently
`history_complete=false` and `activation_allowed=false` are unconditional in
this inventory. Zero backing, a copied consumed list or a new Bitcoin address
does not prove the absence of obligations. Private export uses a new 0600 file.

`check` returns `SUPPORTED_ORIGINAL_GRAPH` only for the same retained immutable
accounting identity without unresolved observation conflicts. It is not a fresh
monetary-permission/Ready check. A new registry, changed verifier/provenance,
different accounting storage or same-key resharing is rejected. A different
custody key also does not authorize moving the source obligations: the migration
protocol is unsupported. `recover` reanchors observations only for the original
graph and cannot remove terminal evidence, change the family or restore shares.

## Remaining acceptance

The actual derivation and fee-purpose qualification flags remain false. Focused
checks execute real native derivation/curve operations and SQLite retention;
canonical chain, completed DKG, provenance and service qualification use explicit
fixtures. No live deployment, real-value migration, fresh proof or combined
Core/FROST/consumer contour is established by these checks.

Before any proposed cross-graph live activation, a separately qualified protocol
must preserve the exact old intents/scripts/recipients, all pending/paid/signed
versions and fee claims, one consumed/backing domain and actual custody under an
independent uncompromised key where required. Unknown live history and the
unpublished intermediate-reorg limitation (§15.11) remain blocking evidence gaps.
No automatic transfer of live assets is performed by this profile.
