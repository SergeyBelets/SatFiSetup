# API Reference

[Documentation Index](./README.md)

This page lists selected current contract APIs. V6 independently selects Ethereum mainnet/Sepolia/local and Bitcoin mainnet/testnet4/regtest; see [networks and automatic progression](NETWORKS_AND_AUTOMATION.md). The [upgrade review](UPGRADE_REVIEW_2026_10_18.md) retains its earlier validation scope. Generated ABIs remain the source of truth for integrations; `python ops/generate_abis.py --check` detects drift.

## ZK / address additions: current code boundary

See [architecture and diagrams](ZK_SPV_ARCHITECTURE.md) for configuration and
evidence scopes. Generated ABIs are authoritative; `python ops/generate_abis.py
--check --zk` also checks the isolated state/verifier/adapter artifacts.

| Component | Entry points / data | Scope |
| --- | --- | --- |
| ZkBitcoinStateV2 / ZkSpvAdapter | Proof-bound state transitions, current roots and header materialization | Mainnet policy V2; [bootstrap/operator API](ZK_SPV_BOOTSTRAP.md) |
| SPVMessageBoard | On-chain key epochs, encrypted REQUEST/ACK/STATUS and canonical inbox | Transport authority only; [runbook](SPV_ENCRYPTED_TRANSPORT.md) |
| SPVServicePaymentRegistry | Exact-payer permissions, admission, payment and immutable economic terms | Explicit issuer/payer policy; [automation](SPV_AUTOMATION_ARCHIVE.md) |
| BitcoinDepositRegistry | `setAddressRegistry`, `registerAddressScript`, `consumeAddress`; shared consumed/backing/wallet-output records | New candidate graph only; one-time installation before base registration |
| BitcoinAddressRegistry | `registerBatch`, `getBinding`, `getStatusOperationHash`, `setStatus`, `requireCustody`, `validateMint` | Immutable bindings and two generation/status scopes; [full tuple schemas](ADDRESS_DEPOSIT_REGISTRY.md) |
| BitcoinAddressMessageBoard | `registerKey`, `request`, `reply`, `packet`, `route`, bounded recipient inbox | Separate authenticated address ciphertext delivery; ACK/status grant no Ready or monetary authority; [service](ADDRESS_REQUESTS_WATCH.md) |
| Address registration client | `registration-prepare/propose/adopt/execute/status/retry/import/export` | V2 requester commitments, canonical public DKG intents and current generation/deadline guards; [schemas/runbook](ADDRESS_REGISTRATION.md) |
| Address SPV worker | `kind=address_deposit`, worker V5 / transport V3; address client `spv-sync` | Registered intent/UTXO review, stable economic need, bounded watch scheduling and guarded admission/quote/payment; [runbook](ADDRESS_SPV_WORKER.md) |
| BitcoinMintAdmission | `openAddressMint`, `openCustodyMint`, `retryGenerationMint`, `getAddressClaim`, `getAttemptAddressClaim`, `claimModes`, `attemptFences` | One common slot; immutable economics/deadline, generation-bound attempts and retained history |
| MiningPoolV1 | `mintProofVersion`, `getAddressMintOperationHash`, `mintFromAddressProof`, `getCustodyMintOperationHash`, `mintFromProofV2` | Candidate address/generation paths share the pool mint nonce; ordinary graphs retain `mintFromProof` |
| BitcoinDepositDerivation | `derive`, `verify`, `PROFILE`, `QUALIFIED` | Arithmetic candidate has `QUALIFIED=false`; not an address activation API |

The address registry begins DERIVATION SUSPENDED. There is no Ready API and no
automatic bootstrap installation. Status operations use separate scope sequences;
they do not spend the mint nonce. The legacy signer refuses this new graph until
generation-aware Bitcoin plans/journal recovery are supported. The separate
mint-only client connects address admission and independently checks funding
proofs/generations; successful address receipts also retain `mintedAddressSats`.

## FROSTAuthorization

File: `web3/contracts/src/FROSTAuthorization.sol`

```solidity
constructor(address coordinator, bytes groupPubkey, address[] participants, uint256 threshold)

function authorizeOperation(uint256 sessionId, bytes32 operationHash, address target) external
function consumeOperation(bytes32 operationHash) external returns (bool)
function isOperationApproved(bytes32 operationHash, address target) external view returns (bool)
function hashOperation(address target, bytes4 selector, bytes32 paramsHash, uint256 nonce)
    external view returns (bytes32)
function getParticipants() external view returns (address[] memory)
function isParticipant(address account) external view returns (bool)
```

Important getters: `coordinator`, `verifier`, `groupPubkey`, `threshold`, `consumedSessions`, `operationTargets`, `approvedOperations`, `executedOperations`.

## DKGAuthorization

New V1 factory deployments use this subclass of `FROSTAuthorization`:

```solidity
constructor(address coordinator, bytes32 dkgId, bytes group, address[] roster, uint256 threshold)
```

It verifies a completed `EVM_AUTH` result and fixes `registry`, `dkgId` and `resultHash`. Authorization additionally requires the signing session to reference that exact completed result; a matching group key in a legacy session is insufficient.

## FROSTCoordinator

File: `web3/contracts/src/FROSTCoordinator.sol`

