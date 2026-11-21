# Staged deployment script acceptance tests

Run the complete Solidity acceptance from the project root, isolating every
environment-dependent test case in its own Forge process:

```sh
python -c 'from ops.local_acceptance import run_solidity_suites; run_solidity_suites("ops/artifacts/solidity-suites.json")'
```

The deployment scripts read explicit process environment variables through the
Foundry VM interface. `vm.setEnv` changes the environment of the whole Foundry
process, and Foundry's per-test EVM snapshots do not restore those values.
Running script tests concurrently can therefore mix inputs from unrelated tests.
The script fixtures restore their own explicit inputs at every test entry. A
combined run with artifact lookups demonstrated that one worker alone does not
prevent one test from seeing another test's environment, even within one suite.
Therefore `ops/local_acceptance.py` discovers every runnable suite, follows local
imports to identify environment-dependent tests, and runs each such test case
in a separate process with `--threads 1`, requiring exactly one passed test per
isolated process. Other suites run once per suite with the same single-thread
setting. Executed names and the complete count must match discovery, with no
failures or skips.
Abstract/interface declarations with no creation bytecode are recorded separately;
they cannot be instantiated by Forge. The production compiler, EVM target and
contract size limits remain unchanged.

A focused test case can be run from `web3` with both filters:

```sh
forge test --threads 1 --match-contract '^DeploymentStagesTest$' \
  --match-test '^testPartialBaseResumesFromOnePreviouslyDeployedLeaf\(' -vv
```

`DeploymentStages.t.sol` covers base deployment without financial parameters,
explicit wallet configuration, preflight rejection before transactions, partial
and complete deployment resumption, dependency and owner mismatches, independent
factory deployment, the explicit integration exemption, and session creator
rights after the deployer renounces the FROST administrator role.

`DeploymentFeeStages.t.sol` covers a separate project authorization epoch, the
project income vault and fee router, saved component validation, the required zero
deployment fee share for stakers, current-nonce payer preparation, sequential
permission execution, already approved operation resumption, stale or wrong
authorizations, both factory permissions, immutable fee setup, partial fee setup
resumption, and actual paid V1 deployments. The paid path checks the exact amount
debited from the caller and credited to the configured project treasury.

The independent recheck adds twelve runtime/reuse regressions:

- `RecheckBaseDependencyTest`: ERC-20 in place of verifier or DKG, a sibling leaf
  with the correct owner, and a canonical existing base with a zero-transaction retry.
- `RecheckScriptArtifactIdentityTest`: exact artifact identity, legitimate distinct
  immutable owners, a changed metadata byte, and inconsistent repeated immutable words.
- `RecheckStandaloneRouterDependencyTest`: direct standalone router infrastructure,
  the transitive verifier binding, canonical project components and idempotent reuse.

Use the automatic full-suite runner above, or select one test with both filters.
Foundry matches ABI signatures: the opening parenthesis is required after the
exact function name, including for fuzz tests with arguments. A zero exit code
with no matching tests is not a successful check. The helper reads the configured
compiled artifacts; `foundry.toml` grants read access to `./contracts/out`.

The tests execute real production child deployments and token transfers. The
large local Solidity script scaffold is placed in the test EVM with `vm.etch`;
no production child contract uses that shortcut. Completed DKG ceremonies are
constructed through the real registry. FROST authorization result responses are
explicit test fixtures, so these suites validate deployment orchestration and
authorization binding rather than reproduce off-chain threshold signing.

The separate E2E paid-deployment scenario also exercises real participant FROST
signing and exact factory-to-project-treasury transfers on a local Anvil chain.
The [18 October acceptance](../../docs/UPGRADE_REVIEW_2026_10_18.md) records the
combined results. A paid public Sepolia deployment has not been run. These script
tests do not replace the Python wrapper's canonical-creation validation for
reused components; see [contract provenance](../../docs/CONTRACT_PROVENANCE.md).
