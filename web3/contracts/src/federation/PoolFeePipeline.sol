// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./FundedFeeConverter.sol";
import "./CustodianDAOFeeRouter.sol";

interface IFeeClaimPool {
    function claimFees(address token, bool isWrap, address recipient, uint256 amount, bytes32 op) external;
    function getFrostMultisig() external view returns (address);
}

/// @notice Atomic actual MP claim -> funded conversion -> explicit fee stream -> DAO allocation.
contract PoolFeePipeline is ReentrancyGuard {
    using SafeERC20 for IERC20;
    FundedFeeConverter public immutable converter;
    CustodianDAOFeeRouter public immutable router;
    FROSTAuthorization public immutable authorization;
    uint256 public nonce;

    constructor(address converter_, address router_) {
        converter = FundedFeeConverter(converter_);
        router = CustodianDAOFeeRouter(router_);
        require(address(converter.rewardToken()) == address(router.rewardToken()), "reward token");
        authorization = router.authorization();
    }

    function operationHash(
        address pool,
        CustodianDAOFeeRouter.FeeStream stream,
        uint256 amount,
        uint256 day,
        uint256 minimum,
        uint256 deadline
    ) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this),
            this.route.selector,
            keccak256(abi.encode(pool, stream, amount, day, minimum, deadline)),
            nonce
        );
    }

    function route(
        address pool,
        CustodianDAOFeeRouter.FeeStream stream,
        uint256 amount,
        uint256 day,
        uint256 minimum,
        uint256 deadline,
        bytes32 claimOp,
        bytes32 op
    ) external nonReentrant {
        require(
            stream != CustodianDAOFeeRouter.FeeStream.Deploy
                && IFeeClaimPool(pool).getFrostMultisig() == address(authorization),
            "pool or stream"
        );
        require(op == operationHash(pool, stream, amount, day, minimum, deadline), "operation");
        authorization.consumeOperation(op);
        nonce++;
        IERC20 input = converter.inputToken();
        uint256 beforeBalance = input.balanceOf(address(this));
        IFeeClaimPool(pool)
            .claimFees(address(input), stream == CustodianDAOFeeRouter.FeeStream.Wrap, address(this), amount, claimOp);
        require(input.balanceOf(address(this)) == beforeBalance + amount, "claim transfer");
        input.forceApprove(address(converter), amount);
        uint256 out = converter.convert(amount, minimum, deadline);
        input.forceApprove(address(converter), 0);
        IERC20 reward = converter.rewardToken();
        reward.forceApprove(address(router), out);
        router.depositFeeV1(day, stream, out);
        reward.forceApprove(address(router), 0);
    }
}
