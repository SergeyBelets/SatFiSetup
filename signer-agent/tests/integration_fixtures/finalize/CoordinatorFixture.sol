// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

// Isolated state-machine fixture. The signing protocol is outside this test.
contract CoordinatorFixture {
    bytes signature;
    bytes32 public immutable operationHash;
    address public immutable origin;

    constructor(bytes32 messageHash) {
        operationHash = messageHash;
        origin = msg.sender;
    }

    event SessionFinalized(uint256 indexed sessionId, bytes aggregatedSignature, bytes32 messageHash);

    function finalizeSession(uint256 id, bytes calldata sig, bytes32 messageHash) external {
        require(id == 42 && messageHash == operationHash && sig.length == 64);
        require(signature.length == 0);
        signature = sig;
        emit SessionFinalized(id, sig, messageHash);
    }

    function getResult(uint256 id) external view returns (bytes memory sig, bytes32 messageHash) {
        require(id == 42 && signature.length != 0);
        return (signature, operationHash);
    }

    function getSession(uint256 id) external view returns (
        uint256, address, bytes memory, bytes32, bool, uint256, uint256, uint64,
        bool, address, uint256, uint256, uint256, uint256, uint256, address,
        uint256, uint16, bytes32, uint256
    ) {
        require(id == 42);
        uint256 state = signature.length == 0 ? 1 : 2;
        return (id, origin, new bytes(32), operationHash, true, 2, 2, 123456,
                true, address(0), state, 2, 2, 0, 1, origin, 0, 0, bytes32(0), 2);
    }
}
