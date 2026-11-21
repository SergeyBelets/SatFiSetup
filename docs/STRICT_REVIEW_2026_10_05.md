# Strict Contract Review: 5 October 2026

[Documentation Index](./README.md) | [Specification Matrix](./CUSTODIAN_DAO_REVIEW.md) | [Bootstrap](./DEPLOYMENT_READINESS.md)

Historical snapshot: the original DKG, Bitcoin payout and deploy-fee integration
gaps below describe October 5. Subsequent implementation and current limitations
are recorded in [the specification audit](TZ_AUDIT_2026_10_17.md) and
[the October 17–18 security review](../security_best_practices_report.md).
System-contract implementation identity now includes
[constructor provenance checks in the client](CONTRACT_PROVENANCE.md).

## Verdict And Scope

The original review identified SR-01 through SR-10. Code remediation and checked-in regression tests are now implemented; the historical descriptions below explain the pre-fix behavior, not the current implementation. Independent re-review and final verification are recorded in the acceptance section. This remains an internal review, not external security certification or a claim that live-market/off-chain integrations are complete.

Review scope: the 25 September Custodian DAO specification, DAO contracts, and the uncommitted October federation/pool/deployer delta. This is an internal AI-assisted review, not an external auditor's sign-off. Findings are source-grounded and separate unauthorized attacks from governance-dependent mistakes and deployment prerequisites. No contract fixes or live-network broadcasts were made during this review pass.

Two independent review-only Codex workers completed under Orca run `run_5e00a63997c8`: DAO/specification review (`ctx_0973d2844e76`) and federation/deployment delta review (`ctx_3f651e4b07e3`). Their source findings were cross-checked by the coordinating agent; not every candidate was accepted. The DAO reviewer reproduced four cases on a private scratch Anvil using the existing compiled contracts and mock dependencies, then stopped that server. The baseline tests were rerun independently as well. No worker modified repository source or documentation.

Priority order of original findings: SR-01/SR-02 (High), SR-05 (Conditional High), SR-03/SR-04/SR-07/SR-08 (Medium), SR-09 (Conditional Medium), SR-06/SR-10 (Low). Conditional severity did not imply an unprivileged exploit in the canonical deployment.

## Remediation

| ID | Implemented change | Checked-in regression evidence |
|---|---|---|
| SR-01 | Integration cursor never moves before UTC start. | `TWAPRemediation.t.sol`: pre-day price/duration and fuzz cases. |
| SR-02 | Reject the 33rd distinct funded yield source before transfer; existing sources can top up, registration remains extensible across days. | `CustodianDAO.t.sol`: source-limit rollback, 32-source settlement, next-day funding and deactivation history. |
| SR-03 | Pool accrual is authoritative; bounded-gas manager notifications defer into `pendingUnreportedFees` on failure; permissionless `syncFeeReports` retries exactly once. | `PoolDKGRemediation.t.sol`: both pools, revocation/retry, gas exhaustion, claims before retry. |
| SR-04 | Signed `emergencyRedeemPartial` binds recipient/shares/minimum/nonce and writes off principal proportionally with ceiling rounding; full exit clears all principal. | `SiloRemediation.t.sol`: partial liquidity/loss, minimum, replay, write-off and rollback. |
| SR-05 | Reject inexact deposit receipts; fee claims check backing and exact sender/recipient balance deltas. | `PoolDKGRemediation.t.sol`: both pools, false-return/taxed transfers and reduced backing. |
| SR-06 | Approval and completion hash validated canonical x-only key bytes; duplicate participant approval across encodings is rejected. | `PoolDKGRemediation.t.sol`: mixed 32/33/64-byte point encodings. |
| SR-07 | Vault enforces yield-stream identity, unique IDs, exact amounts and complete positive-source count. | `CustodianDAO.t.sol`: equal-sum fee alias, duplicate IDs and omitted funded sources. |
| SR-08 | Bounded binary search accounts for rounded-up burned shares before withdrawing; actual receipt and remaining principal are checked afterwards. | `SiloRemediation.t.sol`: 100/197 case, fractional-boundary fuzz, short receipts. |
| SR-09 | Permissionless validated `append` pages (at most 512 each) finalize an immutable daily result; settlement accepts the cached result with an empty round list. First-page poisoning is rejected. | `TWAPRemediation.t.sol`: 601 rounds, invalid/replayed pages, hostile opener, interleaved callers; `CustodianDAO.t.sol`: paginated-price settlement. |
| SR-10 | Largest-remainder LP allocation with address tie-breaking instead of first-entry dust. | `CustodianDAO.t.sol`: 31/32 base units, unequal fractions and conservation/permutation fuzz. |

