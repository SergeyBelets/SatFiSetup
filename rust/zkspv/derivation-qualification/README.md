# Native derivation qualification probe

Historical probe snapshot: the implementation has since moved into the shared
native module with versioned IPC and an EVM verifier candidate. See
[current code and evidence](../../../docs/DEPOSIT_DERIVATION_INPUTS.md).
The report/hashes below retain the original test-only snapshot; they do not
qualify the new IPC, verifier or live address flow.

Status: **native compatibility demonstrated with synthetic keys; full qualification UNKNOWN; production Ready disabled.** This is a separate research artifact for specification 2.16 §§20.3–20.4/20.20. It does not change the common deployment decision or grant any gate PASS.

The implementation is `frost-crypto/src/derivation_qualification.rs`, included only by `#[cfg(test)]` in `main.rs`. It adds no CLI command, IPC request, production derivation API, registry, verifier, address activation or key storage. Existing DKG, legacy wallet keys and transaction construction remain unchanged. No operator secret, reconstructed group secret or signing nonce is recorded. Every DKG and signature in these tests uses deterministic, public **test-only seeds**; these seeds must never be used for custody.

## Exact candidate profile

Profile identifier: `BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1`. The starting package must be the established `frost-secp256k1-tr = 2.2.0` BTC DKG result: upstream `post_dkg` already applies the no-tree Taproot tweak; the application then applies `EvenY`. The probe reproduces `part1`/`part2`/`part3` for three participants, threshold two, and checks this baseline independently using libsecp256k1. Derivation starts from this existing even-Y key; it does not repeat the baseline no-tree operation. The base is the internal key of a *new* intent-specific Taproot output.

The candidate private APIs are:

- `DepositIntent::canonical_bytes()`: validates the profile's fixed shape and serializes the complete intent.
- `derive_public(&PublicKeyPackage, &DepositIntent)`: returns public `Descriptor` and derived `PublicKeyPackage`.
- `derive_key(&KeyPackage, &PublicKeyPackage, &DepositIntent)`: returns the same descriptor and derived participant package after verifying local secret/public share consistency. Source packages are cloned and preserved.

These APIs intentionally do not accept an arbitrary merkle root, script, tree, output key or tweak. They accept already selected DKG packages, not untrusted on-chain claims. Their checks do not establish that a package belongs to an authorized BTC_SPEND ceremony or approved custody graph; that admission is a separate, currently missing production step.

### Intent bytes

The encoding has exactly 382 bytes. It begins with the 25 ASCII bytes `SATOHSI_DEPOSIT_INTENT_V1` followed by one NUL, includes no JSON, ABI offsets or length prefixes, and uses the following fixed order. Integer fields use unsigned big endian. Addresses are the raw 20 bytes. Identifiers/hashes are exactly 32 bytes in their canonical byte order; `bitcoin_genesis` uses conventional displayed block-hash byte order, not Bitcoin wire-order reversal. The probe uses a synthetic identifier instead of claiming a real network.

| Field | Byte offset | Length |
| --- | ---: | ---: |
| domain including NUL | 0 | 26 |
| protocol_version | 26 | 2 |
| derivation_version | 28 | 2 |
| bitcoin_genesis | 30 | 32 |
| evm_chain_id | 62 | 32 |
| deposit_registry | 94 | 20 |
| pool | 114 | 20 |
| mp_token | 134 | 20 |
| custody_graph_id | 154 | 32 |
| dkg_registry | 186 | 20 |
| wallet_id | 206 | 32 |
| wallet_result | 238 | 32 |
| key_epoch | 270 | 8 |
| requester | 278 | 20 |
| recipient | 298 | 20 |
| request_nonce | 318 | 32 |
| service_policy_hash | 350 | 32 |

Both versions must be 1. Zero addresses and zero 32-byte identifiers are rejected as **application shape constraints**. Epoch zero is allowed. These constraints do not check nonce uniqueness, randomness, recipient eligibility, network membership, the real MP token, immutable policy contents or canonical registry relationships. A production caller must establish those facts from authorized finalized state. `service_policy_hash` must commit the immutable funding, recipient and fee-service policy; this probe does not supply that policy's missing canonical schema. Current incident generations must be checked in execution authorization, without rewriting the immutable intent.