```solidity
function bindDKGRegistry(address registry) external
function createSessionFromDKG(bytes32 dkgId, bytes32 messageHash, address target, uint64 deadline)
    external returns (uint256 sessionId)

function createSession(
    bytes groupPubkey,
    address[] participants,
    uint256 threshold,
    uint64 deadline,
    bool enforceSharesCheck,
    address verifierOverride,
    SessionPurpose purpose,
    OriginMeta origin
) external returns (uint256 sessionId)

function bindMessageHash(uint256 sessionId, bytes32 messageHash) external
function registerParticipantPublicKeyShare(uint256 sessionId, address participant, uint256 x, uint256 y) external
function submitNonceCommitments(uint256 sessionId, uint256 dX, uint256 dY, uint256 eX, uint256 eY) external
function submitVerifiableSignatureShare(uint256 sessionId, uint256 share) external
function rejectSignatureRequest(uint256 sessionId, string reason) external
function finalizeSession(uint256 sessionId, bytes aggregateSignature, bytes32 messageHash) external

function getSession(uint256 sessionId) external view returns (...)
function getSessionParticipants(uint256 sessionId) external view returns (address[] memory)
function getNonceCommitmentPoints(uint256 sessionId, address participant) external view returns (...)
function getVerifiedShareStatus(uint256 sessionId, address participant) external view returns (...)
function getNonceSetStatus(uint256 sessionId) external view returns (...)
function getResult(uint256 sessionId) external view returns (bytes signature, bytes32 messageHash)
function getAuthorizationResult(uint256 sessionId) external view returns (...)
```

`bindDKGRegistry` is a one-time `ADMIN_ROLE` setup action. `createSessionFromDKG` loads the completed EVM result, original roster/threshold and public shares, and binds the exact message/target without separate share-registration calls. New DKG-bound deployments use this path. Participant-created legacy sessions are allowed only for enforced, canonical-verifier sessions with a deployed origin target. Legacy overloads remain role-restricted for imported integrations and are rejected by `FROSTAuthorization` if they are not enforced.

## FederationFactory

```solidity
constructor(address dkgCoordinator, address federationDeployer, address frostCoordinator)

function reserveFederationId(bytes32 sessionId, string federationId) external
function reservedFederationSession(string federationId) external view returns (bytes32)
function adoptFederationDKG(bytes32 sessionId, string federationId) external
function configureDeploymentFee(address router, uint256 amount, bool exemption) external
function initiateFederationDKG(address[] participants, uint256 threshold, string federationId)
    external returns (bytes32 sessionId)
function deployFederation(bytes32 sessionId, string shareTokenName, string shareTokenSymbol)
    external returns (address authorization, address multiPool, address shareToken)
function deactivateFederation(string federationId, bytes32 operationHash) external
function getDeactivateFederationOperationHash(string federationId) public view returns (bytes32)
function getFederation(string federationId) external view returns (FederationConfig memory)
function getDKGSession(string federationId) external view returns (bytes32)
```

`reserveFederationId` binds an unused federation ID to one session. Only the
factory deployer (`feeSetupAuthority`) can reserve it. The exact repeat is a
no-op. `adoptFederationDKG` requires the reserved pair, a completed `EVM_AUTH`
ceremony from the canonical registry and a caller in that ceremony's roster.
The deployer may initiate legacy DKG on Anvil/Sepolia for an unreserved ID.
`configureDeploymentFee` is a separate one-time action by the same deployer.
Paid deployment transfers the configured fee atomically.

`getFederation` reads the pool's current fee manager and its NFT/wallet registries, so configured component addresses are discoverable after approved setup. The distributor and treasury are getters on that fee manager.

## ConfederationFactory

```solidity
constructor(address federationFactory, address dkgCoordinator, address deployer, address frostCoordinator)

function configureDeploymentFee(address router, uint256 amount, bool exemption) external
function reserveConfederationId(bytes32 sessionId, string confederationId) external
function reservedConfederationSession(string confederationId) external view returns (bytes32)
function adoptConfederationDKG(bytes32 sessionId, string[] federationIds,
    CustodianMember[] custodians, uint256 totalThreshold, uint256 minCustodians, string confederationId) external
function adoptConfederationFromFederation(bytes32 sessionId, string federationId,
    CustodianMember[] custodians, uint256 totalThreshold, uint256 minCustodians, string confederationId) external
function adoptCustodianConfederationDKG(bytes32 sessionId, string[] federationIds,
    CustodianMember[] custodians, uint256 threshold, uint256 minCustodians, string confederationId) external
function initiateConfederation(
    string[] federationIds,
    CustodianMember[] custodians,
    uint256 totalThreshold,
    uint256 minCustodians,
    string confederationId
) external returns (bytes32 sessionId)
function deployConfederation(bytes32 sessionId, string shareTokenName, string shareTokenSymbol)
    external returns (address authorization, address custodianValidator, address multiPool, address shareToken)
function deactivateConfederation(string confederationId, bytes32 operationHash) external
function getDeactivateConfederationOperationHash(string confederationId) public view returns (bytes32)
function getConfederation(string confederationId) external view returns (ConfederationConfig memory)
function getCustodianDAO(string confederationId) external view returns (CustodianDAOConfig memory)
function getRegisterCustodianDAOOperationHash(string confederationId, CustodianDAOConfig dao)
    public view returns (bytes32)
function registerCustodianDAO(string confederationId, CustodianDAOConfig dao, bytes32 operationHash) external
```

The factory deployer (`feeSetupAuthority()`) binds an unused ID to one DKG session.
The binding is permanent and the exact retry is a no-op. All three adoption methods
require the reserved pair on this factory. A missing or mismatched reservation raises
`InvalidConfederationReservation` for an unused ID. The legacy `initiateConfederation`
is limited to that deployer on local/Sepolia and excludes reserved IDs and sessions.

