// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface ICallbackPoolOrigin {
    function deployedPoolCodehash(address pool) external view returns (bytes32);
    function shareTokenDeployer() external view returns (address);
}

interface ICallbackShareOrigin {
    function deployedTokenCodehash(address token) external view returns (bytes32);
}

interface ICallbackPool {
    struct Withdrawal {
        address requester;
        address mpToken;
        uint256 shareAmount;
        uint256 mpAmount;
        bytes btcScript;
        bytes metadata;
        bool isProcessed;
        uint256 timestamp;
        bytes32 operationHash;
    }
    function getFrostMultisig() external view returns (address);
    function withdrawalEscrow() external view returns (address);
    function shareToken() external view returns (address);
    function getWithdrawal(uint256 id) external view returns (Withdrawal memory);
    function withdrawalPrepared(uint256 id) external view returns (bool);
    function withdrawalCancelled(uint256 id) external view returns (bool);
    function pendingUserPoolShares(address owner, address token) external view returns (uint256);
    function escrowedMP(address token) external view returns (uint256);
}

interface ICallbackShare {
    function multiPool() external view returns (address);
    function confederationPool() external view returns (address);
}

/// @notice Immutable admission policy for genuine non-proxy S/CS callbacks.
/// @dev The existing monetary authority pins this registry and its canonical deployers
/// during bootstrap. No operator or user can append an arbitrary pool. Creation records
/// originate exclusively in the concrete deployers' CREATE paths, including exact runtime
/// hashes (and hence immutable arguments); matching getters alone never grants admission.
contract CallbackPoolRegistry {
    address public immutable escrow;
    address public immutable authorization;
    address public immutable simpleDeployer;
    address public immutable confederationDeployer;

    constructor(address escrow_, address authorization_, address simpleDeployer_, address confederationDeployer_) {
        require(escrow_.code.length > 0 && authorization_.code.length > 0, "callback graph");
        require((simpleDeployer_ == address(0) || simpleDeployer_.code.length > 0)
            && (confederationDeployer_ == address(0) || confederationDeployer_.code.length > 0), "callback origin code");
        require(simpleDeployer_.code.length > 0 || confederationDeployer_.code.length > 0, "callback origins");
        escrow = escrow_;
        authorization = authorization_;
        simpleDeployer = simpleDeployer_;
        confederationDeployer = confederationDeployer_;
    }

    function _created(address deployer, address pool) private view returns (bool) {
        if (deployer == address(0) || pool.code.length == 0) return false;
        bytes32 recorded = ICallbackPoolOrigin(deployer).deployedPoolCodehash(pool);
        return recorded != bytes32(0) && recorded == pool.codehash;
    }

    function isAdmitted(address pool) public view returns (bool) {
        bool simple = _created(simpleDeployer, pool);
        if (!simple && !_created(confederationDeployer, pool)) return false;
        ICallbackPool candidate = ICallbackPool(pool);
        if (candidate.getFrostMultisig() != authorization || candidate.withdrawalEscrow() != escrow) return false;
        address token = candidate.shareToken();
        address origin = ICallbackPoolOrigin(simple ? simpleDeployer : confederationDeployer).shareTokenDeployer();
        bytes32 recorded = ICallbackShareOrigin(origin).deployedTokenCodehash(token);
        if (recorded == bytes32(0) || recorded != token.codehash) return false;
        return simple ? ICallbackShare(token).multiPool() == pool : ICallbackShare(token).confederationPool() == pool;
    }
    function isSimplePool(address pool) external view returns(bool) { return _created(simpleDeployer,pool) && isAdmitted(pool); }

    function validateWithdrawal(
        address pool, uint256 id, address token, address owner, uint256 amount,
        bytes calldata script, bool requirePrepared
    ) external view {
        require(isAdmitted(pool), "callback not admitted");
        ICallbackPool candidate = ICallbackPool(pool);
        ICallbackPool.Withdrawal memory w = candidate.getWithdrawal(id);
        require(
            owner != address(0) && w.requester == owner && w.mpToken == token && w.mpAmount == amount
                && w.shareAmount >= amount && keccak256(w.btcScript) == keccak256(script)
                && !w.isProcessed && !candidate.withdrawalCancelled(id),
            "callback request mismatch"
        );
        require(candidate.pendingUserPoolShares(owner, token) >= w.shareAmount
            && candidate.escrowedMP(token) >= amount, "callback collateral");
        if (requirePrepared) require(candidate.withdrawalPrepared(id), "not approved");
    }
}
