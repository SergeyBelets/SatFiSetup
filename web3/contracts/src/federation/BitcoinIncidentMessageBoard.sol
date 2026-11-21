// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {BitcoinAddressMessageBoard} from "./BitcoinAddressMessageBoard.sol";

/// @notice Separate ciphertext inbox for incident REPORT/ACK/STATUS delivery.
/// @dev Inherits the bounded, sender-authenticated transport only. A report,
/// ACK or transport role never changes issuance status or grants EVM_AUTH.
contract BitcoinIncidentMessageBoard is BitcoinAddressMessageBoard {
    bytes32 public immutable incidentDomain;
    constructor(address registry_) BitcoinAddressMessageBoard(registry_) {
        incidentDomain=keccak256(abi.encode(keccak256("SATOHSI_INCIDENT_MESSAGES_V1"),
            block.chainid,address(this),registry_,registry.graphId(),address(authorization)));
    }
}
