// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

// Ordinary owner-controlled availability switch for local receipt tests.
// No SPV, proof, DKG or threshold-signature behavior is represented here.
contract GasCallerAvailabilityFixture {
    address public immutable controller = msg.sender;
    bool public available = true;

    function setAvailable(bool value) external {
        require(msg.sender == controller, "controller");
        available = value;
    }

    receive() external payable { require(available, "temporarily unavailable"); }
}
