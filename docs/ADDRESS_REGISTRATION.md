# Address registration through the existing FROST quorum

Status: **CODE_IMPLEMENTED_QUALIFICATION_PENDING; Ready disabled**.
The supported derivation verifier still has `QUALIFIED=false`. Registration
preflight rejects it before creating a paid session. Positive component tests
explicitly substitute EVM/DKG/provenance and qualification fixtures; they do not
establish live service or verifier qualification.

## Requester attestation and signing boundary

New address REQUEST plaintext uses version `2`, with a fresh 32-byte
`commitment_salt`. Its on-chain request ID is:

```text
keccak256(abi.encode(
  keccak256("SATOHSI_ADDRESS_REQUEST_COMMITMENT_V2"),
  addressMessageBoard.domain, sha256(intentBytes), commitment_salt,
  requester, service))
```

The requester sends this ID from its own EVM account. Registration publishes
the exact immutable 382-byte intent and an opening `{board,message_id,opening}`.
Each signer authenticates the canonical message board, graph/domain, REQUEST
packet, requester/service route and commitment. No transport secret is exported.
The salt becomes public; it is independent of cryptographic signing nonces.
Legacy V1 requests retain exact retry, ACK and watch semantics but supply no
registration attestation. An explicit new `request --delivery N` produces a V2
delivery of the same economic intent. Neither delivery expiry nor registration
authorization expiry cancels accepted watch obligations or changes the recipient.

`address_registration.py` independently recomputes the complete operation hash
over the ordered batch, graph/derivation scope, custody/derivation generations,
registration nonce and uint64 deadline. It authenticates canonical DKG,
coordinator/verifier/authorization, completed result, enrolled roster/threshold,
role, session creator and public typed intent. The creator must have
`DKGOnchainBoard.canPublishIntent` permission at the verified snapshot: the
original operator, or a custodian admitted by bootstrap's existing delegation.
In custody mode, authority checks use the separate conf-EVM roster/threshold,
excluding the representative. Another enrolled service may export a public
batch for an allowed publisher; an authorized published proposal may be adopted/
executed by another caller. Publication never grants FROST signing authority
or bypasses verifier qualification.
The board is pinned to the canonical FROST coordinator before any intent write.
Even an allowed publisher cannot overwrite/preempt another creator's session
or populate an uncreated session id, including multipart financial payloads.

The participant approval path accepts only `getRegisterOperationHash` for this
target. It requires its enrolled board, exact public intent and confederation
role. The guard runs at the confirmed snapshot and latest block before fresh
nonce creation, cached commitment publication, scalar-share computation and
cached-share retransmission. Current generations, ACTIVE custody, QUALIFIED
derivation, open issuance, actual verifier qualification and the original
registration deadline remain binding. Extending the mutable coordinator deadline
does not extend the typed registration authorization.

## Bounded operator workflow

Use the same private [address-service config](../config/address-service.example.json),
local identity transaction journal and durable gas ledger as request/watch.
Set explicit caps appropriate to measured ECC registration gas. The client never
raises them automatically. The **registration board** below is the enrolled
`DKGOnchainBoard`; config `board` remains the separate address ciphertext board.
Commands do not modify the legacy bootstrap configuration.

```bash
python -m signer_agent.address_transport registration-prepare \
  --config /private/service.json --registration-board "$DKG_BOARD" \
  --registration-intent-id "$INTENT_ID" --deadline "$UNIX_DEADLINE"

python -m signer_agent.address_transport registration-propose \
  --config /private/operator-service.json --registration-board "$DKG_BOARD" \
  --registration-batch "$BATCH_ID"

# Each enrolled conf_ceremony participant reviews/approves the published SESSION
# through the existing participant CLI and its own existing signing state.

python -m signer_agent.address_transport registration-execute \
  --config /private/operator-service.json --registration-board "$DKG_BOARD" \
  --registration-batch "$BATCH_ID"
```

`registration-prepare` accepts repeated `--registration-intent-id`; omitting them
selects at most eight pending local intents. A batch has 1–8 distinct immutable
intents. Its ID commits to the registry and ordered intent IDs. `propose` creates
one DKG-bound session and publishes a canonical typed intent. It resumes the
same EOA transaction key after a lost receipt, persisting the canonical session
anchor before board publication. Participants retain the normal two-round FROST
process. `execute` relays the finalized authorization and calls `registerBatch`,
rechecking the fence after authorization and validating exact native bindings
as its postcondition. Expired registration authorization blocks execution even
when the FROST session was finalized before expiry.

`registration-status` is read-only. Exact canonical bindings are recognized
before checking current permission, nonce, expiry or request packet existence.
This recovers successful execution after a lost response without creating another
session. Consumed authorization alone is insufficient evidence of registration.
`REGISTERED` is a binding result; every command still returns `ready=false`.

## Import, adoption and explicit recovery

`registration-export --registration-batch ID --output FILE` writes only public
intent bytes, registry/pool/board scope and request openings. Another service or
operator uses `registration-import --batch-file FILE --deadline TIME`; it
independently verifies the graph/source and seeds a new genesis watch plan.
No EOA, gas-budget, FROST secret nonce, spent-state or signing package is imported.

`registration-adopt --session N` reads an immutable canonical proposal from the
explicit board, validates its session and preserves its public attempt ordinal.
It can fill a matching locally prepared attempt or initialize local public
history. It cannot silently replace a different pending session.

`registration-retry --registration-batch ID [--deadline TIME]` appends an attempt
only after the previous authorization is definitively unexecutable: registration
nonce advanced, a generation advanced, original deadline expired, or an aborted
unapproved session. An open attempt or an approved, still executable attempt
blocks retry. At most eight attempt ordinals are supported, including after
adoption; prior records remain. A fresh deadline is explicit and does not alter
the intent's recipient, request nonce or service policy. Old generation approvals
remain fenced after resume. A detected session-anchor reorg requires explicit
recovery; the workflow does not silently create another session.

Local history has the configured intent bound; source lookup examines at most
eight saved deliveries. Archive and per-transaction/total gas limits still apply.
Backups/rescans never roll back financial or signer journals. A fresh watch can
recover registered bindings publicly; unregistered obligations still require
their retained data or requester resend.

## Remaining qualification

This code does not enable Ready. Independent verifier/profile review, complete
operational watch coverage, address SPV worker qualification, fee reserve/service,
reconciliation/incident integration, recipient and old-epoch continuity and the
combined Core/prover/payment/FROST/consumer E2E remain pending. Confederation
own-funds payment remains permitted under explicit financial consent; treasury
shortage is not an unconditional service blocker.

The [address SPV worker integration](ADDRESS_SPV_WORKER.md) connects registered
intents to consumer/watch requests and automatic admission/quote/payment.

[Current evidence](../ops/artifacts/zkspv/worker-integration/address-registration-client-report.json)
records focused component checks and their fixture boundaries. Frozen
[Python/Solidity vectors](../web3/test/fixtures/address-registration-operations-v1.json)
cover request commitments, ordered batch IDs and operation hashes.

New registrations enforce the [recipient matrix](ADDRESS_DEPOSIT_REGISTRY.md#recipient-matrix)
on-chain and in each approving client's independent intent validation. Contract
selections bind the exact mode/context/runtime/advanced acknowledgement through
`request_nonce`; existing registration operation hashes and the wire intent size
are unchanged. Current eligibility does not rewrite an accepted historical binding.
