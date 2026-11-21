// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";

/// @dev Unit-test boundary fixture ONLY. Real SPV acceptance is in tests/e2e.
contract UnitEscrowFixture {
    struct Item {
        address pool;
        uint256 id;
        address token;
        uint256 amount;
    }
    address public authorization;
    mapping(bytes32 => Item) public items;

    constructor(address auth) {
        authorization = auth;
    }

    function batches() external view returns (address) {
        return address(this);
    }
    function checkDepositToken(address, address) external pure {}

    function requestKey(address pool, uint256 id) public pure returns (bytes32) {
        return keccak256(abi.encode(pool, id));
    }

    function lock(uint256 id, address token, address, uint256 amount, bytes calldata) external {
        require(items[requestKey(msg.sender, id)].pool == address(0));
        IERC20(token).transferFrom(msg.sender, address(this), amount);
        items[requestKey(msg.sender, id)] = Item(msg.sender, id, token, amount);
    }

    function refund(bytes32 key) external {
        Item memory i = items[key];
        require(i.pool == msg.sender);
        delete items[key];
        IERC20(i.token).transfer(msg.sender, i.amount);
    }

    function deliver(address pool, uint256 id) external {
        bytes32 key = requestKey(pool, id);
        Item memory i = items[key];
        require(i.pool == pool);
        delete items[key];
        PoolMpToken(i.token).burnHeld(i.amount);
        IWithdrawalPool(pool).settleWithdrawal(id);
    }
}
