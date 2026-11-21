// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";
import {FederationFeeManager, IFeeManager} from "../contracts/src/federation/FederationFeeManager.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FederationNFTModifier} from "../contracts/src/tokens/FederationNFTModifier.sol";

/// @dev Models the account binding in real pool NFT requests: caller cannot supply another owner.
contract NFTPoolCaller {
    function request(FederationFeeManager manager, uint256 amount, uint256 token) external returns (uint256) {
        return manager.calculateFeeWithNFT(false,amount,token,msg.sender);
    }
}

/// @dev Real target/auth/validator contracts. Completed DKG and finalized signatures use the explicit epoch fixture.
contract NFTGovernanceTest is ConfederationEpochFixture {
    bytes32 constant ID=keccak256("NFT governance epoch");
    address constant OWNER=address(0xA11CE);
    DKGAuthorization auth;
    FederationFeeManager manager;

    function setUp() public {
        _baseSetUp();
        address[] memory roster=_founders(2);
        _completeCeremony(ID,roster,2,keccak256("EVM_AUTH"),GX);
        auth=new DKGAuthorization(address(coordinator),ID,abi.encodePacked(GX),roster,2);
        manager=_manager(address(0));
    }

    function _manager(address validator) internal returns(FederationFeeManager) {
        IFeeManager.FeeConfig memory config=IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage,100,0,0);
        return new FederationFeeManager(address(auth),validator,address(0x7777),config,config);
    }
    function _create() internal returns(FederationNFTModifier nft) {
        bytes32 op=manager.getCreateNFTModifierOperationHash("Discount","DISC");
        _approve(auth,ID,op,address(manager));
        nft=FederationNFTModifier(manager.createNFTModifier("Discount","DISC",op));
    }
    function _mint(FederationNFTModifier nft) internal returns(bytes32 op) {
        op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);
        _approve(auth,ID,op,address(manager));
        assertEq(manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op),0);
    }
    function _collector(address who,bool enabled) internal {
        bytes32 op=manager.getSetFeeCollectorOperationHash(who,enabled);
        _approve(auth,ID,op,address(manager));manager.setFeeCollector(who,enabled,op);
    }
    function _used(FederationNFTModifier nft) internal view returns(uint256 used) { (,,used,,)=nft.nftMetadata(0); }

    function test_CreateRequiresFROSTAndDoesNotLeavePartialState() public {
        bytes32 op=manager.getCreateNFTModifierOperationHash("Discount","DISC");
        vm.expectRevert(FederationFeeManager.InsufficientFROSTSignatures.selector);
        manager.createNFTModifier("Discount","DISC",op);
        assertEq(address(manager.nftModifier()),address(0));assertEq(manager.operationNonce(),0);
    }
    function test_OnlyExactApprovedNamesCanCreateCollection() public {
        bytes32 op=manager.getCreateNFTModifierOperationHash("Discount","DISC");_approve(auth,ID,op,address(manager));
        vm.expectRevert("Operation hash mismatch");manager.createNFTModifier("Fake","DISC",op);
        vm.prank(OUTSIDER);address collection=manager.createNFTModifier("Discount","DISC",op);
        assertEq(manager.nftCreatedCollections(op),collection);assertTrue(manager.managedNFTCollections(collection));
        assertTrue(auth.executedOperations(op));
    }
    function test_ManagedIssuerRolesDoNotGiveEOAsMintRights() public {
        FederationNFTModifier nft=_create();
        assertTrue(nft.hasRole(nft.DEFAULT_ADMIN_ROLE(),address(manager)));
        assertTrue(nft.hasRole(nft.MINTER_ROLE(),address(manager)));
        assertTrue(nft.hasRole(nft.USAGE_ROLE(),address(manager)));
        assertFalse(nft.hasRole(nft.MINTER_ROLE(),address(auth)));
        vm.expectRevert();nft.mint(OWNER,block.timestamp+1 days,100 ether,2500);
    }
    function test_CannotCreateSecondCollectionOrReplayCreate() public {
        _create();bytes32 op=manager.getCreateNFTModifierOperationHash("Other","NEW");
        _approve(auth,ID,op,address(manager));vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);
        manager.createNFTModifier("Other","NEW",op);assertFalse(auth.executedOperations(op));
    }
    function test_MintRequiresApprovalAndCanBeDeliveredByRelayer() public {
        FederationNFTModifier nft=_create();
        bytes32 op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);
        vm.expectRevert(FederationFeeManager.InsufficientFROSTSignatures.selector);
        manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        _approve(auth,ID,op,address(manager));vm.prank(OUTSIDER);
        uint256 token=manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        assertEq(nft.ownerOf(token),OWNER);assertEq(manager.nftMintedTokenIds(op),token);
        assertEq(manager.nftOperationReceipts(op),keccak256(abi.encode(manager.mintNFT.selector,
            address(nft),OWNER,block.timestamp+1 days,1000 ether,uint256(2500))));
    }
    function test_MintReplayCannotIssueAnotherNFT() public {
        FederationNFTModifier nft=_create();bytes32 op=_mint(nft);uint256 nonce=manager.operationNonce();
        vm.expectRevert("Operation hash mismatch");manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        assertEq(nft.balanceOf(OWNER),1);assertEq(manager.operationNonce(),nonce);
    }
    function test_RecipientExpiryVolumeAndDiscountCannotBeSubstituted() public {
        FederationNFTModifier nft=_create();
        bytes32 op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);
        _approve(auth,ID,op,address(manager));
        vm.expectRevert("Operation hash mismatch");manager.mintNFT(address(nft),OUTSIDER,block.timestamp+1 days,1000 ether,2500,op);
        vm.expectRevert("Operation hash mismatch");manager.mintNFT(address(nft),OWNER,block.timestamp+2 days,1000 ether,2500,op);
        vm.expectRevert("Operation hash mismatch");manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,2000 ether,2500,op);
        vm.expectRevert("Operation hash mismatch");manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,5000,op);
        assertFalse(auth.executedOperations(op));assertEq(nft.balanceOf(OWNER),0);
    }
    function test_CollectionAndTargetCannotBeSubstituted() public {
        FederationNFTModifier nft=_create();
        bytes32 op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);
        _approve(auth,ID,op,address(manager));
        vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);
        manager.mintNFT(address(0x1234),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        FederationFeeManager other=_manager(address(0));
        bytes32 set=other.getSetNFTModifierOperationHash(address(nft));_approve(auth,ID,set,address(other));other.setNFTModifier(address(nft),set);
        vm.expectRevert("Operation hash mismatch");other.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
    }
    function test_InvalidMintTermsRejectBeforeConsumingApproval() public {
        FederationNFTModifier nft=_create();
        vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);manager.mintNFT(address(nft),OWNER,block.timestamp,1,1,bytes32(0));
        vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);manager.mintNFT(address(nft),OWNER,block.timestamp+1,0,1,bytes32(0));
        vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);manager.mintNFT(address(nft),OWNER,block.timestamp+1,1,10001,bytes32(0));
        vm.expectRevert(FederationFeeManager.InvalidNFTConfiguration.selector);manager.mintNFT(address(nft),address(0),block.timestamp+1,1,1,bytes32(0));
    }
    function test_ConfederationCreateAndMintNeedCustodianApprovalsToo() public {
        CustodianValidator validator=new CustodianValidator(_founders(2),2,address(auth));
        manager=_manager(address(validator));
        bytes32 op=manager.getCreateNFTModifierOperationHash("Discount","DISC");_approve(auth,ID,op,address(manager));
        vm.expectRevert(FederationFeeManager.InsufficientCustodianSignatures.selector);manager.createNFTModifier("Discount","DISC",op);
        vm.prank(address(0x100));validator.registerCustodianSignature(op);
        vm.expectRevert(FederationFeeManager.InsufficientCustodianSignatures.selector);manager.createNFTModifier("Discount","DISC",op);
        vm.prank(address(0x101));validator.registerCustodianSignature(op);
        FederationNFTModifier nft=FederationNFTModifier(manager.createNFTModifier("Discount","DISC",op));
        op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);_approve(auth,ID,op,address(manager));
        vm.expectRevert(FederationFeeManager.InsufficientCustodianSignatures.selector);manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        vm.prank(address(0x100));validator.registerCustodianSignature(op);vm.prank(address(0x101));validator.registerCustodianSignature(op);
        manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);assertEq(nft.ownerOf(0),OWNER);
    }
    function test_PublicQuotesNeverConsumeQuota() public {
        FederationNFTModifier nft=_create();_mint(nft);
        vm.prank(OUTSIDER);assertEq(manager.quoteFeeWithNFT(false,100 ether,0,OWNER),0.75 ether);
        assertEq(nft.previewDiscountFor(0,OWNER,100 ether),2500);assertEq(_used(nft),0);
    }
    function test_PublicEOAAndUnapprovedContractCannotConsumeVictimQuota() public {
        FederationNFTModifier nft=_create();_mint(nft);
        vm.prank(OUTSIDER);vm.expectRevert(FederationFeeManager.UnauthorizedFeeCollector.selector);
        manager.calculateFeeWithNFT(false,100 ether,0,OWNER);
        vm.expectRevert(FederationFeeManager.UnauthorizedFeeCollector.selector);manager.calculateFeeWithNFT(false,100 ether,0,OWNER);
        assertEq(_used(nft),0);
    }
    function test_EvenApprovedEOACannotPretendToBePool() public {
        FederationNFTModifier nft=_create();_mint(nft);_collector(OUTSIDER,true);
        vm.prank(OUTSIDER);vm.expectRevert(FederationFeeManager.UnauthorizedFeeCollector.selector);
        manager.calculateFeeWithNFT(false,100 ether,0,OWNER);assertEq(_used(nft),0);
    }
    function test_ApprovedPoolBindsActualOwnerAndConsumesExactlyOnce() public {
        FederationNFTModifier nft=_create();_mint(nft);NFTPoolCaller pool=new NFTPoolCaller();_collector(address(pool),true);
        vm.prank(OUTSIDER);vm.expectRevert(FederationNFTModifier.NotTokenOwner.selector);pool.request(manager,100 ether,0);
        assertEq(_used(nft),0);
        vm.prank(OWNER);assertEq(pool.request(manager,100 ether,0),0.75 ether);assertEq(_used(nft),100 ether);
        _collector(address(pool),false);vm.prank(OWNER);vm.expectRevert(FederationFeeManager.UnauthorizedFeeCollector.selector);pool.request(manager,100 ether,0);
        assertEq(_used(nft),100 ether);
    }
    function test_QuoteAndConsumeApplySameExpiryAndQuotaChecks() public {
        FederationNFTModifier nft=_create();_mint(nft);NFTPoolCaller pool=new NFTPoolCaller();_collector(address(pool),true);
        vm.expectRevert(FederationNFTModifier.VolumeLimitExceeded.selector);manager.quoteFeeWithNFT(false,type(uint256).max,0,OWNER);
        vm.prank(OWNER);vm.expectRevert(FederationNFTModifier.VolumeLimitExceeded.selector);pool.request(manager,1001 ether,0);
        vm.warp(block.timestamp+1 days);
        vm.expectRevert(FederationNFTModifier.NFTExpiredError.selector);manager.quoteFeeWithNFT(false,1,0,OWNER);
        vm.prank(OWNER);vm.expectRevert(FederationNFTModifier.NFTExpiredError.selector);pool.request(manager,1,0);
        assertEq(_used(nft),0);
    }

    function test_TransferPreservesUsageAndRevokesPreviousOwnersAccess() public {
        FederationNFTModifier nft=_create();_mint(nft);NFTPoolCaller pool=new NFTPoolCaller();_collector(address(pool),true);
        vm.prank(OWNER);pool.request(manager,100 ether,0);
        vm.prank(OWNER);nft.transferFrom(OWNER,OUTSIDER,0);
        vm.prank(OWNER);vm.expectRevert(FederationNFTModifier.NotTokenOwner.selector);pool.request(manager,1 ether,0);
        vm.prank(OUTSIDER);pool.request(manager,100 ether,0);
        assertEq(_used(nft),200 ether);
    }

    function test_FailedCollectionMintRollsBackApprovalAndNonce() public {
        // Explicit legacy/external collection fixture whose issuer is not the manager.
        FederationNFTModifier nft=new FederationNFTModifier("External","EXT",address(this));
        bytes32 bind=manager.getSetNFTModifierOperationHash(address(nft));_approve(auth,ID,bind,address(manager));
        manager.setNFTModifier(address(nft),bind);
        bytes32 op=manager.getMintNFTOperationHash(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500);
        _approve(auth,ID,op,address(manager));uint256 nonce=manager.operationNonce();
        vm.expectRevert();manager.mintNFT(address(nft),OWNER,block.timestamp+1 days,1000 ether,2500,op);
        assertEq(manager.operationNonce(),nonce);assertFalse(auth.executedOperations(op));
        assertTrue(auth.isOperationApproved(op,address(manager)));assertEq(nft.balanceOf(OWNER),0);
    }
}
