# DKG protocol V1 — implemented experimental profile

This is the implemented cryptographic protocol V1, not a production acceptance statement.
The current V6 onboarding invitation is a separate format: it commits the required economic and
Bitcoin policy without changing the protocol ID below. See [current acceptance status](UPGRADE_REVIEW_2026_10_18.md) and [first-DKG instructions](FIRST_DKG_ONCHAIN.md). The pre-existing legacy DKG ABI is retained;
new participants exclusively use `createCeremony` and `attestResult`.

## Ceremony creation

On newly deployed registries, `ceremonyRegistrar()` identifies the deployment
account. It creates V1 ceremonies by default. To assign an unused ID to another
account, the registrar calls `authorizeCeremonyCreator(bytes32,address)` before
that account runs `dkg create`. Passing the zero address restores creation by
the registrar. Once a ceremony exists, its creation assignment is final.
The assignment covers one ID and lets the creator choose its manifest. Agree
on the roster, threshold, purpose and transport keys before assigning it.
The registry checks membership separately for packets, attestations and abort.

For an additional ceremony, confirm the registry address, chain and ID, then
send the grant from the registrar's local encrypted Foundry account:

```bash
cast call "$DKG_REGISTRY" 'ceremonyRegistrar()(address)' --rpc-url "$RPC"
cast send "$DKG_REGISTRY" 'authorizeCeremonyCreator(bytes32,address)' \
  "$DKG_SESSION_ID" "$CREATOR" --rpc-url "$RPC" --account "$REGISTRAR_ACCOUNT"
cast call "$DKG_REGISTRY" 'ceremonyCreators(bytes32)(address)' \
  "$DKG_SESSION_ID" --rpc-url "$RPC"
```

Wait for the agreed confirmations before using the grant. Apply the deployment's
gas limits to the transaction. The regular bootstrap deploys the registry and
creates all three ceremonies from the same operator account.

Registries deployed with earlier bytecode keep their original creation policy.
Use the artifacts pinned to an existing deployment when resuming it. A new
registry has a different DKG context and requires a new ceremony.

## Fixed cryptography and serialization

- `frost-secp256k1-tr = 2.2.0`, default features disabled; `serialization` and
  `cheater-detection` enabled. MIT OR Apache-2.0. The crate's native binary
  serialization uses postcard; public packages are hashed as these exact bytes.
- HPKE 0.12.0: DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20-Poly1305,
  base mode. Sender authentication uses eth-account EIP-712 and direct EVM publication.
- Rust Bitcoin 0.32.7 builds the transaction and BIP341 SIGHASH_DEFAULT digests.
- `frost-crypto/Cargo.lock` pins transitive dependencies. Python versions used in
  acceptance are recorded in `signer-agent/requirements.lock`.
- Protocol ID is Ethereum Keccak-256 of
  `SATOHSI_DKG_FROST_SECP256K1_TR_2_2_0_POSTCARD_HPKE_X25519_CHACHA_V1`.
  No client-selected alternative suite is accepted.

ZF's [DKG documentation](https://frost.zfnd.org/tutorial/dkg.html) requires authenticated
confidential round 2 and consistent broadcast round 1. This adapter uses immutable
on-chain package hashes to check the broadcast view before `part2`. This is not
an independent cryptographic audit. The selected crate's license does not establish
maintenance guarantees or audit coverage of this adapter. RFC 9591 does not specify DKG.

## Hash encoding

`signer_agent/dkg_protocol.py`, `DKGHash.sol`, and
`tests/vectors/dkg-v1.json` define Ethereum ABI preimages (never JSON or packed encoding).
Every tag is `keccak256(UTF8(name))`:

```
SATOHSI_DKG_CONTEXT_V1
SATOHSI_DKG_VSS_COMMITMENT_V1
SATOHSI_DKG_ROUND1_PACKAGE_V1
SATOHSI_DKG_HPKE_INFO_V1
SATOHSI_DKG_HPKE_AAD_V1
SATOHSI_DKG_ENCRYPTED_SHARE_V1
SATOHSI_DKG_RESULT_V1
```

The manifest fixes chainId, registry, nonzero session ID, uint64 epoch, purpose
(`keccak256("EVM_AUTH")` or `keccak256("BTC_SPEND")`), protocol ID, ordered
addresses and X25519 public keys, N and threshold. Indexes are positions 1..N.
The bounded profile is 2 <= threshold <= N <= 16. Every participant must complete
DKG; signing requires only threshold participants. No subset renumbering is allowed.

```
rosterHash = keccak256(abi.encode(address[] orderedParticipants))
transportKeysHash = keccak256(abi.encode(bytes32[] transportKeys))
contextHash = keccak256(abi.encode(TAG_CONTEXT, uint256 chainId, address registry,
  bytes32 sessionId, uint64 epoch, bytes32 purpose, bytes32 protocolId,
  bytes32 rosterHash, bytes32 transportKeysHash, uint16 N, uint16 threshold))
commitment[k] = keccak256(abi.encode(TAG_VSS, contextHash, address sender,
  uint32 k, bytes compressedSEC1Point))
round1Hash = keccak256(abi.encode(TAG_R1, contextHash, address sender, bytes package))
info = abi.encode(TAG_HPKE, contextHash, address sender, address recipient)
aad = abi.encode(TAG_AAD, contextHash, uint8(2), address sender, address recipient, uint64(0))
envelope = abi.encode(uint16(1), contextHash, uint8(2), address sender,
  address recipient, uint64(0), bytes enc, bytes ciphertext)
shareHash = keccak256(abi.encode(TAG_SHARE, contextHash, address sender,
  address recipient, bytes envelope))
round1Digest = keccak256(abi.encode(bytes32[] hashesInRosterOrder))
round2Digest = keccak256(abi.encode(bytes32[] senderThenRecipientHashesExcludingSelf))
publicSharesHash = keccak256(abi.encode(address[] roster, bytes[] compressedShares))
resultHash = keccak256(abi.encode(TAG_RESULT, contextHash, round1Digest,
  round2Digest, bool normalizationNegated, bytes32 groupX, publicSharesHash))
```

