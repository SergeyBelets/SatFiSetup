# Fee System

[Documentation Index](./README.md)

The fee system handles wrap/deposit and unwrap/withdrawal fees for federation and confederation pools.

## Contracts

| Contract | Purpose |
|---|---|
| `FederationFeeManager` | Fee configs, governed NFT create/mint, read-only quotes, restricted quota consumption and authorized fee accounting |
| `FeeDistributionCalculator` | Equal or weighted distribution between federation members and custodians |
| `ProjectWalletRegistry` | Per-federation project wallets with FROST-gated changes |
| `GlobalProjectWalletRegistry` | Owner-managed global native-fee distribution from registered fee managers |
| `FederationNFTModifier` | ERC721 fee discount NFTs with expiration and volume limits |
| `FeeSystemDeployer` | Orchestrates four external one-purpose deployers without embedding component creation bytecode |
| Fee-system leaf deployers | Deploy one fee manager, distributor, NFT modifier or project wallet registry per contract |
| `CustodianDAOFeeRouter` | Routes reward-token Wrap/Unwrap/Deploy streams from approved payers; a separate project instance sends deployment fees entirely to project treasury |
| `PoolFeePipeline` | Atomically claims accrued pool fees, converts actual MP to reward tokens and funds the selected DAO fee stream |

## Fee Calculation

`IFeeManager.FeeType` supports:

```solidity
enum FeeType { FixedPercentage, Dynamic }
```

Fixed percentage:

```text
fee = amount * fixedBps / 10000
```

Dynamic:

```text
fee = dynamicMinFee + amount * dynamicCoeffBps / 10000
```

The current implementation caps calculated fee at `amount`, so a bad dynamic input cannot underflow pool accounting. Config validation rejects `fixedBps > 10000` and `dynamicCoeffBps > 10000`.

## NFT Discounts

The discount-related fee APIs have distinct read/write boundaries:

```solidity
function calculateFeeWithDiscount(
    bool isWrap,
    uint256 amount,
    uint256 discountBps
) external view returns (uint256 fee)

function quoteFeeWithNFT(
    bool isWrap, uint256 amount, uint256 nftTokenId, address account
) external view returns (uint256 fee)

function calculateFeeWithNFT(
    bool isWrap,
    uint256 amount,
    uint256 nftTokenId,
    address account
) external returns (uint256 fee)
```

`calculateFeeWithDiscount` is a mathematical view and does not validate an NFT.
`quoteFeeWithNFT` is the public, non-consuming view: when a collection is bound,
it checks ownership, expiry and quota through `previewDiscountFor`.
`calculateFeeWithNFT` is restricted to `authorizedFeeCollectors[msg.sender]`
with contract code. It consumes allowance through `useDiscountFor`, checking
owner, expiry and remaining volume before incrementing `usedVolume`.
Canonical pools bind account to their caller and check shares; arbitrary EOAs
or contracts cannot consume another owner's quota. Governance-approved pools
are trusted consumers and must not permit caller-supplied victim accounts.

Managed collections use `createNFTModifier(name,symbol,operationHash)` and
`mintNFT(collection,to,expiration,volumeLimit,discountBps,operationHash)`.
The manager holds admin/minter/usage; its exact FROST-approved action executes
mint, with additional custodian approvals for confederations. The signer uses
an independent NFT pin and public-board intent. See [commands, nonce domain,
exact retries and mathematics](NFT_CONFIGURATION.md). NFT is optional after
bootstrap; launch JSON has no `nft_*` policy fields.

Quota is consumed at NFT request creation. A whole-transaction revert restores
it, but later cancellation of a successful request does not restore quota.
Service-fee accrual is separate and happens only on Bitcoin settlement.
100% service discount does not remove gas, miner fees or output minima.

## Fee Recording

Fee accounting is restricted:

```solidity
function setFeeCollector(address collector, bool authorized, bytes32 operationHash) external
function recordFeeCollected(uint256 amount, bool isWrap) external
function recordFeeCollectedForToken(address token, uint256 amount, bool isWrap) external
```

Only addresses authorized by target-bound FROST approval can record fees. The multi-pools call `recordFeeCollectedForToken` and the manager exposes `collectedFeesByToken(token, isWrap)`. `totalCollected` remains a sum of raw units across tokens, not a USD value. Authorize the pool as a collector before setting its fee manager; setup also checks the authorization and custodian-validator bindings.

Fee notifications are bounded and deferred on failure: `pendingUnreportedFees(token, isWrap)` retains unreported collection and permissionless `syncFeeReports` retries after collector reauthorization. Revocation does not block processing an existing withdrawal; a new NFT request
cannot consume quota through a revoked collector. Deposits require exact receipts; fee claims verify backing and exact debit/credit, rejecting taxed transfers atomically. The manager cannot be replaced after attachment.

DAO settlement requires unique yield-stream source IDs, exact recorded amounts and complete funded-source reconciliation. At most 32 distinct yield sources can receive funds per day; a 33rd is rejected before transfer. Both factories collect their configured reward-token deployment fee during nonexempt V1 creation, atomically with the new product. The caller approves the factory, and the factory routes the exact fee using `FeeStream.Deploy`. The project setup uses separate authorization and a router with `deployStakerBps=0`, so this payment goes entirely to project treasury. The legacy Anvil/Sepolia staged CLI uses an independent project DKG; Ethereum mainnet V6 binds project authorization to its completed federation EVM result. No unwrap fee is relabeled as deploy fee.