The funded-source limit is an explicit operational bound, not a fallback that silently ignores income. The rejected deposit transfers no tokens and can be routed to a supported source/day only when that attribution is economically correct. The historical missing-market/deploy-fee/off-chain items below remain integration boundaries, not fixed by this table.

## Original Findings (Before Remediation)

### SR-01: High - TWAP Includes Time Before The Calculation Day

Source: [ChainlinkBTCDailyTWAP.sol](../web3/contracts/src/federation/ChainlinkBTCDailyTWAP.sol), `calculate`, lines 34-46.

The first round may precede midnight. If the next consecutive round also precedes midnight, `until` is smaller than `cursor`, yet `cursor = until` moves the integration boundary backwards. Later iterations add pre-day price duration and still divide by 86,400. The accepted result is not the specified daily TWAP.

Impact: an otherwise valid signed report can value BTC incorrectly, changing TVL, base payouts and the reserve target. Settlement requires FROST approval of the round list; this is an oracle correctness bug, not a permissionless ability to settle arbitrary reports.

Required fix/test: never integrate outside `[dayStart, dayEnd]`; require or correctly handle the latest opening round. Test multiple consecutive pre-midnight rounds and prove the result is identical to starting at the latest opening round.

Independent local reproduction: consecutive rounds at `S-7200`, `S-3600`, `S+43200`, `S+86400`, all priced `10000000000`, return `10416666666` for `[1,2,3,4]` and the correct `10000000000` for `[2,3,4]`. The extra pre-day hour yields 25 hours of weighted price divided by 24.

### SR-02: High - More Than 32 Funded Yield Sources Can Block All Settlement

Sources: [CustodianDAOIncomeVault.sol](../web3/contracts/src/federation/CustodianDAOIncomeVault.sol), `setSource`, `deposit`, `releaseDay`; [CustodianDAODailySettlement.sol](../web3/contracts/src/federation/CustodianDAODailySettlement.sol), lines 130-146.

Source registration and deposits have no per-day source-count bound, but a report can contain at most 32 distinct income lines. `releaseDay` requires their amounts to match the entire funded day's yield. Register and fund 33 yield IDs with one unit each, with no fee deposits: any report with at most 32 unique IDs sums to at most 32, not the recorded 33. There is no refund, migration or skip-day path. Sequential settlement then prevents later days as well.

This requires governance to admit enough sources and registered accounts to fund them; an arbitrary outsider cannot register sources. It nevertheless violates extensible-source liveness and can irreversibly lock valid income.

Required fix/test: enforce an achievable funded-source bound before accepting funds, or implement bounded multi-step accumulation. Prove the 33rd deposit cannot create an unfinishable day and test subsequent-day progress.

Independent local reproduction: 33 registrations/deposits succeeded; the 32-line report reverted `ReportMismatch`, the 33-line report reverted `InvalidReport`, and `nextSettlementDay` did not advance. The wrong-stream alias in SR-07 is an invalid provenance workaround, not correct reconciliation.

### SR-03: Medium - Collector Revocation Blocks Pending Withdrawal Processing

Sources: [SimpleMultiPool.sol](../web3/contracts/src/demo/SimpleMultiPool.sol), `processWithdrawal`, `_accrueFee`; [ConfederationMultiPool.sol](../web3/contracts/src/federation/ConfederationMultiPool.sol), corresponding methods; [FederationFeeManager.sol](../web3/contracts/src/federation/FederationFeeManager.sol), `setFeeCollector`, `recordFeeCollectedForToken`.

