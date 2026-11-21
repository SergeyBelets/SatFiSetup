// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "forge-std/Test.sol";
import "./UnitEscrowFixture.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "../contracts/src/demo/SimpleMultiPool.sol";
import "../contracts/src/federation/ConfederationMultiPool.sol";
import "../contracts/src/federation/DKGCoordinator.sol";
import "../contracts/src/vendor/cryptography/Secp256k1.sol";

interface RemediationPool {
    function deposit(address, uint256) external returns (uint256);
    function setFeeManager(address, bytes32) external;
    function getSetFeeManagerOperationHash(address) external view returns (bytes32);
    function addPool(address, string calldata, bytes32) external;
    function getAddPoolOperationHash(address, string calldata) external view returns (bytes32);
    function accruedFees(address, bool) external view returns (uint256);
    function pendingUnreportedFees(address, bool) external view returns (uint256);
    function syncFeeReports(address, bool) external;
    function claimFees(address, bool, address, uint256, bytes32) external;
    function getClaimFeesOperationHash(address, bool, address, uint256) external view returns (bytes32);
    function operationNonce() external view returns (uint256);
    function getTotalValueLocked() external view returns (uint256);
    function requestWithdrawal(address, uint256, bytes calldata) external returns (uint256);
    function getProcessWithdrawalOperationHash(uint256) external view returns (bytes32);
}

contract RemediationAuth {
    mapping(bytes32 => bool) public consumed;
    bool public approved = true;
    function setApproved(bool value) external { approved = value; }
    function isOperationApproved(bytes32 hash, address) external view returns (bool) { return approved && !consumed[hash]; }
    function consumeOperation(bytes32 hash) external returns (bool) { require(approved && !consumed[hash], "Unauthorized"); consumed[hash] = true; return true; }
    function hashOperation(address target, bytes4 selector, bytes32 data, uint256 nonce) external pure returns (bytes32) {
        return keccak256(abi.encode(target, selector, data, nonce));
    }
}

contract RemediationValidator {
    bool public approved = true;
    function setApproved(bool value) external { approved = value; }
    function meetsMinimumCustodians(bytes32) external view returns (bool) { return approved; }
}

contract RemediationManager {
    address public auth;
    address public validator;
    mapping(address => bool) public authorizedFeeCollectors;
    mapping(address => mapping(bool => uint256)) public recorded;
    bool public failing;
    bool public exhaustGas;
    constructor(address a, address v) { auth = a; validator = v; }
    function getFeeSecurityConfig() external view returns (address, address) { return (auth, validator); }
    function setAuthorized(address pool, bool value) external { authorizedFeeCollectors[pool] = value; }
    function setFailing(bool value) external { failing = value; }
    function setExhaustGas(bool value) external { exhaustGas = value; }
    function calculateFee(bool, uint256 amount) external pure returns (uint256) { return amount / 10; }
    function recordFeeCollectedForToken(address token, uint256 amount, bool wrap) external {
        require(authorizedFeeCollectors[msg.sender], "Unauthorized collector");
        require(!failing, "Record unavailable");
        if (exhaustGas) { assembly { invalid() } }
        recorded[token][wrap] += amount;
    }
}

contract RemediationToken is ERC20 {
    // 0: exact, 1: recipient tax, 2: extra sender debit, 3: false return.
    uint256 public mode;
    constructor() ERC20("MP", "MP") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function burnHeld(uint256 amount) external { _burn(msg.sender,amount); }
    function confiscate(address from, uint256 amount) external { _burn(from, amount); }
    function setMode(uint256 value) external { mode = value; }
    function transfer(address to, uint256 amount) public override returns (bool) {
        if (mode == 3) return false;
        return super.transfer(to, amount);
    }
    function transferFrom(address from, address to, uint256 amount) public override returns (bool) {
        if (mode == 3) return false;
        return super.transferFrom(from, to, amount);
    }
    function _update(address from, address to, uint256 amount) internal override {
        if (from != address(0) && to != address(0) && mode == 1) {
            super._update(from, to, amount - 1);
            super._update(from, address(0), 1);
        } else {
            super._update(from, to, amount);
            if (from != address(0) && to != address(0) && mode == 2) super._update(from, address(0), 1);
        }
    }
}