`intentId = SHA256(canonical_intent_bytes)`. Output key/script, Bitcoin amount, txid and vout are not inputs to derivation. This avoids a cyclic descriptor and lets a previously registered binding receive an ordinary payment.

### Taproot derivation and share transformation

1. Let `B` be the existing even-Y base group key. Build exactly one Tapscript leaf with version `0xc0` and 34-byte script `0x6a20 || intentId`, using `ScriptBuf::new_op_return` and `TapNodeHash::from_script`.
2. Compute the standard BIP341 `TapTweak` hash of `x(B) || leaf_hash` using `TapTweakHash::from_key_and_tweak`. Validate its raw 32 bytes with `secp256k1::Scalar::from_be_bytes`; a hash at least the curve order is an error, not a reduction or retry with a different intent. Use `XOnlyPublicKey::add_tweak` to reject an infinity result.
3. Apply upstream `Tweak` to the public and participant packages with the fixed leaf hash. The upstream implementation adds the common tweak to signing shares and its generator multiple to public shares. Compare the unnormalized FROST group point with libsecp256k1, including parity. Then apply upstream `EvenY` to each derived package. No scalar arithmetic, point multiplication, FROST round or hash-to-scalar algorithm is implemented locally.
4. Encode the already checked x-only output as `0x5120 || x(Q)` using `ScriptBuf::new_p2tr_tweaked`. Sign using ordinary FROST round1, round2 and aggregate on the derived packages. **Do not call `sign_with_tweak` or `aggregate_with_tweak` on these already derived packages**, which would apply another tweak.

BIP341 permits a zero tweak: it produces `Q = B`. This profile separately rejects any resulting x-only output equal to the base x-only key. The explicit no-alias policy also covers the nonzero case `Q = -B`; neither case is described as invalid BIP341 arithmetic. The synthetic arithmetic tests cover zero, the curve order, all-ones, infinity (`G + (n-1)G`) and opposite-point alias (`G + (n-2)G`).

The sole committed leaf has `OP_RETURN` as its first opcode and cannot provide a script-path spend. The 32 digest bytes are pushed as data, not executed as opcodes. Leaf version and script are fixed; no caller tree, unknown leaf version or OP_SUCCESS path is accepted. This is the existing BIP341/342 script commitment mechanism, not a new cryptographic primitive. The actual **funding output is P2TR**, without an OP_RETURN output requirement. A future direct verifier must enforce the exact same leaf and serialization, not merely accept an arbitrary Taproot root.

`bindingCommitment` is SHA256 over the following concatenation: ASCII `SATOHSI_DEPOSIT_BINDING_V1` plus NUL; all 382 canonical intent bytes including their domain; compressed even-Y base group (33); fixed leaf script (34); merkle root (32); raw tweak (32); output x-only key (32); output parity (one byte, 0 or 1, before final even-Y normalization); P2TR scriptPubKey (34). All lengths are fixed. `Descriptor` stores these public values and `intentId`. Canonical custody-graph and DKG-result admission must establish the roster/threshold committed indirectly by the intent. The public FROST package alone contains no threshold field.

## Pinned primitive reuse and provenance

No dependency or Cargo.lock change is needed. `source-map.json` records crate checksums from the existing lockfile and SHA256/length of the inspected local upstream sources. The FROST crate's published VCS metadata pins commit `3ffc19d8f473d5bc4e07ed41bc884bdb42d6c29f`. Exact primitive locations are recorded in that map.

| Role | Existing implementation |
| --- | --- |
| DKG, no-tree post-processing, threshold shares, parity | `frost-secp256k1-tr 2.2.0`, `frost-core 2.2.0` |
| TapLeaf/TapTweak hashes and P2TR script | `bitcoin 0.32.7` |
| Strict scalar parsing, point validation, tweak check, independent BIP340 verify | `secp256k1 0.29.1` / libsecp256k1 |
| FROST's elliptic-curve backend | existing `k256 0.13.4` |
| Application domain hashes | existing `sha2` SHA256 |

