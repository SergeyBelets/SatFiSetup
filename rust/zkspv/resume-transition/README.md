# Bounded V2 transition statement

This component joins the already qualified `resume-policy` kernel to a separate
public journal and a bounded, runtime-neutral input format. It is **not a ZK
guest build, receipt, production state contract, or proof verifier**. No existing
V1 component/source/evidence is changed and there is no automatic V1 migration.

`execute` checks the deployment checkpoint descriptor, authenticates the accepted
V2 state through `resume`, and constructs a private `Journal` only after the whole
valid prefix succeeds. An invalid batch returns no journal. A valid deep reorg
returns the safe prefix and permanent halt. Known duplicates do not contribute
work/time/counts; an all-known batch returns `NoChange`. Side branches update the
known map/log without becoming useful for freshness. A shorter heavier branch
can be useful. Ignored suffix bytes remain part of the submitted batch digest.

The mainnet consensus callback is trusted static integration code, not an input
selector. Native qualification includes the unchanged prior adapter and actually
calls the full pinned Citrea crate with the five separately tracked corrections.
The full archive is only used by the host to prepare witnesses; execution uses
authenticated paths and never replays earlier headers.

## Journal and EVM boundary

`journal-schema.json` defines exactly 38 ABI words (1216 bytes). Words 0–31 retain
the V1 field positions with the **distinct** `SATOSHIFI_TRANSITION_V2` domain.
Words 32–34 contain the old known-root/log/count and 35–37 contain the new ones.
Both state commitments use the unchanged `resume-state-v2` framed SHA-256 format.

`BitcoinTransitionV2.sol` strictly decodes this version/length, verifies integer
widths and progress/halt shape, requires the known-count delta to equal the unique
new-header count, and independently reconstructs **both** V2 state commitments.
It compares every old-state field against the supplied accepted state, binds EVM
chain/address/checkpoint, and checks the maximum newly verified timestamp against
the accepting EVM clock. The last/tip/maximum timestamps remain distinct.

The library does not authenticate a proof or write storage. Its caller must first
verify SHA-256 of these **exact** bytes with the fixed real V2 program/image ID,
then validate against authenticated storage before any write. No domain rewrite
to the V1 decoder is used. New program identity, real proof, immutable checkpoint
initialization, state persistence, Fresh guards, canonical-membership adapter and
the selected relay-certificate profile still need production integration/review.

The Solidity harness is deliberately unauthenticated and test-only. Shared
fixtures are synthetic graph transitions, not PoW proofs. Its address rewrite is
test transport only; a real receipt must be regenerated for a different address.

## Input boundary for a future guest

`input-schema.json` specifies a 400-byte fixed prefix, a two-byte item count,
length-framed headers and the existing bounded V2 witness blob. Integers use
fixed narrow big-endian widths. The decoder caps counts/lengths before nested
allocations, checks padding/domain, and rejects trailing bytes. It exposes no
clock, consensus/network/verifier selector, executable code, or dev-mode flag.
Only running states are representable. Their commitments are authenticated by
`resume`; encoding a running view cannot authenticate a halted commitment.

Limits: 64 submitted items, 5120 raw bytes, 2,275,554 witness bytes and 2,281,208
total request bytes. A single append uses 31,805 request bytes (31,317 for its
witness) and one new consensus call. These are input bounds, not measured ZK
cycles. The transport must enforce the outer limit **before** allocating its
input buffer; no RISC Zero SDK transport is implemented by this crate.

`execute_encoded` is the tested runtime-neutral entry boundary. A future guest
must statically link the actual pinned adapter and commit `journal.encode_abi()`
using the SDK's raw-byte commitment operation, with no extra serde framing.
There is no compiled guest/image ID/receipt in this component. The available
Apple Silicon host currently has no RISC Zero guest toolchain; the retained
local-proving report also records the x86 requirement for selected Groth16
wrapping. No remote proving fallback or public transaction is performed.

## Verification

Run `qualify.py` with the same exact Rust 1.88, offline caches, complete upstream
archive/checkout, native target and Forge as preceding components. It checks
upstream dependency pins and all prior source hashes, runs Rust debug/release,
regenerates/checks six shared journal vectors, runs Solidity, and runs four new
native tests plus 32 retained native tests. Temporary upstream edits are restored
in `finally` and all 887 original files are checked afterwards. `tests/zkspv/
test_resume_transition.py` independently decodes and hashes both public states.

These results qualify local components only. New codecs, hashing, context-index
logic and integration require independent review and a real isolated proof run.
