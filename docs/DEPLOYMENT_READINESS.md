# Historical deployment readiness and bootstrap — 5–17 October 2026

**Historical snapshot, not the current deployment runbook.** The earlier claims
below about absent DKG generation, deploy-fee production and unintegrated
components describe their original review dates. They were superseded by the
18 and 22 October implementations. For current work use:

- [First launch with only friends' addresses](FIRST_DKG_ONCHAIN.md): encrypted
  on-chain enrollment/DKG and the complete integration bootstrap.
- [Staged deployment](STAGED_DEPLOYMENT.md): separate project DKG on the
  Anvil/Sepolia legacy CLI, paid factories, sequential payer approvals and
  explicit parameters. Ethereum mainnet uses V6 on-chain bootstrap.
- [Current acceptance status](PARALLEL_NETWORKS_ACCEPTANCE.md) and
  [contract-size checks](CONTRACT_SIZE_AND_DEPLOYMENT.md): tested coverage,
  remaining limits and the 139-byte minimum runtime margin.
- [Contract provenance](CONTRACT_PROVENANCE.md): canonical creation checks
  required for reused infrastructure.

The paid factory scenario has passed local tests. Public Sepolia paid deployment
has not been executed. V6 selects Ethereum mainnet/Sepolia/local and Bitcoin
mainnet/testnet4/regtest independently; see the [network matrix](NETWORKS_AND_AUTOMATION.md).
Public Bitcoin uses independently synchronized Core, a pinned checkpoint policy
and `MainnetSPVGuard`; see [Bitcoin setup](BITCOIN_MAINNET_RUNBOOK.md),
[Ethereum mainnet setup](ETHEREUM_MAINNET_RUNBOOK.md) and the historical
[security review](BITCOIN_MAINNET_SECURITY_REVIEW.md). Ethereum mainnet requires
paid factory policy, at least 12 confirmations and finalized state. A live public deployment and a
funded Bitcoin mainnet payment are not established by local tests.

Earlier fresh-clone and cross-checkout DKG verification: [acceptance report](ACCEPTANCE_REPORT.md). Relative compiler remappings are required for identical runtime bytes on different participant computers.

17 October deployment follow-up: [contract sizes and deployment scripts](CONTRACT_SIZE_AND_DEPLOYMENT.md).

17 October follow-up: see [point-by-point specification audit](TZ_AUDIT_2026_10_17.md) and [first DKG using EVM transport](FIRST_DKG_ONCHAIN.md). Earlier readiness statements below describe their dated scope.

[Documentation Index](./README.md)

Repository cross-check: 5 October 2026. This document covers the reported DAO/bootstrap gaps and the changes made after that check. It does not establish the state of a remote deployment; the local environment has no federation/confederation address or DAO parameters.

The [strict internal review](./STRICT_REVIEW_2026_10_05.md) records code remediation and regression tests for SR-01 through SR-10, including independent re-review. This is not external audit sign-off: selected markets/feed, off-chain DKG and BTC/report integration, deploy-fee production and explicit economic-policy choices remain deployment dependencies.

## Reported Issues

| Observation | Result |
|---|---|
| An old approved rate proposal can overwrite a newer one | Fixed. Execution must occur no later than the voting deadline. Executing proposal `n` supersedes lower IDs for the same parameter; rate and reserve-flag ordering are independent. |
| Fee-manager counters are not DAO income | Correct. Pools now accrue fees by MP token and wrap/unwrap stream, record them in the fee manager, and expose target-bound `claimFees`. The recipient can convert actual MP tokens through `CustodianDAOConversionAdapter`. The chosen converter, minimum output and DAO allocation still need configuration and FROST approval. |
| Pools never call `recordFeeCollected` | Fixed through `recordFeeCollectedForToken`. Wrap fees accrue on deposit; unwrap fees accrue on successful request processing. Pending/cancelled requests do not accrue unwrap fees. `totalCollected` is an aggregate of raw token units, not a USD valuation. |
| DAO addresses exist only in frontend config | Fixed for new deployments. `ConfederationFactory.getCustodianDAO(id)` exposes the seven canonical addresses after one-time, FROST-and-custodian-approved registration. The deployment script prints the registration hash and calldata; deploying alone does not grant registry authority. |
| Fee-system addresses are lost | Once approved setup is complete, factory config getters read the current pool fee manager and its NFT/wallet registries. Treasury and distributor are getters on that manager. Before setup, deployment receipts and leaf-deployer events remain the source of newly created addresses. |
| `btcScript` has a fixed 32-byte representation | Fixed. All federation/confederation withdrawal APIs store raw `bytes` and bind the complete script to approval. Inputs must contain 1-10,000 bytes. This is an ABI change; regenerate ABIs and update the frontend. |
| DKG public shares are arbitrary bytes | Fixed at the point-format boundary. Submit a SEC1 compressed 33-byte or uncompressed 65-byte secp256k1 point. The registry validates it, stores a canonical compressed point and exposes `getPublicKeySharePoint(session, participant)` for signing-session registration. |
| There is no MP deployment script | Two scripts exist: `DeployDemo.s.sol` and `DeployDemoWithCalculator.s.sol`. Both deploy SPV, a mining pool and its MP token, but contain demo identifiers and a placeholder payout script. They are not a configured production bootstrap. |
| There is no off-chain DKG generator | Confirmed. The signer agent imports a ready-made secret scalar; it does not run a distributed key-generation ceremony or exchange encrypted shares. The on-chain DKG registry also cannot create those secrets. |
| `FederationGovernance` is not deployed by the factory | Confirmed. It is an optional proposal wrapper with a constructor and tests; current deployment scripts do not deploy it. Direct target-bound FROST pool operations are supported. |
| There is no active confederation or DAO configuration | Network-specific prerequisite. The local `.env` contains RPC/key settings but no DAO inputs or confederation address. Absence on a particular network must be checked against its factory. |
| Frontend work must wait for network deployment | Contract integration can be developed against Foundry fixtures or local Anvil. A live-network configuration needs the deployed factory and its canonical DAO registry entry. |

## Bootstrap Order

1. Configure the network RPC and deployment account. Simulate `DeployFederation.s.sol` and run the size gate before broadcasting its infrastructure graph.
2. Run a reviewed off-chain DKG ceremony for the federation's exact ordered participant list and threshold. Every participant must retain its own secret share; publish only commitments, encrypted-share hashes and public points.
3. Call `initiateFederationDKG`, complete its commitment/share/public-key phases, collect threshold approvals of the agreed group key and call `completeDKG`. Deploy the federation with `deployFederation`. Use the even-Y, 32-byte x-only group key required by `FROSTAuthorization`.
4. Use the active federation IDs and chosen custodian list to call `initiateConfederation`. Complete a separate DKG for its actual participant list and call `deployConfederation`.
5. Configure the common reward token, BTC/USD feed, governance-token metadata/recipient and initial reserve flag in the [environment template](../web3/.env.example). `DeployCustodianDAO` checks those contracts and token/feed decimals before broadcasting.
6. Simulate and deploy `DeployCustodianDAO.s.sol`. Finalize an enforced FROST session for its printed registration hash with the **confederation factory** as target, authorize it, collect the custodian threshold for the same hash, then call `registerCustodianDAO`. Discover addresses through `getCustodianDAO`.
7. Deploy the selected source adapters, converter and optional fee router; register source accounts through the income vault's FROST operations. Configure actual market addresses and conversion/allocation policy before settlement.
8. Deploy/configure the mining pool, SPV relay and MP token with the intended Bitcoin wallet script and verified header bootstrap. Add its MP token through the target pool's approved `addPool` operation. Tokens are minted by the mining pool against its verified reward/deposit flow, not by adding an address to the multi-pool.
9. Deploy the fee system. Authorize the multi-pool using `FederationFeeManager.setFeeCollector` before `setFeeManager`; confederation setup requires both thresholds. Read the configured component addresses back from the factory and fee manager.

`DeployFederation.s.sol` deploys infrastructure, not a completed federation, confederation, off-chain DKG or funded DAO. Every privileged step above uses the exact target's operation-hash helper and its required nonce.

## Actual Pool Fee To DAO

```text
MP deposit / processed withdrawal
  -> pool.accruedFees(mpToken, isWrap)
  -> FROST-approved claimFees (plus custodian threshold for confederations)
  -> recipient holds actual MP tokens
  -> FROST-approved conversion into DAO_REWARD_TOKEN
  -> registered income-vault source / correct fee stream
  -> funded daily settlement
  -> full-day eligible staker claim
```

`claimFees` is limited to accrued fees and updates pool TVL; it cannot sweep arbitrary collateral. Its signature binds token, stream, recipient and amount. A request's withdrawal fee becomes claimable only after processing. A cancelled request leaves it uncollected. Use the prescribed DAO allocation, not automatically 100% of the gross pool fee. An unwrap fee is not the specification's deploy fee; no deploy-fee charge is introduced by this change.

The conversion adapter accepts actual ERC-20 input and a signed minimum output. A selected market/converter must actually support the chosen MP token. It does not guarantee market liquidity or invent a BTC/USD exchange price. When fees are already denominated in the configured reward token, the fee router can perform the wrap/deploy split directly.

## Bitcoin Script And DKG Inputs

Supply raw `scriptPubKey` hex, preserving its exact length. P2WPKH is `0x0014` followed by a 20-byte witness program (22 bytes total); P2TR is `0x5120` followed by a 32-byte output key (34 bytes total). Do not submit an address string, padded bytes32, key alone or script hash in place of the script. Script spendability and address/network selection must be independently checked by the signer policy.

Use `DKGCoordinator.getPublicKeySharePoint` to obtain `(x,y)` for `FROSTCoordinator.registerParticipantPublicKeyShare`. The point must match the secret share imported into the agent. Point validation is not a verification of the off-chain VSS transcript or a replacement for encrypted share exchange, complaints and key agreement. A participant public share can have odd Y even though the normalized group key has even Y.

## Deployment boundary

The legacy deployment order above is retained for existing integrations. The new
V1 local path includes off-chain DKG, the broadcaster, proof settlement, a funded
converter and DAO registration; use the workflow below for new deployments.
External market/feed selection, independent custody and live migration still
require an environment-specific deployment review. The earlier
[strict review](STRICT_REVIEW_2026_10_05.md) covers its original delta, not the new
cryptographic adapter or Bitcoin settlement lifecycle.

New immutable code requires redeployment. Existing deployed contracts do not
acquire these changes from refreshed ABI files.

## V1 lifecycle update — 16 October 2026

The earlier bootstrap order above describes the legacy ABI. New deployments
create each ceremony with `createCeremony` and reserve each factory ID with
its EVM_AUTH session, formation hash and token metadata hash. After the
all-N V1 result completes, the factory checks the formation at adoption and
the metadata at deployment. [Current bootstrap](BOOTSTRAP.md).

The current code connects proof-only issuance, actual S/MP escrow, exact Bitcoin
payment proof and burn, explicit fee conversion/allocation, and funded DAO
settlement. Unwrap fees now accrue at **proof settlement**, not at preparation.
The operations journal and API expose pending approval states and verify deployment
receipts/code/bindings. See the current [acceptance report](ACCEPTANCE_REPORT.md)
for executed tests. Supported Bitcoin mode remains regtest integration; production
mainnet difficulty, pricing/liquidity and recovery policies are not implied by
successful local acceptance.