Processing a nonzero withdrawal fee synchronously records it in the manager. Governance can revoke the attached pool's collector permission, causing an already approved withdrawal to revert with `UnauthorizedFeeCollector`. The whole call rolls back, including approval consumption; zero-fee processing avoids this dependency.

Recovery is possible by reauthorizing the collector, or cancelling/rejecting the still-pending request under existing rules. The manager attachment is one-shot; replacement is not an available recovery path. This is a governance-triggered liveness regression, not permanent token loss by an unprivileged caller.

Required fix/test: explicitly design collector revocation versus existing settlement obligations, or decouple accounting notification from settlement. Test both pools after a nonzero-fee request followed by revocation, reauthorization and cancellation.

### SR-04: Medium - ERC-4626 Emergency Exit Requires Full Liquidity

Source: [CustodianDAOSilo4626Adapter.sol](../web3/contracts/src/federation/CustodianDAOSilo4626Adapter.sol), `emergencyRedeem`, lines 163-170.

The emergency path always redeems all shares. A vault with a lower `maxRedeem` or partial liquidity can revert even when a partial recovery is possible. The existing unrestricted mock does not cover this case.

Required fix/test: add a signed bounded partial recovery path with explicit principal/write-off accounting and a liquidity-limited vault regression. Do not promise that emergency recovery always succeeds.

### SR-05: Conditional High - Admitted Fee-On-Transfer Tokens Can Be Underbacked

Sources: [SimpleMultiPool.sol](../web3/contracts/src/demo/SimpleMultiPool.sol), `deposit`, `claimFees`; [ConfederationMultiPool.sol](../web3/contracts/src/federation/ConfederationMultiPool.sol), corresponding methods.

Deposits use `safeTransferFrom` but credit the requested amount, not the received balance delta. For a 1,000-unit deposit, a token taking 10% on transferFrom and a 5% pool fee produce 900 units held against 950 units of user claims and 50 accrued fees. Claiming those 50 fees leaves 850 units against 950 user claims.

Canonical exact-transfer MP tokens do not exhibit this behavior. Exploitation requires threshold admission of an incompatible token. `SafeERC20` handles false-return tokens, not transfer taxes or rebases. Accrued fees alone are not proof of surplus collateral for arbitrary ERC-20s.

Required fix/test: enforce exact balance deltas or restrict admission to reviewed exact-transfer implementations. Add false-return, taxed-transfer and rebasing-boundary tests before accepting arbitrary tokens.

### SR-06: Low - Equivalent Group-Key Encodings Split DKG Approvals

Source: [DKGCoordinator.sol](../web3/contracts/src/federation/DKGCoordinator.sol), `approveGroupPublicKey`, `completeDKG`, lines 276-310.

Approvals hash raw key bytes, while completion stores canonical x-only bytes. Threshold participants approving `X` and `02 || X` for the same even-Y point do not contribute to the same count. Each representation can fail completion despite agreement on the curve point.

Required fix/test: hash the canonical representation in both paths, or enforce one ceremony encoding at submission. Test a threshold split across all accepted encodings of the same point.

### SR-07: Medium - Wrong-Stream Aliases Can Replace Yield Source Lines

Sources: [CustodianDAOIncomeVault.sol](../web3/contracts/src/federation/CustodianDAOIncomeVault.sol), lines 122-124 and 175-183; [CustodianDAODailySettlement.sol](../web3/contracts/src/federation/CustodianDAODailySettlement.sol), lines 140-146.

Nonreserved source IDs may be registered as wrap/deploy fee streams. The report rejects only the literal `WRAP_FEE`/`DEPLOY_FEE` hashes; release checks amounts and aggregate totals but does not require each income line to have `Stream.Yield`. Fund real yield IDs with 33 units total and fee alias `bytes32(300)` with 33 wrap units. A report listing only `(alias,33)` as yield plus 33 wrap fees passes, omitting every real yield ID. The reviewer reproduced successful execution as a local `eth_call`.

