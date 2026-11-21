# V2 state and monetary adapter — local component

This separate Foundry project adds an immutable `ZkBitcoinStateV2` and its
`ZkSpvAdapter`. Existing SPV contracts, consumers, deployment scripts and V1/V2
evidence remain unchanged. Production activation is **not** performed. A real
SatoshiFi guest/proof, target/checkpoint/policy selection, independent review,
consumer provenance/bootstrap integration and full monetary E2E are still open.

## Construction and proof boundary

The constructor validates the raw mainnet checkpoint with the qualified
`BitcoinCheckpoint` library. `BitcoinCheckpointV2` derives the exact initial
history/index/log/state roots used by Rust `resume-policy::initialize`, including
all native context fields. Initial roots cannot be supplied independently. Three
actual mainnet checkpoints (32256, 34272, 36288) are compared across languages.
Earlier cumulative work remains trusted checkpoint configuration.

The state creates its own unmodified, pinned `RiscZeroGroth16Verifier` from
`vendor/risc0-ethereum-365e7b2`, using that revision's `ControlID` constants. It pins
the resulting address/code hash and the explicit nonzero program image ID. There
is no verifier injection, proxy, admin upgrade, dev-mode option or anchor reset.
The exact 1216 journal bytes are SHA-256 authenticated through the verifier's
unconditional `verify` API. A seal must be exactly 260 bytes. V2 shape, EVM domain,
checkpoint, old state and timestamps are validated before any state writes.
Replay is an explicit revert. A permanent halt retains its safe prefix and record.

Only explicit `PROOF_ONLY_V1` is implemented; the constructor requires that exact
profile. This is not a decision to use it in production. FROST certificates/relay
authority are not implemented or bypassed by a fallback. Confederation ID is
immutable and bound to this state address; the journal binds the state address.
The actual guest image ID remains unknown. The fixture image ID `0x…007b` does
not identify an implemented guest. Pinned verifier circuit compatibility with a
future guest/prover still requires a real receipt test. Upstream GPL notices and
the project's unresolved whole-build license acceptance are preserved.

## Fresh and cache

Fresh requires no halt, an already accepted useful proof update, and both
`now <= lastUsefulAcceptedAt + maxStateAge` and
`now <= canonicalTipTimestamp + maxTipLag`. Comparisons avoid overflow. Constructor
initialization does not count as useful progress, even at EVM time zero. Only
strictly increased canonical work refreshes `lastUsefulAcceptedAt`; side branches,
materialization and replay do not. An old/nonmonotonic canonical timestamp may
leave a valid catch-up stale immediately. Staleness itself does not set halt.

All limits are explicit immutable inputs; `maxTipLag` cannot exceed the supplied
baseline limit, and the latter must be in the existing guard's 1..86400 range.
The deployment review must authenticate that supplied baseline against actual
consumer bindings. Test limits are not production parameters or a heartbeat SLA.

`materializeHeader(uint64 height, bytes raw80, bytes32[] historyPath)` is
permissionless, verifies the existing 64-level canonical-history proof and binds
the transaction Merkle root through the exact raw header. It stores only height,
transaction root and the accepted canonical root. A canonical-root change
invalidates old entries by equality comparison, without walking history; even an
ordinary append requires re-materialization of a used block. Side-only updates
retain cache validity. This conservative bounded policy is deliberate and its
extra gas must be included in TCO. Diagnostic membership while stale may be
materialized but cannot reopen the monetary methods. Halt rejects materialization.

The adapter preserves the actual three-method `IPaymentSPV` ABI, with
`testHelpersEnabled() == false`. Its monetary methods enforce Fresh and current
canonical membership for every caller. Confirmations are accepted height minus
block height plus one, bounded by checkpoint/head. Transaction siblings remain
the original transaction Merkle path, using the existing relay's SHA256d order
and 32-level limit. The raw state has no monetary SPV methods. Consumer bootstrap
must authenticate the state-created adapter address and the exact runtime graph;
these components do not authorize switching an existing pool themselves.

Update, materialization and money are separate transactions in this profile. A
successful halt update must be retained before any later money call. No combined
money wrapper or additional pool authority is introduced. As with any EVM call,
an enclosing transaction can revert its own uncommitted subcall; no claim of
persisting a halt through an arbitrary reverting wrapper is made.

## Reproduction and evidence

Use exact Rust 1.88 and the preceding offline Cargo cache to build `state_vectors`
in debug/release. `generate_vectors.py --binary PATH --check` checks three real
checkpoint initializations and ten **synthetic** transition/membership vectors.
The latter are not PoW or proof fixtures. The standalone `foundry.toml` uses
Solidity 0.8.30, optimizer runs 1, via-IR and Osaka; aliases refer to unchanged
repository libraries and the pinned upstream verifier. Build outputs belong in
a temporary directory, not in the existing production artifact directory.

`forge test` runs 25 checks under the 16,777,216 transaction gas cap. Positive
state-machine cases use Foundry `mockCall` for the exact `(seal,imageId,digest)`
at the real verifier boundary. No mock verifier exists in deployable code. Those
tests qualify state/Fresh/membership behavior only. Negative tests exercise the
actual verifier and reject zero/dev/wrong-selector/trailing seals and byte/code
changes. Python independently reconstructs state/history hashes and compares the
compiled adapter ABI with the actual `IPaymentSPV` declaration.

Separately run a normal `forge build --skip test`, then `local_receipts.py` with
that artifact directory and the local Anvil binary. It binds a disposable node
only to 127.0.0.1, deploys the complete state/verifier/adapter graph at all three
checkpoints with test accounts, checks runtime against ordinary artifacts outside
declared immutable slots, and records successful creation receipts and reverted
fake-proof receipts. No real proof acceptance or public transaction is claimed.

`qualify.py` takes explicit `--cargo`, `--rustc`, `--rustdoc`, `--cargo-home`,
`--target-dir`, `--forge` and `--local-receipts` arguments. It checks all preceding
source hashes, exact upstream external dependency versions, both vector builds,
Solidity results, ordinary bytecode, ABI and the source/artifact-bound receipt
evidence. `report.json` separates local component PASS from the unfinished real
proof, production, FROST and review gates.
