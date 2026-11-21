# Local Groth16 and EVM bridge

This component continues from the genuine composite receipt in
`../guest-integration/evidence/local-proof`. It does not rebuild the guest or
re-execute Bitcoin input. The pinned RISC Zero 3.0.3 SDK verifies each imported
receipt against the qualified image and exact 1216-byte native journal.
Development receipts are rejected and SDK development mode is disabled at build
time. Existing qualified guest/state sources remain unchanged.

## Stages

1. `ops.zkspv.compress_local` uses an explicit local r0vm to compress the composite
   receipt into a succinct receipt. The SDK uses localhost TCP for child-process
   RPC. The macOS sandbox permits that RPC and denies external IPv4/IPv6 traffic;
   a connection probe must pass before proving begins. Its timeout kills the
   process group. `LOCAL_SUCCINCT_PASS` requires a verified unchanged journal and
   matching source hashes.
2. `ops.zkspv.wrap_local` invokes the separate `wrap_groth16` binary on that
   succinct receipt. SDK identity recursion precedes Docker wrapping. The narrow
   Docker adapter accepts only the command emitted by the pinned SDK, substitutes
   a manifest digest, forces `--network none --pull never`, and uses an explicit
   local Unix socket with an empty Docker configuration. It rejects arbitrary
   commands. The launcher also observes the actual container image/network mode,
   independently verifies the output, and removes its uniquely named container
   on success, error or timeout. The host sandbox allows localhost RPC and that
   one Docker Unix socket, with external connections denied.
3. `ops.zkspv.evm_receipt` independently verifies the returned receipt again,
   exports selector plus 256-byte seal, and uses only disposable loopback Anvil.
   The deployed verifier must match the previously qualified ordinary bytecode
   outside declared immutable slots. SDK control IDs and selector must match the
   pinned Solidity constants. Positive mode requires a genuine Groth16 receipt
   and mined successful verification, then rejection of changed image, journal
   digest and seal. `--preflight` checks only parameter compatibility and zero-seal
   rejection; it cannot produce a positive-proof status.

The current journal binds chain 31337 and test state address
`0x1111111111111111111111111111111111111111`. Standalone verifier acceptance does
not imply `ZkBitcoinStateV2.submitTransition` acceptance. Testing that integration
requires a new input/proof bound to an actually deployed state address. No
production image/profile/checkpoint, state mutation or monetary activation is
selected by these tools.

## Host compatibility and preparation

The [3.0 documentation](https://dev.risczero.com/api/generating-proofs/local-proving)
still contains an x86-only warning. A RISC Zero contributor states in
[issue 1749](https://github.com/risc0/risc0/issues/1749) that the Docker x86
requirement has been removed. `container-pin.json` retains the checked registry
index, ARM64 manifest and config for the exact image tag named in SDK 3.0.3.
Manifest availability alone is not proof-generation qualification: consult the
actual run reports. The Docker image is prepared before the network-denied stage.

Use Rust 1.88 and the pinned Cargo.lock. All external package versions/checksums
are compared with the retained Citrea upstream lockfile. Fetch missing packages
in preparation, then build with `cargo build --locked --offline`. The binaries are
`satoshifi-receipt-bridge` (inspect/export and succinct compression) and
`wrap_groth16` (native-platform Docker wrapping). The general bridge's legacy
Groth16 option is conservatively x86-only; the dedicated wrapper is the path
qualified for the pinned native ARM64 Docker image.

```bash
python3.11 -m ops.zkspv.compress_local \
  --bridge /path/to/satoshifi-receipt-bridge --r0vm /path/to/r0vm \
  --timeout 7200 --output /absolute/path/to/new-succinct-run

python3.11 -m ops.zkspv.wrap_local \
  --wrapper /path/to/wrap_groth16 --bridge /path/to/satoshifi-receipt-bridge \
  --r0vm /path/to/r0vm --docker /path/to/docker \
  --docker-socket /absolute/path/to/local/docker.sock \
  --timeout 7200 --output /absolute/path/to/new-groth16-run

python3.11 -m ops.zkspv.evm_receipt \
  --bridge /path/to/satoshifi-receipt-bridge --anvil /path/to/anvil \
  --artifacts /path/to/ordinary-state-adapter-artifacts \
  --receipt /path/to/groth16.receipt --output /path/to/new-evm-run
```

The local wrapper consumes the canonical `evidence/succinct-local` directory;
promote only a completed verified run there. Every output directory must be new.
The build reports bind the exact binaries used on this machine; rebuild reports
for a new host rather than reusing a macOS binary hash. Docker preparation uses
the immutable ARM64 reference from `container-pin.json`, an explicit Unix socket,
and a separate empty `--config` directory, avoiding user credential helpers.