Adoption checks the exact ordered roster and threshold of a completed `EVM_AUTH` result
on the canonical registry. The mixed paths require caller membership and use each
federation's `members[0]` or all ordered members of one federation, respectively.
Custodians follow founders, with no overlaps. For founder count `f`, custodian count
`c` and threshold `t`, require `c >= ceil(f/2)`, `t >= ceil(7*(f+c)/10)`,
`t > f`, `t-f <= minCustodians <= c`, and `f+c <= 16`.
The custodian-only path admits a DKG participant or source federation representative.
Its key roster contains only custodians, with `2 <= c <= 16`, `2 <= t`,
`t >= ceil(7*c/10)` and `t <= minCustodians <= c`. The Bitcoin epoch is separate.

`CustodianDAOConfig` contains `token`, `staking`, `controller`, `twap`, `incomeVault`, `decisions` and `settlement`. Registration is one-time and requires target-bound FROST plus the custodian threshold; the factory verifies the graph's shared authorization, validator, reward token and component bindings. Before registration the existing confederation's DAO config is zero. `getConfederation` also reads current fee-component addresses from the approved pool configuration.

## DKGCoordinator

New ceremonies use `createCeremony`, `submitRound1`, `submitShareHash`,
`submitPublicKeyShare`, recipient `submitReceipt` and unanimous `attestResult`.
`getCompletedResult` returns context/result hashes, epoch, purpose, canonical group,
ordered roster and threshold. `abortCeremony` stops an incomplete ceremony.

`submitPublicKeyShare(sessionId, bytes)` accepts SEC1 compressed 33-byte or
uncompressed 65-byte points and stores canonical compressed bytes.
`getPublicKeySharePoint(sessionId, participant)` returns `(x,y)`. The local Rust
adapter generates secrets and validates VSS; `DKGOnchainBoard` carries public
round-one packages and HPKE-encrypted directed round-two envelopes through EVM.
Thus computation stays local but the recommended transport requires no off-chain
message channel. Legacy `completeDKG` is separate from the V1 unanimous result
path. See [DKG protocol](DKG_PROTOCOL_V1.md).

V6 batches preserve the participant as the caller:

```solidity
DKGCoordinator.submitShareHashes(bytes32 sessionId, address[] recipients, bytes32[] hashes)
DKGCoordinator.submitReceiptsAndPublicKeyShare(bytes32 id, address[] senders,
    bytes32[] expectedShareHashes, bytes publicKeyShare)
DKGOnchainBoard.publishBatch(bytes32 id, uint8[] kinds, address[] recipients, bytes[] payloads)
```

The board batch accepts 1–16 packets and at most 16384 payload bytes in total.
The client persists encrypted envelopes before publication, validates complete
rounds and retains all-participant result attestation. Registry receipt batching
does not bypass the recipient's local VSS checks.

## MultiPool

`SimpleMultiPool` constructor:

```solidity
constructor(string multiPoolId, address authorization, address shareToken)
```

`ConfederationMultiPool` now takes `(string confederationId, address authorization, address custodianValidator, address shareToken)`. Its share token is created by a controlled `ConfederationTokenDeployer` for the predicted pool address. `MultiPoolDeployer` takes that leaf deployer's address in its constructor; `deployMultiPool(...)` retains its existing parameters and return tuple.

Main user API:

```solidity
function deposit(address mpToken, uint256 amount) external
function requestWithdrawal(address mpToken, uint256 shares, bytes btcScript) external returns (uint256)
function requestWithdrawalWithMetadata(address mpToken, uint256 shares, bytes btcScript, bytes metadata)
    external returns (uint256)
function processWithdrawal(uint256 withdrawalId, bytes32 operationHash) external
function cancelWithdrawal(uint256 withdrawalId) external
function claimFees(address mpToken, bool isWrap, address recipient, uint256 amount, bytes32 operationHash) external
function getClaimFeesOperationHash(address mpToken, bool isWrap, address recipient, uint256 amount)
    public view returns (bytes32)
function accruedFees(address mpToken, bool isWrap) external view returns (uint256)
```

The NFT withdrawal API also uses dynamic `bytes`. Pass the full raw `scriptPubKey`, not padded bytes32. The configured escrow accepts only supported P2WPKH/P2TR outputs meeting its explicit satoshi minima. Both pool paths round net MP down to a multiple of `10**10` base units (one satoshi), assign the remainder to the fee, and reject a zero payout. This changes withdrawal selectors and generated ABIs. Fee-manager setup requires matching authorization/validator bindings and an already authorized fee collector. Wrap fees accrue on deposit; unwrap fees accrue only when proof settlement finalizes the withdrawal. The request locks MP/S; `processWithdrawal` records approval/preparation without burning. Cancellation/refund remains constrained by the payment/signing state. `claimFees` transfers only accrued MP-token fees, with both thresholds for a confederation. See [Fees](./FEES.md) for the conversion path.

Governance helpers include `getAddPoolOperationHash`, `getDeactivatePoolOperationHash`, `getSetFeeManagerOperationHash` and `getProcessWithdrawalOperationHash`. Callers must submit exactly the returned hash.

`ConfederationMultiPool` exposes the equivalent pool flow with additional custodian approval requirements.

## Managed discount NFT and fee quote

`FederationFeeManager` exposes typed governed issuance and separate quote/use:

