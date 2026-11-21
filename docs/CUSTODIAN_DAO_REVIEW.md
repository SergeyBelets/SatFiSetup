# Custodian DAO: Specification Review Matrix

[Documentation Index](./README.md) | [Module Guide](./CUSTODIAN_DAO.md)

Historical matrix for the September specification and October 5 review. The
statuses below preserve that review's scope; they are not the current feature
inventory. Subsequent DKG, fee, CLI and settlement changes are documented in
[the October 18 review](UPGRADE_REVIEW_2026_10_18.md), [current architecture](ARCHITECTURE.md)
and [DAO guide](CUSTODIAN_DAO.md).

Scope: the 25 September 2026 specification supplied in the task, checked against this repository. `Covered` means the stated on-chain rule is implemented and exercised by a focused test; it does **not** mean the contract has passed an independent security audit. `Partial` means the contract accepts an attested input or exposes an API but the end-to-end data or payment integration is absent. `Open` means a required decision or component is not in this repository.

The [strict internal review of 5 October](./STRICT_REVIEW_2026_10_05.md) records the original findings and their code remediation with checked-in regression tests. Integration qualifications below remain separate from remediated code defects and external audit acceptance.

| Spec sections | Status | Implementation and review note |
|---|---|---|
| 1-2: token and fixed issuance | Covered | `CustodianDAOToken`: constructor-supplied metadata, 10 million units, no mint method. Ethereum deployment is not performed here. |
| 3: full UTC-day staking | Covered | `CustodianDAOStaking` checkpoints opening balance, includes exact-00:00 deposits and subtracts same-day eligible withdrawals. Deposits after 00:00 enter the next day; no minimum or lock. |
| 4: signer selection and MPC separation | Covered for contract-defined decisions | `setDAOSigner` supports self, current custodian, or a consenting EOA/ERC-1271 wallet; only the owner can unstake or claim. The signer votes for its owner's stake on non-executing `CustodianDAODecisionBoard` decisions or for one custodian principal on rate/flag proposals. Signer changes do not call MPC contracts. |
| 5: custodian versus DAO signer | Covered | `CustodianDAORateController` counts custodian principals, not tokens. A signer wallet cannot cast two votes on a custodian proposal. The separate decision board counts each staker principal's full-day stake once and cannot execute custodian actions. |
| 6: Chainlink BTC/USD daily TWAP | Covered with feed dependency | SR-01 fixed: no pre-day duration. SR-09 fixed: bounded validated pages finalize immutable daily prices for high-round-count days. Actual feed/history selection remains pending. |
| 7: weighted TVL | Partial | Formula uses reported average BTC satoshis, the verified BTC/USD TWAP and reported stablecoin TVL. Neither BTC quantity nor stablecoin balances are independently verified on-chain; the FROST report commits to an evidence-bundle hash. |
| 8: initial base rate | Covered | Separate `CustodianDAORateController` starts at 120 ppm/day (0.012%). |
| 9: actual yield sources | Partial integration | SR-02/SR-07/SR-08 remediated: maximum 32 distinct funded yield IDs per day enforced before receipt, complete exclusive-stream reconciliation, rounding-safe harvest. Actual markets/provenance remain configuration/integration work. |
| 10-11: sufficient and insufficient yield | Covered for reported inputs | `CustodianDAODailySettlement` computes base, excess, optional reserve cover, LP and staker claims without debt. Tests cover both reserve states and zero yield. |
| 12: 5% reserve | Partial | The 5% refill target is implemented, not a hard balance cap; the specification does not prohibit direct funding above target or require overflow release. Opening reserve is attested, not verified from all confederation addresses/full wrapper supply. |
| 13: distribution cases | Partial | Base/excess/shortfall branches exist, conditional on reported inputs and the oracle fix. LP weights are attested. Redirecting all staker allocations to LPs when eligible stake is zero is an unapproved policy not specified in the task. |
| 14: wrap/deploy fee streams | Partial integration | Funded fee buckets/router, MP fee claim/conversion and strict stream exclusivity exist. Collector revocation no longer blocks withdrawals; failed manager reports can be retried. Native factories have no deploy-fee producer/payment binding. The MP flow uses a mock converter. Unwrap is not deploy fee. |
| 15: proportional staker rewards | Covered | `CustodianDAOStaking` allocates by full-day eligible balance and provides `claim` and `claimableReward`. Rounding dust stays in escrow. |
| 16-17: rate vote by 2/3 custodians | Covered | Active-custodian snapshot fixes `ceil(2N/3)` at proposal creation; 11 requires 8; one custodian and one signer wallet each have at most one vote. Execution takes effect next UTC day. Expired proposals and older proposals superseded by a newer execution for the same parameter are rejected. |
| 18: `USE_RESERVE` governance | Covered | Separate checkpointed flag, changed by the same 2/3 custodian proposal mechanism; it cannot modify the rate. Its deadline and ordering checks are independent of the rate. This is the chosen DAO rule pending product confirmation. |
| 19: source-specific integration | Partial | `CURVE` source registration requires a nonzero minimum and liquidity guard; `CurvePoolLiquidityGuard` reads the current wrapper balance held by the configured pool. Claimable reward-token income, reward-token ERC-4626 appreciation and realized other ERC-20 income have distinct adapters. LP-share appreciation still locked in a position and selected market/pool/wrapper addresses remain open. The liquidity observation is instantaneous, not historical market depth. |
| 20: application UI | Open | This repository has Solidity, scripts and a signer agent, not the user-facing application. Balance, staking, claims, signer, proposal, rate, reserve and flag have contract getters/transactions, but no screens or wallet flow here. |
| 21: fixed interpretation rules | Partial | Emission, staking, custodian votes, shortfall, reserve refill and separate fees are covered as above; the external-data and UI qualifications still apply. |
| 22: deployment choices | Open | Token name/ticker, Chainlink proxy, Curve pool/wrapper addresses and liquidity minimum, initial reserve flag and final flag-governance decision require explicit values. `DAO_REWARD_TOKEN` is already the configured common payout token; Curve guard and source registration are post-deployment FROST operations. |

