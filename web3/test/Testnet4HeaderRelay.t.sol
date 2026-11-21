// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/Testnet4HeaderRelay.sol";
import "../contracts/src/federation/MainnetSPVGuard.sol";

contract Testnet4RelayHarness is Testnet4HeaderRelay {
    uint256 serial;
    constructor(bytes memory h, uint64 height, uint256 work, bytes[] memory prior)
        Testnet4HeaderRelay(h, height, work, prior, 144) {}
    function synthetic(bytes32 parent, uint32 time, uint32 bits) external returns(bytes32 hash) {
        hash=keccak256(abi.encode(parent,++serial,time,bits));
        ParsedHeader memory h=ParsedHeader(parent,bytes32(0),time,bits);
        _storeHeader(hash,h,blocks[parent].height+1,blocks[parent].cumulativeWork+1);
    }
    function expected(bytes32 parent,uint64 height,uint32 time) external view returns(uint32) {
        return _expectedHeaderBits(parent,height,time);
    }
    function validateTimestamp(bytes32 parent,uint64 height,uint32 time) external view {
        _validateHeaderTime(time,parent,height);
    }
}

contract Testnet4HeaderRelayTest is Test {
    bytes headers;
    bytes records;
    uint64 constant FIRST=2006;
    uint64 constant CHECKPOINT=2016;
    uint64 constant LAST=4050;
    Testnet4HeaderRelay relay;
    function setUp() public {
        headers=vm.readFileBinary("test/fixtures/testnet4/headers-2006-4050.bin");
        records=vm.readFileBinary("test/fixtures/testnet4/expected-2006-4050.bin");
        vm.warp(1800000000);
        relay=new Testnet4HeaderRelay(header(CHECKPOINT),CHECKPOINT,work(CHECKPOINT),prior(),144);
    }
    function header(uint64 h) internal view returns(bytes memory raw) {
        raw=new bytes(80);uint256 start=(h-FIRST)*80;
        for(uint256 i;i<80;++i)raw[i]=headers[start+i];
    }
    function work(uint64 h) internal view returns(uint256 value) {
        uint256 offset=(h-FIRST)*68+32;
        for(uint256 i;i<32;++i)value=(value<<8)|uint8(records[offset+i]);
    }
    function digest(bytes memory raw) internal pure returns(bytes32) {return sha256(abi.encodePacked(sha256(raw)));}
    function prior() internal view returns(bytes[] memory p) {
        p=new bytes[](10);for(uint64 i;i<10;++i)p[i]=header(FIRST+i);
    }
    function rawTime(uint64 h) internal view returns(uint32) {
        bytes memory r=header(h);return uint32(uint8(r[68]))|(uint32(uint8(r[69]))<<8)|(uint32(uint8(r[70]))<<16)|(uint32(uint8(r[71]))<<24);
    }
    function rawBits(uint64 h) internal view returns(uint32) {
        bytes memory r=header(h);return uint32(uint8(r[72]))|(uint32(uint8(r[73]))<<8)|(uint32(uint8(r[74]))<<16)|(uint32(uint8(r[75]))<<24);
    }
    function harness() internal returns(Testnet4RelayHarness) {
        return new Testnet4RelayHarness(header(CHECKPOINT),CHECKPOINT,work(CHECKPOINT),prior());
    }
    function testPublicTestnet4HeadersAcrossRetargetAndBoundedBatches() public {
        for(uint64 start=CHECKPOINT+1;start<=LAST;start+=64){
            uint64 stop=start+64;if(stop>LAST+1)stop=LAST+1;
            bytes[] memory batch=new bytes[](stop-start);
            for(uint64 h=start;h<stop;++h)batch[h-start]=header(h);
            relay.addBlockHeaders(batch);
            assertEq(relay.getMainchainHead(),digest(header(stop-1)));
            (,,uint256 total,uint64 height,,uint32 bits)=relay.blocks(relay.getMainchainHead());
            assertEq(total,work(stop-1));assertEq(height,stop-1);assertEq(bits,rawBits(stop-1));
        }
        assertFalse(relay.testHelpersEnabled());assertFalse(relay.halted());
        MainnetSPVGuard guard=new MainnetSPVGuard(address(relay),86400);
        vm.warp(rawTime(LAST));assertTrue(guard.isFresh());
    }
    function testTestnet4RejectsMainnetChildAndRegtestTarget() public {
        bytes memory mainnet=vm.readFileBinary("test/fixtures/mainnet/headers-32246-36288.bin");
        bytes memory other=new bytes(80);for(uint256 i;i<80;++i)other[i]=mainnet[i];
        vm.expectRevert();relay.addBlockHeader(other);
        bytes memory raw=header(CHECKPOINT+1);raw[72]=0xff;raw[73]=0xff;raw[74]=0x7f;raw[75]=0x20;
        vm.expectRevert(MainnetHeaderRelay.TargetAboveMainnetLimit.selector);relay.addBlockHeader(raw);
    }
    function testMinimumDifficultyStrict1200BoundaryAndBranchLocalRecovery() public {
        Testnet4RelayHarness r=harness();bytes32 p=r.checkpointHash();uint32 time=rawTime(CHECKPOINT);uint32 actual=rawBits(CHECKPOINT);
        assertTrue(actual!=0x1d00ffff);
        assertEq(r.expected(p,2017,time+1200),actual);
        assertEq(r.expected(p,2017,time+1201),0x1d00ffff);
        bytes32 easy=r.synthetic(p,time+1201,0x1d00ffff);
        assertEq(r.expected(easy,2018,time+1202),actual);
        bytes32 easyAgain=r.synthetic(easy,time+2402,0x1d00ffff);
        assertEq(r.expected(easyAgain,2019,time+2403),actual);
        bytes32 fork=r.synthetic(p,time+1,actual);
        assertEq(r.expected(fork,2018,time+2),actual);
        assertEq(r.expected(easyAgain,2019,time+3603),0x1d00ffff);
    }
    function testBIP94RetargetUsesFirstBlockNotMinimumDifficultyLastBlock() public {
        Testnet4RelayHarness r=harness();bytes32 p=r.checkpointHash();uint32 time=rawTime(CHECKPOINT);
        for(uint64 h=2017;h<4032;++h)p=r.synthetic(p,time+uint32(h-2016)*600,h==4031?uint32(0x1d00ffff):rawBits(CHECKPOINT));
        uint32 expected=TargetsHelper.targetToBits(TargetsHelper.retargetWithLimit(TargetsHelper.bitsToTarget(rawBits(CHECKPOINT)),int64(2015*600),r.POW_LIMIT()));
        assertEq(r.expected(p,4032,time+2016*600),expected);
        // Period boundaries do not get the 20-minute exception.
        assertEq(r.expected(p,4032,time+2016*600+1201),expected);
        assertTrue(expected!=0x1d00ffff);
    }
    function testBIP94TimeWarpBoundaryAndMedianStillEnforced() public {
        Testnet4RelayHarness r=harness();uint32 time=rawTime(CHECKPOINT);bytes32 p=r.synthetic(r.checkpointHash(),time+3600,rawBits(CHECKPOINT));
        vm.expectRevert(Testnet4HeaderRelay.InvalidBIP94Time.selector);r.validateTimestamp(p,4032,time+2999);
        r.validateTimestamp(p,4032,time+3000);
        vm.expectRevert(MainnetHeaderRelay.InvalidMedianTime.selector);r.validateTimestamp(p,2018,1);
    }
    function testBatchRejectsEmptyOversizeAndRollsBackMalformedChild() public {
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderBatch.selector);relay.addBlockHeaders(new bytes[](0));
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderBatch.selector);relay.addBlockHeaders(new bytes[](65));
        bytes[] memory batch=new bytes[](2);batch[0]=header(CHECKPOINT+1);batch[1]=new bytes(79);
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderLength.selector);relay.addBlockHeaders(batch);
        assertEq(relay.getMainchainHeight(),CHECKPOINT);
        (,,,uint64 height,,)=relay.blocks(digest(batch[0]));assertEq(height,0);
    }
    function testBatchOverlapAndExactRetryPreserveTestnetWorkAndHead() public {
        relay.addBlockHeader(header(CHECKPOINT+1));
        bytes[] memory batch=new bytes[](3);
        for(uint64 i;i<3;++i)batch[i]=header(CHECKPOINT+i);
        vm.recordLogs();relay.addBlockHeaders(batch);
        assertEq(vm.getRecordedLogs().length,2);
        assertEq(relay.getMainchainHead(),digest(header(CHECKPOINT+2)));
        (,,uint256 total,,,) = relay.blocks(relay.getMainchainHead());
        assertEq(total,work(CHECKPOINT+2));
        vm.recordLogs();relay.addBlockHeaders(batch);
        assertEq(vm.getRecordedLogs().length,0);
        assertEq(relay.getMainchainHead(),digest(header(CHECKPOINT+2)));
        (,,uint256 afterWork,,,) = relay.blocks(relay.getMainchainHead());
        assertEq(afterWork,total);
        assertEq(relay.height2hash(CHECKPOINT+1),digest(header(CHECKPOINT+1)));
    }
}
