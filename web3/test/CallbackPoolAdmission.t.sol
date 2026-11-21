// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./WithdrawalRounding.t.sol";
import "./fixtures/LegacyCallbackEscrow.sol" as Legacy;

interface ICallbackLock {
    function lock(uint256, address, address, uint256, bytes calldata) external;
}

/// @dev Returns every self-asserted getter the old escrow trusted, then refuses settlement.
contract CounterfeitCallbackPool {
    address public immutable withdrawalEscrow;
    address public immutable getFrostMultisig;
    constructor(address escrow, address auth) { withdrawalEscrow = escrow; getFrostMultisig = auth; }
    function withdrawalPrepared(uint256) external pure returns (bool) { return true; }
    function lock(address token, address owner, uint256 amount, bytes calldata script) external {
        IERC20(token).approve(withdrawalEscrow, amount);
        ICallbackLock(withdrawalEscrow).lock(0, token, owner, amount, script);
    }
    function settleWithdrawal(uint256) external pure { revert("malicious callback"); }
}

contract CallbackPoolAdmissionTest is WithdrawalRoundingBase {
    function _confederated() internal pure override returns (bool) { return false; }

    function testBaselineCounterfeitPoolLocksRealMPAndRevertsWholeSettlement() public {
        Legacy.LegacyCallbackEscrow old = new Legacy.LegacyCallbackEscrow(address(batches), address(proof), 6, 294, 330);
        ValueFlowDeposits oldDeposits = new ValueFlowDeposits(address(old), address(mining));
        bytes32 op = old.getSetDepositRegistryOperationHash(address(oldDeposits));
        _approve(op, address(old));
        old.setDepositRegistry(address(oldDeposits), op);
        CounterfeitCallbackPool fake = new CounterfeitCallbackPool(address(old), address(authorization));
        mining.mint(address(fake), 1000 * SAT);
        mining.mint(address(this), 1000 * SAT);
        mp.approve(address(old), 1000 * SAT);
        old.requestDirect(1, address(mp), 1000 * SAT, p2wpkh);
        fake.lock(address(mp), address(this), 1000 * SAT, p2wpkh);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = old.requestKey(address(this), 1);
        keys[1] = old.requestKey(address(fake), 0);
        bytes memory raw = _raw(2000);
        bytes32 txid = sha256(abi.encodePacked(sha256(raw)));
        bytes32 batch = bytes32(uint256(42));
        bytes32 input = keccak256(abi.encode(bytes32(uint256(9) << 248), uint32(0)));
        batches.configure(batch, input, BitcoinBatchRegistry.State.Reserved);
        op = old.getBindOperationHash(batch, txid, keys);
        _approve(op, address(old));
        old.bind(batch, txid, keys, op);
        batches.setState(batch, BitcoinBatchRegistry.State.Signing);
        uint256 supply = mp.totalSupply();
        vm.expectRevert("malicious callback");
        old.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertFalse(old.payment(batch).settled);
        assertEq(mp.balanceOf(address(old)), 2000 * SAT);
        assertEq(mp.totalSupply(), supply);
        assertEq(oldDeposits.burnedSats(), 0);
        assertFalse(old.spentInputs(input));
    }

    function testCounterfeitGettersAndRealMPRejectedBeforeLockOrBind() public {
        CounterfeitCallbackPool fake = new CounterfeitCallbackPool(address(escrow), address(authorization));
        mining.mint(address(fake), 1000 * SAT);
        assertFalse(escrow.isCallbackPoolAdmitted(address(fake)));
        vm.expectRevert("callback not admitted");
        fake.lock(address(mp), address(this), 1000 * SAT, p2wpkh);
        assertEq(mp.balanceOf(address(fake)), 1000 * SAT);
        assertEq(mp.balanceOf(address(escrow)), 0);
    }

    function testRealPoolWithWrongCreationOriginRejected() public {
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        MultiPoolShareToken share = new MultiPoolShareToken("Unadmitted", "U", predicted);
        SimpleMultiPool unadmitted = new SimpleMultiPool("same-code", address(authorization), address(share));
        bytes32 op = unadmitted.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(op, address(unadmitted));
        unadmitted.setWithdrawalEscrow(address(escrow), op);
        assertFalse(escrow.isCallbackPoolAdmitted(address(unadmitted)));
    }

    function testRegistryCannotBeReboundAfterAdmission() public {
        assertTrue(escrow.isCallbackPoolAdmitted(address(pool)));
        address registry = address(escrow.callbackPoolRegistry());
        bytes32 op = escrow.getSetCallbackPoolRegistryOperationHash(registry);
        _approve(op, address(escrow));
        vm.expectRevert("callback registry fixed");
        escrow.setCallbackPoolRegistry(registry, op);
    }

    function testCanonicalPoolCannotLockFabricatedRequestFields() public {
        pool.requestWithdrawal(address(mp), 1000 * SAT, p2wpkh);
        vm.prank(address(pool));
        vm.expectRevert("callback request mismatch");
        escrow.lock(0, address(mp), address(0xBAD), 1000 * SAT, p2wpkh);
    }

    function testCanonicalPoolRuntimeChangeRejected() public {
        assertTrue(escrow.isCallbackPoolAdmitted(address(pool)));
        vm.etch(address(pool), hex"00");
        assertFalse(escrow.isCallbackPoolAdmitted(address(pool)));
    }

    function testCanonicalShareRuntimeChangeRejected() public {
        assertTrue(escrow.isCallbackPoolAdmitted(address(pool)));
        vm.etch(address(shares), hex"00");
        assertFalse(escrow.isCallbackPoolAdmitted(address(pool)));
    }

    function testUnpreparedCanonicalRequestCannotBind() public {
        uint256 id = pool.requestWithdrawal(address(mp), 1000 * SAT, p2wpkh);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), id);
        bytes32 batch = bytes32(uint256(17));
        bytes32 txid = sha256(abi.encodePacked(sha256(_raw(1000))));
        batches.configure(batch, keccak256(abi.encode(bytes32(uint256(9) << 248), uint32(0))),
            BitcoinBatchRegistry.State.Reserved);
        bytes32 op = escrow.getBindOperationHash(batch, txid, keys);
        _approve(op, address(escrow));
        uint256 nonceBefore = escrow.nonce();
        vm.expectRevert("not approved");
        escrow.bind(batch, txid, keys, op);
        assertEq(escrow.nonce(), nonceBefore);
        assertEq(escrow.payment(batch).txid, bytes32(0));
    }

    function testCanonicalPoolAndDirectMPSettleAtomicallyInOneBatch() public {
        uint256 id = pool.requestWithdrawal(address(mp), 1000 * SAT, p2wpkh);
        IValueFlowPool.Withdrawal memory w = pool.getWithdrawal(id);
        _approve(w.operationHash, address(pool));
        SimpleMultiPool(address(pool)).processWithdrawal(id, w.operationHash);
        mining.mint(address(this), 1000 * SAT);
        escrow.requestDirect(7, address(mp), 1000 * SAT, p2wpkh);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = escrow.requestKey(address(pool), id);
        keys[1] = escrow.requestKey(address(this), 7);
        bytes32 batch = bytes32(uint256(19));
        bytes memory raw = _raw(2000);
        bytes32 txid = sha256(abi.encodePacked(sha256(raw)));
        batches.configure(batch, keccak256(abi.encode(bytes32(uint256(9) << 248), uint32(0))),
            BitcoinBatchRegistry.State.Reserved);
        bytes32 op = escrow.getBindOperationHash(batch, txid, keys);
        _approve(op, address(escrow));
        escrow.bind(batch, txid, keys, op);
        batches.setState(batch, BitcoinBatchRegistry.State.Signing);
        uint256 mpSupply = mp.totalSupply();
        uint256 shareSupply = shares.totalSupply();
        escrow.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertTrue(escrow.payment(batch).settled);
        assertTrue(pool.getWithdrawal(id).isProcessed);
        assertEq(mp.totalSupply(), mpSupply - 2000 * SAT);
        assertEq(shares.totalSupply(), shareSupply - 1000 * SAT);
        assertEq(deposits.burnedSats(), 2000);
        assertEq(mp.balanceOf(address(escrow)), 0);
    }

    function _raw(uint64 sats) private view returns (bytes memory) {
        bytes8 little;
        for (uint256 i; i < 8; ++i) little |= bytes8(uint64(uint8(sats >> (i * 8))) << ((7 - i) * 8));
        return abi.encodePacked(hex"0200000001", bytes32(uint256(9)), hex"0000000000ffffffff01", little,
            bytes1(uint8(p2wpkh.length)), p2wpkh, hex"00000000");
    }
}
