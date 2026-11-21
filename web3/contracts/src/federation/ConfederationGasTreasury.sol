// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "@openzeppelin/contracts/utils/math/Math.sol";
import "@openzeppelin/contracts/utils/Address.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice Immutable native-currency distribution, independent of Bitcoin custody.
/// @dev Cumulative pull accounting follows OpenZeppelin PaymentSplitter v4.9.6
/// (MIT); uses the repository's OZ 5.4 Math.mulDiv and ReentrancyGuard. No audit
/// assurance from the historical reference is implied for this implementation.
contract ConfederationGasTreasury is ReentrancyGuard {
    uint256 public constant MAX_PAYEES = 32;
    address public immutable paymentRegistry;
    address public immutable authorization;
    bytes32 public immutable confederationScope;
    uint64 public immutable epoch;
    bytes32 public immutable rosterHash;
    uint256 public immutable totalWeight;
    uint256 public totalReleased;
    address[] private payees;
    mapping(address => uint256) public weight;
    mapping(address => uint256) public released;

    event NativeReceived(address indexed sender, uint256 amount);
    event NativeClaimed(address indexed payee, address indexed caller, uint256 amount);

    constructor(address registry_, address authorization_, bytes32 scope_, uint64 epoch_,
        address[] memory payees_, uint256[] memory weights_)
    {
        require(registry_.code.length != 0 && authorization_.code.length != 0, "binding");
        require(scope_ != 0 && epoch_ != 0, "scope/epoch");
        require(payees_.length != 0 && payees_.length <= MAX_PAYEES
            && payees_.length == weights_.length, "roster length");
        uint256 sum;
        for (uint256 i; i < payees_.length; ++i) {
            address payee = payees_[i];
            require(payee != address(0) && payee != address(this), "payee");
            require(weights_[i] != 0 && weight[payee] == 0, "weight/duplicate");
            weight[payee] = weights_[i];
            sum += weights_[i];
        }
        paymentRegistry = registry_;
        authorization = authorization_;
        confederationScope = scope_;
        epoch = epoch_;
        rosterHash = keccak256(abi.encode(payees_, weights_));
        payees = payees_;
        totalWeight = sum;
    }

    // Donations, including transfers to an old epoch, never mark a job paid.
    receive() external payable { emit NativeReceived(msg.sender, msg.value); }

    function getPayees() external view returns (address[] memory) { return payees; }

    function receivedTotal() public view returns (uint256) {
        return address(this).balance + totalReleased;
    }

    function claimable(address payee) public view returns (uint256) {
        if (weight[payee] == 0) return 0;
        return Math.mulDiv(receivedTotal(), weight[payee], totalWeight) - released[payee];
    }

    function claim() external nonReentrant returns (uint256) { return _claim(msg.sender); }

    /// @notice A funded caller can deliver native gas to a zero-balance payee.
    function claimFor(address payee) external nonReentrant returns (uint256) { return _claim(payee); }

    function _claim(address payee) private returns (uint256 amount) {
        amount = claimable(payee);
        if (amount == 0) return 0;
        released[payee] += amount;
        totalReleased += amount;
        Address.sendValue(payable(payee), amount);
        emit NativeClaimed(payee, msg.sender, amount);
    }
}