```solidity
function createNFTModifier(string name, string symbol, bytes32 operationHash)
    external returns (address collection)
function mintNFT(address collection, address to, uint256 expiration, uint256 volumeLimit,
    uint256 discountBps, bytes32 operationHash) external returns (uint256 tokenId)
function getCreateNFTModifierOperationHash(string name, string symbol) public view returns (bytes32)
function getMintNFTOperationHash(address collection, address to, uint256 expiration,
    uint256 volumeLimit, uint256 discountBps) public view returns (bytes32)
function quoteFeeWithNFT(bool isWrap, uint256 amount, uint256 nftTokenId, address account)
    external view returns (uint256 fee)
function calculateFeeWithNFT(bool isWrap, uint256 amount, uint256 nftTokenId, address account)
    external returns (uint256 fee)
```

Create/mint binds every argument, target, chain, authority and operation nonce.
A permissionless relayer may deliver the exact approved call; it cannot change
the recipient or issue another NFT by replay. Confederations also require
custodian minimum approvals. Canonical managed collections grant admin/minter/
usage to the fee manager. `nftOperationReceipts`, `nftCreatedCollections` and
`nftMintedTokenIds` support exact completion; token ID 0 is valid.

Public quote is read-only. Consuming calculation is restricted to approved
contract collectors; canonical pools pass account=msg.sender. A direct EOA call
cannot consume someone else's NFT quota. Follow the [NFT runbook](NFT_CONFIGURATION.md)
for independent pins, `--nft-config`, public intents and legacy migration limits.

## CustodianValidator

```solidity
function registerCustodianSignature(bytes32 operationHash) external
function countCustodianSignatures(bytes32 operationHash) external view returns (uint256)
function meetsMinimumCustodians(bytes32 operationHash) external view returns (bool)
function addCustodian(address custodian, bytes32 approvalHash) external
function removeCustodian(address custodian, bytes32 approvalHash) external
function setMinCustodians(uint256 minimum, bytes32 approvalHash) external
```

Management functions consume FROST authorization and require the current custodian threshold for the same hash.

## Custodian DAO

The separate Custodian DAO deployment takes an active confederation's `CustodianValidator` and `FROSTAuthorization`. Details and units are in [Custodian DAO](./CUSTODIAN_DAO.md).

```solidity
CustodianDAOToken(string name, string symbol, address recipient)
CustodianDAOStaking(address stakeToken, address rewardToken, address custodianValidator)
CustodianDAORateController(address custodianValidator, address staking, bool initialUseReserve)
CustodianDAODecisionBoard(address staking, address authorization)
ChainlinkBTCDailyTWAP(address btcUsdFeed)
CustodianDAOIncomeVault(address rewardToken, address authorization)
CustodianDAOClaimAdapter(address incomeVault, address protocol, bytes32 sourceId)
CustodianDAOSilo4626Adapter(address incomeVault, address siloVault)
CustodianDAOConversionAdapter(address incomeVault, address inputToken,
                              address converter, bytes32 sourceId)
CustodianDAOFeeRouter(address incomeVault, address treasury,
                      uint16 wrapStakerBps, uint16 deployStakerBps)
CurvePoolLiquidityGuard(address wrapperToken, address curvePool)
CustodianDAODailySettlement(address rewardToken, address staking, address controller,
                            address authorization, address twap, address incomeVault,
                            address treasury)

CustodianDAOStaking.stake(uint256 amount)
CustodianDAOStaking.unstake(uint256 amount)
CustodianDAOStaking.setDAOSigner(address signer, bytes consentSignature)
CustodianDAOStaking.claim(uint256[] days)
CustodianDAOStaking.claimableReward(address owner, uint256[] days)
CustodianDAOStaking.eligibleStake(address owner, uint256 day)

CustodianDAORateController.propose(Parameter parameter, uint256 value, address custodian)
CustodianDAORateController.vote(uint256 proposalId, address custodian)
CustodianDAORateController.execute(uint256 proposalId)
CustodianDAORateController.rateForDay(uint256 day)
CustodianDAORateController.useReserveForDay(uint256 day)
CustodianDAORateController.canVote(uint256 proposalId, address custodian)
CustodianDAODecisionBoard.getCreateOperationHash(string metadataURI, bytes32 contentHash,
    uint256 duration, uint16 quorumBps)
CustodianDAODecisionBoard.createDecision(string metadataURI, bytes32 contentHash,
    uint256 duration, uint16 quorumBps, bytes32 operationHash)
CustodianDAODecisionBoard.vote(uint256 id, address owner, Choice choice)
CustodianDAODecisionBoard.finalize(uint256 id)
CustodianDAODecisionBoard.canVote(uint256 id, address owner)

ChainlinkBTCDailyTWAP.calculate(uint256 day, uint80[] roundIds)
CustodianDAOIncomeVault.getSetSourceOperationHash(bytes32 sourceId, address account,
    Stream stream, bool active, address liquidityGuard, uint256 minimumLiquidity)
CustodianDAOIncomeVault.setSource(bytes32 sourceId, address account, Stream stream,
    bool active, address liquidityGuard, uint256 minimumLiquidity, bytes32 operationHash)
CustodianDAOIncomeVault.deposit(uint256 day, bytes32 sourceId, uint256 amount)
CustodianDAOClaimAdapter.getClaimOperationHash(uint256 day, bytes protocolCall, uint256 minimumReceived)
CustodianDAOClaimAdapter.claimAndDeposit(uint256 day, bytes protocolCall,
    uint256 minimumReceived, bytes32 operationHash)
CustodianDAOSilo4626Adapter.getSupplyOperationHash(address payer, uint256 assets, uint256 minimumShares)
CustodianDAOSilo4626Adapter.supply(address payer, uint256 assets, uint256 minimumShares, bytes32 operationHash)
CustodianDAOSilo4626Adapter.getHarvestOperationHash(uint256 day, uint256 minimumYield)
CustodianDAOSilo4626Adapter.harvest(uint256 day, uint256 minimumYield, bytes32 operationHash)
CustodianDAOSilo4626Adapter.getWithdrawOperationHash(address recipient, uint256 assets)
CustodianDAOSilo4626Adapter.withdrawPrincipal(address recipient, uint256 assets, bytes32 operationHash)
CustodianDAOSilo4626Adapter.getEmergencyRedeemOperationHash(address recipient, uint256 minimumReceived)
CustodianDAOSilo4626Adapter.emergencyRedeem(address recipient, uint256 minimumReceived, bytes32 operationHash)
CustodianDAOConversionAdapter.getConvertOperationHash(uint256 day, address payer,
    uint256 amountIn, uint256 minimumReward, bytes converterCall)
CustodianDAOConversionAdapter.convertAndDeposit(uint256 day, address payer,
    uint256 amountIn, uint256 minimumReward, bytes converterCall, bytes32 operationHash)
CustodianDAOFeeRouter.getSetFeeSharesOperationHash(uint16 wrapBps, uint16 deployBps)
CustodianDAOFeeRouter.setFeeShares(uint16 wrapBps, uint16 deployBps, bytes32 operationHash)
CustodianDAOFeeRouter.getSetTreasuryOperationHash(address newTreasury)
CustodianDAOFeeRouter.setTreasury(address newTreasury, bytes32 operationHash)
CustodianDAOFeeRouter.getSetPayerOperationHash(address payer, bool allowed)
CustodianDAOFeeRouter.setPayer(address payer, bool allowed, bytes32 operationHash)
CustodianDAOFeeRouter.depositFee(uint256 day, bool isWrap, uint256 amount)
CurvePoolLiquidityGuard.availableLiquidity()
CustodianDAODailySettlement.getSettlementOperationHash(DayReport report, uint80[] roundIds)
CustodianDAODailySettlement.settleDay(DayReport report, uint80[] roundIds, bytes32 operationHash)
CustodianDAODailySettlement.getSettlement(uint256 day)
CustodianDAODailySettlement.fundReserve(uint256 amount)
CustodianDAODailySettlement.claimLiquidityIncome()
```

