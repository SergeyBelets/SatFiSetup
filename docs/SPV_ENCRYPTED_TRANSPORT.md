# Encrypted SPV requests, acknowledgments and status

See [the consumer update and funding diagrams](ZK_SPV_ARCHITECTURE.md) for the
current code boundary and the separate, unactivated address-request profile.

The mainnet ZK bootstrap now creates a separate `SPVMessageBoard` bound to its
confederation factory and `ZkSPVServiceStateView`. Existing DKG packet slots and
DKG/FROST domains are unchanged. This transport stores complete ciphertext on
chain; no HTTP/P2P service or external payload URL is required between clients.

This is implemented code with focused checks and Solidity compilation. The
combined real proving/payment/consumer E2E and deployment qualification remain
pending. No live contracts, operator keys or public transactions were used for
this implementation phase.

## Authentication and confidentiality

The contract authenticates publication with `msg.sender` from the signed EVM
transaction. Federation members may request service for their active federation
inside the bound confederation. A member can explicitly grant/revoke an
operational requester delegate; delegates cannot create further delegates. Service
identities are members of the confederation's existing authorization roster or
explicit operational delegates of those members. These permissions authorize
transport only: they grant no mint/spend/relay certificate or budget authority.
The requester selects a concrete registered service identity as recipient.

Each identity registers a separate HPKE public key with a monotonically increasing
epoch. Old registrations remain immutable/readable. A private key file contains
only an independently generated transport secret/public pair and epoch. Custody,
DKG shares and FROST nonce packages do not belong here. This delivery role does
not adopt the proposed majority relay profile or change any monetary threshold.

Clients authenticate the exact board runtime and canonical constructor, the
confederation factory and the mainnet state/view/adapter policy bindings. They
reuse the existing Rust HPKE X25519/HKDF-SHA256/ChaCha20Poly1305 implementation.
HPKE Base supplies confidentiality; its sender authentication comes from the
canonical board transaction and membership checks. SPV-specific context binds
chain/contract/domain, route, type, sequence, sender/recipient, both key epochs and
expiry. It does not reuse `DKGEnvelope`. An old root/version in the encrypted body
is a hint; it cannot authorize accepting that root.

Public routing metadata includes EVM addresses, board/domain, federation ID on
request publication, opaque request/route/message IDs, type/sequence, transport
key epochs, expiry, transaction time, size and number of messages. HPKE ciphertext
and public transport keys are also public. Transaction IDs, consumer operations,
SLA, state hints, paid-job link and application response body stay encrypted.
Ordinary payment-registry transfers/terms and proof public inputs retain their
existing public visibility. Encryption does not conceal transaction metadata.

## Provisioning and running

Use [the transport config template](../config/spv-transport.example.json) and
replace all placeholders with reviewed deployment values. Copy the exact enrolled
Bitcoin policy V2. Config and identity/key files must be `0600`; the transport
directory must be owned by this OS account and `0700`. Both federation and service
clients need their own config/directory/EOA and key files, but agree on the same
chain, finality, confederation, factory, board and state/policy binding. RPC URLs
and private paths remain local. The template intentionally fails validation.

```sh
python -m signer_agent.spv_transport keygen --epoch 1 --output /private/path/spv-key-1.json
python -m signer_agent.spv_transport register-key --config /private/path/transport.json --epoch 1
```

Key generation does not send a transaction. Registration and every other publish
use the existing shared EOA nonce engine with durable signed bytes and explicit
transport gas/total-cost/attempt limits. Communication gas is funded by the local
sender account; an empty treasury does not create a free publication or automatic
debt. It is accounted separately from worker state/proving publication budgets.

Members can explicitly register an operational delegate:

```sh
python -m signer_agent.spv_transport delegate --config transport.json \
  --federation "$FEDERATION" --recipient "$DELEGATE" --revision 1
python -m signer_agent.spv_transport delegate --config transport.json \
  --service --recipient "$SERVICE_DELEGATE" --revision 2
```

Add `--revoke` to revoke. `--revision` is an explicit local operation identifier;
repeat the same revision/arguments to resume, and use a new revision for a later
re-grant or revocation. This does not rotate custody or grant payment permission.
Clients recheck requester/service roles at confirmed and latest snapshots before
accepting a new request. Revocation does not cancel previously accepted operations
or remove their Bitcoin/payment obligations.

