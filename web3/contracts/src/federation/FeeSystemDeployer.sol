// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./FederationFeeManagerDeployer.sol";
import "./FeeDistributionCalculatorDeployer.sol";
import "./ProjectWalletRegistryDeployer.sol";
import "./FederationNFTModifierDeployer.sol";
import "../tokens/FederationNFTModifier.sol";
import "./IFeeManager.sol";
import "./ControlledDeployer.sol";

/**
 * @title FeeSystemDeployer
 * @notice Deploys the complete fee system (FeeManager + FeeDistributionCalculator + NFTModifier + ProjectWalletRegistry)
 * @dev Separated from main deployers to stay within EVM bytecode limits
 */
contract FeeSystemDeployer is ControlledDeployer {
    FederationFeeManagerDeployer public immutable feeManagerDeployer;
    FeeDistributionCalculatorDeployer public immutable feeDistributorDeployer;
    FederationNFTModifierDeployer public immutable nftModifierDeployer;
    ProjectWalletRegistryDeployer public immutable walletRegistryDeployer;

    event FeeSystemDeployed(
        address feeManager, address feeDistributor, address nftModifier, address walletRegistry, address indexed caller
    );

    error InvalidSubDeployer();

    constructor(
        address _feeManagerDeployer,
        address _feeDistributorDeployer,
        address _nftModifierDeployer,
        address _walletRegistryDeployer
    ) {
        if (
            _feeManagerDeployer == address(0) || _feeDistributorDeployer == address(0)
                || _nftModifierDeployer == address(0) || _walletRegistryDeployer == address(0)
        ) revert InvalidSubDeployer();

        feeManagerDeployer = FederationFeeManagerDeployer(_feeManagerDeployer);
        feeDistributorDeployer = FeeDistributionCalculatorDeployer(_feeDistributorDeployer);
        nftModifierDeployer = FederationNFTModifierDeployer(_nftModifierDeployer);
        walletRegistryDeployer = ProjectWalletRegistryDeployer(_walletRegistryDeployer);
    }

    /**
     * @notice Deploy the complete fee system
     * @param frostMultisig FROST multisig address
     * @param custodianValidator Custodian validator address (address(0) for federations)
     * @param treasury Treasury address for collected fees
     * @param wrapConfig Fee configuration for wrap (deposit) operations
     * @param unwrapConfig Fee configuration for unwrap (withdrawal) operations
     * @param distConfig Distribution configuration for fee splitting
     * @param projectWallets Array of project wallet addresses
     * @param projectShares Array of project wallet share basis points
     * @param projectLabels Array of project wallet labels
     * @param nftName Name for the NFT collection
     * @param nftSymbol Symbol for the NFT collection
     * @return feeManager Deployed FederationFeeManager address
     * @return feeDistributor Deployed FeeDistributionCalculator address
     * @return nftModifier Deployed FederationNFTModifier address
     * @return walletRegistry Deployed ProjectWalletRegistry address
     */
    function deployFeeSystem(
        address frostMultisig,
        address custodianValidator,
        address treasury,
        IFeeManager.FeeConfig memory wrapConfig,
        IFeeManager.FeeConfig memory unwrapConfig,
        FeeDistributionCalculator.DistributionConfig memory distConfig,
        address[] memory projectWallets,
        uint256[] memory projectShares,
        string[] memory projectLabels,
        string memory nftName,
        string memory nftSymbol
    )
        external
        onlyAuthorizedDeployer
        returns (address feeManager, address feeDistributor, address nftModifier, address walletRegistry)
    {
        feeManager = feeManagerDeployer.deployFederationFeeManager(
            frostMultisig, custodianValidator, treasury, wrapConfig, unwrapConfig
        );
        feeDistributor = feeDistributorDeployer.deployFeeDistributionCalculator(frostMultisig, distConfig);
        nftModifier = nftModifierDeployer.deployFederationNFTModifier(nftName, nftSymbol, address(this));

        FederationNFTModifier nft = FederationNFTModifier(nftModifier);
        // Typed manager operations execute mint after FROST (+ custodians).
        // An authorization contract itself cannot forward ERC721 role calls.
        nft.grantRole(nft.DEFAULT_ADMIN_ROLE(), feeManager);
        nft.grantRole(nft.MINTER_ROLE(), feeManager);
        nft.grantRole(nft.USAGE_ROLE(), feeManager);
        nft.renounceRole(nft.MINTER_ROLE(), address(this));
        nft.renounceRole(nft.DEFAULT_ADMIN_ROLE(), address(this));

        walletRegistry = walletRegistryDeployer.deployProjectWalletRegistry(
            frostMultisig, projectWallets, projectShares, projectLabels
        );

        emit FeeSystemDeployed(feeManager, feeDistributor, nftModifier, walletRegistry, msg.sender);
    }
}