## Governance

Federation fee changes require FROST approval. Confederation fee changes require FROST approval plus custodian threshold when a custodian validator is configured.

Target-bound operation hash helpers:

```solidity
function getSetFeeConfigOperationHash(bool isWrap, FeeConfig memory config) public view returns (bytes32)
function getSetTreasuryOperationHash(address newTreasury) public view returns (bytes32)
function getSetNFTModifierOperationHash(address nftModifier) public view returns (bytes32)
function getSetFeeDistributorOperationHash(address feeDistributor) public view returns (bytes32)
function getSetWalletRegistryOperationHash(address walletRegistry) public view returns (bytes32)
function getSetProjectWalletShareBpsOperationHash(uint256 shareBps) public view returns (bytes32)
function getSetFeeCollectorOperationHash(address collector, bool authorized) public view returns (bytes32)
function getCreateNFTModifierOperationHash(string memory name, string memory symbol) public view returns (bytes32)
function getMintNFTOperationHash(address collection, address to, uint256 expiration, uint256 volumeLimit, uint256 discountBps)
    public view returns (bytes32)
```

The expected flow is:

1. Build the operation hash through the target contract helper.
2. Finalize an enforced `FROSTCoordinator` session for that exact hash and relay it through `FROSTAuthorization`.
3. For confederations, collect custodian signatures for the same hash.
4. Execute the config change on the fee contract.

## Distribution

`FeeDistributionCalculator` splits a fee amount into member and custodian groups:

```solidity
enum DistributionMode { Equal, ByPercentage }

struct DistributionConfig {
    uint256 memberShareBps;
    uint256 custodianShareBps;
    DistributionMode memberMode;
    DistributionMode custodianMode;
}
```

Validation requires `memberShareBps + custodianShareBps <= 10000`.

Equal mode divides a group amount by active recipient count. ByPercentage mode uses recipient weights.

## Project Wallets

`ProjectWalletRegistry` stores local project wallets:

```solidity
struct WalletEntry {
    address wallet;
    uint256 shareBps;
    string label;
    bool isActive;
}
```

Current validation rejects zero wallets, duplicate wallets, zero shares and total active shares above 10000 bps.

`GlobalProjectWalletRegistry` is separate. It is owner-managed and only accepts `recordFeeReceived` calls from registered fee managers.

## MultiPool Integration

Deposits use `calculateFee(true, mpAmount)` and mint shares for `mpAmount - fee`.

Withdrawals calculate the nominal fee using:

- `calculateFee(false, shareAmount)` for normal requests;
- `calculateFeeWithNFT(false, shareAmount, nftTokenId, msg.sender)` for NFT-discounted requests.

For both paths, the net MP payout is rounded down to a multiple of `10^10`
base units (one satoshi), and the final fee is `shareAmount - mpAmount`.
The rounding remainder therefore belongs to the fee. A zero payout reverts;
escrow also enforces the explicit minimum for the chosen P2WPKH/P2TR output.
This does not change deposit-fee arithmetic.

Both `SimpleMultiPool` and `ConfederationMultiPool` use SafeERC20 for MP-token deposits. Withdrawal escrow locks actual S and MP tokens. `processWithdrawal` prepares a request; only verified Bitcoin-payment settlement burns S/MP and completes processing. Cancellation before the signing boundary releases escrowed tokens.

Pools track `accruedFees(mpToken, isWrap)`. Deposit fees accrue immediately; withdrawal fees accrue only after proven Bitcoin settlement and burn, so cancellation does not collect a fee. `getClaimFeesOperationHash` binds the MP token, stream, recipient and amount. Approved `claimFees` transfers that amount, reduces accrued fees and pool TVL, and cannot extract user collateral beyond the accrued balance. Confederation claims also require the custodian threshold.

## Custodian DAO Fee Streams

The vault records `WRAP_FEE`, `UNWRAP_FEE` and `DEPLOY_FEE` separately. Use
`depositFeeV1(day, FeeStream, amount)` with router enum values `Wrap=0`,
`Unwrap=1`, `Deploy=2`. Wrap and Unwrap both use `wrapStakerBps`; Deploy uses
`deployStakerBps`. Register the router as the vault source for each stream that
will receive a nonzero DAO allocation. Vault `Stream` values differ:
`Yield=0`, `WrapFee=1`, `DeployFee=2`, `UnwrapFee=3`.
The legacy boolean API maps `false` to Deploy, so it must not be used for unwrap.
For ABI compatibility, the daily report's `stakerWrapFeeIncome` and vault's
`dayWrapFeeIncome` aggregate wrap plus unwrap; `dayUnwrapFeeIncome` exposes the
unwrap component separately. Reports must reconcile the complete funded amounts.

The integration bootstrap uses `PoolFeePipeline.route` for atomic claim,
conversion and router funding. `CustodianDAOConversionAdapter` also supports a
separate approved conversion from a payer that already holds the fee tokens.
Both require a configured converter, actual liquidity and an approved minimum
output. Counters alone do not transfer or price assets. Empty LP/staker groups
follow the immutable-treasury policy described in [Custodian DAO](CUSTODIAN_DAO.md).
See the [funded-flow diagram](FEE_FLOW.md) and [project fee deployment runbook](STAGED_DEPLOYMENT.md)
for the separate authorization and treasury paths.