V3 introduced this route; the current [worker template](../config/zkspv-worker.example.json)
is V4 and retains it alongside permanent archive and optional funding automation.
`transport.config_file` names this service client's private config.
`federation_consumers` is the operator's reviewed mapping from federation ID to
its admitted pool/escrow addresses; every address must also occur in the worker
allowlist. This mapping is immutable for the worker directory. It must be derived
from the reviewed enrolled deployment graph, not from the incoming string claim.
The worker authenticates its transport domain on first use and rejects changes.
`transport=null` permits local-only V3 operation; V1/V2 retain their earlier modes.

```sh
python -m signer_agent.zk_worker run --config /private/path/worker-v3.json --boundary all
```

This is the designated publisher/service loop and explicitly permits transactions.
Other participants compute locally using [prepare/prove](DISTRIBUTION.md);
plain `run` defaults to computation and does not poll/publish transport messages.
The publisher worker polls canonical on-chain bytes, decrypts locally, checks scope and roles,
then independently validates the actual consumer/Bitcoin inclusion through the
existing admission path. It queues an encrypted ACK only after validation.
Transient node errors retain the request with bounded retries/backoff, allowing
other requests to proceed. Malformed requests are rejected/quarantined. The contract rejects expired new
publications; a previously canonical timely publication may be read late, retaining
its original service deadline and a visible missed-SLA status. Accepted obligations
survive delivery expiry and transport reorgs. ACK/status
messages never invoke a monetary method.

For the standard participant mint/settlement path use:

```sh
# Add these options to the existing btc-mint-* or btc-settle command:
--zk-transport-config /private/path/federation-transport.json \
--zk-request-federation "$FEDERATION" --zk-service-recipient "$SERVICE_IDENTITY"
```

This is an alternative to `--zk-worker-config`; specifying both is rejected.
The existing funding allocation/withdrawal checks run before the remote request.
When state/header is insufficient, the command queues/sends encrypted work and
returns `AWAITING_ZK_STATE`. Retry the ordinary operation after preparation.
Readiness is checked against canonical state and local Core, followed by all
existing inclusion, FROST and monetary checks. A peer's `READY` reply is a hint,
not authorization or a substitute for those checks.

For an explicit trusted local request file containing the ordinary deposit or
withdrawal need schema:

```sh
python -m signer_agent.spv_transport request --config transport.json \
  --file private-need.json --federation "$FEDERATION" --recipient "$SERVICE_IDENTITY"
python -m signer_agent.spv_transport flush --config transport.json
python -m signer_agent.spv_transport poll --config transport.json
python -m signer_agent.spv_transport status --config transport.json
```

`status` prints only counters and bounded status projections, not decrypted
application bodies. `poll` validates/decrypts and reconciles ACK/status. Publication
and canonical delivery are separate states. With no worker supervisor on the
requester side, repeat `flush`/`poll` under the operator's process supervisor.

## Grouping and payment

V3 `batching.local_service_seconds` explicitly sets the default local SLA. Remote
requests retain their original encrypted service deadlines. A merged execution
group has its own ID, all need IDs, original paid-job IDs, proving attempts and
the earliest deadline. It groups only one worker's authenticated immutable
confederation/state/adapter/checkpoint/program/policy scope. It adds no waiting
window: immature needs wait independently while ready needs/heartbeat progress.
Partial catch-up, rebase, restart, additional compatible needs and another
publication do not extend the deadline or create another full service charge.

An execution group is not a replacement payment job. Canonical paid jobs and
fixed admission terms remain in the payment registry; the worker rechecks their
semantics and canonical payment. New grouping never calls `fundRequest`.
ACKs expose the opaque local need and semantic key privately so the existing
reviewed issuer admission/payment workflow can link them. An optional
`--payment-job <64 hex without 0x>` supplies an existing job hint; the worker
attaches it only if the real registry terms match that need. Group jobs covering
several need IDs retain their existing server-side link and are not rewritten
into singleton jobs. A hint cannot debit a payer or invent `paid`.

If a sufficient Fresh root already exists, the consumer need can reuse it and
materialize a missing header under the bounded confederation maintenance budget.
Confederation-funded fallback remains available as configured, without creating
reimbursement rights or consuming BTC/MP backing. Monetary completion remains
separate from the group's `COVERED` SPV state.

