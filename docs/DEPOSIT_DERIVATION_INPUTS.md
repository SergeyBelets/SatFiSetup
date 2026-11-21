# Address derivation and per-input crypto

Status: **CODE_IMPLEMENTED_QUALIFICATION_PENDING; Ready disabled**. This is the
arithmetic/encoding layer of specification 2.16 §20.4/20.11. It is not an activated
address registry/mint flow. Existing live SMP1/coinbase mint, Bitcoin plan V2
and signer nonce journals retain their routes. No public transaction or new
ZK proof was run.

The next candidate contract layer is now implemented separately:
[immutable registry, common accounting and issuance fences](ADDRESS_DEPOSIT_REGISTRY.md).
It remains unqualified. The explicit personal-fee bootstrap profile can now
install it before base registration; the versioned signer supports derived
inputs subject to qualification. Legacy base-only plans retain their route.

## Shared statement and IPC

`frost-crypto/src/deposit_derivation.rs` is shared by native tests and pipe-only
IPC. `derivation_qualification.rs` retains synthetic qualification tests and the
unchanged frozen public vector. The 382-byte intent, post-DKG even-Y base,
fixed TapLeaf, strict BIP341 tweak/parity and binding match
[the original profile](../rust/zkspv/derivation-qualification/README.md).
The immutable Python `DepositIntentV1` codec independently checks native hashes,
encoding, script and binding; Python implements no curve arithmetic.

All IPC uses inherited pipes with `--ipc`. Never put private packages or nonces
in argv or logs. `bitcoin_derive_deposit_v1` accepts exactly `op`, `base_group`
(compressed even-Y 33-byte hex) and `intent` (canonical 382-byte hex). Hex has no
`0x` prefix. The public result includes the descriptor and `input_key`;
`ready` is always false. Caller-chosen tweaks, trees and output keys are rejected.

Every input descriptor has numeric `version: 1`, `mode`, `base_group` and
`wallet_context`. BASE has exactly these four fields. DERIVED additionally has
`intent` and `binding_commitment`. Its wallet context is recomputed as SHA256 of:

```text
"SATOHSI_DEPOSIT_WALLET_CONTEXT_V1\0" ||
bitcoin_genesis32 || evm_chain_id32 || deposit_registry20 || custody_graph_id32 ||
dkg_registry20 || wallet_id32 || wallet_result32 || key_epoch8
```

Numbers use the intent's big-endian encoding. This permits different pool and
recipient intents under one wallet epoch. BASE context remains an assertion
for a future canonical DKG reader to authenticate. Neither a hash nor a matching
curve point proves current provenance or authority.

`bitcoin_build_v1` takes existing payment fields plus numeric
`input_key_version: 1` and ordered `input_keys`. All descriptors must share a
base and wallet context, and match their exact prevout scripts. The existing
16-input/32-output outgoing template remains bounded; external funding uses
its separate parser. The legacy builder rejects versioned descriptors.

`bitcoin_nonce_v1` takes `input_key`, base `key_package` and `public_package`.
`bitcoin_sign_v1` additionally takes `nonces`, `commitments` and a 32-byte
`message` sighash. `bitcoin_aggregate_v1` takes `input_key`, base
`public_package`, `commitments`, `shares` and `message`. Derived private packages
remain internal. Ordinary FROST sign/aggregate operate on already-derived
packages without a second Taproot tweak. Base packages remain unchanged.

`bitcoin_attach_v1` takes `package`, `unsigned` and ordered `signatures`;
it rebuilds the transaction and independently verifies each signature with
libsecp256k1 against that input's exact script. These primitives do not manage
nonce consumption, reservations, canonical authorization or SPV. Live plan V2
does not accept them. Future integration must bind descriptors into the plan,
check current provenance before the first nonce and share the durable spent journal.

## Candidate EVM verifier and evidence

`BitcoinDepositDerivation` reconstructs the leaf/root/tweak and computes
`Q = B + tG` with existing vendored `Secp256k1Arithmetic`, following
[BIP341](https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki).
It checks scalar range, coordinates, infinity and base aliases. `derive` returns
computed values; `verify` compares exact output/parity/binding without a quorum
attestation. It does not authenticate an intent's DKG/chain/graph/recipient.
The immutable contract has **`QUALIFIED = false`**. Vendored curve source/audit
provenance and independent review remain unqualified. The candidate address
registry can pin this component, but `QUALIFIED=false` prevents activation of
its derivation scope. Bootstrap and live clients do not enable that profile.

The native mixed-input case signs an actual three-input Bitcoin transaction
using synthetic 2-of-3 keys: BASE plus DERIVED of both output parities. Real
BIP341 sighashes and threshold signatures pass independent libsecp256k1 checks.
This is offline witness verification, **not Core acceptance or SPV**. Negative
cases cover swapped descriptors/signatures, script/binding substitution,
cross-epoch contexts, foreign packages, ambiguous modes and legacy downgrade.
Sixteen public Rust vectors match the EVM verifier. Focused EVM cases cover all
intent fields, parity, encoding, scalar limits, infinity and both base aliases.
Per-derive gas excludes registry storage, calldata pricing, SPV/mint and target
network qualification. Current evidence:
`ops/artifacts/zkspv/worker-integration/address-input-code-report.json`.

The [candidate registry](ADDRESS_DEPOSIT_REGISTRY.md) now implements immutable
batch registration, the [recipient matrix](ADDRESS_DEPOSIT_REGISTRY.md#recipient-matrix), custody/profile
generations, shared consumed/backing/reservations and address mint. Remaining:
qualified verifier admission, authenticated address transport, watch/rescan and
recovery and operational recipient-profile qualification. All applicable conditions remain required before Ready.
The [explicit Bitcoin signing profile](BITCOIN_ADDRESS_SIGNING.md) now connects
V3 plans, per-input registry/provenance/generation checks and common journal recovery.
The separate mint-only client now uses generation-aware admission and Core/proof
review; it does not enable Bitcoin signing or qualify the verifier.
Long Core/proving/consumer E2E remains deferred until the combined contour.
