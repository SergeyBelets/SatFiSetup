# Experimental Citrea arithmetic corrections

This separate patch series is for review and local regression testing. It is not
applied to the retained vendor files or any production verifier. It does not close
the qualification, independent integration review, native baseline or real proof
gates. The earlier baseline manifests describe the unchanged source and remain
unchanged.

The series targets Citrea commit
`2ed54463bb7c4c3f1ff29dcd66d4b28c26fb63c8`, specifically
`crates/bitcoin-da/src/verifier.rs` with SHA-256
`a7784140e36beedee14f89b0077d88c98e272b35dab2c7c9971c97ed2b05c444`.
Apply the three patches in the order in `series.json`. Every patch, intermediate
source and final source has a recorded hash. The helper applies the series only
to a temporary copy and creates a new output file; it refuses modified input and
never overwrites an existing output.

| Patch | Narrow correction | Primary reference |
| --- | --- | --- |
| 0001 | Remove the target-one exception; the existing work formula returns 2^255 | Bitcoin Core `GetBlockProof` |
| 0002 | Preserve `const` compact encoding for zero, small values, leading sign-bit normalization and 256-bit values | Bitcoin Core `arith_uint256::GetCompact`; rust-bitcoin 0.32.7 `Target::to_compact_lossy` |
| 0003 | Subtract timestamps as signed 64-bit values before clamping; convert the nonnegative clamped span for multiplication | Bitcoin Core `CalculateNextWorkRequired` |

The retained Core sources are pinned at
`ee42d59d4de970769ebabf77b89ff4269498f61e` in `../vendor/bitcoin-core-ee42d59d`.
The reference source hashes and test evidence are in `report.json`.
The compact conversion is a small translation of the reference arithmetic, not
a new header, chain selection or Bitcoin consensus implementation.

## Reproduce

```sh
python3 rust/zkspv/patches/apply_patches.py --output /tmp/reviewed-verifier.rs
python3 rust/zkspv/patches/prepare_probe.py
python3 rust/zkspv/patches/generate_vectors.py
python3 rust/zkspv/patches/test_patch_application.py
cargo test --locked --offline --manifest-path rust/zkspv/patches/probe/Cargo.toml
cargo test --locked --offline --release --manifest-path rust/zkspv/patches/probe/Cargo.toml
```

The local test run used preinstalled Rust 1.89.0 and isolated task-local Cargo and
target directories, with all dependencies already cached and networking disabled
for Cargo. It is not a repeat of the full upstream Rust 1.88.0 baseline build.
Run `cargo fetch --locked` separately if the locked test dependencies are absent.
No new guest or EVM verifier is produced.

`prepare_probe.py` extracts the four original function bodies unchanged and the
corresponding bodies from the hash-verified patched source. Separate test-only
wrappers expose their results. The vector generator uses exact Python integers
for the retained Core equations: 832 target values and 80 mainnet retarget cases.
The compact vectors also compare against the pinned rust-bitcoin implementation.
Tests reproduce the original failures and test corrected results in debug and
release profiles; passing an original-defect observation is not a consensus PASS.

Pinned rust-bitcoin 0.32.7 itself maps target one to maximum work. That result is
explicitly recorded as a discrepancy; it is not used as the target-one oracle.
The Core formula gives `floor(2^256 / (1 + 1)) = 2^255`. Work comparisons with
rust-bitcoin omit this one value. Zero target is invalid for Core block proof;
this patch preserves the upstream helper's zero convention and explicitly leaves
validation of invalid targets outside its scope.

Remaining work includes compact decoding overflow/sign validation, unchecked
chainwork accumulation, future-time policy, fork choice/LCA/history, full native
baseline execution, independent review of all changed critical code, supported
isolated proof generation and EVM verification. Retarget vectors cover mainnet's
positive fixed 1,209,600-second period and pow limit. They do not qualify arbitrary
network parameters, zero epoch duration or wider-target multiplication behavior.

## Source notices

Citrea's original licensing remains in `../vendor/citrea-2ed54463/LICENSE`.
The compact correction and reference equations follow Bitcoin Core, copyright
2009–2010 Satoshi Nakamoto and 2009–2022 The Bitcoin Core developers, distributed
under MIT; its full notice is retained in `../vendor/bitcoin-core-ee42d59d/COPYING`.
The supplemental rust-bitcoin source carries `SPDX-License-Identifier: CC0-1.0`.
These notices do not establish license compatibility of the complete Citrea or
RISC Zero integration; the existing qualification license gate remains open.
