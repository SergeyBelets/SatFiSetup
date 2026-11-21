# Custodian DAO

[Documentation Index](./README.md)

The separate [common ULR staking](ULR_STAKING.md) and [treasury route](ULR_TREASURIES.md)
implement the new daily 90%-of-excess policy with shared historical wallet+stake
custodian weights. The contracts below describe the legacy per-confederation DAO;
its existing obligations remain funded and its bootstrap is not silently changed.

[ULR candidates and scoped voting](ULR_CANDIDATES_AND_VOTING.md) add opt-in
candidate admission and a common-staking adapter for `CustodianDAODecisionBoard`.
Delegated stake does not create extra custodian-count approvals or transfer rewards.

The [18 October implementation review](UPGRADE_REVIEW_2026_10_18.md) records the current integrated DKG, Bitcoin settlement, fee routing and DAO tests. The [5 October strict review](STRICT_REVIEW_2026_10_05.md) covers its earlier SR-01 through SR-10 delta. Production market/feed selection, independent custody and public-network acceptance remain deployment dependencies; the reports are internal verification, not external audit sign-off.

Source specification: [Custodian DAO / Governance Token](https://docs.google.com/document/d/11lpLbB1wL-Fxehm6MP5oxfTh9xbb3Uc5u8ILyViFuzw/edit?usp=drivesdk). The implementation was prepared from the complete text supplied in the task.

## Contracts

| Contract | Responsibility |
|---|---|
| `CustodianDAOToken` | Transferable ERC-20; exactly 10,000,000 tokens with 18 decimals minted once to the deployment recipient. No mint function. Name and symbol are constructor values. |
| `CustodianDAOStaking` | Free stake/unstake, per-day eligibility, reward escrow and claims, DAO signer selection. Token ownership and custodian status stay separate. |
| `CustodianDAORateController` | Base rate and `USE_RESERVE`, both changed by a snapshot of active custodians with `ceil(2N/3)` votes. |
| `CustodianDAODecisionBoard` | FROST-approved registry of token-holder decisions. Staked-token votes use the last completed eligible UTC day as a weight snapshot and the owner's current DAO signer. Decisions are non-executing; they cannot change the rate, reserve flag, custodians or MPC. |
| `ChainlinkBTCDailyTWAP` | Recalculates BTC/USD time-weighted price from sequential historical Chainlink rounds. |
| `CustodianDAOIncomeVault` | FROST-governed source registry and exact ERC-20 accounting by UTC day and yield/wrap/unwrap/deploy stream. Settlement can release only the complete recorded day. |
| `CustodianDAOClaimAdapter` | Calls one immutable protocol under FROST approval and deposits only the measured increase in the configured reward token. Deploy one instance per claimable Yield Basis/Curve source, where its claim call pays that token directly to the adapter. |
| `CustodianDAOSilo4626Adapter` | Holds principal in one configured ERC-4626 vault whose asset is the DAO reward token, harvests only appreciation above principal, and has a FROST-approved emergency redemption path. |
| `CustodianDAOConversionAdapter` | Converts an actual ERC-20 transfer from the payer through one immutable converter under exact FROST approval, refunds unused input and deposits only measured reward-token proceeds above signed minimum output. Supports yield and wrap/unwrap/deploy fee streams after the fee asset is in the payer's custody. |
| `CustodianDAOFeeRouter` | Receives actual reward-token fees from FROST-approved payers. Wrap/Unwrap use the configured operational share; Deploy has its own share. The remainder goes to treasury. Fee-share, treasury and payer changes require target-bound FROST approval. |
| `CurvePoolLiquidityGuard` | Reports the wrapper balance held by a configured Curve pool for an enforceable minimum-liquidity check on Curve-source deposits. |
| `CustodianDAODailySettlement` | Sequential daily settlement, LP/staker income, fee stream and reserve accounting. |

Funded yield is limited to 32 distinct source IDs per day before transfer; settlement enforces exact unique yield-only reconciliation. TWAP supports validated pages of up to 512 rounds and an immutable finalized daily price. The 5% reserve value is a refill target, not a hard balance cap.

The existing `CustodianValidator` supplies the current custodian set. `FROSTAuthorization` approves each daily report. DAO signer changes never edit FROST participants or MPC shares.

## Stake And Delegation

`day = floor(block.timestamp / 86400)` and the accounting interval is `[day*86400, (day+1)*86400)` UTC. Eligibility uses the balance immediately before 00:00 plus deposits timestamped exactly 00:00, minus eligible tokens withdrawn before day's end. A deposit after 00:00 starts earning on the next UTC day. A withdrawal at 00:00 prevents any later deposit with that same timestamp from restoring eligibility already lost that day. If a user stakes and unstakes on the same day, newly added stake is withdrawn first for eligibility purposes, preserving the earlier full-day stake where possible. There is no minimum stake or lock. The first eligible day is the deployment day only if deployment occurs exactly at 00:00; otherwise it is the next UTC day.

`setDAOSigner` may select the owner's own address, a current custodian address, or another wallet that signs an EIP-712 `SignerConsent` message. Changing the signer does not move tokens or grant unstake/claim rights. A custodian-count vote resolves the signer from the custodian **principal**, never from the amount delegated by other users. One custodian principal and one signing wallet can each cast at most one vote per proposal.

Claims use `claim(uint256[] days)` with strictly ascending days, at most 32 per transaction. `claimableReward(owner, days)` supports the same range for UI queries. Reward days become claimable only after a funded settlement. Small rounding remainders remain in reward escrow; they are not reassigned to another user.

## Governance

The initial rate is **120 ppm per day**, equal to **0.012%**. `propose(BaseRate, newPpm, custodian)` snapshots all active custodians and fixes `requiredVotes = ceil(2N/3)`. For 11 custodians, 8 votes are required. `propose(UseReserve, 0|1, custodian)` uses the same threshold. A proposal accepts votes for seven days. Anyone may execute after the threshold is reached; the value takes effect at the **next** UTC day, so a later settlement cannot retroactively change a completed day's rate or reserve policy. Owner, backend, token balances and stake sizes have no privileged parameter setter.

`execute` rejects proposals after the voting deadline and lower-ID proposals for a parameter whose newer proposal has already executed. The cutoff is inclusive: execution exactly at the deadline is allowed. Rate and reserve-flag ordering are independent. `lastExecutedProposal(parameter)` exposes the ordering checkpoint, and superseded proposals cannot receive further votes.

Ordinary staker decisions use `CustodianDAODecisionBoard`: a target-bound FROST approval opens a decision with a metadata URI, content hash, voting duration and quorum. Voting power is the stake eligible for the last **completed** UTC day at creation, so later staking/unstaking does not change that proposal's weight. The owner's currently selected DAO signer casts at most one vote for that owner; an existing custodian wallet can sign for multiple owners without gaining any extra **custodian-count** vote. For/against/abstain participation counts toward the signed quorum; finalization records the outcome but does not execute arbitrary calls. Only the separate custodian controller can change the base rate or `USE_RESERVE`.

## Daily Settlement

`DAO_REWARD_TOKEN` is the configured payout token for the whole DAO system: the vault receives it and staking, liquidity providers and reserve accounting all use the same token. The current USD accounting requires that token to represent one USD per whole token and have at most 18 decimals; non-USD or depegged assets require a price-conversion rule before funded settlement.

1. Supply consecutive Chainlink BTC/USD round IDs covering the whole finished UTC day, including the price active at 00:00 and an update at or after 24:00. The contract verifies positive answers, times, order, no omitted round IDs and a maximum 24-hour price age. The feed must expose eight decimals.
2. Register each income or fee source in `CustodianDAOIncomeVault` using a separate FROST-approved `setSource` operation. The source address may be an EOA treasury account or a protocol adapter. For the `CURVE` source ID, a deployed liquidity guard and nonzero minimum are mandatory; `CurvePoolLiquidityGuard` measures wrapper tokens held by the chosen pool.
3. Each registered source transfers its actual USD reward-token allocation to the vault with `deposit(day, sourceId, amount)`. The vault measures exact received balances and tracks yield, wrap, unwrap and deploy fees separately. A claim adapter can make a FROST-approved external claim and deposit its measured proceeds in one transaction; the Silo adapter can withdraw measured ERC-4626 appreciation and deposit it. Deposits before the first DAO settlement day or after that day has been released are rejected. The existing fee manager's counters are not a substitute for this token transfer.
4. Build a `DayReport` with average BTC satoshis, stablecoin TVL, BTC liquidity reserve at day start, individual non-fee income lines, the aggregate operational allocation `stakerWrapFeeIncome` (wrap **plus unwrap**), separate `stakerDeployFeeIncome`, LP liquidity weights, and a nonzero `evidenceHash`. The current `dao-report` CLI computes **SHA-256 of its exact canonical evidence bytes**. Give reviewers those same bytes and supporting BTC/UTXO, wrapper-supply, stablecoin and LP records. The contract stores a nonzero `bytes32` commitment but neither enforces the hash algorithm nor validates the underlying evidence contents.
5. Get the hash from `getSettlementOperationHash(report, roundIds)`. Finalize an enforced `FROSTCoordinator` session for that hash and target, then call `FROSTAuthorization.authorizeOperation`.
6. Call `settleDay`. The vault releases only if every reported yield source and both fee amounts equal the complete amounts it received for that day. The settlement checks exact receipt, consumes the FROST authorization and distributes the funds. Days must settle in chronological order, including zero-income days.
7. The staking contract receives the staker share, LP balances become claimable, and reserve funds remain locked for later shortfalls.

The formulas use integer rounding down. LP allocations conserve the full payable amount: floor proportional shares first, then assign remaining units by largest fractional remainder with ascending-address tie-breaking, independent of report order.

```text
BTC_USD = averageBtcSats * BTC_USD_TWAP_8 * 10^rewardDecimals / 10^16
TVL = BTC_USD + stablecoinTvl
baseTarget = TVL * ratePpm / 1_000_000
reserveTarget = 5% * (openingLiquidityReserveSats valued at the same TWAP)

if actualIncome >= baseTarget:
    basePaid = baseTarget
    excess = actualIncome - baseTarget
    stakerYield = floor(excess / 2)
    reserveOrLP = excess - stakerYield
    reserveAdd = min(reserveOrLP, max(0, reserveTarget - reserveBalance))
    lpIncome = basePaid + reserveOrLP - reserveAdd
else:
    coverage = (providers.length > 0 && USE_RESERVE)
        ? min(baseTarget - actualIncome, reserveBalance) : 0
    basePaid = actualIncome + coverage
    excess = 0
    lpIncome = basePaid

stakerIncome = stakerYield + stakerWrapFeeIncome + stakerDeployFeeIncome
```

LP income is allocated by reported liquidity weight. Empty `report.providers`
is valid. Eligible stake is read from `staking.totalEligibleStake(report.day)`.
After reserve accounting, distribution follows this matrix:

| Eligible stakers | LP providers | Distribution |
| --- | --- | --- |
| Present | Present | Existing staker share and LP claims |
| Absent | Present | Staker share is added to LP claims |
| Present | Absent | Staker share is funded; LP share is transferred to immutable treasury |
| Absent | Absent | Both distributable shares are transferred to immutable treasury |

With no LP, the prior reserve is never spent, even when `USE_RESERVE` is true.
Current excess can still replenish it. `staking.fundDay(day, 0)` is called when
the funded staker share is zero. No new LP claims are created without providers;
earlier unpaid LP claims remain funded. `getSettlement(day)` appends
`treasuryIncome` after the existing fields, and `TreasuryIncomeTransferred(day,
amount)` records the same-transaction transfer. The transfer checks exact token
balance changes; failure reverts the whole day. `lpIncome` is zero for that day.

No shortfall becomes protocol debt. Positive excess first refills the reserve to
its current target. `fundReserve` also accepts an actual token transfer. Missing
legitimate price history, out-of-order days and replay still reject, including
zero-income days and days without recipients.

The opening BTC reserve quantity is valued using that day's BTC/USD TWAP. The
recipient matrix above is the implemented 17 October specification policy.

Income lines cannot use `WRAP_FEE`, `UNWRAP_FEE` or `DEPLOY_FEE` as source IDs, and duplicate source IDs are rejected. The two report fee allocations and the evidence hash are recorded in `getSettlement(day)` and funded regardless of excess or reserve use. Source addresses are FROST-governed, but the vault cannot prove that an authorized address actually harvested a named external protocol; that still requires adapter or treasury evidence.

## Deployment

Deploy the federation/confederation graph and finish the confederation DKG first. Infrastructure deployment alone does not create an active confederation or its private shares; follow [first on-chain DKG](FIRST_DKG_ONCHAIN.md) for the integration bootstrap or [staged deployment](STAGED_DEPLOYMENT.md) for project infrastructure. `DeployCustodianDAO.s.sol` reads the confederation's `CustodianValidator` and `FROSTAuthorization` from its factory and checks the coordinator binding. It also checks that no DAO is registered, metadata/recipient are nonempty, the reward token has at most 18 decimals and the feed has eight decimals before broadcasting. Configure:

```text
PRIVATE_KEY
CONFEDERATION_FACTORY
CONFEDERATION_ID
DAO_TOKEN_NAME
DAO_TOKEN_SYMBOL
DAO_TOKEN_RECIPIENT
DAO_REWARD_TOKEN
DAO_TREASURY
BTC_USD_CHAINLINK_FEED
DAO_USE_RESERVE
```

`DAO_TREASURY` is a required nonzero address and is the seventh settlement
constructor argument. It cannot equal the settlement itself. Integration
bootstrap takes it from the invitation's explicit `treasury`; it is not inferred
from the token recipient or deployer.

Run a simulation before `--broadcast`:

```bash
cd web3
forge script script/DeployCustodianDAO.s.sol:DeployCustodianDAO --rpc-url "$RPC_URL"
forge script script/DeployCustodianDAO.s.sol:DeployCustodianDAO --rpc-url "$RPC_URL" --broadcast
```

The mainnet BTC/USD feed is listed in [Chainlink's Ethereum BTC/USD directory](https://data.chain.link/feeds/ethereum/mainnet/btc-usd). Verify the current full proxy address, code, decimals and network immediately before deployment. No feed address or token name is hardcoded.

After deploying all seven components, the script prints a `ConfederationFactory.getRegisterCustodianDAOOperationHash` digest and the registration calldata. Finalize and authorize that exact operation for the **factory** target, collect the custodian threshold for the same hash, then call `registerCustodianDAO`. The factory checks the authorization, validator, common reward token and graph bindings and records one canonical DAO per confederation. `getCustodianDAO(confederationId)` returns its seven addresses without a frontend-only address list. Registration requires the updated factory code and is a separate approved transaction, not a permissionless side effect of deploying components.

The script deploys the decision board, income vault and settlement, and binds the vault to the settlement; it does not invent income-source addresses or fee-share parameters. After deployment, register source accounts via `getSetSourceOperationHash` -> enforced FROST session -> `authorizeOperation` -> `setSource`. Register `YIELD_BASIS`, `SILO` and `CURVE` only after selecting the actual accounts/adapters. Deploy `CurvePoolLiquidityGuard(wrapperToken, curvePool)` and specify a minimum in wrapper-token base units before activating `CURVE`. Deploy the fee router separately with the chosen share parameters if full reward-token fees need on-chain splitting. MP/S-token fee counters alone are not USD income.

For a protocol that pays `DAO_REWARD_TOKEN` through a claim call, deploy `CustodianDAOClaimAdapter(incomeVault, protocol, sourceId)`, register the adapter as that source account, and authorize the exact `getClaimOperationHash(day, protocolCall, minimumReceived)` for target `adapter`. The call must send the reward token to the adapter; it cannot merely return a claim amount. For an ERC-4626 Silo market with `asset() == DAO_REWARD_TOKEN`, deploy `CustodianDAOSilo4626Adapter(incomeVault, siloVault)`, register it under `keccak256("SILO")`, then authorize `supply`, `harvest` and `withdrawPrincipal` using their respective operation hashes. `emergencyRedeem` attempts to redeem **all** held shares and writes off the prior principal accounting if successful. Signed `emergencyRedeemPartial(recipient, shares, minimumReceived, operationHash)` supports limited liquidity and proportional principal write-off, rounded up; full exit clears all principal. Harvest uses a conservative share bound, permits a bounded authorized minimum at rounding boundaries, and always verifies actual receipt and post-withdraw principal atomically. Both recovery paths remain subject to vault liquidity. Review the selected vault's withdrawal behavior before funding.

If realized income or a fee allocation is held in another ERC-20, deploy `CustodianDAOConversionAdapter(incomeVault, inputToken, converter, sourceId)`, register it as the correct yield/wrap/unwrap/deploy source, and authorize `getConvertOperationHash(day, payer, amountIn, minimumReward, converterCall)`. Only that payer can execute. The adapter receives exactly `amountIn`, allows the fixed converter to spend at most that amount, returns unused input, verifies the actual increase in `DAO_REWARD_TOKEN` against `minimumReward`, and deposits it in the vault. The FROST signers must independently check the converter, calldata and minimum output; this adapter has no price oracle and does not extract unwithdrawn fees from a pool. Each operation requires its own enforced FROST session and authorization.

When the full fee is already held in `DAO_REWARD_TOKEN`, `CustodianDAOFeeRouter(incomeVault, treasury, wrapStakerBps, deployStakerBps)` provides on-chain splitting. Register the router as the `WRAP_FEE`, `UNWRAP_FEE` and/or `DEPLOY_FEE` source account for each nonzero DAO allocation in use. Authorize each payer using a separate current-nonce `getSetPayerOperationHash` FROST operation, then have the payer approve and call `depositFeeV1(day, stream, amount)` (`Wrap=0`, `Unwrap=1`, `Deploy=2`). Wrap and Unwrap share `wrapStakerBps`; Deploy uses `deployStakerBps`. The legacy boolean API maps `false` to Deploy, never Unwrap. The router checks actual token receipt, funds the chosen stream and pays the remainder to treasury. `setFeeShares` and `setTreasury` are FROST-governed. The payer must hold and transfer the actual fee token.

That general DAO fee split is separate from the project deployment-fee setup.
The [project stage](STAGED_DEPLOYMENT.md) fixes `deployStakerBps=0` explicitly,
uses separate project authorization (an independent DKG on the Anvil/Sepolia
staged CLI; the federation EVM result in Ethereum mainnet V6), creates no settlement or DEPLOY_FEE vault source,
and transfers the entire deployment fee to the project treasury.

`DeployCustodianDAOIncomeAdapter.s.sol` deploys one selected adapter and prints the pending `setSource` operation hash. Run it once per selected source, then complete that hash through the enforced FROST flow with the **income vault** as target and call `setSource` separately. Its env inputs are `PRIVATE_KEY`, `DAO_INCOME_VAULT`, `DAO_ADAPTER_KIND` (`CLAIM`, `SILO_ERC4626` or `CONVERSION`), `DAO_SOURCE_NAME` and `DAO_SOURCE_STREAM` (`0` yield, `1` wrap, `2` deploy, `3` unwrap; these are vault enum values). Set `DAO_CLAIM_PROTOCOL`, `DAO_SILO_VAULT`, or `DAO_INPUT_TOKEN` plus `DAO_CONVERTER` for the chosen kind. For Curve, also set `DAO_LIQUIDITY_GUARD` and `DAO_MINIMUM_LIQUIDITY`; the script rejects a missing guard before broadcast. Simulate with `forge script script/DeployCustodianDAOIncomeAdapter.s.sol:DeployCustodianDAOIncomeAdapter --rpc-url "$RPC_URL"` before adding `--broadcast`.

## Integration And Release Gaps

The [current implementation review](UPGRADE_REVIEW_2026_10_18.md) and [DAO CLI](DAO_CLI.md) describe the tested integration and operational interface. The older [Custodian DAO Review Matrix](CUSTODIAN_DAO_REVIEW.md) records its historical scope. The local DKG-to-settlement flow is integrated; production custody and selected external markets remain separate release work.

- The repository has no graphical frontend. The [DAO CLI](DAO_CLI.md) supports inspection and user/governance operations; UI work remains separate.
- The claim adapter covers protocols with a claim call that pays the configured reward token; the Silo adapter covers ERC-4626 vaults using that token as the asset; the conversion adapter covers actual ERC-20 assets already held by a payer. Multi-pool `claimFees` can supply accrued MP-token fees to that payer under approval; the chosen converter must support the MP token and the DAO allocation must be authorized. Chosen Yield Basis/Curve market APIs and LP-value appreciation still need a reviewed realization path. Do not label a source account as Yield Basis, Silo or Curve without verifying the actual contract and proceeds.
- Reconciliation of reported BTC/stablecoin TVL and opening reserve against all confederation BTC addresses, wrappers and external positions is still an attested off-chain process. FROST signers must inspect the evidence bundle; `evidenceHash` binds it to the settlement, not proves it.
- Curve's exact wrapper/pool addresses and minimum acceptable wrapper balance remain deployment inputs. The guard measures instantaneous wrapper balance held by the specified pool, not historical liquidity, full market depth or executable swap size; a same-transaction balance change can manipulate this observation. FROST source governance and independent liquidity evidence remain necessary.
- Chainlink proxy phase transitions require a consecutive historical round path; operators must verify a day's round list on the chosen feed before settlement. A missing historical round or stale feed blocks settlement rather than substituting a price.
- This module is a new audit delta. Its deployment and the attestation/reconciliation process require independent review before handling production funds.
- Custodian membership is managed by the existing validator's separate FROST and custodian approval path. Review that membership-change policy alongside the 2/3 rate vote, since the controller snapshots the membership list that exists when a proposal is created.

## Current integration workflow

`bootstrap_plan.py` now creates the full graph, waits for enforced signatures and
custodian approvals, calls `ConfederationFactory.registerCustodianDAO`, and checks
`getCustodianDAO`. Actual MP fees reach the vault through PoolFeePipeline and a
prefunded local converter. WRAP, UNWRAP and DEPLOY use explicit stream values;
see [FEE_FLOW.md](FEE_FLOW.md) for the legacy aggregate report field semantics.

`frost-signer dao-report --rpc URL --settlement ADDRESS --inputs INPUTS.json
--evidence EVIDENCE.json --intent INTENT.json` reads vault source totals and
historical TWAP at one pinned EVM block and writes exact canonical UTF-8 evidence
bytes plus a participant intent. It refuses existing output files. Inputs contain
string `day`, `round_ids`, approved `reward_token`, `average_btc_sats`,
`stablecoin_tvl`, `opening_reserve_sats`, and `providers` pairs [address, weight].
The evidence hash is SHA-256 of those exact bytes. BTC TVL/provider accounting is
an explicit attested input, not magically proved by the hash; every signer must
verify its underlying records. The report never substitutes latest price for
missing historical rounds. On-chain settlement rejects changed funded totals.