## Review Blockers

1. **Economic data provenance.** Define the accounting source and evidence bundle for average BTC quantity, stablecoin TVL, every confederation BTC address, full wrapper supply, opening liquidity reserve and each LP weight. FROST authorization binds `evidenceHash` and report values, but does not independently prove the BTC or off-chain values.
2. **External protocols and conversion.** Select and review the actual Yield Basis/Silo/Curve markets, claim calls, ERC-4626 asset compatibility, converter/router and proof of provenance. The adapters cover direct configured-token claims, configured-token ERC-4626 appreciation and conversion of an ERC-20 already held by a payer. Multi-pool accrued fees now have an approved token-transfer path; signers must still review the recipient, DAO allocation, selected converter and minimum output. Income embedded in an arbitrary LP token needs its own realization path.
3. **Product decisions.** The configured `DAO_REWARD_TOKEN` is authoritative. Confirm its USD unit/depeg policy, Curve threshold, initial `USE_RESERVE`, and whether custodian 2/3 is the final rule for changing that flag. Confirm the documented choices for reserve valuation by daily TWAP and no-eligible-staker allocation to LPs.
4. **Executable token-holder decisions.** The decision board implements FROST-approved proposals, previous completed-day stake snapshots, signer delegation, one vote per owner and signed quorum. It records non-executing outcomes only. No arbitrary token-holder executor or timelock is provided because the specification defines no executable token-holder action or authority; the 2/3 custodian rate path cannot be bypassed through this board.
5. **Application.** Integrate the contract calls and read models in the separate frontend repository. No application-level workflow can be reviewed here.

## October Remediation

- **Stale governance execution:** fixed with inclusive voting-deadline checks and per-parameter proposal ordering. Tests cover expiry, execution at the deadline, superseded approvals and independent rate/flag ordering.
- **Pool fee realization and address discovery:** fee accrual/transfer, fee-manager recording, factory fee metadata, approved canonical DAO registration and deployment preflight were added. Review this new delta, including the leaf confederation-token deployer, alongside DAO contracts. See [Deployment Readiness](./DEPLOYMENT_READINESS.md) for the complete bootstrap and remaining off-chain dependencies.

## Remediated Contract Review Issues

- **SR-01/SR-09:** clamped TWAP and immutable paginated daily calculation.
- **SR-02/SR-07:** safe funded-source bound and complete yield-only reconciliation.
- **SR-03/SR-05:** deferred bounded notifications, exact token transfers/backing checks.
- **SR-04/SR-08:** partial loss recovery and rounding-safe principal-preserving harvest.
- **SR-06:** canonical group-key approval hash across accepted encodings.
- **SR-10:** order-independent largest-remainder LP distribution.

The proposed hard 5% reserve cap finding was rejected after comparing with the original specification: it requires a refill target, not a cap or overflow recipient. See the report's cross-check section.

Exact historical reproductions, implemented changes, independent-review evidence and regression tests are in the [strict review report](./STRICT_REVIEW_2026_10_05.md). Remediation includes source and tests, not only documentation edits.

Tests establish the listed regressions; they do not certify unselected live markets or replace external delta review.

## Audit Boundary

The current Solidity delta can be reviewed for token issuance, UTC staking, signer delegation, custodian governance, Chainlink paging, source custody/reconciliation, adapters, actual fee-token routing and settlement arithmetic. The original SR-01..SR-10 defects have code remediation. Entire-system acceptance still requires the integration blockers and economic-policy choices above to be resolved and reviewed together. The previous federation audit does not cover this module.

Run `forge test --offline` and `forge build --sizes` from `web3/` for the current contract suite. `test/CustodianDAO.t.sol` contains the focused examples; tests with a mock feed, mock coordinator and mock reward token do not establish mainnet feed or external income-source behavior.
