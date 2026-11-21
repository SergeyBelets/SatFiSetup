# Regtest deposit through an ordinary monetary consumer

This is an explicitly **TEST_ONLY regtest** integration, as selected by the
user. The mainnet guest and `ZkBitcoinStateV2` are unchanged. The completed
result, hashes, timings and gas are recorded in `report.json`; incomplete
attempts must never be treated as PASS.

The path is:

1. Start isolated loopback Bitcoin Core 30.3 regtest and Anvil chain 31337.
2. Run real 2-of-3 `EVM_AUTH` and `BTC_SPEND` DKG ceremonies using the existing
   participant processes and Rust cryptography.
3. Deploy ordinary `DKGAuthorization`, `BitcoinBatchRegistry`,
   `BitcoinWithdrawalEscrow`, `BitcoinDepositRegistry`, `MiningPoolV1` and its
   `PoolMpToken`. Bind both escrow and deposit registry to the state-created
   ordinary `ZkSpvAdapter`; authorize registrations with real on-chain FROST.
4. Fund two real transactions to the DKG-derived P2TR script. Each commits its
   exact EVM recipient, pool, chain, output and amount using the existing SMP1
   allocation. Mine six confirmations. Bitcoin Core independently checks both
   inclusions with `gettxoutproof` / `verifytxoutproof`.
5. Prove six linked regtest headers from an explicit checkpoint at height 2016.
   The guest uses the pinned corrected Citrea regtest verifier for PoW, bits,
   previous hash and MTP. Its regtest path intentionally returns zero work, so
   the test adapter accumulates the validated headers' work with pinned
   rust-bitcoin `Work` arithmetic. Input work is bounded and overflow impossible
   for at most 64 regtest headers. No synthetic nonce-as-work callback is used.
6. Independently verify the composite STARK, succinct receipt and Groth16;
   submit the exact Groth16 to the ordinary pinned Solidity verifier through
   the test state. Materialize the deposit header against the proven history
   root, then call the unmodified `MiningPoolV1.mintFromProof`.
7. Check minted balance, total supply, consumed outpoint, backing satoshis,
   allocation commitment, nonce and event. Reject a second consumption even
   with a fresh real threshold authorization at the current nonce. Expire
   Fresh and check that the second independently funded/authorized deposit
   remains unconsumed, unminted and retains its approval.

The preserved unit convention is `1 satoshi = 10^10 MP base units`.

## Recorded result — 2026-10-30

`LOCAL_REGTEST_ZK_DEPOSIT_PASS`: the real Core transaction
`3dedbe0adbe7c42542c4a9516b9526e37d27aa61c6fc6a362c0c9f514919f6a0`
funded 1,000,000 regtest satoshis and minted exactly 10,000,000,000,000,000 MP
base units after six confirmations. Backing, supply, recipient balance,
consumed outpoint, nonce and allocation were asserted on the actual contracts.

| Stage | Seconds / gas |
|---|---:|
| Composite STARK | 1720.629 s |
| Succinct compression | 275.472 s |
| Groth16 wrapping | 108.567 s |
| State acceptance | 377080 gas |
| Header materialization | 457447 gas |
| Mint transaction | 353349 gas |

The 1521-byte Groth16 receipt has SHA-256
`bdbec55ca16532600250366f0830e00dd16c8c45981858d9c05c72f651b43e1d`.
Every stage was independently verified, including wrong-image/changed-journal
rejection. Native debug/release passed three tests each with identical vectors;
the combined focused Python suite passed 30 tests with no failures or skips.
The two event-decoding warnings during acceptance discard non-`Minted` logs
from the multi-contract receipt; the actual `Minted` event was decoded and
asserted. The Python suite has one existing websockets deprecation warning.

Mint was rejected before proof, before header materialization, with a wrong
Merkle path/index or recipient, on replay, and for an already consumed outpoint
even after a new valid FROST authorization. The second valid deposit was rejected
after Fresh expiry without consuming backing, approval or its mint nonce.
Task-owned Core and Anvil nodes were stopped after acceptance.

## Explicit boundary

