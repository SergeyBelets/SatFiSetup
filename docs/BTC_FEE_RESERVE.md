# BTC fee reserve and address reconciliation

Status, 1 November 2026: **CODE_IMPLEMENTED_QUALIFICATION_PENDING; Ready disabled**.
This is the graph's unconditional BTC reserve. The separate per-owner BTC/MP/S
credit, consent and refund implementation is documented in
[WITHDRAWAL_FEE_SUBSIDY.md](WITHDRAWAL_FEE_SUBSIDY.md). Both remain unqualified. The actual
address verifier remains `QUALIFIED=false`. No new proof, deployment, live value
transfer or long combined Core/FROST/prover/consumer test was run for this change.

## Scope and money

`BitcoinFeeReserve` is immutable and bound once through the existing confederation
FROST authorization to `BitcoinBatchRegistry`, after escrow binding and before
the first batch. Its scope commits chain ID, reserve/address/common/batch/escrow/
Bitcoin signing board, Bitcoin genesis, graph ID, DKG registry, BTC result and
epoch, base script, protocol fee cap and maximum signed vbytes. Known funding,
plan and BIP340 verifier runtimes and exact constructors are checked independently.
The local signer graph fingerprint includes the reserve and its dependencies.
Legacy graphs with `feeReserve=0` retain their separate behavior; mint-only trust
does not confer permission to spend BTC.

`BitcoinDepositRegistry.totalBackingSats` and its live outpoint inventory retain
principal. Every ordinary/address mint adds its full sats; settlement removes
only burned MP principal and moves the remainder onto verified wallet change.
**1 sat = 10^10 MP** throughout. The reserve never haircuts principal, blocks a
valid mint because of low fees, or deletes accepted address watch obligations.

The canonical ledger distinguishes:

| Quantity | Meaning |
| --- | --- |
| `freeSats` | All authenticated, unspent reserve credits, including locked credits |
| `promisedSats` | Caps of allocations not yet bound to a Bitcoin family |
| `lockedSats` | Whole fee-credit UTXOs selected by bound families |
| `available()` | `freeSats - lockedSats - promisedSats` |

An allocation earmarks a cap, maximum miner fee and optional treasury purpose.
Treasury request key must equal its allocation ID; the cap covers payout plus
maximum fee. The current profile admits one treasury request per family, which
may also pay ordinary MP withdrawals. Whole-input locking may require releasing
an unused promise or selecting different inputs; it cannot strand other promises.
No local JSON file can substitute for this canonical lock.

`preparePlan` validates bounded inputs/outputs/requests and conservative signed
vbytes. Inputs must consist entirely of canonical backing and fee credits.
Unknown/unminted receipts are refused. External outputs exactly match obligations;
change uses the canonical base script. Fee plus treasury debit cannot exceed
free input sats, allocation cap or protocol fee cap. The native signer also
rebuilds the public V3 plan, checks its hash, actual Core UTXOs, provenance and
confirmed/latest generations before fresh **and cached** signing work.

Reservation, `beginSigning`, treasury ingress and escrow settlement call the
reserve contract. Bypassing the new client with an old reserve route therefore
cannot start a fee-enabled batch. BTC nonce IDs, encrypted state, spent journal
and recovery scope remain the original common signing lane.

## Top-up source and purpose

The implemented source profile is deliberately explicit: a confirmed ordinary
Bitcoin transaction with exactly one externally controlled P2TR input, no
OP_RETURN outputs, and an output to the graph's base script. The donor signs a
BIP340 message over the exact reserve scope, consensus txid, vout, sats and script
hash, consenting to an **unconditional fee donation**. The canonical funding
verifier checks source/output flags, inclusion and depth; native review checks
actual full/stripped transactions, current unspent state including mempool,
private accepted watch scripts and donor signature. Coinbase source funding is
unsupported by this proof profile, rather than silently assigned six-block depth.

