// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import "../contracts/src/federation/DKGOnchainBoard.sol";
import "../contracts/src/federation/DKGCoordinator.sol";
import "./IntentSessionFixture.sol";
contract DKGOnchainBoardTest is Test {
    DKGOnchainBoard board;
    DKGCoordinator registry;
    address alice=address(11); address bob=address(12);
    bytes32 id=keccak256("onchain-dkg");
    function setUp() public {
        address[] memory roster=new address[](2);roster[0]=alice;roster[1]=bob;
        vm.prank(alice);board=new DKGOnchainBoard(bytes("public invitation"),roster);
        registry=new DKGCoordinator();
        vm.prank(alice);board.bindRegistry(address(registry));
        IntentSessionFixture sessions = new IntentSessionFixture(address(registry));
        sessions.open(900,alice);sessions.open(7,alice);
        vm.prank(alice);board.bindIntentCoordinator(address(sessions));
        bytes32[] memory keys=new bytes32[](2);keys[0]=bytes32(uint256(1));keys[1]=bytes32(uint256(2));
        registry.createCeremony(id,roster,2,keys,1,keccak256("EVM_AUTH"),registry.PROTOCOL_V1());
    }
    function testEnrollmentCannotBeSpoofedOrReplaced() public {
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);board.enroll("foreign");
        vm.startPrank(alice);board.enroll("signed public keys");board.enroll("signed public keys");
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);board.enroll("replacement");vm.stopPrank();
        assertEq(board.enrollment(alice),bytes("signed public keys"));
        assertEq(board.enrollment(bob).length,0);
    }
    function testCiphertextSenderRecipientAndImmutableRetry() public {
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);board.publish(id,2,bob,"ciphertext");
        vm.startPrank(alice);
        board.publish(id,2,bob,"ciphertext");board.publish(id,2,bob,"ciphertext");
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);board.publish(id,2,bob,"different");
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);board.publish(id,2,alice,"self");
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);board.publish(id,2,address(99),"foreign");
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);board.publish(id,0,bob,"misdirected public");
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);board.publish(id,1,address(0),new bytes(16385));
        vm.stopPrank();
        assertEq(board.packet(id,2,alice,bob),bytes("ciphertext"));
        assertEq(board.packet(id,2,bob,alice).length,0);
    }
    function testRegistryBindingCannotBeReplaced() public {
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);board.bindRegistry(address(registry));
        DKGCoordinator other=new DKGCoordinator();
        vm.startPrank(alice);board.bindRegistry(address(registry));
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);board.bindRegistry(address(other));vm.stopPrank();
    }
    function testIntentIndexIsAppendOnlyAndRetryDoesNotDuplicate() public {
        vm.startPrank(alice);
        board.publishIntent(900, "first");
        board.publishIntent(7, "second");
        board.publishIntent(900, "first");
        assertEq(board.intentCount(), 2);
        assertEq(board.intentSessions(0), 900);
        assertEq(board.intentSessions(1), 7);
        assertEq(board.intents(900), bytes("first"));
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);
        board.publishIntent(900, "changed");
        vm.stopPrank();
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        board.publishIntent(8, "foreign");
        assertEq(board.intentCount(), 2);
    }
    function testNoPrebindingWritesOrCoordinatorReplacement() public {
        address[] memory members=new address[](2);members[0]=alice;members[1]=bob;
        vm.prank(alice);DKGOnchainBoard fresh=new DKGOnchainBoard("public invitation",members);
        vm.startPrank(alice);fresh.bindRegistry(address(registry));
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);fresh.publishIntent(1,"poison");
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);fresh.appendFeeIntent(1,3,0,"bad");
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);fresh.commitFeeIntent(1);
        address original=board.intentCoordinator();board.bindIntentCoordinator(original);
        IntentSessionFixture replacement=new IntentSessionFixture(address(registry));
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);board.bindIntentCoordinator(address(replacement));
        vm.stopPrank();
    }
    function testOnlyOperatorCanInitiallyBindCoordinatorAfterRegistry() public {
        address[] memory members=new address[](2);members[0]=alice;members[1]=bob;
        vm.prank(alice);DKGOnchainBoard fresh=new DKGOnchainBoard("public invitation",members);
        address canonical=board.intentCoordinator();
        vm.prank(alice);vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        fresh.bindIntentCoordinator(canonical);
        vm.prank(alice);fresh.bindRegistry(address(registry));
        vm.prank(bob);vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        fresh.bindIntentCoordinator(canonical);
        assertEq(fresh.intentCoordinator(),address(0));
        vm.prank(alice);fresh.bindIntentCoordinator(canonical);
        vm.prank(alice);fresh.bindIntentCoordinator(canonical);
        assertEq(fresh.intentCoordinator(),canonical);
    }
    function testInvalidCoordinatorCannotPoisonInitialBinding() public {
        address[] memory members=new address[](2);members[0]=alice;members[1]=bob;
        vm.prank(alice);DKGOnchainBoard fresh=new DKGOnchainBoard("public invitation",members);
        vm.prank(alice);fresh.bindRegistry(address(registry));
        IntentSessionFixture foreign=new IntentSessionFixture(address(new DKGCoordinator()));
        vm.startPrank(alice);
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);fresh.bindIntentCoordinator(address(0));
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);fresh.bindIntentCoordinator(bob);
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);fresh.bindIntentCoordinator(address(foreign));
        assertEq(fresh.intentCoordinator(),address(0));
        fresh.bindIntentCoordinator(board.intentCoordinator());
        assertEq(fresh.intentCoordinator(),board.intentCoordinator());
        vm.stopPrank();
    }
}
