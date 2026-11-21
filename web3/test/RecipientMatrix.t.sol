// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.t.sol";
import {RecipientPolicy} from "../contracts/src/federation/RecipientPolicy.sol";
import {RecipientWalletV1} from "../contracts/src/federation/RecipientWalletV1.sol";
import {IERC1271} from "@openzeppelin/contracts/interfaces/IERC1271.sol";

contract UnverifiedRecipient { function value() external pure returns(uint256) { return 1; } }

/// @dev Local production wallet/policy/registry/token execution. The inherited
/// DKG, threshold, SPV and qualification boundaries are explicitly TEST fixtures.
contract RecipientMatrixTest is BitcoinAddressRegistryTest {
    uint256 constant OWNER_KEY=0x123456;
    function _wallet() internal returns(RecipientWalletV1) {
        return RecipientWalletV1(payable(addresses.recipientPolicy().createWallet(vm.addr(OWNER_KEY),bytes32(uint256(1)))));
    }
    function _choice(address recipient,RecipientPolicy.Mode mode) internal view returns(RecipientPolicy.Choice memory c) {
        RecipientPolicy p=addresses.recipientPolicy();
        bytes32 ctx=p.context(address(addresses),address(pool),address(pool.mpToken()),REQUESTER,recipient);
        c=RecipientPolicy.Choice(mode,recipient.codehash,mode == RecipientPolicy.Mode.WALLET_V1 ? vm.addr(OWNER_KEY) : address(0),
            bytes32(uint256(77)),mode == RecipientPolicy.Mode.ADVANCED_CONTRACT ? p.riskAcknowledgement(ctx,recipient.codehash) : bytes32(0));
    }
    function _select(address recipient,RecipientPolicy.Mode mode) internal returns(bytes32 nonce) {
        RecipientPolicy.Choice memory c=_choice(recipient,mode);
        RecipientPolicy p=addresses.recipientPolicy();address token=address(pool.mpToken());
        vm.prank(REQUESTER);
        return p.select(address(addresses),address(pool),token,recipient,c);
    }
    function _registerSelected(address recipient,bytes32 nonce) internal returns(bytes32 id) {
        bytes[] memory intents=new bytes[](1);intents[0]=_intent(uint256(nonce),recipient);
        _registerBatch(intents);return sha256(intents[0]);
    }
    function _fundAndMint(bytes32 id) internal {
        bytes memory raw=_funding(1,2,0,_script(id),bytes32(uint256(900)),false);
        _mint(raw,_claim(id,raw,0));
    }
    function testMatrixSmartWalletMintsThenOwnerTransfersApprovesAndWithdrawsMP() public {
        PoolMpToken token=pool.mpToken();
        RecipientWalletV1 wallet=_wallet();bytes32 nonce=_select(address(wallet),RecipientPolicy.Mode.WALLET_V1);
        bytes32 id=_registerSelected(address(wallet),nonce);_fundAndMint(id);
        assertEq(token.balanceOf(address(wallet)),1000*1e10);
        vm.prank(vm.addr(OWNER_KEY));
        wallet.execute(address(token),0,abi.encodeCall(token.transfer,(RECIPIENT,200*1e10)));
        assertEq(token.balanceOf(RECIPIENT),200*1e10);
        vm.prank(vm.addr(OWNER_KEY));
        wallet.execute(address(token),0,abi.encodeCall(token.approve,(address(escrow),400*1e10)));
        bytes memory destination=abi.encodePacked(hex"5120",bytes32(uint256(0x9876)));
        vm.prank(vm.addr(OWNER_KEY));
        wallet.execute(address(escrow),0,abi.encodeCall(escrow.requestDirect,(uint256(42),address(token),400*1e10,destination)));
        BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(escrow.requestKey(address(wallet),42));
        assertEq(r.owner,address(wallet));assertEq(r.amount,400*1e10);
        assertEq(token.balanceOf(address(wallet)),400*1e10);
    }
    function testMatrixWalletERC1271AndOwnerOnlyExecution() public {
        RecipientWalletV1 wallet=_wallet();bytes32 hash=keccak256("pinned local wallet signature");
        (uint8 v,bytes32 r,bytes32 s)=vm.sign(OWNER_KEY,hash);
        bytes memory sig=abi.encodePacked(r,s,v);
        assertEq(wallet.isValidSignature(hash,sig),IERC1271.isValidSignature.selector);
        assertEq(wallet.isValidSignature(keccak256("changed"),sig),bytes4(0xffffffff));
        assertEq(wallet.isValidSignature(hash,hex"00"),bytes4(0xffffffff));
        PoolMpToken token=pool.mpToken();bytes memory data=abi.encodeCall(token.transfer,(RECIPIENT,1));
        vm.expectRevert("wallet owner only");wallet.execute(address(token),0,data);
    }
    function testMatrixThirdPartyWalletRecipientNeedsNoRecipientSignature() public {
        RecipientWalletV1 wallet=_wallet();assertTrue(REQUESTER != wallet.owner());
        bytes32 id=_registerSelected(address(wallet),_select(address(wallet),RecipientPolicy.Mode.WALLET_V1));
        assertEq(addresses.getBinding(id).recipient,address(wallet));
    }
    function testMatrixAdvancedRequiresExactRiskAckAndCanReceiveWithoutAnExitInterface() public {
        UnverifiedRecipient recipient=new UnverifiedRecipient();RecipientPolicy p=addresses.recipientPolicy();
        RecipientPolicy.Choice memory c=_choice(address(recipient),RecipientPolicy.Mode.ADVANCED_CONTRACT);
        address token=address(pool.mpToken());
        c.acknowledgement=0;
        vm.prank(REQUESTER);vm.expectRevert("recipient advanced acknowledgement");
        p.select(address(addresses),address(pool),token,address(recipient),c);
        bytes32 id=_registerSelected(address(recipient),_select(address(recipient),RecipientPolicy.Mode.ADVANCED_CONTRACT));
        _fundAndMint(id);assertEq(pool.mpToken().balanceOf(address(recipient)),1000*1e10);
        // Reception itself makes no claim that this third-party contract can
        // ever extract MP. No protocol rescue/reassignment privilege is added.
        PoolMpToken mp=pool.mpToken();vm.expectRevert();mp.transferFrom(address(recipient),RECIPIENT,1);
    }
    function testMatrixSelectionCannotBeReplayedForAnotherRecipient() public {
        RecipientWalletV1 a=_wallet();
        address b=addresses.recipientPolicy().createWallet(vm.addr(OWNER_KEY),bytes32(uint256(2)));
        bytes32 nonce=_select(address(a),RecipientPolicy.Mode.WALLET_V1);
        bytes[] memory raw=new bytes[](1);raw[0]=_intent(uint256(nonce),b);
        bytes32 op=addresses.getRegisterOperationHash(raw,1,2,uint64(block.timestamp+3600),0);_approve(op,address(addresses));
        vm.expectRevert("recipient selection context");addresses.registerBatch(raw,1,2,uint64(block.timestamp+3600),0,op);
        assertEq(addresses.registrationNonce(),0);
    }
    function testMatrixAdvancedDoesNotBypassProtocolSinkBan() public {
        address target=address(pool.mpToken());bytes32 nonce=_select(target,RecipientPolicy.Mode.ADVANCED_CONTRACT);
        bytes[] memory raw=new bytes[](1);raw[0]=_intent(uint256(nonce),target);
        bytes32 op=addresses.getRegisterOperationHash(raw,1,2,uint64(block.timestamp+3600),0);_approve(op,address(addresses));
        vm.expectRevert("recipient sink");addresses.registerBatch(raw,1,2,uint64(block.timestamp+3600),0,op);
    }
    function testMatrixCodeChangeBlocksNewMintAndRetainsImmutableBindingAndApproval() public {
        UnverifiedRecipient recipient=new UnverifiedRecipient();
        bytes32 id=_registerSelected(address(recipient),_select(address(recipient),RecipientPolicy.Mode.ADVANCED_CONTRACT));
        bytes memory original=addresses.getBinding(id).intent;
        // Synthetic code-change boundary; not a claim of a real chain reorg.
        vm.etch(address(recipient),hex"60006000f3");
        bytes memory raw=_funding(1,1,0,_script(id),bytes32(uint256(900)),false);
        _mintFailure(raw,_claim(id,raw,0),"recipient code profile");
        assertEq(addresses.getBinding(id).intent,original);
        assertFalse(common.consumed(keccak256(abi.encode(_txid(raw),uint32(0)))));
    }
    function testMatrixDelegationAndCounterfactualAreNotSupportedWalletProfiles() public {
        address recipient=address(0x98765);RecipientPolicy p=addresses.recipientPolicy();address token=address(pool.mpToken());
        RecipientPolicy.Choice memory c=RecipientPolicy.Choice(RecipientPolicy.Mode.WALLET_V1,recipient.codehash,vm.addr(OWNER_KEY),bytes32(uint256(9)),0);
        vm.prank(REQUESTER);vm.expectRevert("recipient code profile");
        p.select(address(addresses),address(pool),token,recipient,c);
        vm.etch(recipient,abi.encodePacked(hex"ef0100",address(pool)));
        c=_choice(recipient,RecipientPolicy.Mode.ADVANCED_CONTRACT);
        vm.prank(REQUESTER);vm.expectRevert("recipient code profile");
        p.select(address(addresses),address(pool),token,recipient,c);
    }
    function testMatrixUnknownContractCannotClaimWalletProfile() public {
        UnverifiedRecipient recipient=new UnverifiedRecipient();
        RecipientPolicy.Choice memory c=_choice(address(recipient),RecipientPolicy.Mode.WALLET_V1);
        RecipientPolicy p=addresses.recipientPolicy();address token=address(pool.mpToken());
        vm.prank(REQUESTER);vm.expectRevert("recipient wallet profile");
        p.select(address(addresses),address(pool),token,address(recipient),c);
    }
    function testMatrixPermissionlessOpeningDoesNotGrantRegistrationAuthority() public {
        RecipientWalletV1 wallet=_wallet();RecipientPolicy p=addresses.recipientPolicy();
        address token=address(pool.mpToken());RecipientPolicy.Choice memory c=_choice(address(wallet),RecipientPolicy.Mode.WALLET_V1);
        vm.prank(address(0x9999));
        bytes32 nonce=p.selectFor(address(addresses),address(pool),token,REQUESTER,address(wallet),c);
        assertEq(p.check(address(addresses),address(pool),token,REQUESTER,address(wallet),nonce),1);
        vm.expectRevert("recipient selection context");
        p.check(address(addresses),address(pool),token,address(0x9999),address(wallet),nonce);
        bytes[] memory intents=new bytes[](1);intents[0]=_intent(uint256(nonce),address(wallet));
        uint64 deadline=uint64(block.timestamp+3600);
        bytes32 op=addresses.getRegisterOperationHash(intents,1,2,deadline,0);
        vm.expectRevert();addresses.registerBatch(intents,1,2,deadline,0,op);
        assertEq(addresses.registrationNonce(),0);
        _approve(op,address(addresses));addresses.registerBatch(intents,1,2,deadline,0,op);
        assertEq(addresses.getBinding(sha256(intents[0])).recipient,address(wallet));
    }
}
