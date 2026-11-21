# Proof bound to an actually deployed state

This experiment extends the genuine standalone Groth16/EVM verification to the
unchanged `ZkBitcoinStateV2` and its constructor-created verifier and adapter.
It uses ordinary qualified bytecode, the existing guest image, two real local
deployments and a new receipt. There is no injected verifier, mocked proof,
code/storage override or production activation.

`python3.11 -m ops.zkspv.state_trial prepare --output PATH` starts a disposable
loopback Anvil and deploys two identical configurations at distinct addresses.
Both initial states must equal the complete qualified mainnet checkpoint state.
The previous genuine proof, which binds `0x1111…1111`, must be rejected without
changing stored state. The input frame then changes only bytes 68..88 (the
20-byte deployment address after the transport/domain/chain fields). The
expected 1216-byte journal changes only ABI word 3. Bitcoin header, parent state
and all witnesses remain identical. The new receipt must authenticate those
expected journal bytes; this derivation is not claimed as a new native execution.

For the prepared directory, run the separate stages:

```sh
python3.11 -m ops.zkspv.state_trial_prove composite --output PATH --timeout 7200
python3.11 -m ops.zkspv.state_trial_prove succinct --output PATH --timeout 7200
# Prepare the pinned ARM64 Docker image before the next isolated stage.
python3.11 -m ops.zkspv.state_trial_prove groth16 --output PATH --timeout 7200
python3.11 -m ops.zkspv.state_trial accept --output PATH
```

The current launcher uses the task-local qualified tool paths from the preceding
Groth16 experiment. It verifies tool/source/input hashes at every stage and
records independent receipt verification. Composite proving denies all network
access. Compression permits only localhost RPC. Groth16 additionally permits
the explicit local Docker socket and forces the pinned container to use
`--network none --pull never`. Its container is observed and removed by unique
name, including on timeout. Each stage has a new output directory and report.
Never replace a previous receipt or promote an incomplete stage to VERIFIED.

Acceptance checks the actual deployment runtimes and current stored state before
submission. It requires wrong-state rejection, successful state advancement,
unchanged state/version after replay and journal tampering, useful-progress
freshness, real-header materialization against an independently reconstructed
two-leaf canonical tree, and denial of adapter methods once state age expires.
The Anvil clock is explicitly historical for these Fresh checks. That is a
controlled test-clock condition, not live Bitcoin freshness or a production SLA.

The preparation node stays running through proving; acceptance closes only its
recorded task-owned Anvil process. A failed intermediate stage leaves the node
available for diagnosis/retry. Any manual cleanup must verify the recorded PID
still belongs to this exact Anvil executable before stopping it.

One append and local acceptance do not qualify all fork/retarget/halt scenarios,
production checkpoint/profile selection, FROST relay authorization, monetary
consumer activation, actual Bitcoin transaction funding or independent review.

## Recorded local result

The completed run is `LOCAL_STATE_PROOF_PASS` in `report.json`. Composite proving
took 3351.018 seconds and independent verification 33.278 seconds. Succinct
compression took 562.443 seconds; Groth16 wrapping took 87.019 seconds. The final
2929-byte receipt authenticates the same guest image and the deployment-bound
journal digest `cef579ed8053289bc7a78a67111768cf30fd33955702f77dc6ff4e27fec493ee`.

The ordinary state at `0x5FbDB2315678afecb367f032d93F642f64180aa3` accepted the
proof in 437624 gas, advancing mainnet height 32256 to 32257 and state version
0 to 1. The second deployed state rejected it. Replay and a changed journal also
reverted without changing state. Header 32257 was materialized in 457517 gas;
the adapter reported one confirmation while Fresh, and denied status/inclusion
after the configured state-age limit. This is not a positive transaction-inclusion
or monetary-consumer test. The test-owned Anvil was then stopped.

Docker Desktop was temporarily stopped with explicit user authorization to free
memory during STARK generation. All nine previously running project containers
were restored after that stage, before compression/wrapping. Their restoration
is recorded in `evidence/docker-pause.json`.

Eighteen focused regression checks passed in
`ops/artifacts/zkspv/state-proof-regression.xml`. Assemble and validate the final
source/artifact manifest after acceptance and tests with:

```sh
python3.11 -m ops.zkspv.state_trial_collect --output PATH
```