`DayReport` carries separate `stakerWrapFeeIncome` and `stakerDeployFeeIncome` fields plus a nonzero `evidenceHash`; the returned `Settlement` records all three. Vault source changes require enforced FROST authorization and settlement pulls only the full recorded day. The contract-core versus end-to-end coverage is tracked in the [review matrix](./CUSTODIAN_DAO_REVIEW.md).

`CustodianDAODecisionBoard` records non-executing token-holder decisions. `CustodianDAOFeeRouter.depositFee` routes actual reward tokens held by an authorized payer; neither it nor the income vault can spend a fee counter. `CustodianDAORateController.execute` rejects expired proposals and proposals older than the last executed one for the same parameter. Execution exactly at the voting deadline remains allowed. The Silo adapter's all-shares emergency redemption remains subject to the underlying vault's withdrawal limits.

Additional remediation APIs:

```solidity
SimpleMultiPool.pendingUnreportedFees(address token, bool isWrap)
SimpleMultiPool.syncFeeReports(address token, bool isWrap)
ConfederationMultiPool.pendingUnreportedFees(address token, bool isWrap)
ConfederationMultiPool.syncFeeReports(address token, bool isWrap)
CustodianDAOIncomeVault.MAX_YIELD_SOURCES_PER_DAY()
CustodianDAOIncomeVault.getFundedYieldSources(uint256 day)
ChainlinkBTCDailyTWAP.append(uint256 day, uint80[] roundIds)
ChainlinkBTCDailyTWAP.finalizedPrice(uint256 day)
CustodianDAOSilo4626Adapter.getEmergencyRedeemPartialOperationHash(address recipient, uint256 shares, uint256 minimumReceived)
CustodianDAOSilo4626Adapter.emergencyRedeemPartial(address recipient, uint256 shares, uint256 minimumReceived, bytes32 operationHash)
```

TWAP pages contain at most 512 rounds; the first page must reach beyond UTC day start. A finalized price can be used in settlement with empty `roundIds`. Partial emergency redemption remains subject to actual vault liquidity. Original SR-01..SR-10 defects and their regression evidence are documented in the [strict review](./STRICT_REVIEW_2026_10_05.md).

## Controlled Deployment

```solidity
function setAuthorizedDeployer(address deployer, bool authorized) external
function authorizedDeployers(address deployer) external view returns (bool)
```

Important leaf API:

```solidity
FROSTAuthorizationDeployer.deployFROSTAuthorization(
    address coordinator, bytes groupPubkey, address[] participants, uint256 threshold
)

SimpleMultiPoolDeployer.deploySimpleMultiPool(
    string federationId,
    address[] members,
    uint256 threshold,
    string shareTokenName,
    string shareTokenSymbol,
    address coordinator,
    bytes groupPubkey
) returns (address multiPool, address authorization, address shareToken)
```

`FeeSystemDeployer.deployFeeSystem(...)` retains its component configuration/return tuple and delegates each component creation to a controlled leaf deployer.

## SPV And Redemption

Legacy `SimpleSPVContract` controls (not the mainnet relay):

```solidity
function addBlockHeader(bytes header) public
function checkTxInclusion(bytes32 blockHash, bytes32 txid, bytes32[] siblings, uint256 index)
    external view returns (bool)
function setPoolContract(address pool) external
function registerPoolUTXO(bytes32 txid, uint32 vout) external
function markPoolUTXOSpent(bytes32 txid, uint32 vout) external
function disableTestHelpers() external
```

