// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/Pausable.sol";

contract MultiPoolERC20Staking is Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /* ========== CONSTANTS ========== */

    uint256 private constant ACC_PRECISION = 1e18;

    /* ========== ROLES ========== */

    address public guardian;

    event GuardianSet(address indexed guardian);
    event EmergencyPaused(address indexed by);

    /* ========== STRUCTS ========== */

    struct Pool {
        IERC20 stakeToken;
        IERC20 rewardToken;
        uint256 apyBP;        // APY in basis points
        uint256 lockPeriod;   // seconds
        uint256 exitCooldown; // seconds
        uint256 totalStaked;
        bool active;
    }

    struct StakeInfo {
        uint256 amount;
        uint256 rewardDebt; // high precision (1e18)
        uint256 lastUpdate;
        uint256 unlockTime;
    }

    struct ExitRequest {
        uint256 amount;
        uint256 reward; // normal precision
        uint256 readyAt;
    }

    /* ========== STORAGE ========== */

    uint256 public poolCount;
    mapping(uint256 => Pool) public pools;

    mapping(uint256 => mapping(address => StakeInfo)) public stakes;
    mapping(uint256 => mapping(address => ExitRequest)) public exitQueue;

    /* ========== EVENTS ========== */

    event PoolCreated(uint256 indexed poolId);
    event PoolFunded(uint256 indexed poolId, uint256 amount);
    event PoolActiveSet(uint256 indexed poolId, bool active);
    event PoolAPYUpdated(uint256 indexed poolId, uint256 oldAPY, uint256 newAPY);

    event Staked(uint256 indexed poolId, address indexed user, uint256 amount);
    event ExitRequested(
        uint256 indexed poolId,
        address indexed user,
        uint256 amount,
        uint256 reward
    );
    event ExitClaimed(
        uint256 indexed poolId,
        address indexed user,
        uint256 amount,
        uint256 reward
    );
    event EmergencyWithdraw(
        uint256 indexed poolId,
        address indexed user,
        uint256 amount
    );
    event RewardsSettled(uint256 indexed poolId, address indexed user, uint256 rewardDebt);

    /* ========== MODIFIERS ========== */

    modifier onlyGuardianOrOwner() {
        require(
            msg.sender == guardian || msg.sender == owner(),
            "not authorized"
        );
        _;
    }

    modifier validPool(uint256 poolId) {
        require(poolId < poolCount, "invalid pool");
        _;
    }

    /* ========== CONSTRUCTOR ========== */

    constructor() Ownable(msg.sender) {}

    /* ========== ADMIN / GOVERNANCE ========== */

    function setGuardian(address _guardian) external onlyOwner {
        guardian = _guardian;
        emit GuardianSet(_guardian);
    }

    function pause() external onlyGuardianOrOwner {
        _pause();
        emit EmergencyPaused(msg.sender);
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function createPool(
        IERC20 stakeToken,
        IERC20 rewardToken,
        uint256 apyBP,
        uint256 lockPeriod,
        uint256 exitCooldown
    ) external onlyOwner whenNotPaused {
        pools[poolCount] = Pool({
            stakeToken: stakeToken,
            rewardToken: rewardToken,
            apyBP: apyBP,
            lockPeriod: lockPeriod,
            exitCooldown: exitCooldown,
            totalStaked: 0,
            active: true
        });

        emit PoolCreated(poolCount);
        poolCount++;
    }

    function setPoolActive(uint256 poolId, bool active)
        external
        onlyOwner
        whenNotPaused
        validPool(poolId)
    {
        pools[poolId].active = active;
        emit PoolActiveSet(poolId, active);
    }

    /**
     * @notice Update pool APY
     * @dev WARNING: This changes APY for all future reward calculations.
     *      Call settleRewards() for active stakers BEFORE changing APY
     *      to lock in their accumulated rewards at the old rate.
     */
    function setPoolAPY(uint256 poolId, uint256 apyBP)
        external
        onlyOwner
        whenNotPaused
        validPool(poolId)
    {
        uint256 oldAPY = pools[poolId].apyBP;
        pools[poolId].apyBP = apyBP;
        emit PoolAPYUpdated(poolId, oldAPY, apyBP);
    }

    /**
     * @notice Settle (snapshot) rewards for a batch of users before APY change
     * @dev Must be called BEFORE setPoolAPY to avoid retroactive rate change
     * @param poolId The pool to settle
     * @param users Array of user addresses to settle
     */
    function settleRewards(uint256 poolId, address[] calldata users)
        external
        onlyOwner
        validPool(poolId)
    {
        for (uint256 i = 0; i < users.length; i++) {
            _updateRewards(poolId, users[i]);
            emit RewardsSettled(poolId, users[i], stakes[poolId][users[i]].rewardDebt);
        }
    }

    function fundPool(uint256 poolId, uint256 amount)
        external
        onlyOwner
        whenNotPaused
        validPool(poolId)
    {
        pools[poolId].rewardToken.safeTransferFrom(
            msg.sender,
            address(this),
            amount
        );

        emit PoolFunded(poolId, amount);
    }

    /* ========== STAKING ========== */

    function stake(uint256 poolId, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        validPool(poolId)
    {
        Pool storage p = pools[poolId];
        require(p.active, "pool inactive");
        require(amount > 0, "zero amount");

        StakeInfo storage st = stakes[poolId][msg.sender];
        _updateRewards(poolId, msg.sender);

        p.stakeToken.safeTransferFrom(msg.sender, address(this), amount);

        st.amount += amount;

        // Only extend unlock if new deadline is further out
        uint256 newUnlock = block.timestamp + p.lockPeriod;
        if (newUnlock > st.unlockTime) {
            st.unlockTime = newUnlock;
        }

        p.totalStaked += amount;

        emit Staked(poolId, msg.sender, amount);
    }

    function requestExit(uint256 poolId)
        external
        nonReentrant
        whenNotPaused
        validPool(poolId)
    {
        StakeInfo storage st = stakes[poolId][msg.sender];
        Pool storage p = pools[poolId];

        require(st.amount > 0, "nothing staked");
        require(block.timestamp >= st.unlockTime, "locked");
        require(exitQueue[poolId][msg.sender].amount == 0, "already queued");

        _updateRewards(poolId, msg.sender);

        uint256 reward = st.rewardDebt / ACC_PRECISION;

        exitQueue[poolId][msg.sender] = ExitRequest({
            amount: st.amount,
            reward: reward,
            readyAt: block.timestamp + p.exitCooldown
        });

        p.totalStaked -= st.amount;

        emit ExitRequested(poolId, msg.sender, st.amount, reward);

        st.amount = 0;
        st.rewardDebt = 0;
        st.lastUpdate = block.timestamp;
    }

    function claimExit(uint256 poolId)
        external
        nonReentrant
        whenNotPaused
        validPool(poolId)
    {
        ExitRequest memory req = exitQueue[poolId][msg.sender];
        Pool storage p = pools[poolId];

        require(req.amount > 0, "no exit");
        require(block.timestamp >= req.readyAt, "cooldown");

        delete exitQueue[poolId][msg.sender];

        p.stakeToken.safeTransfer(msg.sender, req.amount);
        if (req.reward > 0) {
            p.rewardToken.safeTransfer(msg.sender, req.reward);
        }

        emit ExitClaimed(poolId, msg.sender, req.amount, req.reward);
    }

    /* ========== EMERGENCY ========== */

    function emergencyWithdraw(uint256 poolId)
        external
        nonReentrant
        validPool(poolId)
    {
        StakeInfo storage st = stakes[poolId][msg.sender];
        require(st.amount > 0, "nothing staked");
        require(exitQueue[poolId][msg.sender].amount == 0, "exit pending");

        uint256 amount = st.amount;
        pools[poolId].totalStaked -= amount;

        st.amount = 0;
        st.rewardDebt = 0;
        st.lastUpdate = 0;
        st.unlockTime = 0;

        pools[poolId].stakeToken.safeTransfer(msg.sender, amount);

        emit EmergencyWithdraw(poolId, msg.sender, amount);
    }

    /* ========== REWARDS ========== */

    function _updateRewards(uint256 poolId, address user) internal {
        StakeInfo storage st = stakes[poolId][user];
        if (st.amount == 0) {
            st.lastUpdate = block.timestamp;
            return;
        }

        Pool storage p = pools[poolId];
        uint256 delta = block.timestamp - st.lastUpdate;

        uint256 rewardAccrued =
            st.amount
            * p.apyBP
            * delta
            * ACC_PRECISION
            / (365 days * 10000);

        st.rewardDebt += rewardAccrued;
        st.lastUpdate = block.timestamp;
    }

    /* ========== VIEW ========== */

    function pendingRewards(uint256 poolId, address user)
        external
        view
        returns (uint256)
    {
        StakeInfo memory st = stakes[poolId][user];
        if (st.amount == 0) return 0;

        Pool memory p = pools[poolId];
        uint256 delta = block.timestamp - st.lastUpdate;

        uint256 rewardAccrued =
            st.amount
            * p.apyBP
            * delta
            * ACC_PRECISION
            / (365 days * 10000);

        return (st.rewardDebt + rewardAccrued) / ACC_PRECISION;
    }
}