The 66 units are actually funded. Aggregate yield/fee totals remain 33 each, so this does **not** demonstrate extra token creation or an unbacked double payment. It demonstrates duplicated source attribution and false per-source reconciliation, under exact FROST approval of source configuration/report.

Required fix/test: enforce stream exclusivity for all IDs and inclusion of every positive yield source exactly once. Test equal-sum fee aliases and omitted real yields, not merely omission with a different aggregate amount.

### SR-08: Medium - ERC-4626 Rounding Can Block Profitable Harvest

Source: [CustodianDAOSilo4626Adapter.sol](../web3/contracts/src/federation/CustodianDAOSilo4626Adapter.sol), `harvest`, lines 115-126.

`previewRedeem` floors value, while `withdraw` rounds shares burned upwards. Harvesting the entire calculated surplus can make the remaining preview value one base unit below principal and revert, despite a smaller safe harvest being possible. The signed `minimumYield` is only a lower bound, not a selectable withdrawal amount.

Independent local reproduction with the repository's actual OpenZeppelin ERC-4626 mock: supply 100 base units into an empty vault, add 197 assets, then call an approved `harvest(day,1,hash)`. Position value is 295, computed yield 195, withdrawal burns 67 shares and leaves 33 shares/102 assets with preview value 99 against principal 100. The complete adapter call reverts `InsufficientYield`. A donation of 100 in the same control setup succeeds; that control is not the finding.

Required fix/test: choose a principal-preserving withdrawal/share bound that respects both rounding directions, and test fractional-price boundaries and exact net receipts.

### SR-09: Conditional Medium - More Than 512 Required Feed Rounds Blocks Progress

Sources: [ChainlinkBTCDailyTWAP.sol](../web3/contracts/src/federation/ChainlinkBTCDailyTWAP.sol), lines 11, 32, 41; [CustodianDAODailySettlement.sol](../web3/contracts/src/federation/CustodianDAODailySettlement.sol), lines 130, 159, 226.

A valid day needing more than 512 consecutive rounds including opening/closing coverage cannot be evaluated. Skipping rounds violates adjacency; full coverage violates the hard limit. Sequential settlement has no alternate validated accumulator or recovery path, so one such day blocks later days too. No selected live feed or historical occurrence was established during this review.

Required fix/test: test 513+ valid consecutive rounds; provide bounded accumulation or an explicit supported-feed activity/liveness policy supported by actual feed-history checks. This is a conditional contract limit, not a claim that a real BTC/USD feed has already hit it.

### SR-10: Low - LP Dust Depends On Report Order

Source: [CustodianDAODailySettlement.sol](../web3/contracts/src/federation/CustodianDAODailySettlement.sol), `_allocateProviders`, lines 250-257.

Every provider except index zero receives a floored share; index zero gets the complete remainder. With 32 equal weights and 31 base units, the first provider gets 31 and every other provider zero. The remainder is below the provider count in base units, usually immaterial for 6/18-decimal tokens but more visible for allowed low-decimal tokens. The signed report controls ordering.

Required fix/test: obtain explicit acceptance of the first-provider dust rule or use an order-independent dust/escrow policy. Test low-decimal rewards and permutation of equal-weight providers. This is a proportional-allocation policy qualification, not an unprivileged drain.

## Candidate Rejected During Cross-Check

The DAO worker proposed treating reserve funding above 5% as a specification violation. The original sections 12-13 define a **refill target**, stop routing the reserve half once the target is reached, and do not mandate a hard balance cap, automatic overflow distribution, or a prohibition on donations. `fundReserve` overfunding and a falling target therefore do not by themselves prove noncompliance. The current behavior is documented as a target, not a hard cap. No new cap or overflow recipient was invented during review.

## Specification Gaps And Trust Boundaries