Test helper mutation methods must not remain enabled in production.

For fees and project wallets, see [FEES.md](./FEES.md). For protocol invariants, see [FROST.md](./FROST.md).

## Mainnet relay and Bitcoin settlement

```solidity
MainnetHeaderRelay(bytes rawCheckpoint, uint64 height, uint256 chainWork,
                   bytes[] precedingHeaders, uint64 reorgLimit)
MainnetHeaderRelay.addBlockHeader(bytes raw)
MainnetHeaderRelay.addBlockHeaders(bytes[] headers)
MainnetHeaderRelay.getBlockStatus(bytes32 blockHash)
MainnetHeaderRelay.checkTxInclusion(bytes32 blockHash, bytes32 txid,
                                    bytes32[] siblings, uint256 index)
MainnetHeaderRelay.getMainchainHead()
MainnetHeaderRelay.getMainchainHeight()
MainnetHeaderRelay.testHelpersEnabled()

BitcoinBatchRegistry(address authorization, address registry, bytes32 walletId)
BitcoinWithdrawalEscrow(address registry, address relay, uint32 confirmations,
                         uint256 minP2WPKHSats, uint256 minP2TRSats)
BitcoinWithdrawalEscrow.settle(bytes32 batch, bytes raw, bytes32 blockHash,
                               bytes32[] siblings, uint256 index)
BitcoinSigningBoard(address registry, uint256 maximumPacketBytes)
BitcoinSigningBoard.publishPlan(bytes32 batchId, bytes payload)
BitcoinSigningBoard.plan(bytes32 batchId)
BitcoinSigningBoard.getPlanHash(bytes32 batchId, bytes payload)
BitcoinSigningBoard.publish(bytes32 batchId, uint32 inputIndex, PacketKind kind, bytes payload)
BitcoinSigningBoard.publishBatch(bytes32 batchId, PacketKind kind, bytes[] payloads)
BitcoinSigningBoard.packet(bytes32 batchId, uint32 inputIndex, PacketKind kind, address sender)
```

`MainnetHeaderRelay` takes a trusted period-boundary checkpoint, its chainwork and
ten consecutive preceding headers for MTP; `reorgLimit` is explicit. Its inclusion
argument order is **block hash, then txid**. Consumers separately require canonical
membership and confirmation depth. A reorg beyond the rollback limit permanently
halts proof acceptance; there is no administrator recovery. See [Mainnet relay](MAINNET_RELAY.md).
`Testnet4HeaderRelay` exposes the same relay interface with BIP94 consensus rules.
New CLI header batches contain at most `min(policy.max_headers_per_sync, 16)`
headers. Already signed pending batches keep their original bytes and nonce.

`BitcoinBatchRegistry` binds the canonical registry and a completed `BTC_SPEND`
roster/threshold matching its EVM authorization. Its `Signing` state does not
permit timeout refunds. Bounded replacements keep the same inputs and leave older
signed versions payable. `BitcoinWithdrawalEscrow.settle` verifies a matching
Bitcoin payment and proof depth, records spent deposit inputs, then burns locked
MP and finalizes the pool withdrawal/S burn. See [Bitcoin withdrawals](BITCOIN_WITHDRAWALS.md).

`BitcoinSigningBoard` carries the approved immutable public plan (maximum 16384
bytes), commitments and signature shares. The CLI validates canonical public
packet types and bindings, verifies and aggregates shares, broadcasts and settles
via SPV. Secret nonces/KeyPackages never belong in these records. V6 selects
Bitcoin mainnet/testnet4/regtest independently of EVM. Local tests exercise these
branches; they do not establish a funded public-network deployment or payment.
The signing batch must contain every input in the fixed plan, in order, from
one sender; 1–16 packets and at most 16384 aggregate payload bytes are allowed.

The `frost-signer dkg` actions are `btc-deploy-board`, `btc-prepare`, `btc-propose`,
`btc-execute`, `btc-publish-plan`, `btc-pending`, `btc-status`, `btc-commit`,
`btc-sign`, `btc-broadcast`, and `btc-settle`. `--board` identifies the DKG board;
`--signing-board` identifies the Bitcoin board. `--operation reserve|bind`
selects the two independently authorized operations. See the
[payment command reference and runbook](BITCOIN_ONCHAIN_RUNBOOK.md).
`flow dkg`, `flow bootstrap`, `flow payment` and `flow relay` poll these checked
operations with durable recovery; see [automation](NETWORKS_AND_AUTOMATION.md).
New financial intents and custodian approvals still require explicit review.

## Operational API v1 (2026-10-16)

The implemented loopback service and role/idempotency contract are documented in
[OPERATIONAL_API.md](OPERATIONAL_API.md) and [openapi.json](openapi.json).
`processWithdrawal` now prepares a payment; it does **not** burn tokens. Consumers
must distinguish `withdrawalPrepared` from `WithdrawalProcessed`, emitted only by
SPV settlement. Read current generated ABIs under `signer-agent/contracts`.


## Bitcoin mainnet network policy

`dkg btc-*` derives its network from the authenticated V4 invitation and pinned
wallet graph. Mainnet uses the same on-chain plan/commit/share/payment commands,
with local `--bitcoin-rpc` and `--bitcoin-cookie`; it never infers mainnet from a
remote RPC response. `dkg btc-network-status` checks Core, confirmed relay state
and the SPV guard; `dkg btc-relay` submits the bounded next header batch. Both use
`--board`, `--role btc_ceremony` and the participant's existing local state, without
requiring a payment batch or shared policy file.

