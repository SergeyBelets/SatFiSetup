// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinDepositRegistry.sol";
import {FROSTVerifier} from "../FROSTVerifier.sol";

interface IFeeFundingContext {
    function deposits() external view returns (address);
    function batches() external view returns (address);
    function donorVerifier() external view returns (address);
    function walletScriptHash() external view returns (bytes32);
    function donationHash(bytes32 txid,uint32 vout,uint256 sats) external view returns (bytes32);
}

/// @notice Stateless restricted external donation proof, without budget or signing authority.
contract BitcoinFeeFundingVerifier {
    struct Funding { bytes raw; bytes previous; uint32 vout; bytes signature; bytes32 blockHash; bytes32[] siblings; uint256 index; }
    function verify(address reserve_, Funding calldata d) external view returns(bytes32 txid,uint256 amount) {
        IFeeFundingContext reserve=IFeeFundingContext(reserve_);
        BitcoinDepositRegistry deposits=BitcoinDepositRegistry(reserve.deposits());
        BitcoinBatchRegistry batches=BitcoinBatchRegistry(reserve.batches());
        require(d.raw.length <= 100000 && d.previous.length <= 100000 && d.siblings.length <= 32
            && d.index >> d.siblings.length == 0,"fee funding bounds");
        (bytes32[] memory prev,uint32[] memory vouts)=BitcoinTxParser.parseAllInputs(d.raw);
        require(prev.length == 1 && prev[0] == BitcoinTxParser.txId(d.previous),"fee donor source");
        (bytes32[] memory ancestor,uint32[] memory ancestorVouts)=BitcoinTxParser.parseAllInputs(d.previous);
        require(!(ancestor.length == 1 && ancestor[0] == 0 && ancestorVouts[0] == type(uint32).max),"fee external coinbase source unsupported");
        (,bytes memory source)=BitcoinTxParser.outputAt(d.previous,vouts[0]);
        require(source.length == 34 && source[0] == 0x51 && source[1] == 0x20
            && deposits.scriptPools(keccak256(source)) == address(0),"fee external source profile");
        bytes32 oldKey=keccak256(abi.encode(prev[0],vouts[0]));
        require(!deposits.consumed(oldKey) && !deposits.walletOutput(oldKey) && deposits.backingSats(oldKey) == 0
            && batches.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[0]),vouts[0]))) == 0,"fee protected source");
        uint256 count=BitcoinTxParser.outputCount(d.raw); require(count <= 32,"fee funding outputs");
        for(uint32 i;i<count;++i) { (,bytes memory s)=BitcoinTxParser.outputAt(d.raw,i); require(s.length == 0 || s[0] != 0x6a,"fee deposit marker"); }
        (uint64 sats,bytes memory script)=BitcoinTxParser.outputAt(d.raw,d.vout); amount=sats;
        require(amount > 0 && keccak256(script) == reserve.walletScriptHash(),"fee funding script");
        txid=BitcoinTxParser.txId(d.raw); bytes32 key=keccak256(abi.encode(txid,d.vout));
        require(!deposits.consumed(key) && !deposits.walletOutput(key)
            && batches.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid),d.vout))) == 0,"fee funding used");
        bytes memory donor=new bytes(32); for(uint256 i;i<32;++i)donor[i]=source[i+2];
        require(FROSTVerifier(reserve.donorVerifier()).verify(donor,reserve.donationHash(txid,d.vout,amount),d.signature,"Schnorr"),"fee donor consent");
        (bool canonical,uint64 confirmations)=deposits.spv().getBlockStatus(d.blockHash);
        require(canonical && confirmations >= deposits.depth()
            && deposits.spv().checkTxInclusion(d.blockHash,txid,d.siblings,d.index),"fee funding proof");
    }
}
