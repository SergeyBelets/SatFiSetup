// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface IBitcoinFeeReserve {
    function batches() external view returns (address);
    function deposits() external view returns (address);
    function onReserve(bytes32 id, bytes32 planHash, bytes32[] calldata inputs) external;
    function checkSigning(bytes32 id) external view;
    function checkTreasuryRequest(bytes32 key, uint256 satoshis) external view;
    function settle(bytes32 batch, bytes calldata raw, uint256 burnedSats, bytes32 blockHash) external;
}