The guard is necessary because FROST 2.2.0 internally reduces its hash to a scalar, whereas BIP341 rejects `t >= n`. `TapTweakHash::to_scalar` panics on that rare input, so this probe uses its raw bytes and the fallible parser. The rejection occurs before either public or secret FROST tweak is applied.

Primary protocol sources: [BIP341](https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki), [BIP342](https://github.com/bitcoin/bips/blob/master/bip-0342.mediawiki). Exact library source: [FROST pinned commit](https://github.com/ZcashFoundation/frost/blob/3ffc19d8f473d5bc4e07ed41bc884bdb42d6c29f/frost-secp256k1-tr/src/lib.rs), [rust-bitcoin 0.32.7](https://docs.rs/bitcoin/0.32.7/src/bitcoin/taproot/mod.rs.html), [rust-secp256k1 0.29.1](https://docs.rs/secp256k1/0.29.1/src/secp256k1/key.rs.html).

Source/version provenance and successful native tests are not an external audit or a qualified contract verifier. Existing project Solidity curve code has not acquired source/audit qualification merely because it sits in a vendor directory.

## Reproduction and evidence

From the repository root:

```sh
cargo test --manifest-path frost-crypto/Cargo.toml --locked --offline
cargo check --manifest-path frost-crypto/Cargo.toml --locked --offline
python3 rust/zkspv/derivation-qualification/verify_public_vector.py
```

`public-vector.json` fixes one public descriptor for deterministic synthetic three-party DKG with `rand 0.8.6 StdRng` seed `[42;32]`. The Rust frozen-vector test compares the result byte-for-byte; the Python standard-library checker independently reconstructs intent encoding, TapLeaf/TapTweak hashes, P2TR script and binding bytes without implementing any curve operation. The ignored `emit_public_vector` test is a deliberate fixture-generation utility, not a skipped requirement. Regenerating a fixture is not evidence that its contents are correct.

The remaining tests preserve the nine existing Rust tests and add six native checks: the baseline key transform; immutable base packages; full intent field binding and fixed leaf; invalid/zero/alias arithmetic; foreign and same-group package mismatch rejection; and eight real threshold signatures spanning all three participant pairs and both output parities. Every signature uses fresh synthetic nonces and passes independent libsecp256k1 BIP340 verification; base/wrong-recipient keys reject it. These messages are synthetic 32-byte inputs, **not Bitcoin transaction sighashes**. There is no claim of Bitcoin Core transaction acceptance.

`qualification-report.json` and `evidence/` record exact commands, results and hashes. An additional source review inside this agent team found and closed missing participant-pair and package-mismatch coverage. It is an internal review, not the independently required integration audit.

## Work required before any address can be Ready

The native profile still needs its separately qualified immutable on-chain verification path, exact public inputs and validation provenance. A quorum signature over an arbitrary output key cannot replace that proof. The existing public key relationship alone establishes neither share availability nor the authorized custody graph, roster, threshold, current generation or recipient eligibility.

Production derivation/descriptor admission must be implemented in the client and **each** signer before nonce generation, with canonical DKG/graph/policy checks and current custody/derivation status. Per-input BASE/DERIVED descriptors need a versioned transaction-plan schema and exact commitment. Current `bitcoin.rs::build`/`attach`, signer `bitcoin_onchain_plan.py`, worker signing and aggregator still use the existing wallet key; changing them is outside this test-only probe. Mixed input signing must select the correct derived package per input, preserve the common family's spent-nonce journal, and bind each nonce to the full transaction/descriptor and current authorization. A derivation label must not create a fresh namespace that permits nonce reuse.

Still unimplemented here: on-chain transport/registration/validated binding and Ready; runtime provenance/revocation admission; full funding→mint→mixed-key Bitcoin transaction→settlement/backing; pinned Bitcoin Core transaction acceptance; externally reviewed derivation/integration; production operator signing availability. ZK-SPV qualification and a real production-profile proof also remain independent requirements. External BTC withdrawal-fee purpose (§22) needs a distinct qualified schema and domain; this deposit-only probe does not cover it.