Before public-Bitcoin enrollment, the operator generates a public checkpoint candidate with
`bitcoin checkpoint --bitcoin-network mainnet` and explicit policy parameters.
The invitation carries that policy on chain. See the complete flags and steps in
[the mainnet runbook](BITCOIN_MAINNET_RUNBOOK.md). Testnet4 uses its own network selector and BIP94 relay; switching an
existing wallet's network is rejected and requires a new deployment and DKG.

Mainnet readiness requires confirmed EVM observations and each participant's own
synced, unpruned Bitcoin Core with txindex. Cached public-packet retries retain
those checks. A stale/halted relay stops SPV acceptance at the contract boundary.
V6 retains Ethereum mainnet bootstrap with paid factory policy, at least twelve
confirmations plus finalized state, and explicit local gas budgets. No live
public Ethereum deployment or funded Bitcoin mainnet payment is claimed by
local validation; see [V6 instructions](ETHEREUM_MAINNET_RUNBOOK.md).

## BTC fee reserve and reconciliation

See [the reserve runbook](BTC_FEE_RESERVE.md) for the graph-scoped canonical allocations,
principal-preserving settlement, independent Core inventory and typed FROST commands.
Use `bitcoin fee-reserve` and the private `--fee-reserve-config` on participants.
Address service V2 pins the same policy without resetting watch or nonce journals.
Ready and operational qualification remain pending; long combined E2E is deferred.

## Personal withdrawal fees

`bitcoin withdrawal-fee` exposes owner consent, exact version approval, fee-purpose funding/watch/admission, token returns and separate BTC refunds. Participant `btc-import-plan` accepts the canonical V4 public plan after native checks. Typed FROST targets are `WithdrawalFeePayments.prepare` and `WithdrawalFeeFunding.register`. Schema, limits, phase isolation and recovery: [WITHDRAWAL_FEE_SUBSIDY.md](WITHDRAWAL_FEE_SUBSIDY.md). Worker V6 / encrypted transport V4 add explicit fee-credit graph pins; receipt / coverage reuse prevents redundant paid proof work. Ready remains false.
## Owner withdrawal commands