An unknown base receipt, a memo saying “fee”, zero backing, or missing SMP1 does
not establish a donation. Every address-derived receipt remains protected deposit
ingress. Ordinary donation funding needs at least six confirmations (or the
common registry's larger configured depth). Normal deposit mint rules remain
six ordinary / 100 coinbase; Bitcoin spending separately requires 101 for a
coinbase input. Donation marking prevents the same output from minting later.
Voluntary confederation own funds can use the same authenticated external donor
profile; insufficient project treasury is not an unconditional blocker. No
reimbursement claim is created.

## Complete inventory and recovery

Use [the private configuration example](../config/btc-fee-reserve.example.json),
chmod 600, with every service's retained address watch DB. Reconciliation checks
all registered intents plus previously observed private accepted intents, native
immutable descriptors, genesis scan coverage at the exact Core tip, actual
backing/credit UTXOs, full `scantxoutset` over base and accepted scripts, and
`MP.totalSupply == backing * 10^10`. Unknown confirmed receipts remain pending,
never free. Physical file paths are excluded from public evidence hashes.
The deposit and fee observers can use the launch configurator's
`bitcoin_watch_start_height` to use Core's compact filters for earlier blocks.
Reconciliation checks the coverage record, cursor and stored tip hash in
one journal snapshot. An archival remote Core can supply full blocks over HTTPS.
For fast mode, explicitly opt in to the exact trusted HTTPS endpoint in the
private config or use numeric loopback. After an upgrade, scan each watch
journal to refresh coverage. Old scan hashes are trimmed and payment
observations are retained in SQLite.
[RPC setup and storage limits](ADDRESS_REQUESTS_WATCH.md#working-without-a-local-bitcoin-node).

The fee journal is identity-bound outside watch backups. The transaction journal
retains a monotonic set of seen private intents and nondecreasing custody/derivation
generations. A watch rollback or generation rollback cannot recreate old permission. Missing/replaced journals or
lost watch coverage require recovery. Complete unpublished watch history before
the first retained observation remains an external inventory boundary (§15.11);
a local report does not prove that unknown history never existed.

| Observation | Behavior |
| --- | --- |
| Complete, consistent inventory | Eligible for a bounded, threshold-authorized HEALTHY report |
| Known signed family in mempool | Chain UTXOs remain in-flight collateral; whole-input locks prevent free reuse and preserve RBF |
| Known confirmed spend, EVM settlement pending | `SETTLEMENT_REQUIRED`; preserve evidence and allow exact settlement |
| Unknown missing backing/credit or supply mismatch | `RECOVERY_REQUIRED`; pause the common Bitcoin signing lane |
| RPC, reorg race, incomplete scan/watch or immature credit | `OBSERVATION_UNAVAILABLE`; no credit or implicit recovery |

Positive reports require each participant's independent full native observation,
typed public intent, original enrolled DKG board/quorum, a permitted public
publisher (operator or bootstrap-delegated custodian), current
generations and a bounded window. The contract report is a threshold attestation
of Core completeness, not a trustless negative UTXO proof. A later healthy local
scan never clears an incident automatically. Explicit recovery requires a new
canonical HEALTHY report and matching scope/generations, retains the incident
receipt, and clears only this fee pause. Ordinary payment recovery cannot clear
it; unrelated pauses cannot be overwritten. Never restore/reset FROST nonce or
spent state from a watch backup.

RBF keeps exactly the same input set, at most 16 prepared versions and one
collateral lock. Caps can only increase under new authorization and within root
collateral. Fees of mutually exclusive versions are not summed. Once signing
starts, timeout does not release funds. Settlement accepts the exact old winner
after a generation change/pause, charges its actual debit once, preserves the
principal and credits remaining free change. No new private work is licensed by
old-winner recovery.

## Commands and integration

Commands print public JSON; network mutations remain explicit. `prepare` and
`reconcile --deadline` only prepare requests. `propose` opens/publishes the typed
session; existing participants approve it with `sign-evm --fee-reserve-config`.
`execute` authorizes/applies after quorum completion. `reconcile --deadline` also
prepares a suspension report for critical/unavailable observations; unavailable
Core data never produces a positive balance assertion. Durable EOA transactions,
per-job/total/attempt gas budgets and canonical action receipts support retries
and lost-receipt recovery. A consumed authorization alone is not completion.

```bash
frost-signer bitcoin fee-reserve bootstrap-plan \
  --registry "$ADDRESS_REGISTRY" --board "$BITCOIN_SIGNING_BOARD" \
  --fee-cap 500000 --max-vbytes 5000

frost-signer bitcoin fee-reserve reconcile --config /private/btc-fee.json
frost-signer bitcoin fee-reserve status --config /private/btc-fee.json --full

frost-signer bitcoin fee-reserve reconcile --config /private/btc-fee.json \
  --bundle /private/conf-ceremony.json --deadline "$DEADLINE" > report-request.json
frost-signer bitcoin fee-reserve propose --config /private/btc-fee.json \
  --bundle /private/conf-ceremony.json --file report-request.json > report-intent.json
frost-signer bitcoin fee-reserve execute --config /private/btc-fee.json \
  --bundle /private/conf-ceremony.json --file report-intent.json
```

`bootstrap-plan` exports a deployment extension; merge its steps into the reviewed
bootstrap profile. Its binding descriptor identifies the separate enrolled FROST
`getSetFeeReserveOperationHash` / `setFeeReserve` action. It does not deploy or
silently approve anything. Binding review checks canonical runtime, constructor,
reciprocal graph and the first-batch boundary. Reinspect/pin the BTC signing graph
with `--address-signing --fee-reserve-config /private/btc-fee.json` after binding.

For `prepare --method METHOD --file args.json --deadline ...`, integers are decimal
strings, bytes are hex and booleans are JSON booleans. The CLI inserts current
control `(nonce,custodyGeneration,derivationGeneration,deadline)`:

| Method | `args.json` before control insertion |
| --- | --- |
| `allocate`, `raiseCap` | `[allocationId, cap, maxFee, treasuryBoolean]` |
| `release` | `[allocationId]` |
| `admitDonation` | `[[strippedRaw, strippedPrevious, vout, donorSignature, consensusBlockHash, siblings, index]]` |
| `preparePlan` | `[allocationId, batchId, previousBatchOrZero, planHash, unsignedRaw, withdrawalRefs]`; also pass `--bitcoin-plan` with the full public V3 plan |

`donation-challenge --txid ... --vout ...` supplies the scope-bound donor message;
it does not create credit. `recover --manifest ... --state ... --state-key-file ...`
uses the existing BTC manifest/state and explicitly recovers only a fee incident.
No private key belongs in request JSON. Retain old public intents and their
receipts on retries; old pending authorizations remain generation/nonce fenced.

Address service config **V2** pins `fee_reserve_config` separately while retaining
the existing watch, transport and financial journals. Its encrypted status exposes
reserve/reconciliation and low-runway states. V2 `scan` and `serve-once` refresh
the full read-only inventory; status publications recheck the cached inventory
and current Core tip. Missing watch coverage remains unknown. Report publication
still requires the separate typed FROST workflow. V1 stays readable with
`FEE_SERVICE_PROFILE_REQUIRED` until explicit V2 adoption; subsequent downgrade
fails closed. Runway conservatively covers configured future
services using the larger of protocol cap and static stress-rate × maximum
service vbytes. It is a static policy, not a live fee estimator or measured SLA.
An insufficient immutable cap reports `FEE_CAP_PROFILE_REQUIRED`. All profiles
still return `ready=false`; reserve sufficiency does not qualify the verifier,
recipient/epoch continuity or production service operation.

[Evidence](../ops/artifacts/zkspv/worker-integration/btc-fee-reserve-report.json)
separates real native crypto/SQLite and contract logic from explicit Core/EVM/DKG/
SPV/qualification fixtures. Full operational and combined E2E acceptance remains
deferred, along with per-owner credits/subsidies and cross-graph/old-epoch service.