- **TVL and opening reserve (sections 7, 12):** BTC quantities, stablecoin TVL, LP weights and the full-wrapper-supply reconciliation are reported scalars. The signature and `evidenceHash` bind the report but do not verify its economic truth. No automatic aggregate of confederation BTC addresses/wrapper reserves is implemented.
- **Income markets (sections 9, 19):** generic claim, ERC-4626 and conversion contracts exist. A named source ID does not prove Yield Basis/Silo/Curve provenance. Selected markets, withdrawability, converter liquidity and LP-position realization still need integration tests.
- **Deploy fee production (sections 14, 19):** the funded `DeployFee` bucket and router exist, but native factory deployment does not charge, collect or bind a deploy-fee receipt to deployment. An external producer may fund that stream, but no such producer/payment binding is present here. Defining a converter or an address does not implement fee generation; the specification supplies no fee amount to invent silently. Resolve the producer/collection rule and then implement/test that integration.
- **No eligible stakers (sections 13-15):** settlement redirects the staker allocation, including separately funded fees, to LPs when eligible stake is zero. The specification does not define this recipient policy. It is an implementation choice, not a confirmed requirement; obtain explicit approval or change it.
- **Reward units:** settlement assumes one whole configured reward token equals one USD. It validates decimals, not market value. A non-USD/depegged token invalidates the intended economics without a valuation policy.
- **Curve liquidity:** the guard observes instantaneous wrapper balance in the configured pool, not durable executable market liquidity. Names/addresses and the minimum require review.
- **DKG:** curve-point validity does not prove VSS/share derivation. For an odd group key, a BIP340 ceremony must normalize all secret/public shares consistently, not merely discard the group Y coordinate. The agent imports a scalar unchanged; a real ceremony-to-signature test is absent.
- **Canonical DAO registration:** graph getters and dual approval establish approved address relationships, not implementation identity or upgrade safety. Getter-compatible replacements can satisfy those checks if governance approves them.
- **BTC payout:** multi-pool processing consumes approvals and burns shares but does not create, broadcast or SPV-confirm a BTC payout. That integration cannot be inferred from the event.
- **Application (section 20):** UI is outside this repository and outside the requested contract implementation. Its absence is not a request to add screens here.

## Confirmed Implementation Checks

- Fixed 10,000,000-token supply, deployment-supplied metadata and no mint method.
- UTC-day staking, additions eligible only for full days, same-day eligible withdrawals removed, owner-only unstake/claim, delegated signers without asset powers.
- Rate starts at 120 ppm/day; independent rate and reserve-flag histories; snapshot `ceil(2N/3)`, 8 of 11, no token-weight multiplier; expired and superseded execution rejected.
- Funded sufficient/insufficient income arithmetic, reserve **refill target** of 5%, bounded shortfall coverage, no debt, and independent aggregate fee balances. These checks are conditional on valued/attested inputs and the oracle/provenance findings.
- Full dynamic BTC script and metadata binding; target/nonce/recipient/amount binding on fee claims; confederation dual approval; cancellation does not accrue unwrap fees.
- DAO graph discovery after approved registration; controlled leaf token deployment preserves roles and stays within bytecode limits.

## Verification And Acceptance

Final run on 5 October: `forge test --offline --summary` passes 178 tests (91 critical, 59 DAO, 7 pool/DKG, 10 Silo, 11 TWAP), including three 256-run fuzz tests. `forge build --sizes` exits zero. Federation simulation succeeds (35,994,618 simulated gas); a fresh local Anvil broadcast produced 31/31 successful receipts. No Sepolia broadcast or live market/feed test was performed. The closest runtime is `SimpleMiningPoolWithCalculator`: 23,181 bytes, 1,395 below EIP-170.

Two independent read-only re-review tasks (`task_3f002c15d531`, `task_c501f2a060da`) reviewed the remediation. The second identified a residual conservative Silo rounding rejection: principal 2, donation 2, authorized yield 1. This was fixed by allowing the bounded authorized minimum with atomic post-withdraw principal validation and added as `test_PostWithdrawalRoundingAllowsAuthorizedMinimum`; the final 178-test suite includes this fix. This last small adjustment has regression coverage but was not separately re-reviewed by the worker. Original SR-01..SR-10 have code remediation; this is not a guarantee of absence of other defects. Zero-staker/valuation and deploy-fee production policies and actual report/market/reserve integration remain separate. External delta review remains required before production.
