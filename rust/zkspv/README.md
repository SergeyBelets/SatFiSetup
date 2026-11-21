# Bitcoin ZK-SPV qualification workspace

This directory contains reproducible qualification evidence, pinned reference
sources and a test-only arithmetic probe. **It is not an enabled ZK payment
backend or a production Bitcoin verifier.** The existing SatoshiFi SPV remains
the active path.

The specified Citrea revision builds unchanged, but does not meet the complete
SatoshiFi contract. Its linear verifier lacks fork choice/LCA/canonical history
and the future-time limit, saturates chainwork, and uses unsigned epoch time
subtraction. Exact upstream arithmetic also reproduces a target-one work mismatch
and compact-encoder panic. These require reviewed upstream patches; the separate
fork/history integration requires its own critical-code review. Audit coverage of
older Citrea commits cannot close these gates.

On the supplied ARM macOS machine, the RISC Zero 3.0 documented Groth16 wrapping
path is unsupported. A real network-isolated proof and EVM verification have not
been completed. There is no mock fallback, synthesized image ID or cloud prover
request. See `ops/artifacts/zkspv/selection-report.md` and the JSON manifests for
the separate source, build, test, proof, compatibility and license statuses.

## Reproduce source and upstream baseline

Use Python 3.9+ and the exact Rust 1.88.0 toolchain. The baseline source checkout
is intentionally separate from the retained review excerpts in `vendor/`.

```sh
python3 rust/zkspv/tools/verify_upstream.py
python3 rust/zkspv/tools/fetch_upstream.py /tmp/satoshifi-citrea-baseline
cd /tmp/satoshifi-citrea-baseline
cargo +1.88.0 build --locked -p bitcoin-da --lib
cargo +1.88.0 test --locked -p bitcoin-da --features native --lib verifier::tests -- --nocapture
```

The full upstream `native` feature is needed for its JSON test fixtures; enabling
a standalone `serde_json` feature is not supported. Do not edit upstream manifests
or algorithms to make these original tests pass. An offline repeat uses `--frozen`
after fetching all dependencies. The recorded local execution used explicit
Rust binary paths and task-local Cargo/target directories rather than changing
the user's default toolchain. Actual commands and logs are in the build manifest.

