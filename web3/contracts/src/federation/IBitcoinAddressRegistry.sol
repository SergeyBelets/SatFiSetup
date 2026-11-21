// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface IBitcoinAddressRegistry {
    struct MintClaim {
        bytes32 intentId;
        bytes32 bindingCommitment;
        bytes32 txid;
        uint32 vout;
        address recipient;
        uint64 satoshis;
        uint256 custodyGeneration;
        uint256 derivationGeneration;
        bytes32 validationProvenance;
    }
    function deposits() external view returns (address);
    function authorization() external view returns (address);
    function escrow() external view returns (address);
    function custodyScope() external view returns (bytes32);
    function custodyGeneration() external view returns (uint256);
    function derivationGeneration() external view returns (uint256);
    function requireCustody(uint256 generation) external view;
    function validateMint(address pool, MintClaim calldata claim) external view returns (bytes32 scriptHash);
}
