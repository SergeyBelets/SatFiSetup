// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {RecipientWalletV1} from "./RecipientWalletV1.sol";

/// @notice Immutable recipient choices, not a registry of universally usable contracts.
/// @dev Address intents bind their exact choice through the existing request nonce.
contract RecipientPolicy {
    enum Mode { ORDINARY, WALLET_V1, ADVANCED_CONTRACT }
    struct Choice { Mode mode; bytes32 runtimeHash; address owner; bytes32 seed; bytes32 acknowledgement; }
    struct Selection { bytes32 context; Choice choice; }
    address public immutable creator;
    mapping(address=>address) public wallets;
    mapping(address=>bytes32) public walletHashes;
    mapping(bytes32=>Selection) private selections;
    event WalletCreated(address indexed wallet,address indexed owner,bytes32 salt,bytes32 runtimeHash);
    event RecipientSelected(bytes32 indexed nonce,bytes32 indexed context,uint8 mode,address recipient);
    constructor() { creator=msg.sender; }
    function predictWallet(address owner,bytes32 salt) public view returns(address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(hex"ff",address(this),keccak256(abi.encode(owner,salt)),
            keccak256(abi.encodePacked(type(RecipientWalletV1).creationCode,abi.encode(owner))))))));
    }
    function createWallet(address owner,bytes32 salt) external returns(address wallet) {
        wallet=predictWallet(owner,salt);
        if(wallets[wallet] != address(0))return wallet;
        wallet=address(new RecipientWalletV1{salt:keccak256(abi.encode(owner,salt))}(owner));
        wallets[wallet]=owner;walletHashes[wallet]=wallet.codehash;
        emit WalletCreated(wallet,owner,salt,wallet.codehash);
    }
    function context(address namespace,address pool,address token,address requester,address recipient) public view returns(bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_RECIPIENT_CONTEXT_V1"),block.chainid,address(this),namespace,pool,token,requester,recipient));
    }
    function riskAcknowledgement(bytes32 ctx,bytes32 runtimeHash) public pure returns(bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_ADVANCED_MP_MAY_BE_PERMANENTLY_INACCESSIBLE_V1"),ctx,runtimeHash));
    }
    function nonceFor(bytes32 ctx,Choice calldata c) public pure returns(bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_RECIPIENT_CHOICE_V1"),ctx,c));
    }
    function selection(bytes32 nonce) external view returns(Selection memory) { return selections[nonce]; }
    function _delegated(address recipient) private view returns(bool) {
        bytes memory code=recipient.code;
        return code.length == 23 && code[0] == 0xef && code[1] == 0x01 && code[2] == 0x00;
    }
    function _review(bytes32 ctx,address recipient,Choice memory c) private view {
        require(recipient != address(0) && recipient != address(this),"recipient sink");
        if(c.mode == Mode.ORDINARY) {
            require(recipient.code.length == 0,"recipient profile");return;
        }
        require(recipient.code.length != 0 && recipient.codehash == c.runtimeHash && !_delegated(recipient),"recipient code profile");
        if(c.mode == Mode.WALLET_V1) {
            require(c.owner != address(0) && c.owner.code.length == 0 && wallets[recipient] == c.owner
                && walletHashes[recipient] == c.runtimeHash && RecipientWalletV1(payable(recipient)).owner() == c.owner
                && c.acknowledgement == 0,"recipient wallet profile");
        } else {
            require(c.owner == address(0) && c.acknowledgement == riskAcknowledgement(ctx,c.runtimeHash),"recipient advanced acknowledgement");
        }
    }
    function select(address namespace,address pool,address token,address recipient,Choice calldata c) external returns(bytes32 nonce) {
        return _select(namespace,pool,token,msg.sender,recipient,c);
    }
    /// @notice Opens an already committed choice. This grants no requester authority:
    /// registration separately authenticates the exact intent containing this nonce.
    function selectFor(address namespace,address pool,address token,address requester,address recipient,Choice calldata c) external returns(bytes32 nonce) {
        require(requester != address(0),"recipient requester");
        return _select(namespace,pool,token,requester,recipient,c);
    }
    function _select(address namespace,address pool,address token,address requester,address recipient,Choice calldata c) private returns(bytes32 nonce) {
        require(c.mode != Mode.ORDINARY && c.seed != 0,"recipient explicit choice");
        bytes32 ctx=context(namespace,pool,token,requester,recipient);_review(ctx,recipient,c);nonce=nonceFor(ctx,c);
        Selection storage old=selections[nonce];
        if(old.context != 0) { require(old.context == ctx,"recipient choice conflict");return nonce; }
        selections[nonce]=Selection(ctx,c);
        emit RecipientSelected(nonce,ctx,uint8(c.mode),recipient);
    }
    function check(address namespace,address pool,address token,address requester,address recipient,bytes32 nonce) external view returns(uint8) {
        bytes32 ctx=context(namespace,pool,token,requester,recipient);
        Selection storage s=selections[nonce];
        if(s.context == 0) {
            _review(ctx,recipient,Choice(Mode.ORDINARY,0,address(0),0,0));return 0;
        }
        require(s.context == ctx,"recipient selection context");_review(ctx,recipient,s.choice);return uint8(s.choice.mode);
    }
}
