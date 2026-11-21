# Configurable Bitcoin backend in bootstrap

The standard bootstrap, participant graph verifier and consumers support an
explicit mainnet and testnet4 ZK backends with distinct pinned guests. This is implemented production-path code with local
evidence, **not production activation or approval of a live checkpoint**.

| Bitcoin configuration | Policy | State / SPV boundary | EVM choices |
| --- | --- | --- | --- |
| mainnet | version `1` | MainnetHeaderRelay → MainnetSPVGuard | 1, 11155111, 31337 |
| testnet4 | version `1` | Testnet4HeaderRelay → MainnetSPVGuard | 1, 11155111, 31337 |
| regtest | no Bitcoin policy | RegtestHeaderRelay | 1, 11155111, 31337 |
| mainnet ZK | version `2` | ZkBitcoinStateV2 → ZkSpvAdapter | 1, 11155111, 31337 |
| testnet4 ZK | version `2` | ZkBitcoinTestnet4StateV2 → ZkTestnet4SpvAdapter | 1, 11155111, 31337 |

EVM finality, economic components, deployment fees, custody thresholds and
Bitcoin confirmation depth keep their existing independent checks. Existing V1
profiles select the same contracts and constructor arguments as before. Regtest
ZK demonstration guests are not accepted by this production backend.

Address mode remains an unqualified candidate. A plain bootstrap without
`--system-policy` does not install `BitcoinAddressRegistry`. The explicit full
[system bootstrap](ULR_BOOTSTRAP.md) does deploy/bind the candidate address graph,
request/signing boards and fee reserve, while retaining `ready=false`. This
configuration does not qualify its verifier or activate production Ready addresses.
A status-enabled graph requires the new client/signer profile; the
legacy graph reader rejects it. See [address registry](ADDRESS_DEPOSIT_REGISTRY.md)
and [current architecture](ZK_SPV_ARCHITECTURE.md).

For the full testnet4 setup and local build commands use
[Bitcoin testnet4 + Sepolia](ZK_TESTNET4_SEPOLIA.md). The mainnet example below
retains its original image; it cannot be reused for testnet4. The offline
`zk_spv_cli policy` command selects the network pin from a reviewed V1 checkpoint.

## Policy and enrollment

Start with an independently reviewed mainnet Bitcoin policy using the ordinary
checkpoint workflow. Keep its explicit genesis, raw checkpoint, height,
cumulative work, ten predecessor headers and operational bounds. Change
`version` to `"2"`, set `max_reorg_depth` to `"100"`, and add exactly:

```json
{
  "zk_spv": {
    "backend": "zk-state-v2",
    "relay_profile": "PROOF_ONLY_V1",
    "program_image_id": "a4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea",
    "confederation_id": "<64 lowercase hex characters: keccak256(UTF-8 confederation_id from invitation)>",
    "max_state_age_seconds": "3600",
    "max_tip_lag_seconds": "7200"
  }
}
```

This fragment is a schema illustration, not a complete deployment policy.
Choose Fresh limits explicitly: state age is 1..86400 seconds; tip lag is
1..`max_tip_age_seconds`, whose existing bound is 86400. The image has a fixed
100-block reorg limit. The submission CLI also enforces the reviewed
`max_headers_per_sync` batch bound (1..64); the image/contract consensus ceiling
is 64. Only the two pinned public-network images are supported; other images, networks and relay-certificate profiles,
including FROST relay authorization, are rejected until implemented and pinned.
FROST consumer authorization remains mandatory and unchanged.

Use the complete policy file with the existing onboarding `--bitcoin-policy`
option, normally invitation V6. The signed invitation includes the entire
policy, so changing backend, image, confederation, checkpoint or Fresh bounds
invalidates participant enrollment. V4/V5 mainnet invitations can also carry
the policy; existing V1 invitations require no migration. The config does not
accept a provider-supplied verifier, an arbitrary adapter or a test state.

## Build and bootstrap

Build new test deployments with the pinned RISC Zero 3.1.0 verifier and seal
selector `0xe71e6620`. The SDK verifies the verifier binding before deployment
signing and rejects old selectors during publication. An existing state with
an immutable 3.0.0 verifier needs a separate migration. Follow the
[current proving setup](ZK_PROVER_LINUX.md) on each participant host.

From the repository root, using the repository's pinned Solidity toolchain:

```sh
forge build --root web3
forge build --root rust/zkspv/state-adapter
python ops/generate_abis.py --zk
python ops/generate_abis.py --check --zk
```

The state/verifier build is intentionally isolated: moving its sources into the
ordinary build would change Solidity metadata and invalidate existing bytecode
qualification. The artifact loader has a fixed mainnet/testnet4 state/adapter and shared verifier allowlist for
this build, with no caller-supplied artifact path.
Without `--zk`, ABI generation/checking keeps the existing ordinary-build
workflow, so legacy-only configurations do not require a ZK build.

