// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinDepositDerivation.sol";
contract BitcoinWithdrawalFeeDerivation is BitcoinDepositDerivation {
    bytes public constant FEE_DOMAIN="SATOHSI_WITHDRAWAL_FEE_V1\x00";
    bytes32 public constant FEE_PROFILE=keccak256("BASE_POST_DKG_EVEN_Y__WITHDRAWAL_FEE_TAPLEAF_V1");
    bool public constant FEE_QUALIFIED=false;
    function deriveFee(bytes calldata raw,bytes calldata group) external view returns(Descriptor memory d) {
        require(raw.length == FEE_DOMAIN.length+478 && keccak256(raw[:FEE_DOMAIN.length]) == keccak256(FEE_DOMAIN),"fee intent encoding");
        bytes calldata deposit=raw[FEE_DOMAIN.length:FEE_DOMAIN.length+382];_checkIntent(deposit);
        require(group.length == 33 && group[0] == 0x02,"fee base encoding");
        d.intentId=sha256(raw);d.walletContext=sha256(abi.encodePacked("SATOHSI_DEPOSIT_WALLET_CONTEXT_V1\x00",deposit[30:114],deposit[154:278]));
        bytes memory leaf=abi.encodePacked(hex"6a20",d.intentId);bytes32 tag=sha256("TapLeaf");
        d.merkleRoot=sha256(abi.encodePacked(tag,tag,hex"c022",leaf));tag=sha256("TapTweak");
        d.tweak=sha256(abi.encodePacked(tag,tag,group[1:33],d.merkleRoot));(d.outputKey,d.outputParityOdd)=_addTweak(bytes32(group[1:33]),uint256(d.tweak));
        d.bindingCommitment=sha256(abi.encodePacked("SATOHSI_WITHDRAWAL_FEE_BINDING_V1\x00",raw,group,leaf,d.merkleRoot,d.tweak,d.outputKey,
            uint8(d.outputParityOdd ? 1 : 0),hex"5120",d.outputKey));
    }
}