The 2026-10-28 retry completed all six original `verifier::tests` on the unchanged
full archive tree with Rust 1.88.0. A separate corrected native run passes ten
tests with one explicitly aligned target-one assertion, a checked-work test and
three compact-target tests against executed pinned Core arithmetic.
See [native run evidence](header-qualification/README.md#original-native-suite).
The prior ENOSPC attempt is retained as historical evidence. Neither run executes
the other 39 native DA/service tests or supplies a real ZK proof.

## Reproduce arithmetic observations

```sh
python3 rust/zkspv/tools/prepare_arithmetic_probe.py
cargo +1.88.0 test --locked --manifest-path rust/zkspv/qualification-probe/Cargo.toml
cargo +1.88.0 test --locked --release --manifest-path rust/zkspv/qualification-probe/Cargo.toml
```

The generator refuses any change to the pinned verifier hash and extracts four
function bodies verbatim. This probe is a diagnostic, **not the complete upstream
baseline, a patched core, an independent consensus implementation, or a proof**.
Tests named `observed_*` succeed when they reproduce the stated incompatibility;
their success must not be interpreted as Bitcoin compatibility PASS.

## Experimental arithmetic corrections

The separate [patch series](patches/README.md) supplies three small reference
corrections for target-one work, compact encoding and signed retarget time.
It verifies original/intermediate hashes and leaves `vendor/` unchanged.
Corrected-function debug/release tests and Core-equation vectors are recorded
in `patches/report.json`. These runs use the available Rust 1.89.0; they do not
replace the full upstream 1.88.0 native tests or qualify a production verifier.
Full fork/history/time qualification and actual proving remain separate blockers.

The subsequent [historical header qualification](header-qualification/README.md)
adds checked-chainwork and checked-compact-target patches and exercises complete extracted header
transitions on 6,111 unchanged Mainnet/Testnet4 headers. It checks epoch-boundary
state restoration with exact upstream Rust 1.88.0 and dependency versions.
It also compares 45,064 selected compact/limit results with executed Core arithmetic.
The original native verifier tests and corrected tests pass; full DA/service,
guest/proof and independent review qualification remains open.

The [header time policy](time-policy/README.md) adds an application maximum over
all verified new-header timestamps and an EVM acceptance check at `block.timestamp`
plus two hours. It has no host clock input or new Bitcoin consensus implementation.
Native mainnet integration, chunk summaries, shared Rust/Solidity ABI and boundary
checks are tested locally. Real proof/public-output binding and the production
state/adapter remain unimplemented, so the authenticated time gate remains open.

The [fork/history component](fork-policy/README.md) now provides local selection,
LCA, retained branch contexts, permanent halt at reorg >100, atomic batch errors,
persistent canonical roots, Rust/Solidity membership and complete replay recovery.
It reuses the pinned Bitcoin core through the native integration and ports the
existing relay's skip/selection policy. Its new history/policy code requires
independent review. Bounded proving witnesses, real proof binding and state/adapter
activation remain open.

The [checkpoint factory](checkpoint-policy/README.md) validates mainnet anchor PoW,
target, exactly ten linked predecessors and supplied cumulative work, then derives
the initial difficulty/MTP context and binds every native field before initializing
fork/history. Native tests cover 4032 descendants, two retargets and full replay;
Rust/Solidity contexts and commitments agree on three actual mainnet fixtures.
Earlier chainwork remains trusted. Production checkpoint selection/authentication,
independent review and real guest/state integration are still required.

The [complete transition kernel](transition-policy/README.md) now composes
checkpoint initialization, authenticated-state replay, fork/history and time
into one versioned 1024-byte journal. It binds old/new state, checkpoint, accepting
EVM chain/address, ordered batch and exact safe-prefix halt position. Solidity
checks strict encoding, stale-state/replay, deployment domain and acceptance time.
Native tests resume across both retarget boundaries using the same pinned verifier.
This prepares the statement format; it does not authenticate a real proof or supply
a bounded resume witness, guest image or activated state/adapter.

The separate [V2 resume component](resume-policy/README.md) adds an authenticated
context index and exact LCA witnesses, preserving V1 canonical-history encoding.
Bounded verification reads only fixed-depth proofs and newly processed headers;
the complete archive stays in the witness builder. A mainnet append across either
retarget needs 31,317 witness bytes and one consensus call, independent of prior
history length. V2 also commits admission order. Its state commitment differs
from V1. Its dedicated journal/EVM component is described below; compiled guest,
real proof and independent review remain open.
No automatic migration or production activation is performed.

The [V2 journal/input component](resume-transition/README.md) now wraps bounded
resume in a separate 1216-byte statement. Solidity recomputes both complete V2
state commitments and checks known-count progression, EVM deployment, current
state, time and halt. The strict runtime-neutral input codec bounds the complete
request to 2,281,208 bytes; a retarget append is 31,805 bytes and one native core
call. Six shared Rust/Solidity vectors and native mainnet tests qualify the local
boundary only. No RISC Zero SDK transport, compiled guest/image ID, real receipt
or production state adapter is supplied by this component.

The separate [state/adapter V2 component](state-adapter/README.md) now implements
immutable proof-only state, checkpoint-derived V2 roots, two-bound Fresh and
permissionless root-bound materialization with the actual IPaymentSPV ABI. The
constructor creates the pinned real Groth16 verifier and its own adapter. Local
Anvil receipts cover ordinary-bytecode creation and real-verifier rejection;
positive state-machine tests use explicitly scoped proof-boundary mocks. No real
guest/proof, selected production profile, consumer switch or external review is
claimed. FROST relay authorization is still separate unfinished work.

The [guest integration](guest-integration/README.md) now links the complete pinned,
corrected Bitcoin verifier through its public mainnet API into a RISC Zero guest.
It bounds the outer input before allocation, publishes the exact V2 journal and
packages the user ELF with the pinned default kernel. Native tests cover 4032
actual descendants and both retargets. Build/image evidence and isolated STARK
evidence are recorded separately; neither selects a production image nor completes
Groth16/EVM acceptance, consumer activation or independent review.
The extended local run produced a genuine composite STARK for one mainnet append
in 3408.052 seconds and verified it independently in 27.336 seconds, including
exact journal/image matching and tamper rejection. See
[the proof report](guest-integration/evidence/local-proof/proof-report.json).
The subsequent [Groth16 bridge](groth16-integration/README.md) now records real
succinct/Groth16 conversion and positive verification by the pinned ordinary
EVM verifier on disposable Anvil. Its journal retains the original test address.
The separate [state proof integration](state-proof-integration/README.md) produces
a new proof for an actual deployment and records `LOCAL_STATE_PROOF_PASS`:
height 32256 to 32257, version 1, 437624 gas, replay/domain/tamper rejection,
real-header materialization and Fresh expiry. Eighteen regression checks pass.
This historical-clock local test leaves full proof scenarios, production
selection, monetary-consumer acceptance and independent review unfinished.

The subsequent [proof scenarios](proof-scenarios/README.md) record genuine
STARK/succinct/Groth16 and ordinary EVM verification for a two-header mainnet
retarget and explicitly TEST_ONLY fork/halt models. The mainnet guest is unchanged.
The separate model image/domain uses synthetic work and does not verify Bitcoin
PoW, bits or MTP. Its test receivers accept reorg 100 and halt depth 101, reject
replay/tampering, and remain incompatible with the mainnet image/decoder.
Twenty-five focused regression checks pass. The suite does not authenticate the
retarget starting state's recursive history or qualify mainnet fork/halt,
production consumers or independent review.

## Licenses and further integration

The separate [native derivation qualification](derivation-qualification/README.md)
exercises an intent-bound Taproot profile using the existing pinned Bitcoin and
FROST libraries. It is compiled only in tests and records public vectors and
synthetic threshold signatures. It does not add a production derivation verifier,
address registry, per-input transaction signing or a Ready address.

Original copyright/license notices remain in `vendor/`. Citrea's `bitcoin-da`
manifest says MIT OR Apache-2.0 and its LICENSE contains Apache-2.0, while direct
`citrea-primitives` and `sov-rollup-interface` dependencies inherit GPL-3.0-only.
The official RISC Zero Ethereum repository has an Apache-2.0 root license, but
its Groth16 verifier source carries GPL-3.0 terms. See `license-manifest.json`;
no blanket permissive license or project-license compatibility is asserted.

Do not implement another consensus core to bypass these qualification results.
A later change must preserve pins, update the explicit diff/reuse map, reproduce
upstream tests, review minimal arithmetic patches and new policy/history code,
then produce and verify a real SatoshiFi proof on supported controlled hardware
with external networking denied. Public deployment remains a separate action.

The separate [regtest deposit integration](deposit-integration/README.md) now
records actual Core funding, six confirmations and a genuine STARK → Groth16
accepted through the ordinary adapter/deposit registry/MiningPoolV1 mint path.
Its guest and one-shot receiver are explicitly TEST_ONLY; the mainnet guest and
V2 state are unchanged. Mainnet consumer/bootstrap qualification remains open.
