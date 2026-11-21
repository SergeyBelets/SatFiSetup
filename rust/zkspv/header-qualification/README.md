# Historical header transition qualification

This is a **test-only extraction harness**, not an enabled Bitcoin verifier,
production guest, full upstream native build, or independent consensus core.
The pinned vendor and existing SatoshiFi Bitcoin payment path are unchanged.

The harness copies the complete Citrea header transition and validation helper
bodies from commit `2ed54463bb7c4c3f1ff29dcd66d4b28c26fb63c8`, before and after
the separate experimental patch series. `prepare.py` verifies source, fixture,
parent-series and patch hashes before generating `src/lib.rs`. The whole crate
is gated by `#![cfg(test)]`; it exposes no production verification API.

## Experimental corrections

`0004-checked-chainwork.patch` follows the three arithmetic corrections in
`../patches/series.json` without modifying that historical series. It replaces
the three saturating cumulative-work additions with one helper using the existing
`crypto-bigint::CheckedAdd` primitive and a distinct `ChainworkOverflow` error.
Representable sums are retained exactly; unrepresentable sums are rejected.

`0005-checked-compact-target.patch` adds the pinned Bitcoin Core
`SetCompact`/`DeriveTarget` range checks before target/work arithmetic: negative,
zero, overflowing and above-limit targets are rejected. The existing decoder
is used only after these checks. Testnet4 validates its carried normal target
even when the next historical header uses the minimum-difficulty exception.
This does not add a minimal-encoding rule: Core-permitted equivalent encodings
remain valid at the range-check layer; the transition still requires its exact
expected bits. No timestamp or chain-selection algorithm is replaced.

The patch includes Mainnet, Testnet4 and upstream custom Signet call sites.
Only Mainnet and Testnet4 historical transitions are exercised here. Signet
and Regtest policy are **not qualified** by this result.

## Coverage and boundaries

The unchanged historical fixtures contain 6,111 distinct headers:

| Network | Heights | Headers |
| --- | --- | ---: |
| Mainnet | 40310–42346 | 2,037 |
| Mainnet | 872918–874954 | 2,037 |
| Testnet4 | 40310–42346 | 2,037 |

The Mainnet files are retained vendor fixtures. The Testnet4 file is copied
unchanged from the SHA-pinned full upstream archive; its exact member/hash are
recorded in `series.json`. Initial checkpoint literals come from the original
upstream tests. Their total work starts at zero, so this checks **relative
accumulation**, not absolute canonical Bitcoin chainwork at those heights.

Every transition compares all state fields between original and corrected
methods. Separate pinned rust-bitcoin calls check header PoW, work and Mainnet
retarget results. Each fixture crosses two retarget boundaries. Six resume
cases restore the complete saved state after the final block of an epoch,
including the next target bits, epoch start and MTP window. This does not prove
authenticated checkpoint/LCA discovery, competing fork choice, or history
availability. Tests also check explicit prior-state/header ordering and the
checked-addition representable/unrepresentable boundary.

The compact tests compare against **executed pinned Core C++ arithmetic** and
the verbatim `DeriveTarget` body. The oracle covers 11,266 distinct inputs at four
limits (Mainnet/Testnet4, custom Signet, Regtest and uint256 maximum): 45,064
comparisons per Rust run. Inputs span every exponent/sign with selected mantissa
boundaries plus deterministic samples. This is not exhaustive uint32 coverage
or execution of a complete Core node. The same three compact test bodies run
in the extracted harness and corrected full native crate. The harness also
checks Testnet4 prior-target validation using a valid historical header.

The current extracted run passes 13 tests in each of debug and release. Previous
four-patch sources, reports and logs are retained under `evidence/chainwork-v1/`
with an archive manifest; their PASS is not assigned to the five-patch source.

Small adapters use rust-bitcoin for header decoding/hashing and expose the
existing transition accessor types. They contain no consensus validation rules.
They compute hashes directly, so upstream cached-hash behavior and Borsh wire
serialization are outside this harness. DA transactions, native services,
upstream test-module wiring and proof circuits are also outside it.

## Reproduce

Use Python 3.11+ and exact Rust/Cargo **1.88.0**. All 17 resolved external packages
match the name/version/source/checksum entries in the original Citrea Cargo.lock.
Prepare dependencies separately; the actual qualification commands are offline.
Keep Cargo/target directories separate from the original source checkout.

