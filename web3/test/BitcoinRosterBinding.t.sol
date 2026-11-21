// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";

contract BitcoinRosterBindingTest is ConfederationEpochFixture {
    DKGAuthorization private auth;
    bytes32 private constant AUTH_ID = keccak256("authorization epoch");

    function setUp() public {
        _baseSetUp();
        _completeCeremony(AUTH_ID, _roster(2, 2), 3, keccak256("EVM_AUTH"), GX);
        auth = new DKGAuthorization(address(coordinator), AUTH_ID, abi.encodePacked(GX), _roster(2, 2), 3);
    }

    function testAcceptsDistinctBtcKeyAndEpochWithSameOrderedRosterAndThreshold() public {
        bytes32 walletId = keccak256("bitcoin wallet epoch");
        _completeCeremony(walletId, _roster(2, 2), 3, keccak256("BTC_SPEND"), SECOND_GX);
        BitcoinBatchRegistry registry = new BitcoinBatchRegistry(address(auth), address(dkg), walletId);
        (, bytes32 result,,, bytes memory btcKey, address[] memory roster, uint256 threshold) = dkg.getCompletedResult(walletId);
        assertEq(registry.walletId(), walletId);
        assertEq(registry.walletResult(), result);
        assertEq(address(registry.authorization()), address(auth));
        assertEq(address(registry.dkg()), address(dkg));
        assertEq(roster, auth.getParticipants());
        assertEq(threshold, auth.threshold());
        assertTrue(auth.dkgId() != walletId);
        assertTrue(keccak256(btcKey) != keccak256(auth.groupPubkey()));
    }

    function testRejectsChangedBtcParticipant() public {
        address[] memory roster = _roster(2, 2);
        roster[3] = OUTSIDER;
        _rejectRoster(roster, 3);
    }

    function testRejectsReorderedBtcParticipants() public {
        address[] memory roster = _roster(2, 2);
        (roster[0], roster[1]) = (roster[1], roster[0]);
        _rejectRoster(roster, 3);
    }

    function testRejectsDifferentBtcRosterLength() public { _rejectRoster(_roster(2, 3), 3); }
    function testRejectsDifferentBtcThreshold() public { _rejectRoster(_roster(2, 2), 4); }

    function _rejectRoster(address[] memory roster, uint256 threshold) private {
        bytes32 id = keccak256("incompatible btc wallet");
        _completeCeremony(id, roster, threshold, keccak256("BTC_SPEND"), SECOND_GX);
        vm.expectRevert("wallet roster");
        new BitcoinBatchRegistry(address(auth), address(dkg), id);
    }

    function testRejectsWrongPurposeBeforeRoster() public {
        vm.expectRevert("wallet purpose");
        new BitcoinBatchRegistry(address(auth), address(dkg), AUTH_ID);
    }

    function testRejectsWrongRegistryBeforeResult() public {
        DKGCoordinator other = new DKGCoordinator();
        vm.expectRevert("DKG registry mismatch");
        new BitcoinBatchRegistry(address(auth), address(other), bytes32(uint256(1)));
    }

    function testRejectsUnfinishedBtcWallet() public {
        bytes32 id = keccak256("unfinished wallet");
        _startCeremony(id, _roster(2, 2), 3, keccak256("BTC_SPEND"));
        vm.expectRevert(DKGCoordinator.DKGNotComplete.selector);
        new BitcoinBatchRegistry(address(auth), address(dkg), id);
    }
}