`bitcoin withdrawal prepare` generates an exact public quote for `payout` or
`total` calculations in MP/S. `submit --quote FILE --accept HASH` creates or
resumes the same primary request and its fee consent. `status --operation-id ID`
reports a partial operation; `version-quote` and `approve-version --accept HASH`
provide exact batch/RBF consent without handwritten ABI arrays. BTC fee mode
uses its separate funding/watch path. Full command examples and qualification
boundaries: [WITHDRAWAL_FEE_SUBSIDY.md](WITHDRAWAL_FEE_SUBSIDY.md#owner-withdrawal-in-both-calculation-modes).
## Wallet-funded SMP1 commands

`bitcoin deposit prepare` computes the deposit output index, SMP1 commitment,
wallet change and exact fee in a private reviewable PSBT quote. `sign`, `export`,
`import-signed`, `broadcast` and `cancel` take the exact `--accept` quote hash;
`status` separates BTC observation, header/SPV coverage, mint admission and EVM
finality. The transaction is immutable after signing. See
[wallet preparation and recovery](BITCOIN_MINT_AND_TREASURY.md#automatic-smp1-wallet-preparation)
for the Core 30.x/native-segwit profile and local configuration. Existing
`bitcoin deposit-commitment` and federation mint commands remain available.

## Recipient profiles

`python -m signer_agent.address_transport recipient-preview` produces an exact
selection for `ordinary`, `wallet-v1` or `advanced-contract`. Contract selections
require `prepare --recipient-file FILE --recipient-accept HASH`; the existing
request nonce binds the on-chain immutable profile without changing intent bytes.
`wallet-create`, `wallet-transfer`, `wallet-approve` and `wallet-withdraw` expose
the canonical `RecipientWalletV1` owner path through the shared EOA journal.
`bitcoin deposit recipient-preview` and the same selection flags add explicit
recipient review to SMP1 wallet preparation. The three profiles, unsupported
special cases, limitations and examples are documented in the
[recipient matrix](ADDRESS_DEPOSIT_REGISTRY.md#recipient-matrix).

## Incident evidence and issuance statuses

`bitcoin incident` adds private report prepare/send/forward/import/export,
`receive-once`, `flush`, `notify` and `backup` over a dedicated incident board.
`status-prepare/propose/adopt/review/approve/execute/retry/recover` and `status`
operate the existing threshold-authorized CUSTODY/DERIVATION state machine.
Signing participants use `dkg sign-evm --incident-config FILE` after explicit
local review/approval. Generation/sequence, canonical receipts, pending finality
and crash/reorg recovery are checked separately from delivery.
[Workflow and limits](ADDRESS_DEPOSIT_REGISTRY.md#incident-and-status-client).

## Old Bitcoin epoch service

`bitcoin epochs run-once --config FILE` observes a bounded set of original
wallet/address/board/batch graphs, restores registered descriptors and retains
late-deposit/refund observations. `--schedule-spv` opts into each entry's existing
admission/quote/payment policy. `review-start --departing ADDRESS --output FILE`,
participant-local `attest`, and `review --challenge-file FILE --attestation-file
FILE ...` check fresh retention claims for each original BTC_SPEND threshold.
Every command uses `--config FILE`; none authorizes rotation or spending.

The address CLI and `dkg btc-commit/btc-sign` accept
`--epoch-service-config FILE`. Durable enrollment retains the service guard
when subsequent calls omit the option. [Commands, configuration and recovery](BITCOIN_EPOCH_SERVICE.md).

## Bitcoin graph continuity

`bitcoin continuity inspect --config FILE --output FILE` exports bounded canonical
binding/backing/fee facts and optional `--request-id` / `--batch-id` locators.
`check --config FILE --candidate-config FILE` reviews original-graph compatibility;
`recover --config FILE` explicitly reanchors adequate original-graph observations.
No command transfers assets, qualifies complete history or grants production Ready.
Graph/family checks use the existing private identity and independent signer
journals. [Profile, automatic guards and limits](BITCOIN_CONTINUITY.md).

`bitcoin continuity handover prepare|review|enroll-signer --config FLEET`
records a restrictive original-source/separate-target service plan. Prepare uses
`--source-context/--target-context`; later actions use `--plan-id`. Signer enrollment
also uses its existing `--state/--password-file`. No assets, bindings or authority
move. New registration under an enrolled target requires a fresh service review;
the [canonical family root](BITCOIN_KEY_FAMILY.md) separately enforces admitted
family/status/known-alias continuity on one independently pinned EVM root.
`bitcoin continuity pin-root --root-policy FILE --identity-file FILE --evm-rpc URL`
retains an independently reviewed pin without needing a pool. `root-plan` accepts
`--root-policy/--auth-bundle/--registry-target/--deadline/--output` and produces
a read-only original-authority enrollment recipe. These commands move no assets.

`python -m ops.zkspv.contour inspect|step --config FILE` controls reviewed
stages through a private durable journal. Handover review and encrypted consumer
request/ACK/status, admission/quote/payment, preparation/fresh-proof/state-update
adapters are connected. Optional structural bootstrap and exact Bitcoin funding
adapters reuse original deployment/wallet journals. Membership and selected
SMP1/address mint, reviewed V4 withdrawal preparation/BTC/MP/S settlement and
reconciliation adapters reuse their original consent/authorization/receipt paths.
Standalone reviewed incident/recovery and old-epoch observation/retention are
connected; the multi-need orchestrator and combined collector remain.
Missing drivers/capabilities stay explicit and all acceptance/Ready flags remain
false. [Driver contract, recovery and commands](ZKSPV_CONTOUR.md).

`python -m ops.zkspv.contour_plan --worker-config FILE --request-file FILE
--directory PRIVATE_DIR --output NEW_FILE [--worker-only] [--service-config FILE]
[--bootstrap-config FILE] [--funding-config FILE] [--materialize] [--mint-config FILE]
[--consumer-only] [--withdrawal-config FILE] [--reconciliation-config FILE]
[--settlement-only] [--incident-config FILE] [--epoch-config FILE]` builds a private
plan without RPC/proving/transfers, pins config/request hashes and explicitly
retains missing coverage. Each fresh-proof action computes at most one local
stage; state publication only verifies an existing receipt and reuses the worker
transaction engine. Recovery cannot allocate a new unsigned state transaction.
The optional service configuration pins all transport actors and selects `PAID`
or explicit `CONFEDERATION` funding. Own funding uses publisher balance and
durable cost limits without depending on project treasury balance. Each service
action handles at most one EVM operation; interrupted substeps retain their
original journals and payment receipts. Final encrypted status does not assert
monetary readiness.
The contour bootstrap adapter accepts a generated ZK profile matching the
worker's selected Bitcoin network, policy, EVM chain and confirmations; it pins
the profile hash and advances
one original unit per action (a group has at most eight envelopes). It defers
freshness explicitly and does not grant Ready. Funding selects read-only mature
inclusion observation or an original SMP1 operation with an accepted quote hash;
it cannot create a new quote or repeat an ambiguous wallet signature.
`--mint-config` connects membership and the selected SMP1/address mint; the other
mint remains missing. `--consumer-only` creates just membership/mint against an
existing fresh graph. Original admission, participant histories, exact receipt/
allocation checks and independent caller gas budgets apply. The runner waits for
FROST authorization and never creates a share or implicitly retries a session.
V2 SMP1 uses the generation-bound custody methods; V1 keeps its original route.
Withdrawal mode pins one already prepared V4 initial version and exact accepted
owner/fee hashes, separate BTC_SPEND/EVM_AUTH bundles and original actor journals.
It requires the original SDK's request/consent/funding/freshness prerequisites;
it cannot be combined with bootstrap/mint/funding in the same plan. BTC broadcast
recovery uses original signed bytes before new generation checks. Settlement
requires the exact txid, paid owner request, closed fee budget and original
receipt. Reconciliation retains one complete observation and typed report;
negative reports preserve incident pauses and never pass as healthy.
`--incident-config` is exclusive and always builds a partial standalone plan.
`bitcoin incident status-preview --output FILE` exports a read-only exact status
decision and its confirmation hash before transport/session creation. The runner
pins that accepted decision, original private evidence and EVM_AUTH enrollment;
delivers only the selected encrypted request/ACK/status; and waits for external
private evidence approvals and FROST. Original nonce/cost ledgers, exact receipt
recovery and confirmed/latest generation/terminal/service guards remain in force.
It never clears a financial pause or implicitly retries/reanchors the decision.
`--epoch-config` is exclusive and creates a standalone original-fleet plan.
One action advances one bounded `run_once(schedule_spv=False)`. Positive probes
require fresh original retention review of external signed claims, generations,
qualification, complete watch/archive coverage, reserve and summed payer funding.
The challenge must already exist in the original identity lane. The adapter
does not sign claims, send payments, rotate keys or renew expiry.