```sh
python3.11 rust/zkspv/header-qualification/prepare.py --check
python3.11 rust/zkspv/header-qualification/core_target_vectors.py --check
cargo +1.88.0 fetch --locked --manifest-path rust/zkspv/header-qualification/Cargo.toml
python3.11 rust/zkspv/header-qualification/qualify.py \
  --cargo /path/to/rust-1.88.0/bin/cargo \
  --rustc /path/to/rust-1.88.0/bin/rustc \
  --rustdoc /path/to/rust-1.88.0/bin/rustdoc \
  --cargo-home /path/to/task-cargo-cache \
  --target-dir /path/to/task-target
PYTHONPATH=signer-agent python3.11 -m pytest tests/zkspv/test_header_qualification.py -q
```

`qualify.py` records debug/release logs, compiler/binary/dependency/source hashes
and verifies source stability across the run. A failed run does not create a new
passing report. Read `report.json` together with its current source hashes;
older reports do not attest changed code. Cargo `--offline` is not a substitute
for demonstrating denied external egress during actual proving/wrapping.

The Core oracle uses a local C++20 compiler (recorded Apple clang on macOS),
retained source headers and no additional dependency download. Its current
linker flags use Darwin `-dead_strip`; port those flags explicitly for another
platform. `core-reference-manifest.json` pins the archive and all 11 retained
files. `core-target-report.json` binds source/driver/vector hashes and counts.
Both Rust runners reject stale oracle evidence before compiling tests.

## Original native suite

`qualify_native.py` separately executes the original six `verifier::tests` with
the real `bitcoin-da` native feature, without the extraction adapters. It requires
a fresh full archive checkout and verifies every archive file before and after
the run. Dependency retrieval and compilation can be prepared separately with
the same explicit Rust/Cargo 1.88.0 paths and environment as the recorded build.
Keep at least 3 GiB free during the build; native dependencies include substantial
database/RPC libraries. The extraction harness does not replace this suite.

```sh
python3.11 rust/zkspv/header-qualification/qualify_native.py \
  --source /path/to/unchanged-citrea-archive-tree \
  --archive /path/to/pinned-citrea.tar.gz \
  --cargo /path/to/rust-1.88.0/bin/cargo \
  --rustc /path/to/rust-1.88.0/bin/rustc \
  --rustdoc /path/to/rust-1.88.0/bin/rustdoc \
  --cargo-home /path/to/task-cargo-cache \
  --target-dir /path/to/native-target
```

The recorded original run passes all six verifier tests with no ignored tests;
39 tests outside that module are filtered out. All 887 archive files match before
and after the run. Its result is `original-native-report.json` and
`evidence/original-native.log`.
The original work test retains upstream's target-one expectation. Passing this
baseline suite is not a claim that this expectation matches Bitcoin Core, nor
that arithmetic patches, the whole DA suite, or the production policy are accepted.

`qualify_corrected_native.py` accepts the same arguments for a separate run of
the five corrections in the full native crate. Use a dedicated checkout with no
other build running. It temporarily changes only the pinned verifier file and
restores/verifies the original archive tree in `finally`. The test-only adjustment
changes the original target-one expectation to Bitcoin Core's value and adds a
checked-work unit test and three shared compact tests; no original verifier test
is skipped. Its exact diff is `evidence/native-test-adjustment.patch`.
The recorded corrected run passes ten
tests; its outcome is in `corrected-native-report.json`. An interrupted/killed
process must pass archive verification again before the checkout is reused.

## Remaining gates and notices

Other upstream native DA/service tests, authenticated future-time bounds,
fork choice/LCA/history, real isolated proof,
EVM verification and independent critical-code review remain open. The complete
compatibility/production gates are not promoted by this local result.

The original notices remain in `../vendor/`. Extracted Citrea verifier code
retains its MIT OR Apache-2.0 provenance; the copied `LatestDaState` declaration
comes from GPL-3.0-only `sov-rollup-interface`. This combined test harness declares
GPL-3.0-only. The patches use the existing Apache-2.0/MIT crypto-bigint API.
Core reference files retain their original MIT notices and `COPYING` under
`references/bitcoin-core/`.
The complete project's distribution/license review remains a separate gate.