Coefficient hashes correspond to 33-byte SEC1 points from the deserialized library
package. Identity points and malformed packages are rejected, requiring a fresh ceremony.
`N*(N-1)` directed envelopes are persisted encrypted before publishing their hashes.
Retry sends the same saved bytes. Zero hashes and self-directed round 2 are rejected.

EIP-712 domain: name `Satohsi DKG Transport`, version `1`, actual chainId,
verifyingContract = registry. Types:

```
DKGTransportKey(bytes32 contextHash,address participant,bytes32 transportPublicKey)
DKGEnvelope(bytes32 contextHash,address sender,address recipient,bytes32 shareHash)
DKGReceipt(bytes32 contextHash,address sender,address recipient,bytes32 shareHash,bool verified)
```

The mailbox authenticates transport bindings/envelopes. Recipient receipts are currently
submitted directly on-chain by the recipient **after successful `part3` validation**;
relay publication of receipt signatures is not implemented. The receipt storage hash is
`keccak256(abi.encode(contextHash,sender,recipient,shareHash,true))`.

## Library transformations — do not tweak twice

ZF 2.2.0 `post_dkg` already normalizes the internal group to even Y and applies the
BIP341 no-script-tree TapTweak. The participant records the raw coefficient-sum group,
internal x-only key, tweak, library output before final normalization, normalized output,
and final normalization flag. It calls `EvenY` on the entire KeyPackage and
PublicKeyPackage together, never on individual public shares. The certificate group and
shares refer to this final library-derived group (also for EVM_AUTH). Original commitment
hashes remain unchanged. Odd-Y individual verifying shares are valid.

BTC outputs use `OP_1 PUSH32 <final group X>` / `rawtr(final group X)`. Do **not** call
another Taproot tweak or use `tr(final group X)`. Bitcoin and EVM purposes use separate
ceremonies and package contexts. The local integration checks signatures with both the
canonical Solidity verifier and independent libsecp256k1 consumers.

## Registry and trust boundary

Each phase uses the enrolled chain policy: 24 hours on Ethereum chain 1,
one hour on Anvil/Sepolia. Timeout fails V1 rather than accepting a reduced
roster. Any participant can abort before completion. Completed/Failed/Aborted are terminal.
If a V1 phase expires, `advancePhase` records `Failed` in a separate transaction.
The factories use `requireReservationTransfer(previous, replacement)` to check
the registry state before moving an unused ID. The old V1 EVM_AUTH ceremony
must have recorded `Failed` or `Aborted`. The replacement has the same ordered
roster and threshold, a distinct session ID and a live phase or completed result.
[Confirmed recovery and operator command](RECOVERY.md#unused-factory-id-recovery).
The registry validates point format, immutable message hashes and unanimous attestations;
it does not perform polynomial VSS verification on-chain. Every local participant checks
all published public shares against its own verified library output before attesting.

`FROSTCoordinator.bindDKGRegistry` is a one-time ADMIN_ROLE deployment action.
`createSessionFromDKG` uses that registry, completed EVM_AUTH result and original roster,
and forces checked shares/canonical verifier. Existing share registration cannot overwrite
the populated registry shares. `DKGAuthorization` additionally fixes registry/session/result
at construction. Legacy factory deployment paths do not automatically select this class.


## On-chain carrier

`DKGOnchainBoard` transports the same V1 public round1 and HPKE envelope bytes.
Enrollment is EVM-signed against a canonical invitation fingerprint before the
registry exists; the later EIP-712 DKGTransportKey binding still covers the full
registry/session/context. Board discovery verifies the operator's known address
and the locally built runtime. The immutable board-to-registry binding is checked
by every participant. On-chain publication is authenticated by msg.sender and
bounded to 16 KiB per packet; immutable retries repeat identical payload bytes.
No external mailbox is required for the recommended onboarding path. Receipt
transactions continue to come directly from the validating recipient.

## Ethereum mainnet phase policy

On Ethereum chain 1, current V6 (and historical V5) invitations commit `phase_duration="86400"`, matching
`DKGCoordinator.PHASE_DURATION()` (24 hours per phase). New V6 invitations for
Anvil or Sepolia may commit 3600–86400 seconds, with 3600 as the default.
On those chains the registrar may configure the duration once, before the
first V1 ceremony. Existing sessions retain the duration of their registry.
The longer phase supports on-chain encrypted recipient
delivery with finalized Ethereum observations; it does not suppress
timeouts or reduce the initial all-participant DKG requirement. Run participants
and all three independent role ceremonies concurrently. Current endpoints batch
each sender's transport packets, share hashes and verified receipts; full-round
checks and all-N result attestation remain mandatory. Monitor remaining time and
start a fresh ceremony after expiry. Legacy invitation schema support does not
make an old board/registry runtime compatible with the current build.
In a `bitcoin_custody` invitation, the all-N requirement is per role:
federation EVM includes the representative, while confederation EVM and Bitcoin
are separate custodian-only ceremonies. Equal roster/threshold for the latter
does not mean a shared key. Representative clients skip those two roles.
See the [Ethereum mainnet instructions](ETHEREUM_MAINNET_RUNBOOK.md).