`TestRegtestDepositState` is a separate, single-use, chain-31337-only test
receiver. It requires a real proof under a distinct image and the exact
`SATOSHIFI_REGTEST_DEPOSIT_V1` / `bitcoin-regtest` journal domains. It never
accepts a host-supplied root without a proof. Its immutable checkpoint includes
the anchor height/work and eleven raw linked headers. The guest validates
their regtest PoW; earlier history and cumulative work are trusted checkpoint
inputs, not recursively proven.

The unchanged fork/history library and adapter retain their historical
`mainnet-anchor` / `bitcoin-mainnet` hash-encoding labels. This test reuses that
encoding for compatibility; **network isolation is provided by the distinct
guest/image, journal domain, immutable checkpoint and separate test state**.
It is not evidence of a mainnet transition. The test does not qualify V2 bounded
resume, fork/halt, production bootstrap, address mode or fee subsidies.
Participant processes use the existing local E2E ceremony/intent harness and
real on-chain authorization sessions. This does not qualify production
onboarding, the new encrypted SPV request transport or mint-admission recovery.

The receiver accepts only one linear extension and has no reset, owner mint,
development verifier or production enrollment. The ordinary adapter's
`testHelpersEnabled()` remains false because its proof checks are not bypassed;
that getter alone is not a production provenance/qualification decision.
Production deployment selection must continue to reject this test state.

Local EVM time is explicitly advanced for tip freshness and expiration tests.
No public transaction or real funds are involved. Test wallets and private DKG
state stay under the task-owned `/private/tmp` working directory, outside the
public evidence. The runner stops only its recorded Core and Anvil processes.

## Evidence and reproduction

`evidence/current.json` selects the successful preparation directory. Each run
retains raw Core blocks, original and stripped funding transactions, Merkle
paths, standard Core Merkle-block proof, native journal, deployment receipts,
source hashes, proof receipts, EVM checks and cleanup. Failed preparation
directories retain their own logs and cannot produce a completed report.

The build uses the same pinned compiler/archive and external packages as the
qualified mainnet guest (`../guest-integration/toolchain.json` and lockfiles).
Stage the checked vendor tree from the earlier prepared guest workspace at
`/private/tmp/satoshifi-deposit-build`, the RISC Zero compiler at
`/private/tmp/satoshifi-deposit-rust`, and locked Cargo dependencies at
`/private/tmp/satoshifi-deposit-cargo`. Public downloads are separate from
offline compilation and network-denied proving. `deposit_build.py` verifies
vendor hashes, external lockfile equivalence, compiler/runtime pins and ordinary
contract sizes before recording its result. The build output is intentionally
not a replacement production deployment package.

```sh
forge build --root rust/zkspv/deposit-integration
forge build --root web3
python -m ops.zkspv.deposit_build
python -m ops.zkspv.deposit_bridge
python -m ops.zkspv.deposit_trial prepare --output NEW_EVIDENCE_DIR --work NEW_PRIVATE_TMP_DIR
python -m ops.zkspv.deposit_verify_core --output NEW_EVIDENCE_DIR
python -m ops.zkspv.deposit_collect native --output NEW_EVIDENCE_DIR
python -m ops.zkspv.deposit_prove composite --output NEW_EVIDENCE_DIR --timeout 14400
python -m ops.zkspv.deposit_prove succinct --output NEW_EVIDENCE_DIR --timeout 14400
python -m ops.zkspv.deposit_prove groth16 --output NEW_EVIDENCE_DIR --timeout 14400
python -m ops.zkspv.deposit_trial accept --output NEW_EVIDENCE_DIR
```

Prepare the pinned ARM64 Groth16 image before wrapping. The wrapper forces
`--network none --pull never`; `RISC0_DEV_MODE=0` and independent SDK checks apply
to all stages. New attempts use new directories and do not replace receipts.
The separate SDK bridge accepts exactly the 512-byte test journal/domain, with
the same pinned external dependencies and cryptographic verification APIs. The
original mainnet bridge remains unchanged. If a completed STARK was launched
with that original 1216-byte inspector, `deposit_prove recover-composite`
preserves the failed inspector attempt and independently verifies the exact
unchanged receipt with the regtest bridge; the report explicitly records reuse,
not a second proving run.
Native verification checks debug/release equivalence; pytest independently
reconstructs PoW, linkage, MTP, SMP1 commitment, transaction and history Merkle
paths, then verifies proof/EVM evidence binding. Run the focused suite and
`deposit_collect collect` to assemble the final manifest.
