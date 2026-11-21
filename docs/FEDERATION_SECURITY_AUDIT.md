# Federation / Confederation Security Audit

Historical federation/FROST audit baseline. For the current V1 authorization,
Bitcoin and deployment graph, use [architecture](ARCHITECTURE.md) and
[the October 18 integration review](UPGRADE_REVIEW_2026_10_18.md).

For the current code and client review, including findings that supersede
historical closed statuses below, read [17 October 2026 security review](../security_best_practices_report.md).

[Documentation Index](./README.md)

Branch: `demo-audit-full-frost`
Original remediation baseline: `30f45fe443790c15f8b15017d6b6ee99007de41b`
Current FROST authorization delta: 2026-08-24

## Status

The previously documented Solidity findings are remediated in the current production path. A later cross-check found that the compatibility authorization contract still exposed an address-counter mode and allowed a deployment caller to select an aggregate verifier. That contract and its deployer are now removed.

The current path is:

```text
DKG-approved key/config
  -> immutable factory coordinator
  -> enforced FROSTCoordinator session
  -> verified threshold shares + canonical aggregate verifier
  -> FROSTAuthorization
  -> one target, one operation, one consumption
```

This document records implementation status, not an independent third-party opinion on the new delta. External delta review remains mandatory before production.

The [strict internal review](./STRICT_REVIEW_2026_10_05.md) records code remediation and regression tests for SR-01 through SR-10, including independent re-review. This is not external audit sign-off: selected markets/feed, off-chain DKG and BTC/report integration, deploy-fee production and explicit economic-policy choices remain deployment dependencies.

## Verification

```bash
cd web3
forge build --sizes
forge test --offline
PRIVATE_KEY=1 forge script script/DeployFederation.s.sol:DeployFederation

cd ../signer-agent
pytest -q
python -m py_compile signer_agent/*.py
```

Current results:

- 178 Solidity tests passed, including 59 Custodian DAO tests;
- 5 signer-agent tests passed in the earlier baseline; not rerun in this review environment (`pytest` is not installed);
- all runtime bytecodes below 24,576 bytes;
- local deployment simulation passed;
- the current federation graph local Anvil broadcast produced 31/31 successful receipts after the October pool/DKG delta;
- Custodian DAO deployment script passed a configured factory test, but has not been broadcast to a live network;
- real 1-of-1 test covers D/E commitments, transcript binding, on-chain share verification, BIP-340 finalization, authorization and consumption.

Slither and Mythril were not available in this environment.

## Finding Matrix

| ID | Status | Current control |
|---|---|---|
| CR-01 | Closed | SPV helpers are admin-only and permanently disable-able. |
| CR-02 | Closed | Pool registration and UTXO mutation are controlled and source-bound. |
| CR-03 | Closed in production path | Address counters removed; authorization requires finalized cryptographic FROST. |
| CR-04 | Closed | Target helpers derive chain/authorization/target/selector/params/nonce-bound hashes. |
| CR-05 | Closed | Only the bound target can consume once. |
| CR-06 | Closed | Fee manager setup requires target-bound authorization. |
| CR-07 | Closed | Per-pool balance and pending-share accounting prevents collateral substitution. |
| CR-08 | Closed | BTC proof checks txid, source, payout script and amount, including batch settlement. |
| CR-09 | Closed | Reserve ledger and UTXO state transitions prevent reuse/under-reservation. |
| HI-01 | Closed | Withdrawal process hash is stored/derived from immutable request data. |
| HI-02 | Closed | Request paths lock/escrow; final settlement burns; cancellation/rejection refunds. |
| HI-03 | Closed | Federation/confederation deployment is one-time per config. |
| HI-04 | Closed | Fee configs are bounded and collectors are authorized. |
| HI-05 | Closed | Pool existence is separate from active state; reactivation preserves accounting. |
| HI-06 | Closed | Explicit Bitcoin miner fee and reserve accounting. |
| HI-07 | Closed | RBF sequences and `SIGHASH_ALL | SIGHASH_ANYONECANPAY`. |
| HI-08 | Closed; delta review required | D/E commitments, binding factors, Lagrange coefficients, public-share checks, nonce reuse prevention and participant finalization. |
| HI-09 | Closed | Approved/rejected states are distinct and rejection refunds escrow. |
| ME-01 | Closed | Threshold group-key approval and secp256k1/even-Y validation. |
| ME-02 | Closed | Threshold DKG completion path implemented. |
| ME-03 | Closed | Custodian management requires both approval systems. |
| ME-04 | Closed | Wallet sets reject zero, duplicate and over-10000-bps entries. |
| ME-05 | Closed | Fee counters accept only authorized/registered callers or actual receipt paths. |
| ME-06 | Closed | NFT discount ownership and usage are checked. |
| ME-07 | Closed | Pending bridge deposits cannot be overwritten. |
| ME-08 | Closed | Deposits use `SafeERC20`. |
| LO-01 | Closed | Vote methods perform actual authorization/custodian registration before events. |
| LO-02 | Closed | Deployment graph is allowlisted and emits canonical caller context. |

