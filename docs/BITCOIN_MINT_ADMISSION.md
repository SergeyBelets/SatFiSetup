# Canonical mint admission for the existing pool nonce

`BitcoinMintAdmission` adds a shared on-chain slot to new `MiningPoolV1`
deployments. Its scope is §21.5–21.7: serialize standard clients before creating
a mint FROST session and preserve a recoverable record of attempts. It covers
legacy, custody-generation and address-generation claims; SPV proving/payment
remain separate components.

The candidate [address registry](ADDRESS_DEPOSIT_REGISTRY.md) uses explicit
address/generation-bound mint selectors. Explicit address commands and mint-only
enrollment connect that profile to admission with independent signer proof
review. The actual verifier remains unqualified and no Ready address is issued.
Ordinary configurations keep their current route. See the
[current architecture and state diagrams](ZK_SPV_ARCHITECTURE.md).

The federation binds one admission to the pool through `setMintAdmission`.
This operation has a separate selector and fixed one-time authorization nonce;
it does not consume the pool's mint nonce. The admission's pool, authorization,
coordinator and DKG result are immutable. Binding is not an upgrade of an
existing deployed pool.

## Economic claim and authorization attempts

For direct BTC-funded S issuance on the current V3 reserve, the recipient
locks the fee for the exact Bitcoin outpoint and amount before broadcasting.
Use the same gross allocation for the mint claim. The SDK checks the
confirmed quote before requesting ZK work or submitting the mint; the reserve
checks it again during settlement. [Direct deposit procedure](CONFEDERATION_DAO_IN_KIND.md#прямой-депозит-btc--s).

`getClaimId(txid, vout)` identifies an outpoint in this exact pool. The contract
address and EVM chain identify its storage and authorization domain. A claim
stores its recipients, amounts and original deadline once. `claimOperator`
preserves the first proposer even after an operator transfer. The operator can
open a new claim; another participant can adopt the same active claim without
creating another FROST session.

`slotSession(pool.nonce())` is the single slot across all claims. Creating the
coordinator session and recording the slot are one transaction. Concurrent
requests for different claims cannot both succeed for that nonce. A rejected
request leaves no partial claim or increased attempt count. The caller's EOA
nonce and its cryptographic signing nonces remain separate concerns.

An attempt records the claim ID, exact operation hash, pool nonce, session ID,
its deadline and retirement state. The admission is the actual session
creator, so it can atomically abort an expired open session. Every attempt and
claim remains readable after retirement; the contract never rewrites an old
session for a new nonce. There are at most eight sessions per claim across all
pool nonces. A renewed window, proposer or allocation cannot reset that budget.
On the current issuer and admission pair, each new signing window is limited
to seven days. The originally recorded deadline remains in the claim history.
An explicit retry includes the expected previous session. A subsequent retry
at the same pool nonce therefore has distinct transaction bytes and cannot be
mistaken for a retransmission of the preceding signed EVM transaction. Operator
transfer proposals similarly bind their administrative nonce and retained
predecessor session, including after a subsequent operator rotation.

## Recovery rules

`claimModes` distinguishes LEGACY (0), CUSTODY (1) and ADDRESS (2).
`openAddressMint` validates the full immutable claim; `openCustodyMint` binds the
ordinary SMP1 allocation and custody generation for a status-enabled graph.
Both occupy the same slot as legacy `openMint`. `getAddressClaim` retains the
original snapshot; `getAttemptAddressClaim` applies that attempt's stored
`attemptFences`. `retryGenerationMint` explicitly takes the previous session,
current pool nonce and current generations without changing economic terms.
`openMint` / `retryMint` cannot bypass the new generation profile.

An attempt is permanently unexecutable when its custody generation has advanced,
or (address mode only) its derivation generation has advanced. It may then be
retired even if finalized/approved at the same pool nonce. Resume never revives
it. Derivation suspension leaves independent custody mint available; custody
suspension fences both. An unavailable view alone cannot release an executable
authorization. The stored attempt deadline and global eight-attempt budget still apply.

The current admission build can also retire an attempt when the enrolled deposit
registry has permanently classified its exact outpoint as consumed or as a
wallet output. Check the pool's allocation before reporting completion. A
retired attempt can coexist with a still-recorded authorization whose outpoint
is rejected by the mint entry point. Retirement conserves the pool mint nonce.
This rule applies to the updated admission bytecode. The previous, precisely
identified deployment remains verifiable for recovery under its original rules.
The one-time pool binding keeps its deployed admission address.

### Retire an expired slot and renew the same claim

Any member of the enrolled DKG can run `btc-mint-retire --mint-admission ADDRESS
--session SESSION`. After the attempt deadline, the current issuer advances
the nonce only when the bound admission retires that exact current slot. The
advance and the slot release are one Ethereum transaction. A retired approval
remains in the original authorization history. Its nonce no longer matches any
mint entry point on that issuer. Other pending authorizations for the same
issuer nonce also become stale, as they would after a successful mint.

Mint can still execute after the deadline until retirement is included. If the
mint wins that race, it consumes the output and advances the nonce once.
Retirement then only clears the old slot. If retirement wins, the old mint
reverts on its operation hash and the output remains available for a fresh
approval. No BTC or token balance changes at retirement.

After confirmation, the current operator can run `btc-mint-renew
--mint-admission ADDRESS --session SESSION --deadline UNIX_TIME`. The operator
must specify a future deadline within seven days and a retired session whose
stored deadline has passed. The same outpoint, recipients and allocation
use a fresh FROST session and the original eight-attempt budget. The new
authorization uses a higher issuer nonce or, for an irreversibly fenced
generation, a different current generation from the enrolled registry.
Original deposit validity, proof, maturity and quote checks still apply.
Use the renewed attempt deadline for subsequent mint commands.

New admission deployment requires an issuer that supports nonce retirement.
Previously deployed issuers and their one-time admission bindings use their
original retirement rules. A known prior build can be verified from its
exact artifact and creation evidence. A finalized legacy slot can still
require completion or a permanent registry/generation fence. Enroll the
custody gateway during deployment before opening public mint admissions.

Before reviewing a base mint for signature, the SDK checks the exact Bitcoin
transaction against the issuer's enrolled custody gateway. Funds sent through
that gateway's handover flow follow its cross-wallet settlement procedure.

Address completion requires exact `mintedAllocations`, `mintedAddressIntents`,
`mintedAddressSats`, immutable binding/provenance and consumed state. It does not
require an unspent output or currently active generations. See the
[client commands and proof checks](ADDRESS_DEPOSIT_REGISTRY.md#mint-client-and-admission).

Clients first inspect the pool's exact `mintedAllocations` result. A matching
outpoint and allocation is complete; a different allocation is a conflict.
Neither a session being finalized nor an operation being approved proves mint
completion. The contract rejects further admission for any minted outpoint.

| Canonical observation | Allowed transition |
| --- | --- |
| Same current nonce, OPENED and deadline not passed | Continue the same session. |
| Same current nonce, expired OPENED | The current build aborts the coordinator session and advances the issuer nonce atomically. |
| Same current nonce, ABORTED with no approved operation | The operator may retire and retry within a live window and its remaining budget. |
| Exact outpoint recorded as consumed or as a wallet output | Retire the attempt. Verify the recorded allocation separately to determine whether the claimant received MP or S. |
| Same current nonce, FINALIZED or operation approved | Exact execution remains possible until retirement. On the current build explicit retirement can advance the nonce after expiry. |
| Pool nonce demonstrably advanced | Retire the superseded attempt. For an incomplete claim, retry within the existing window or renew after expiry with the remaining budget. |
| RPC/coordinator observation unavailable | Do not infer retirement or completion. |

The coordinator can finalize an existing session after its deadline. Therefore,
Expiry alone leaves the old authorization available. It becomes stale at the
nonce change or when the enrolled registry permanently fences the output. The
admission's stored deadline is independent of the coordinator's mutable session
deadline. Signers use the stored attempt deadline for new commitments and shares.

The contract provides a single occupied slot with explicit backpressure, not a
complete waiting-queue service. End-to-end bounded waiting queues, latency and
throughput measurement across address and legacy mint modes remain
qualification work. A consumer must retain the original pending-request deadline
and distinguish waiting for admission from Bitcoin, SPV and FROST waits.

## Operator loss and signer checks

Typed public claim and attempt records permit participants to reconstruct the
mint intent without a fresh publication by a lost DKG board operator. Operator
authority transfers through the existing federation's FROST authorization.
`openOperatorTransfer` is available to a roster participant; its public record
contains the new operator, independent `operatorNonce`, hash and original
deadline. At most one transfer session occupies that administrative nonce.
Finalized transfers are preserved just like finalized mint authorizations.
Transferring the operator does not advance the pool mint nonce or reset claims.
This requires the existing signing quorum. Replacing an operator service or
transport writer does not recover a lost threshold share or reduce the threshold.

For a bound pool, standard signing checks require its canonical admission,
current slot and attempt context before a commitment, fresh share or cached
share submission. They verify the typed outpoint/allocation, operation and DKG
authority. Each public Bitcoin mint signer supplies its own
`--bitcoin-rpc "$BTC_RPC"` and `--bitcoin-cookie "$BTC_COOKIE"` to
`dkg sign-evm --session "$SESSION" --mint-admission "$MINT_ADMISSION"`.
The signer checks the same BTC funding, maturity, outpoint, allocation and
inclusion used by the executor and rechecks the exact EVM session after the
observation. The address route applies its additional generation checks.
The pool's on-chain mint path verifies the proof and allocation at execution.

The existing `mintFromProof` entry point and nonce semantics are preserved.
An independently authorized legacy caller may still advance the pool nonce.
The contract's nonce, common deposit consumption and exact allocation remain
the final execution guards. Admission does not claim to eliminate that race;
recovery uses a bounded new attempt without changing the economic claim.

## Qualification boundary

Unit state-machine fixtures explicitly distinguish synthetic DKG/session state
from real cryptographic authorization. They cannot qualify FROST signatures or
the complete address-deposit monetary path. Current source hashes and executed
checks are recorded separately in `ops/artifacts/zkspv`.

The local contract run passed 25 new state-machine tests and 13 existing
`MiningDepositAllocation` tests with a 16,777,216 gas ceiling and Osaka's
transaction gas cap enabled. The new admission runtime is 11,783 bytes and
initcode was 13,255 bytes in the recorded solc 0.8.30 optimizer/via-IR
configuration. Test harness deployment loads compiled fixtures with `deployCode`
to avoid embedding every component's initcode in the test contract itself.
See [contract evidence](../ops/artifacts/zkspv/mint-admission-contract-report.json).
These sizes and counts describe that earlier snapshot. The generation-aware
extension has [separate current evidence](../ops/artifacts/zkspv/worker-integration/address-mint-client-report.json).

For a standalone repeat when no E2E process is using the shared artifacts:

```sh
forge test --root web3 --match-contract '^(BitcoinMintAdmissionTest|MiningDepositAllocationTest)$' \
  --gas-limit 16777216 --enable-tx-gas-limit --isolate --fuzz-seed 0x01 --fuzz-runs 256
```

The recorded run used a private copy of the exact import closure and retained
its source hashes, compiler settings, command and test results. Tests do not
measure deployed service throughput or end-to-end economic latency.

The actual local Anvil/Bitcoin Core scenario passed full bootstrap, adoption of
one claim from another participant home without an extra transaction/session,
operator transfer, continuation of that claim and two proof-backed mints. Three
isolated participant directories used the unchanged 3-of-3 signing quorum,
including the original operator as an ordinary signer. The old DKG transport
operator remained unchanged; typed admission records supplied the new intents.
This is local integration evidence, not a test of quorum availability after
losing a signing share or a production latency measurement.

After the transfer, a read-only check on that deployed graph verified the
bootstrap rule: initial operator at the canonical deployment receipt, immutable
pool binding at the current block. The prior perpetual-operator check correctly
failed on the changed role. See [client evidence](../ops/artifacts/zkspv/mint-admission-client-report.json)
and [receipt check](../ops/artifacts/zkspv/mint-admission-receipt-check.json).

No new SPV job or fee is created by this contract. The eventual service-job
integration must preserve that property when an authorization is superseded.
Address `Ready`, production activation, paid proving and existing live deployment
migration remain outside the completed admission component.
