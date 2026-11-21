"""Run after forge build. --check fails on client/build drift."""
import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--check',action='store_true')
p.add_argument('--zk',action='store_true',help='Also require the isolated mainnet ZK state/verifier build')
a=p.parse_args()
root=Path(__file__).resolve().parents[1]
# The owner withdrawal client independently checks the canonical NFT discount.
for name in ('BitcoinCustodyTransfer','FederationNFTModifier','RecipientWalletV1','RecipientPolicy','BitcoinIncidentMessageBoard','FundedFeeSale'):
    source = root/'web3/contracts/out'/f'{name}.sol'/f'{name}.json'
    expected = json.dumps(json.loads(source.read_text())['abi'], indent=2)+'\n'
    output = root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text() != expected: raise SystemExit('ABI_DRIFT: '+name)
    else: output.write_text(expected)
for name in ('MainnetSPVGuard','Testnet4HeaderRelay','MainnetHeaderRelay','BitcoinSigningBoard','DAOTestUSD','DAOMockFeed','DKGOnchainBoard','FROSTCoordinator','DKGCoordinator','DKGAuthorization','BitcoinBatchRegistry','SimpleMultiPool','ConfederationMultiPool','FederationFactory','ConfederationFactory','BitcoinWithdrawalEscrow','CallbackPoolRegistry','BitcoinMintAdmission','ConfederationGasTreasury','SPVServicePaymentRegistry','SimpleMultiPoolDeployer','MultiPoolDeployer','MultiPoolShareTokenDeployer','ConfederationTokenDeployer','BitcoinDepositRegistry','MiningPoolV1','RegtestHeaderRelay','PoolFeePipeline','FundedFeeConverter','CustodianDAOFeeRouter','CustodianDAOIncomeVault','CustodianDAODailySettlement','CustodianDAOStaking','CustodianDAORateController','CustodianDAODecisionBoard','GovernanceRegistry','FederationGovernance','FederationFeeManager','PoolMpToken','MultiPoolShareToken','ConfederationToken'):
    artifact=json.loads((root/'web3/contracts/out'/f'{name}.sol'/f'{name}.json').read_text())
    expected=json.dumps(artifact['abi'],indent=2)+'\n'
    output=root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text()!=expected:raise SystemExit('ABI_DRIFT: '+name)
    else:output.write_text(expected)

# Common ULR staking and the opt-in treasury graph have independent deployment lifecycles.
for name in ('ULRStartupRewards', 'ULRStaking', 'ULRCustodianSnapshots', 'ULRConfederationTreasury', 'ULRConfederationVoting', 'CustodianValidator'):
    source = root/'web3/contracts/out'/f'{name}.sol'/f'{name}.json'
    expected = json.dumps(json.loads(source.read_text())['abi'], indent=2)+'\n'
    output = root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text() != expected: raise SystemExit('ABI_DRIFT: '+name)
    else: output.write_text(expected)

# An ABI does not activate address mode or qualify the candidate verifier.
for name in ('BitcoinKeyFamilyRegistry', 'BitcoinAddressRegistry', 'BitcoinDepositDerivation', 'BitcoinAddressMessageBoard', 'BitcoinFeeReserve', 'BitcoinFeeReserveUser', 'BitcoinFeeFundingVerifier', 'BitcoinFeePlanVerifier', 'WithdrawalFeeCredits', 'WithdrawalFeePayments', 'WithdrawalFeeFunding', 'WithdrawalFeePlanVerifier', 'BitcoinWithdrawalFeeDerivation'):
    source = root/'web3/contracts/out'/f'{name}.sol'/f'{name}.json'
    expected = json.dumps(json.loads(source.read_text())['abi'], indent=2)+'\n'
    output = root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text() != expected: raise SystemExit('ABI_DRIFT: '+name)
    else: output.write_text(expected)

# Read-only payment view is built with the ordinary contracts.
for name in (('ZkSPVServiceStateView', 'SPVMessageBoard') if a.zk else ()):
    source = root/'web3/contracts/out'/f'{name}.sol'/f'{name}.json'
    expected = json.dumps(json.loads(source.read_text())['abi'], indent=2)+'\n'
    output = root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text() != expected: raise SystemExit('ABI_DRIFT: '+name)
    else: output.write_text(expected)

# These use the isolated, pinned state/verifier Foundry build.
for name in (('ZkBitcoinStateV2', 'ZkSpvAdapter', 'RiscZeroGroth16Verifier', 'ZkBitcoinTestnet4StateV2', 'ZkTestnet4SpvAdapter', 'ZkTestnet4SPVServiceStateView') if a.zk else ()):
    source = root/'rust/zkspv/state-adapter/out'/f'{name}.sol'/f'{name}.json'
    artifact = json.loads(source.read_text())
    expected = json.dumps(artifact['abi'], indent=2)+'\n'
    output = root/'signer-agent/contracts'/f'{name}.json'
    if a.check:
        if not output.exists() or output.read_text() != expected: raise SystemExit('ABI_DRIFT: '+name)
    else: output.write_text(expected)
