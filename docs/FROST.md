# FROST

[Documentation Index](./README.md)

For the current end-to-end scenario, use [FROST and ZK lifecycle](FROST_ZK_LIFECYCLE.md):
three independent role ceremonies, VSS and Schnorr equations, one-time nonce
recovery, self-computed ZK transitions and Bitcoin settlement. Economic voting,
FROST authorization, Bitcoin signing and proof validation have separate weights
and permissions. A valid ZK proof does not supply missing threshold signatures.

## Current authorization stack

| Component | Responsibility |
|---|---|
| `DKGCoordinator` | Ceremony manifests, transcript hashes, public shares and unanimous attestations of each completed V1 result |
| `DKGOnchainBoard` | Public invitations/enrollments, public round-one packages, HPKE-encrypted round-two envelopes and bootstrap signing intents |
| `FROSTCoordinator` | On-chain signing sessions, D/E commitments, public shares, verified scalar shares and final result |
| `FROSTVerifier` | Final BIP-340 Schnorr or supported ECDSA verification |
| `FROSTAuthorization` / `DKGAuthorization` | Immutable target authorization; new deployments additionally bind one completed DKG registry/session/result |
| `signer-agent/` | Restart-safe participant implementation and optional relayer |

Research contracts under `crypto/` and `demo/FROSTWithDKG.sol` are not used as the federation authorization boundary. V6 selects Ethereum mainnet/Sepolia/local independently from Bitcoin mainnet/testnet4/regtest; see the [network matrix](NETWORKS_AND_AUTOMATION.md). Start with the [first-DKG runbook](FIRST_DKG_ONCHAIN.md) or [Ethereum V6 instructions](ETHEREUM_MAINNET_RUNBOOK.md). Profile support is not production custody approval.

## Immutable Binding

Each `FROSTAuthorization` is constructed with:

```solidity
constructor(
    address coordinator,
    bytes memory groupPubkey,
    address[] memory participants,
    uint256 threshold
)
```

It stores the coordinator, that coordinator's `defaultVerifier`, the 32-byte group key, ordered participant hash and threshold. None can be changed. Participant or threshold rotation therefore requires a new DKG and a newly deployed federation authorization graph.

New V1 factory deployments instantiate `DKGAuthorization`, which inherits these checks and additionally fixes `registry`, `dkgId` and `resultHash` from a completed `EVM_AUTH` ceremony. Matching a group key alone cannot substitute a legacy session or another DKG result. There is no counter-signature mode, verifier setter or direct aggregate-signature submission method on the authorization contract.

## Session Creation

New DKG-bound deployments use:

```solidity
createSessionFromDKG(bytes32 dkgId, bytes32 messageHash, address target, uint64 deadline)
    returns (uint256 sessionId)
```

It loads the completed result, original roster and public shares from the coordinator's one-time registry binding, binds the message immediately, and requires canonical verification. The bootstrap verifies this binding and renounces coordinator `ADMIN_ROLE`.

The older, explicit-share integration overload remains:

```solidity
createSession(
    bytes groupPubkey,
    address[] participants,
    uint256 threshold,
    uint64 deadline,
    bool enforceSharesCheck,
    address verifierOverride,
    SessionPurpose purpose,
    OriginMeta origin
) returns (uint256 sessionId)
```

An account with `SESSION_CREATOR_ROLE` may use the integration API. Without that role, any listed participant may create a session only when:

- `enforceSharesCheck == true`;
- `verifierOverride == address(0)`;
- `origin.originContract` is a deployed contract.

This removes a permanent session-creation leader while keeping downgrade and verifier-substitution paths closed. The session creator binds the exact operation hash and registers participant public shares. A malicious creator can only spoil its own session; it cannot finalize a signature under the configured group key without valid threshold shares, and another participant can open a replacement session.

## Signing Protocol