## Bounds, rotation and recovery

The contract bounds a ciphertext to 16 KiB, each envelope TTL to one day, one
immutable ACK and up to 64 sequenced statuses per request route. Request/reply
TTL must include publication and finality time. The client bounds plaintext to
12,000 bytes, inbox/outbox retained history, work per poll, retries/backoff and
transaction/lifetime gas reservations. History includes sent/completed rows;
without the archive option these rows also count toward the admission cap.
The journal preserves previously accepted rights and spending records.

Set optional `history_max_bytes` to a decimal string between 8 MiB and 1 TiB,
for example `"134217728"`, to use separate live admission counts in the existing
`transport.sqlite`. Only unresolved outgoing and incoming operations count
toward `max_pending_messages`. The example config enables this policy with 128 MiB.
Confirmed outgoing ciphertext, handled inbound
records, delivery identifiers, validation failures and all budget/nonce records
stay in the journal. Removing the history policy from an upgraded journal fails
closed. `status` reports the active counts and observed database, WAL and SHM
bytes. The byte limit stops new admissions. Existing obligations may still
need writes, including for rediscovery and reorg recovery. Keep additional
filesystem capacity for the signed transaction and payment journals.

With that option enabled, `poll` and `status` rebuild ACK and status projections
after a confirmed EVM reorg in pages of `max_messages_per_poll`. Repeat the
operation when it reports `SPV_REPLY_HISTORY_RECHECK_REQUIRED`. Payment handling
waits for these pages to finish. A lagging RPC leaves the cursor unchanged.
After an EVM fork a backlog may temporarily exceed the active admission limit.
Previously signed bytes, exact ciphertext and expiry, validation evidence and
payment records are retained throughout the recheck and after restart.

Repeat publication reuses exactly the stored ciphertext, epochs and expiry and
then the existing signed EVM transaction. It does not encrypt again on a retry.
Drain or explicitly recover pending transactions before key rotation: a new
unpublished slot must use the currently registered sender/recipient epochs.
Already canonical old slots remain readable/retryable. A new key epoch affects
new envelopes; retain all private key files needed for old messages. Missing historical keys pause polling with a recovery error.
An unreadable packet is quarantined without logging its body or blocking other
readable packets. Replacing a key file is not a custody/FROST recovery mechanism.

Poll cursors are anchored to canonical EVM blocks. Reorg rewinds canonical reads,
rechecks outgoing delivery and ACK/status projections, and preserves ciphertext,
accepted needs, payment records and all cost/nonce journals. Conflicting observed
bodies for the same route/message are quarantined. Ambiguous signed transactions
use the existing explicit recovery path; do not delete the journals.
`rescan` resets the read cursor and requeues validation-review records; it does
not erase accepted obligations, outgoing bytes or gas/nonce reservations.

An unpublished envelope that expires cannot silently acquire another expiry or
ciphertext in the same slot. After reviewing its signed transaction state, an
operator may explicitly use a new request `--revision` to create another delivery
route. The same consumer need still deduplicates at the worker: existing accepted
SLA/payment identity survives. New delivery does not imply a second service fee.

Heartbeat notices use established request routes and share their sequence/cost
limits. Delivery is tracked per recipient route. Without a registered request
route there is no invented recipient: notices remain in the private local outbox.
A failed/revoked/expired delivery does not fabricate Fresh or halt evidence. Status
and ACK retries never move the heartbeat anchor. A separate long-lived heartbeat
subscription/route renewal policy remains an operational extension.

The focused report records actual Rust HPKE checks, durable retry and grouping
unit tests with explicitly mocked chain/provenance boundaries. Contract runtime
is 10,161 bytes and initcode 12,634 bytes in this build. Combined on-chain transport,
real fresh proving, service payment and consumer execution must still be exercised
together before activation; no production readiness follows from compilation.

## Automatic admission/quote/payment update

Transport V2 can attach an explicitly consented private payment offer. Worker V4
can issue a stable canonical admission and encrypted QUOTE status; the opt-in
payer independently rechecks policy, registry and Core before a bounded transfer.
Shared coverage does not create another full bill per requester. Existing V1
request/manual-payment paths remain supported. See [automation and archive](SPV_AUTOMATION_ARCHIVE.md).
This supersedes the earlier manual quote boundary; combined E2E is still pending.