contract PoolDKGRemediationTest is Test {
    function _pool(bool conf) internal returns (RemediationPool pool, RemediationToken token, RemediationManager manager, RemediationAuth auth) {
        auth = new RemediationAuth();
        RemediationValidator validator = new RemediationValidator();
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        if (conf) {
            ConfederationToken share = new ConfederationToken("S", "S", predicted);
            pool = RemediationPool(address(new ConfederationMultiPool("C", address(auth), address(validator), address(share))));
        } else {
            MultiPoolShareToken share = new MultiPoolShareToken("S", "S", predicted);
            pool = RemediationPool(address(new SimpleMultiPool("S", address(auth), address(share))));
        }
        UnitEscrowFixture escrow=new UnitEscrowFixture(address(auth));
        if(conf) {
            ConfederationMultiPool p=ConfederationMultiPool(address(pool));
            p.setWithdrawalEscrow(address(escrow),p.getSetWithdrawalEscrowOperationHash(address(escrow)));
        } else {
            SimpleMultiPool p=SimpleMultiPool(address(pool));
            p.setWithdrawalEscrow(address(escrow),p.getSetWithdrawalEscrowOperationHash(address(escrow)));
        }
        token = new RemediationToken();
        pool.addPool(address(token), "MP", pool.getAddPoolOperationHash(address(token), "MP"));
        manager = new RemediationManager(address(auth), conf ? address(validator) : address(0));
        manager.setAuthorized(address(pool), true);
        pool.setFeeManager(address(manager), pool.getSetFeeManagerOperationHash(address(manager)));
        token.mint(address(this), 10000);
        token.approve(address(pool), type(uint256).max);
    }

    function _recovery(bool conf) internal {
        (RemediationPool pool, RemediationToken token, RemediationManager manager,) = _pool(conf);
        token.mint(address(this), 10000 * 1e10);
        manager.setFailing(true);
        assertEq(pool.deposit(address(token), 1000 * 1e10), 900 * 1e10);
        manager.setAuthorized(address(pool), false);
        assertEq(pool.deposit(address(token), 1000 * 1e10), 900 * 1e10);
        assertEq(pool.accruedFees(address(token), true), 200 * 1e10);
        assertEq(pool.pendingUnreportedFees(address(token), true), 200 * 1e10);
        manager.setFailing(false);
        vm.prank(address(0x123)); pool.syncFeeReports(address(token), true);
        assertEq(manager.recorded(address(token), true), 0);
        assertEq(pool.pendingUnreportedFees(address(token), true), 200 * 1e10);
        manager.setAuthorized(address(pool), true);
        vm.prank(address(0x123)); pool.syncFeeReports(address(token), true);
        pool.syncFeeReports(address(token), true);
        assertEq(manager.recorded(address(token), true), 200 * 1e10);
        assertEq(pool.pendingUnreportedFees(address(token), true), 0);
        assertEq(pool.accruedFees(address(token), true), 200 * 1e10);
        pool.deposit(address(token), 100 * 1e10);
        assertEq(manager.recorded(address(token), true), 210 * 1e10);
        manager.setAuthorized(address(pool), false);
        uint256 id = pool.requestWithdrawal(address(token), 100 * 1e10, hex"51");
        bytes32 hash = pool.getProcessWithdrawalOperationHash(id);
        if (conf) ConfederationMultiPool(address(pool)).processWithdrawal(id);
        else SimpleMultiPool(address(pool)).processWithdrawal(id, hash);
        UnitEscrowFixture(address(SimpleMultiPool(address(pool)).withdrawalEscrow())).deliver(address(pool),id);
        assertEq(pool.accruedFees(address(token), false), 10 * 1e10);
        assertEq(pool.pendingUnreportedFees(address(token), false), 10 * 1e10);
        manager.setAuthorized(address(pool), true);
        pool.syncFeeReports(address(token), false);
        assertEq(manager.recorded(address(token), false), 10 * 1e10);
    }
    function test_SimpleReportRecoveryAndWithdrawalLiveness() public { _recovery(false); }
    function test_ConfederationReportRecoveryAndWithdrawalLiveness() public { _recovery(true); }

    function test_BothPoolsReportGasExhaustionAndClaimsBeforeRetry() public {
        for (uint256 i; i < 2; i++) {
            (RemediationPool pool, RemediationToken token, RemediationManager manager,) = _pool(i == 1);
            token.mint(address(this), 10000 * 1e10);
            manager.setExhaustGas(true);
            pool.deposit(address(token), 1000 * 1e10);
            assertEq(pool.pendingUnreportedFees(address(token), true), 100 * 1e10);
            uint256 id = pool.requestWithdrawal(address(token), 100 * 1e10, hex"51");
            bytes32 hash = pool.getProcessWithdrawalOperationHash(id);
            if (i == 1) ConfederationMultiPool(address(pool)).processWithdrawal(id);
            else SimpleMultiPool(address(pool)).processWithdrawal(id, hash);
        UnitEscrowFixture(address(SimpleMultiPool(address(pool)).withdrawalEscrow())).deliver(address(pool),id);
            assertEq(pool.pendingUnreportedFees(address(token), false), 10 * 1e10);
            hash = pool.getClaimFeesOperationHash(address(token), true, address(0x456), 100 * 1e10);
            pool.claimFees(address(token), true, address(0x456), 100 * 1e10, hash);
            assertEq(pool.accruedFees(address(token), true), 0);
            assertEq(pool.pendingUnreportedFees(address(token), true), 100 * 1e10);
            manager.setExhaustGas(false);
            pool.syncFeeReports(address(token), true);
            pool.syncFeeReports(address(token), false);
            pool.syncFeeReports(address(token), true);
            assertEq(manager.recorded(address(token), true), 100 * 1e10);
            assertEq(manager.recorded(address(token), false), 10 * 1e10);
            assertEq(pool.pendingUnreportedFees(address(token), true), 0);
        }
    }

    function _transfers(bool conf) internal {
        (RemediationPool pool, RemediationToken token, RemediationManager manager, RemediationAuth auth) = _pool(conf);
        for (uint256 mode = 1; mode <= 3; mode += 2) {
            token.setMode(mode);
            vm.expectRevert(); pool.deposit(address(token), 1000);
            assertEq(pool.getTotalValueLocked(), 0);
            assertEq(pool.accruedFees(address(token), true), 0);
            assertEq(token.balanceOf(address(pool)), 0);
            assertEq(token.balanceOf(address(this)), 10000);
            assertEq(manager.recorded(address(token), true), 0);
        }
        token.setMode(0);
        pool.deposit(address(token), 1000);
        address recipient = address(0x456);
        bytes32 hash = pool.getClaimFeesOperationHash(address(token), true, recipient, 100);
        uint256 nonce = pool.operationNonce();
        for (uint256 mode = 1; mode <= 3; mode++) {
            token.setMode(mode);
            vm.expectRevert(); pool.claimFees(address(token), true, recipient, 100, hash);
            assertEq(pool.accruedFees(address(token), true), 100);
            assertEq(pool.getTotalValueLocked(), 1000);
            assertEq(pool.operationNonce(), nonce);
            assertFalse(auth.consumed(hash));
            assertEq(token.balanceOf(recipient), 0);
            assertEq(token.balanceOf(address(pool)), 1000);
        }
        token.setMode(0);
        // The token balance exceeds the fee amount but does not cover user backing.
        token.confiscate(address(pool), 200);
        vm.expectRevert("Insufficient tracked backing"); pool.claimFees(address(token), true, recipient, 100, hash);
        assertFalse(auth.consumed(hash));
        token.mint(address(pool), 200);
        auth.setApproved(false);
        vm.expectRevert(); pool.claimFees(address(token), true, recipient, 100, hash);
        auth.setApproved(true);
        pool.claimFees(address(token), true, recipient, 100, hash);
        assertEq(pool.getTotalValueLocked(), 900);
        assertEq(token.balanceOf(address(pool)), 900);
        assertEq(token.balanceOf(recipient), 100);
    }
    function test_SimpleExactTransfersAndBacking() public { _transfers(false); }
    function test_ConfederationExactTransfersAndBacking() public { _transfers(true); }

    function test_BothPoolsRequireCollectorAuthorizationAtAdmission() public {
        for (uint256 i; i < 2; i++) {
            RemediationAuth auth = new RemediationAuth();
            RemediationValidator validator = new RemediationValidator();
            address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
            RemediationPool fresh;
            if (i == 0) {
                MultiPoolShareToken share = new MultiPoolShareToken("S", "S", predicted);
                fresh = RemediationPool(address(new SimpleMultiPool("S", address(auth), address(share))));
            } else {
                ConfederationToken share = new ConfederationToken("S", "S", predicted);
                fresh = RemediationPool(address(new ConfederationMultiPool("C", address(auth), address(validator), address(share))));
            }
            RemediationManager unauthorized = new RemediationManager(address(auth), i == 0 ? address(0) : address(validator));
            bytes32 hash = fresh.getSetFeeManagerOperationHash(address(unauthorized));
            vm.expectRevert("Fee collector not authorized");
            fresh.setFeeManager(address(unauthorized), hash);
            assertFalse(auth.consumed(hash));
            assertEq(fresh.operationNonce(), 0);
        }
    }

    function _dkg() internal returns (DKGCoordinator dkg, bytes32 session, address[] memory participants) {
        dkg = new DKGCoordinator();
        participants = new address[](3);
        participants[0] = address(1); participants[1] = address(2); participants[2] = address(3);
        session = dkg.initiateDKG(participants, 2, "encoding");
        bytes32[] memory commitments = new bytes32[](2);
        commitments[0] = bytes32(uint256(1)); commitments[1] = bytes32(uint256(2));
        for (uint256 i; i < 3; i++) { vm.prank(participants[i]); dkg.submitVSSCommitments(session, commitments); }
        for (uint256 i; i < 3; i++) for (uint256 j; j < 3; j++) {
            vm.prank(participants[i]); dkg.submitShareHash(session, participants[j], keccak256(abi.encode(i,j)));
        }
        for (uint256 i; i < 3; i++) {
            vm.prank(participants[i]); dkg.submitPublicKeyShare(session, abi.encodePacked(bytes1(0x02), bytes32(Secp256k1.GX)));
        }
    }
    function test_DKGMixedEncodingApprovalsAndCompletion() public {
        for (uint256 encoding; encoding < 3; encoding++) {
            (DKGCoordinator dkg, bytes32 session, address[] memory participants) = _dkg();
            bytes memory x = abi.encodePacked(bytes32(Secp256k1.GX));
            bytes memory compressed = abi.encodePacked(bytes1(0x02), bytes32(Secp256k1.GX));
            bytes memory full = abi.encodePacked(bytes32(Secp256k1.GX), bytes32(Secp256k1.GY));
            vm.prank(participants[0]); dkg.approveGroupPublicKey(session, x);
            vm.expectRevert(DKGCoordinator.AlreadySubmitted.selector);
            vm.prank(participants[0]); dkg.approveGroupPublicKey(session, compressed);
            vm.expectRevert(DKGCoordinator.AlreadySubmitted.selector);
            vm.prank(participants[0]); dkg.approveGroupPublicKey(session, full);
            vm.prank(participants[1]); dkg.approveGroupPublicKey(session, compressed);
            assertEq(dkg.getGroupPublicKeyApprovalCount(session, keccak256(x)), 2);
            dkg.completeDKG(session, encoding == 0 ? x : encoding == 1 ? compressed : full);
            assertEq(dkg.getGroupPublicKey(session), x);
        }
    }
}
