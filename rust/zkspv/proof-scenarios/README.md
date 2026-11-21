# Retarget and explicitly test-only fork/halt proofs

These scenarios keep the already qualified mainnet guest, receipts and contracts
unchanged. A completed result requires genuine composite, succinct and Groth16
receipts, independent SDK verification, and ordinary local EVM verification.
Native execution or a compiled guest alone is never a proof PASS.

| Scenario | Guest and input | Evidence boundary |
|---|---|---|
| retarget-34272 | Original mainnet image; actual headers 34271 and 34272 | Computes next target on the last epoch block and validates the first new epoch block. Starting state is obtained by native replay, not a recursive proof of that history. |
| fork-work-reorg100 | Distinct TEST_ONLY image; synthetic work | Losing and equal-work branches keep the head, then a shorter/heavier branch rolls back exactly 100 blocks. |
| halt101-prefix | Distinct TEST_ONLY image; synthetic work | Accepts one safe extension, permanently halts at depth 101, ignores malformed suffix, and checks terminal-state rejection inside the guest. |

`retarget_vectors.rs` also emits the 36287/36288 pair and checks it in debug and
release. That second vector remains native-only unless its own proof is run.
The generator includes both headers because the pinned verifier computes the
next target at height `2016k-1`; proving only `2016k` would miss that calculation.
Native vector construction uses the available Rust 1.89 compiler, recorded
separately from the unchanged mainnet guest build and its image ID.

The test guest uses the unchanged bounded resume/history implementation, with
an explicit synthetic nonce-as-work callback derived from the existing test
support. It does **not** check Bitcoin PoW, difficulty or MTP. Its image ID is
`91cbdff8eaa0f6e2968e8cdce61d1be57a2aadd75c1f34e0f5f0a518de16a948`.
Journal words 0 and 1 are `SATOSHIFI_TEST_POLICY_V1` and `synthetic-no-pow`.
The production decoder rejects these domains, and the original mainnet image
rejects the test proof. No witness option enables this model in the mainnet guest.

`TestPolicyReceiver` is a new test harness whose constructor explicitly accepts
a synthetic starting state. It authenticates the original model journal with
the real verifier before adapting the two domain words for the existing Solidity
transition validator. Its deployments, state updates and halt are local test
evidence only; it is not a production state/adapter or proof of checkpoint history.

Prepared directories under `evidence/` pin source/input/program/journal bytes.
For each prepared directory `PATH`, the isolated stages are:

```sh
python3.11 -m ops.zkspv.scenario_prove composite --output PATH --timeout 14400
python3.11 -m ops.zkspv.scenario_prove succinct --output PATH --timeout 7200
python3.11 -m ops.zkspv.scenario_prove groth16 --output PATH --timeout 7200
python3.11 -m ops.zkspv.scenario_evm --output PATH
```

All stages disable dev mode and deny external networking. Composite permits no
network; compression permits loopback; wrapping additionally permits the exact
local Docker socket and pins the native image with `--network none --pull never`.
Download the pinned image during preparation, outside the proving sandbox.
Never overwrite a receipt or change sources bound to an active attempt.

The two model receivers are prepared together with `python3.11 -m
ops.zkspv.model_evm prepare`. This starts a task-owned Anvil kept until both
model proofs have been accepted. For each model case, run `python3.11 -m
ops.zkspv.model_evm accept --scenario NAME`. The last acceptance stops that node.
On interruption, verify the recorded process identity before cleaning up this
node, and restore any temporarily stopped Docker services.

After all stages, EVM checks, regression tests and process restoration finish,
`python3.11 -m ops.zkspv.scenario_collect` binds the final evidence. Its status
distinguishes conditional mainnet retarget verification from TEST_ONLY fork/halt.
Full mainnet fork/halt, production deployment/consumer integration and independent
review remain separate requirements; this suite must not mark them qualified.

## Completed local evidence — 2026-10-30

All three scenarios produced genuine composite, succinct and Groth16 receipts
with dev mode disabled. Independent SDK verification matched the pinned program
and native journal at every stage. The ordinary EVM verifier accepted each seal
(250,644 gas each) and rejected wrong images, wrong journals and modified seals.

| Scenario | Composite seconds | Succinct seconds | Groth16 seconds | Acceptance scope |
|---|---:|---:|---:|---|
| retarget-34272 | 7712.844 | 1180.414 | 117.440 | Mainnet conditional transition; standalone EVM verifier |
| fork-work-reorg100 | 10091.143 | 1735.373 | 119.923 | TEST_ONLY receiver, version 1, 433,846 gas |
| halt101-prefix | 8283.027 | 1460.050 | 128.397 | TEST_ONLY receiver, version 1, 460,050 gas |

The fork receiver accepted height 32356 → 32257 with higher work. The halt
receiver retained the safe extension at height 32357, recorded depth 101 and
position 2, and reported not Fresh. Both receivers rejected replay and modified
journals; the production decoder rejected the model domains and the mainnet
image rejected model proofs. Their historical-clock Fresh checks are specific
to the test harness, not qualification of production two-bound Fresh.

Twenty-five focused Python regression checks passed. All nine pre-existing
Docker services were restored, task prover containers and the model Anvil were
stopped, and the temporary pinned prover image was removed after use. Receipts,
programs, journals and logs remain under `evidence/`; the final [report](report.json)
binds their hashes. Times are observed wall-clock runs, including the documented
resource pause for retarget, not a complete workload benchmark or SLA.