## Additional Delta Findings

### DA-01: Caller-selected verifier during deployment

Status: closed.

The alternate cryptographic factory methods accepted a verifier from any deployment caller. A malicious first caller could deploy the canonical federation config with an always-true verifier. Alternate methods were removed; both factories now pin one coordinator in their constructors, and authorization reads its immutable default verifier.

### DA-02: Counter approvals remained reachable

Status: closed.

The compatibility adapter included address-call counters alongside aggregate verification. The contract and deployer were deleted. `FROSTAuthorization` has no signer voting API and accepts only coordinator-finalized sessions.

### DA-03: Finalized session not bound to federation configuration

Status: closed.

`FROSTAuthorization` now checks exact coordinator, canonical verifier, group key, ordered participants, threshold, verified share count, message hash and target, then re-verifies the aggregate signature.

### DA-04: Central session creator

Status: closed for production enforced sessions.

Any listed participant may create an enforced session with zero verifier override and a deployed target. Role-based creation remains for integrations. Any participant may also aggregate and finalize. This removes a permanent leader without weakening authorization checks.

### DA-05: Membership/threshold mutation without key rotation

Status: closed.

Governance member/threshold proposal methods require a new DKG configuration. The immutable authorization manifest cannot diverge from the group key.

## EIP-170

The full size build passes. Current narrowest margins:

| Contract | Runtime | Margin |
|---|---:|---:|
| `SimpleMiningPoolWithCalculator` | 23,181 | 1,395 |
| `FROSTCoordinator` | 20,420 | 4,156 |
| `ConfederationGovernance` | 20,411 | 4,165 |
| `MultiPoolDeployer` | 21,089 | 3,487 |
| `SimpleMultiPoolDeployer` | 19,883 | 4,693 |
| `FROSTCustodianDeployer` | 10,512 | 14,064 |

The failed Sepolia deployer/factory chain must be redeployed because immutable dependencies point to addresses whose creations reverted. Independently successful contracts may be reused only after checking chain bytecode, ownership and configuration.

## October Integration Delta

The 5 October cross-check adds expired/superseded DAO proposal rejection, dynamic Bitcoin payout-script bytes, validated SEC1 public shares and x-only group-key storage, actual MP fee accrual/claiming, automatic fee-manager recording, live fee metadata in factory getters and dual-approved canonical DAO graph registration. `ConfederationTokenDeployer` removes nested share-token creation from `ConfederationMultiPool`; its constructor and `MultiPoolDeployer`'s constructor changed while `deployMultiPool(...)` retains its external signature. The deployment script validates DAO inputs before broadcast and prints registration calldata.

Review these changed contracts as a new delta and regenerate withdrawal ABIs. See [Deployment Readiness](./DEPLOYMENT_READINESS.md) for the remaining off-chain DKG, target-network and broadcaster dependencies. The multi-pool withdrawal event itself is not SPV proof of a BTC payout; the separately tested mining-pool SPV path must be integrated with it.

## Residual Risks

| Area | Required work |
|---|---|
| Independent review | Delta-review all changed/new Solidity and signer-agent files before production. |
| Static analysis | Run Slither/Mythril and retain reports in CI artifacts. |
| DKG ceremony | Review share generation/export, participant indexes, parity normalization and complaint handling. |
| Key custody | Integrate HSM/secure enclave and prohibit raw production shares in environment files. |
| Signer policy | Decode and display exact target parameters; never approve opaque hashes. |
| BTC broadcaster | Review RBF/CPFP, fee inputs, retries, replacement tracking and payout reconciliation. |
| Monitoring | Alert on stale sessions, refusals, invalid shares, pending redemptions and reserve mismatches. |
| Upgrade/rotation | Document new-DKG redeployment, registry migration and emergency response. |

## Manual Release Gate

- Confirm deployed factory `frostCoordinator()` values match the reviewed coordinator bytecode.
- Confirm every federation authorization reports the same coordinator and canonical verifier.
- Recompute group key, participant order and threshold from the DKG ceremony artifacts.
- Verify signer-agent release hash and encrypted nonce-state backup policy.
- Simulate full federation and confederation signing/consumption on the target network.
- Disable SPV helpers and verify pool/fee collector allowlists.
- Reconcile wrapped supply, pending liabilities and actual Bitcoin UTXOs before enabling redemptions.
