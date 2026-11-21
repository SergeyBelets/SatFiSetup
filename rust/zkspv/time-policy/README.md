# Header batch time policy

Application component for specification 2.16 §5.5: every newly processed Bitcoin
header must satisfy the future-time bound at EVM acceptance. Bitcoin timestamps
can decrease, so the last timestamp is insufficient. This component computes the
maximum over the complete verified sequence and checks it against **EVM
`block.timestamp` plus 7,200 seconds**, with an inclusive boundary.

## Implementation and trust boundary

`src/lib.rs` is a dependency-free `no_std` library. `verify_batch` calls the
provided consensus transition for each header, then accumulates the timestamp
returned for that same header. Failure returns no resulting state or summary;
empty batches are rejected. There is no prover/host clock argument. The native
integration binds this callback to the pinned, five-patch Citrea mainnet verifier
and uses its MTP, PoW, target, retarget and checked-chainwork validation unchanged.
Checkpoint context is copied verbatim from its historical fixture tests.

`TimeSummary::then` merges consecutive verified chunks with checked count
addition, preserving the earlier maximum and the later chunk's last timestamp.
It does not prove chunk continuity; the caller must authenticate matching states
and history roots. Native tests check continuity for actual historical chunks.
The current wrapper supports linear processing only. Fork choice, halt-prefix
semantics and canonical history remain separate critical code.

The versioned 128-byte ABI component is:

| Word | Solidity type | Meaning |
| --- | --- | --- |
| 0 | bytes32 | ASCII `SATOSHIFI_HEADER_TIME_V1`, right-padded with zeros |
| 1 | uint64 | Number of newly verified headers, nonzero |
| 2 | uint32 | Maximum timestamp of all those headers |
| 3 | uint32 | Timestamp of the last processed header |

Integers occupy a 32-byte big-endian ABI word with zero upper padding. This is
**not a complete transition journal**. The future guest must commit these exact
bytes inside public outputs bound to the program ID, network, checkpoint,
previous/new state and canonical history. The state contract must verify the real
proof and that binding before invoking `BitcoinHeaderTime.validate`, and validate
time before writing state. Passing arbitrary bytes to this library is not proof
verification. No production contract currently imports the library.

`web3/contracts/src/core/BitcoinHeaderTime.sol` rejects incorrect length, domain,
integer widths, empty batches, last > maximum and maximum > EVM time + two hours.
Subtraction after comparison avoids addition overflow at large EVM timestamps.
A future-time failure can be retried later; it does not establish permanent halt.
The last processed timestamp is separate from the selected canonical-head
timestamp required for Fresh policy. Neither field alone implements Fresh.

## Local qualification

`report.json` binds the source hashes and raw logs. The recorded checks are:

- 10 Rust component tests in debug and 10 in release, exact Rust/Cargo 1.88.0.
- 15 full native verifier tests: the previous 10 plus five time-policy integration
  tests. These cover 4,074 distinct historical mainnet headers, real nonmonotonic
  timestamps, split/whole equivalence, atomic errors and the upstream MTP boundary.
- Nine Solidity tests, including two fuzz tests with 256 cases each, EVM-time
  boundary/retry behavior, encoding rejection and a full-width EVM clock.
- 15 shared scalar fixtures encoded by Rust and consumed by Solidity. These
  fixtures have no header/proof authentication and are labelled accordingly.

The native run temporarily adds test integration to a dedicated full archive
checkout, then restores and verifies all 887 original files. The checked-in
vendor and previous five-patch qualification reports stay unchanged. Solidity
tests run in a temporary project using copies of the exact Foundry configuration
and forge-std sources; production build artifact directories are untouched.

```sh
# One-time local build of the vector encoder, using the pinned toolchain.
cargo +1.88.0 build --locked --offline \
  --manifest-path rust/zkspv/time-policy/Cargo.toml --bin encode_times
python3.11 rust/zkspv/time-policy/generate_vectors.py \
  --encoder /path/to/target/debug/encode_times --check
python3.11 rust/zkspv/time-policy/qualify.py \
  --cargo /path/to/rust-1.88.0/bin/cargo \
  --rustc /path/to/rust-1.88.0/bin/rustc \
  --rustdoc /path/to/rust-1.88.0/bin/rustdoc \
  --cargo-home /path/to/prepared-cargo-cache \
  --target-dir /path/to/time-policy-target \
  --forge /path/to/forge \
  --source /path/to/unchanged-citrea-archive-tree \
  --archive /path/to/pinned-citrea.tar.gz \
  --native-target-dir /path/to/native-target
```

Prepare the existing native dependencies and Solidity compiler separately.
Qualification runs offline and performs no RPC, deployment or transaction.
This is not proof-process network-isolation evidence. The complete authenticated
time gate remains open until real guest/proof/state integration and independent
review; production readiness remains false.

New standalone application/Solidity code is MIT. The temporary combined native
test build retains the upstream licenses, including GPL components; no new
project-wide license qualification is implied.