The existing bootstrap plan deploys one ZkBitcoinStateV2 for the confederation.
Its constructor validates the raw checkpoint and creates its own pinned
Groth16 verifier and adapter. For compatibility, manifest key `relay` names
the ZK state and `spv_guard` names the adapter; `zk_verifier` records the child
verifier. Artifact names distinguish them from the ordinary relay profile.
BitcoinWithdrawalEscrow and BitcoinDepositRegistry bind the same adapter;
MiningPoolV1 uses these ordinary consumer contracts.

The readiness barrier waits for a useful proof update. A checkpoint alone is
not Fresh. Resume reports `AWAITING_BITCOIN_RELAY` with reason
`AWAITING_ZK_PROOF`; it never tries `addBlockHeaders` on a ZK state. Each resume,
verification and participant monetary check rechecks freshness and independent
Core agreement. A latest-observed permanent halt blocks operation immediately,
even before the configured EVM confirmation depth.

## Proof and header operations

Each participant computes working Bitcoin transition proofs independently.
The [participant archive](DISTRIBUTION.md) contains code/program/pins, without
ready receipts, working inputs/journals or caches. Each participant prepares
its own attempt from its Core and archive, then runs `zk_worker prove`.
Default worker `run` also computes locally without publishing. The designated
publisher explicitly uses `zk_worker publish` or `run --boundary all` and the
worker checks local origin plus receipt validity. See the
[multi-participant sequence](DISTRIBUTION.md#рабочий-переход-у-каждого-участника).
The supported local runner platforms are macOS arm64 and Linux/WSL2 x86-64.
Follow the [Linux prover runbook](ZK_PROVER_LINUX.md) before enrollment.
Unsupported platforms, including Linux ARM64 full proving, remain blocked.
Component checks do not qualify fresh proving or physical WSL2 operation.

The self-hosted prover must produce a receipt for the **actual deployed state
address, EVM chain, checkpoint and previous state**. A receipt from a different
deployment cannot be rebound. Submit the exact 1216-byte journal and 260-byte
Groth16 seal using the low-level durable operator entry point:

```sh
python -m signer_agent.zk_spv_cli submit \
  --rpc "$EVM_RPC" --chain-id "$EVM_CHAIN_ID" \
  --evm-confirmations "$EVM_CONFIRMATIONS" --policy "$BITCOIN_POLICY" \
  --zk-state "$ZK_STATE" --bitcoin-rpc "$BITCOIN_RPC" \
  --bitcoin-cookie "$BITCOIN_COOKIE" --identity-file "$EVM_IDENTITY" \
  --journal transition.journal --seal seal.bin
```

Use `status` with the same network/policy arguments to inspect readiness.
`materialize --header-proof header.json` uses the same arguments and identity.
The header JSON must contain exactly `height` (decimal string), `header` (160
lowercase hex characters, no `0x`) and `siblings` (64 lowercase 32-byte hashes).
The path authenticates the header in the current canonical history root; it is
separate from the transaction's Bitcoin Merkle inclusion path.

Proof submission is its own journaled EVM transaction, independent of
materialization and money calls. The identity's shared nonce lane and stable
operation ID support retry/resume without signing another transaction for the
same operation. A confirmed halt cannot be rolled back by a later consumer
revert. Existing header-relay sync/keeper commands reject V2 explicitly; this
entry point does not automatically run a prover or synthesize a history path.
The [consumer-driven local worker](ZK_SPV_WORKER.md) supplies that orchestration,
with its own private inbox and budgets. The low-level helper above validates
the public transition; it does not attest which participant computed its proof
or enforce the worker's local HMAC record. Use the worker workflow for normal
participant operation. The contract verifies the proof claim and does not count
independent physical prover hosts.

Every canonical root change, including append, invalidates the adapter's
cached headers. Re-materialize the deposit/payment header before mint/settle.
The ordinary transaction proof and all SMP1/allocation/FROST checks are still
required. Participant settlement reports `AWAITING_ZK_HEADER_MATERIALIZATION`
when the current root has no valid cached header. A different backend requires
a separately enrolled deployment; existing immutable consumer bindings cannot
be switched in place.

## Evidence and remaining boundary

[Local report](../ops/artifacts/zkspv/bootstrap-integration/report.json) records
a production bootstrap **slice** on disposable Anvil, genuine historical
mainnet Groth16 acceptance, current-root materialization, real 3-of-4 EVM and
Bitcoin DKG, FROST-approved ordinary consumer bindings, participant constructor
provenance, policy mismatch rejection and Fresh expiry. No code/storage
override or mock proof is used. Historical EVM time is explicit; live Core and
the full DAO/onboarding/bootstrap sequence were not exercised by this test.

The state constructor and verifier bytecode retain the earlier qualified pins.
Unit tests cover all three EVM choices and legacy profiles. The previous real
regtest mint remains historical evidence for its original sources; changed
client sources are preserved in `bootstrap-integration/baseline`, and the
overall report does not promote that old receipt to a test of the new clients.

Public deployment, a selected live checkpoint/configuration, continuous
self-hosted proof/transport operation, successful monetary mainnet mint,
mainnet fork/halt qualification and independent review remain separate gates.
