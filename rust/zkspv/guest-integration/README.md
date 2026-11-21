# Mainnet guest integration

This component supplies a real RISC Zero 3.0.3 guest entrypoint, the static mainnet
adapter, deterministic guest packaging and a separate local proof command.
It does not activate production contracts or qualify the complete specification.

`src/lib.rs` constructs Citrea's public `HeaderWrapper` from the raw 80 bytes and
calls `DaVerifier::verify_header_chain(Some(parent), header, Network::Mainnet)`.
The complete pinned `bitcoin-da` dependency is linked, with the previously
qualified five-correction verifier file. There is no extracted replacement core,
test-only visibility change, input-selected network or verifier callback in the
guest. The revealed-transaction prefix and transaction fields are unused by this
header-only API. Header time/Fresh checks against EVM time remain in the contract.

The private input is a four-byte little-endian length followed by the existing
V2 request. The length must be 1..2,281,208 before allocation, and EOF must follow
the request. Inner codec bounds still apply. `env::commit_slice` publishes exactly
1216 ABI bytes after successful verification. A panic produces no acceptable
Halted(0) receipt. No environment clock, network, key, randomness, RPC or remote
prover is required by the guest.

Preparation verifies the complete 887-file Citrea archive and changes exactly
`crates/bitcoin-da/src/verifier.rs` in a new build tree. Existing vendor and
qualified policy sources are untouched. Native and guest lockfiles are separate:
the guest uses precisely the five acceleration/compatibility patches from the
pinned upstream guest lockfile. Every external package's name, version, source
and checksum must match its upstream lock. This is not a blanket dependency audit.

The image tool follows `risc0-build` 3.0.3: user ELF plus the pinned
`risc0-zkos-v1compat` 2.2.0 kernel become a R0BF program; `risc0-binfmt` and official
`r0vm --id` must independently agree. The resulting **experimental** image ID is
recorded in `report.json`. It is not automatically selected in the production
manifest. Build flags mirror the official helper, with source/cache path remaps.
Reproducibility on another machine and licensing acceptance still need review.

## Reproduce

Prepare tools/dependencies while networking is available, using `toolchain.json`
and the Cargo locks. The qualification command itself is offline:

```sh
python3 qualify.py --source /path/to/unchanged-citrea \
  --archive /path/to/pinned-citrea.tar.gz --build-dir /new/build-directory \
  --target-dir /path/to/build-cache --cargo /path/to/cargo-1.88 \
  --rustc /path/to/rustc-1.88 --rustdoc /path/to/rustdoc-1.88 \
  --guest-rustc /path/to/risc0-r0.1.88.0/bin/rustc \
  --cargo-home /path/to/prepared-cargo-cache --r0vm /path/to/r0vm-3.0.3
```

Eight tests run in debug and release: 4032 actual mainnet descendants with every
context field checked across two retargets; bounded input/journal at heights
32257, 34272 and 36288; overlap; invalid PoW, height, parent, difficulty, MTP and
work overflow; forged witnesses/stale state; input bounds, truncation and EOF.
Native invalid-PoW witness construction is explicitly test-only. Guest linking
uses only the production static adapter. Generated inputs contain public historic
headers and dummy deployment chain/address, not a selected production deployment.

After qualification, on macOS with permission to launch `sandbox-exec`:

```sh
python3 prove_local.py --r0vm /path/to/r0vm-3.0.3 \
  --verifier /path/to/build-cache/image-tool/debug/verify_receipt \
  --height 32257 --timeout 600 --output /new/proof-output
```

This command first confirms OS denial of IPv4/IPv6 operations, then runs the
local composite STARK prover and offline receipt verifier under `deny network*`.
It clears inherited environment, disables dev mode, bounds runtime and records
artifact hashes. The verifier is compiled with `disable-dev-mode`, rejects Fake
receipts and trailing encodings, authenticates the image and exact journal, and
checks rejection of a different image and modified journal. Only a successful
`proof-report.json` demonstrates a real receipt. A timeout/incomplete report is
not proof evidence. The build report alone never claims a proof.

Before spawning the prover it requires at least 8 GiB free disk for temporary
files and memory-pressure headroom. This is a conservative launch guard, not a
benchmark of the prover's memory requirements; insufficient space is recorded as
an incomplete attempt and no prover is started.

Groth16 wrapping, positive EVM verification, full guest branch/halt/retarget
coverage, production checkpoint/profile/image selection, consumer activation,
performance/TCO qualification and independent review remain open. A local STARK
receipt cannot be submitted as the state's 260-byte Groth16 seal. Review the
upstream notices: transitive GPL dependencies are not relicensed by the MIT
integration files.