1. Obtain `operationHash` from the target's `get*OperationHash(...)` helper.
2. Call `createSessionFromDKG` with the authorization's exact DKG ID, operation hash, target and deadline; this loads and binds the original participant shares automatically.
3. Each signer independently reconstructs the operation from the target helper and checks the session/result binding.
4. For the older explicit-share integration only, the creator instead binds the message and registers shares separately. This path cannot authorize a `DKGAuthorization` deployment.
5. Threshold participants publish one-time D/E nonce points through `submitNonceCommitments(...)`.
6. The coordinator locks the first threshold commitment set and computes the transcript and binding factors.
7. Participants submit scalar shares through `submitVerifiableSignatureShare(...)`; each share is checked against its public share, nonce, Lagrange coefficient and BIP-340 challenge.
8. If aggregate R has odd Y, all participants and the contract use `-R`.
9. Any session participant aggregates verified shares and calls `finalizeSession(sessionId, Rx || z, operationHash)`.
10. Any relayer calls `FROSTAuthorization.authorizeOperation(sessionId, operationHash, target)`.
11. The target checks `isOperationApproved(...)` and calls `consumeOperation(...)` in the same privileged transaction.

## Authorization Checks

`authorizeOperation` requires all of the following:

- finalized coordinator session;
- enforced share verification;
- zero session verifier override;
- exact group key, ordered participants and threshold;
- at least threshold verified shares;
- exact message hash and origin target;
- successful re-verification by the canonical verifier;
- unused session and unexecuted operation.

`consumeOperation` only succeeds for the bound target and only once. A public relayer may authorize, but cannot consume or redirect the operation.

## Operation Hash Domain

Target helpers call:

```solidity
authorization.hashOperation(target, selector, paramsHash, nonce)
```

The digest includes `SATOHSI_FROST_AUTHORIZATION_V1`, `block.chainid`, the authorization address, target, selector, parameter hash and nonce. Signers must reject raw hashes that were not independently reconstructed from the intended target action.

## DKG Requirements

New ceremonies use `createCeremony`, `submitRound1`, encrypted-share hashes,
recipient receipts and `attestResult`. Completion requires **all N participants**
to attest the same result; later signing requires the configured threshold. The
three-friend custody bootstrap selects federation EVM 3/3 and two independent
confederation EVM/Bitcoin keys 2/2, excluding the representative. Initial DKG
requires all members of each particular role, not all invitation addresses for
every role. Historical profiles without `bitcoin_custody` retain 3/3 in all
three ceremonies. See [Bootstrap](BOOTSTRAP.md) and the
[role/key diagram](BITCOIN_CUSTODY_TRANSFER.md).

The participant CLI and pinned Rust adapter perform real distributed key generation,
local VSS validation, HPKE encryption/decryption and encrypted export/import. The
recommended `--board` path carries public packages and encrypted envelopes through
EVM; no shared mailbox or external channel is required. Secret generation and
validation stay local. The registry checks point formats, transcript hashes and
attestations; it does not prove polynomial VSS on-chain. A failed/timed-out ceremony
requires a new session, not a reduced roster.

The library's Taproot transform and group parity normalization must be applied to
complete KeyPackage/PublicKeyPackage objects consistently. Individual public shares
may have odd Y; do not negate each share independently or tweak the group twice.
The registry stores canonical compressed public shares and an x-only group key.
See [DKG protocol V1](DKG_PROTOCOL_V1.md) for exact transformations, and
[Signing protocol](SIGNING_PROTOCOL.md) for durable nonce handling. Legacy
`approveGroupPublicKey` / `completeDKG` remain separate compatibility interfaces;
new participants use the unanimous V1 result path.

## Operational Rules

The optional [NFT governance workflow](NFT_CONFIGURATION.md) reuses EVM_AUTH
to approve typed collection creation and mint calls on the fee manager. It
requires each signer's local NFT pin and public intent; confederation custodian
approvals are an additional check. The manager executes the approved NFT action;
the authorization contract remains an approval/consume boundary.

- Never reuse a D/E nonce pair, including after crashes or rejected transactions.
- Treat missing encrypted nonce state as a hard stop.
- Use bounded deadlines and replace stale sessions instead of mutating signed content.
- Monitor refusals, stale commitments, failed share verification and finalized-but-unrelayed sessions.
- Rotate participants or threshold only through new DKG and redeployment.
- Independently review signer binaries, HSM integration and policy rendering before production.

See the [signer-agent guide](../signer-agent/README.md) for commands and state handling.
