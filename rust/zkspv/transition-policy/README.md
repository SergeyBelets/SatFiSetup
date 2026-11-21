# Complete transition journal component

This integrates the existing checkpoint, fork/history and time components into
one deterministic application transition. It is NEW_INTEGRATION, not a real ZK
proof, a guest image, or an activated state contract. No production path imports
the Solidity library yet, and no mock receipt is accepted as production evidence.

`execute` validates the checkpoint descriptor, reconstructs the full admitted
history with the pinned verifier, compares **every** field with the externally
accepted View, and processes one ordered batch of 1..64 items. It accepts no
serialized native context or host clock. The verifier and context adapters must
be statically selected trusted code in the future guest, never witness callbacks.
This component adds no Bitcoin consensus rules or arithmetic.

Only a successful transition produces a Journal and next state. Invalid unknown
headers produce no output or caller-state mutation. A valid forbidden reorg
preserves the safe prefix, retains the last safe canonical head, sets permanent
halt and records its exact one-based input position. Later bytes are not processed.
Exact known headers contribute neither work nor time; a pure overlap batch returns
`NoChange`. New losing branches change the state commitment but are not useful
head progression and must not refresh `lastUsefulAcceptedAt`.

The time summary is produced inside the same verified-header callback as the fork
transition and checked against its new-header count/maximum. It includes newly
verified losing branches and the halt candidate, excludes replayed history,
duplicates and ignored suffixes, and keeps canonical-head time separate from the
maximum and last-new-header time. EVM time remains the acceptance authority.

## Public bytes

`journal-schema.json` is the precise word map. `encode_abi` returns exactly 1024
bytes (32 fixed ABI words), with strict widths/padding and no trailing bytes.

| Words | Meaning |
| --- | --- |
| 0–4 | V1 domain, Bitcoin mainnet, EVM chain ID, accepting state address, checkpoint descriptor |
| 5–6 | Immutable anchor hash and height |
| 7–10 | Previous/next state commitments and canonical-history roots |
| 11–18 | Previous/next canonical head, height, cumulative work and tip timestamp |
| 19–21 | Complete submitted-batch commitment, submitted count and processed count |
| 22–25 | Unchanged header-time ABI: domain, unique new count, maximum, last new timestamp |
| 26–31 | Halt flag, one-based position (zero if running), safe head, candidate, LCA, depth |

Bitcoin hashes use raw SHA256d bytes, as the existing components do. Integers are
unsigned big endian; addresses/integers are left-zero-padded and bytes32 domain
strings are right-zero-padded. Work remains uint256, not a truncated host integer.
The batch commitment uses the existing fork framed-SHA256 format with domain
`submitted-batch-v1`, one part per submitted byte string. It commits ignored suffix
bytes without claiming they were interpreted or validated. Fork state commitments
transitively bind all retained difficulty/MTP contexts, side branches and history.

The program/image ID is pinned by the eventual real proof verifier. It is not a
host-generated placeholder or self-referential field inside this program's image.
The future contract must authenticate the SHA256 digest of these **exact** bytes
against that fixed program before accepting the statement. Neither the kernel nor
the decoder establishes this authentication. Relay certificates, if selected,
must bind the same statement and do not replace the real proof.

`BitcoinTransition.sol` checks exact encoding, widths, counts, unchanged-head
semantics, monotone chainwork, halt/position bounds, accepting chain/address,
checkpoint descriptor, complete current state and EVM future-time limit. It is
an internal library with no storage writes and no proof-verification substitute.
Its `useful` result only describes chainwork progression: full Fresh policy,
storage updates, membership materialization and monetary adapter remain separate.

## Qualification and remaining work

14 Rust tests run in both debug and release. Six synthetic graph fixtures exercise
the complete ABI, including nonmonotonic time, losing branch, shorter/heavier,
depth 100 and safe-prefix halt at 101. These fixtures do not claim Bitcoin PoW.
Eight Solidity tests check the same bytes, domains, replay, malformed encodings,
time boundaries and halt. Their harness is explicitly unauthenticated test code.

Four new full-native tests run with the actual pinned five-correction verifier,
alongside the 24 retained tests. They resume across both historical retargets,
compare exact native contexts and work, process real nonmonotonic timestamps and
overlap, and reject invalid unknown headers, no-ops and forged accepted metadata.
The native checkout is restored and checked against all 887 archived files.

Full replay is still unbounded; this is not a compact proving witness or a catch-up
aggregation solution. Guest input decoding/resource limits, real proof/image ID,
network-isolated proving/wrapping, production state/adapter, relay profile,
production checkpoint selection and independent review remain open. Existing
component evidence is retained byte-for-byte. Overall production gates stay closed.

```sh
python rust/zkspv/transition-policy/qualify.py \
  --cargo /path/to/cargo-1.88.0 --rustc /path/to/rustc-1.88.0 \
  --rustdoc /path/to/rustdoc-1.88.0 --cargo-home /path/to/prepared-cargo-cache \
  --target-dir /path/to/transition-target --forge /path/to/forge \
  --source /path/to/pinned-full-citrea-checkout --archive /path/to/pinned-citrea.tar.gz \
  --native-target-dir /path/to/prepared-native-target
```

All qualification commands are offline; report/evidence bind source, dependency,
compiler and log hashes. No public deployment, RPC, transaction or hosted proving.
