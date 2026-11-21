# FUNDING_STRIPPED_100K_V1: EVM parsing component

`web3/contracts/src/core/BitcoinFundingParser.sol` implements the separate
transaction parsing component of §21.2–21.3. It consumes a **complete canonical
non-witness serialization of at most 100,000 bytes**. There is no artificial
256-input/output cap. Each input needs at least 41 bytes and each output at
least 9; counts are checked against the available bytes before allocating memory.

`parse(raw, targetVout)` scans every input and output once, checks canonical
CompactSize, all lengths, sequence fields, the locktime boundary and the exact
end of the serialization. It returns all input outpoints, the output count, the
selected amount/script and the raw double-SHA256 digest. Input hashes use their
serialized byte order. The returned transaction hash matches the existing
`BitcoinTxParser.txId`; reverse it for RPC display order. Unselected output
scripts are skipped without allocating or rescanning them.

For a witness transaction, the existing `inspect-funding` command obtains the
stripped form through pinned `rust-bitcoin 0.32.7`. The EVM component rejects
marker/flag serialization; the full original transaction remains available for
recovery. It imposes neither a P2TR-only input policy nor mempool standardness.
Parsing does not execute Bitcoin scripts or prove inclusion or unspentness.

## Current integration boundary

The parser is now wired to candidate `BitcoinDepositRegistry.consumeAddress`
and `MiningPoolV1.mintFromAddressProof`. That path validates the immutable binding,
current custody/derivation generations and provenance, SPV inclusion/depth, and
**every input** against consumed/wallet-output/reservation state. It atomically
shares the existing outpoint/backing ledger. See [registry](ADDRESS_DEPOSIT_REGISTRY.md)
and [architecture diagrams](ZK_SPV_ARCHITECTURE.md).

Legacy SMP1 retains its parser and 32-output allocation limit. Native derived
per-input crypto exists; live generation-aware signer/client/watch/recovery and
independent verifier qualification remain pending. The actual verifier candidate
has QUALIFIED=false. Calling this pure parser grants no mint authority or Ready.

Transactions outside the envelope require the protected accounting and qualified
continuity/recovery path of §21.4, which is still unimplemented. Such transactions
must not be silently treated as absent or free treasury backing. Target-network
calldata limits and the gas for the complete storage/SPV/mint path are UNKNOWN.

## Reproducible local evidence

The deterministic fixtures are in
`web3/test/fixtures/funding-stripped-v1.json`, with separate raw hex and compact
metadata files. Python constructs independent serializations and compares every
input/output with the pinned Rust codec. The EVM tests compare the same txids,
target fields and an ABI hash of **all** input outpoints.

Fixtures cover mixed scripts, 33/256/257 outputs, 257 inputs, maximum structural
counts of 2438 inputs and 11105 outputs, a 65536-byte script and exactly 100000
bytes. They exercise serialization and are not claimed to be mined transactions.
Structural rejection tests cover truncated fields, nonminimal CompactSize,
witness encoding, missing/extra bytes and invalid counts/indices; legacy overlap
is also checked with fuzz tests.

```sh
PYTHONPATH=signer-agent python3.11 tests/zkspv/funding_vectors.py --check --rust
PYTHONPATH=signer-agent python3.11 -m pytest tests/zkspv/test_funding_parser_vectors.py -q
PYTHONPATH=signer-agent python3.11 -m ops.zkspv.funding_benchmark
```

The benchmark uses a temporary project with the existing compiler settings,
private build artifacts and a fresh loopback Anvil. Both Forge checks and actual
transactions use a 16,777,216 gas ceiling with the Osaka transaction cap enabled;
the repository gas/code limits are not raised. Receipt gas includes calldata,
dispatch, parsing and the harness's input/script hashes. The harness has no
custody storage, SPV or mint operations. Its cost therefore cannot close the
production funding-compatibility gate.

The 27 October 2026 local run passed 23 Solidity tests and 11 actual transaction
receipts, including selection of both long scripts. Selected receipt measurements:

| Structural fixture | Stripped bytes | Receipt gas |
| --- | ---: | ---: |
| 257 outputs | 2657 | 305481 |
| 257 inputs | 10986 | 686120 |
| 2438 inputs | 99979 | 6686909 |
| 11105 outputs | 99998 | 11649932 |
| Exact envelope, short selected output | 100000 | 4021760 |

The largest measured receipt leaves 5127284 gas below the local ceiling. The
100000-byte fixture is calldata-floor-bound under EIP-7623, so its receipt minus
ordinary intrinsic gas is not a measurement of pure execution. These figures
are fixture results, not a universal worst-case bound for the future consumer.

Results, exact commands, versions, source hashes and receipt evidence are in
`ops/artifacts/zkspv/funding-parser-report.json`. The standalone library has no
deployment/upgrade authority. Refer to [ZK-SPV status](ZK_SPV.md) and
[migration boundaries](ZK_SPV_MIGRATION.md) before integrating it.

The canonical encoding rules can be checked against Bitcoin Core's
[CompactSize reader](https://github.com/bitcoin/bitcoin/blob/ee42d59d4de970769ebabf77b89ff4269498f61e/src/serialize.h)
and [transaction serialization](https://github.com/bitcoin/bitcoin/blob/ee42d59d4de970769ebabf77b89ff4269498f61e/src/primitives/transaction.h).
