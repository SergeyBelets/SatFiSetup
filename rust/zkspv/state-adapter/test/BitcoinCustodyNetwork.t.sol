// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {FixtureBase} from "./StateAdapter.t.sol";
import {ZkBitcoinStateV2} from "../contracts/ZkBitcoinStateV2.sol";
import {ZkBitcoinTestnet4StateV2} from "../contracts/ZkBitcoinTestnet4StateV2.sol";
import {BitcoinCustodyNetwork} from "satoshifi-core/../federation/BitcoinCustodyNetwork.sol";
import {MainnetHeaderRelay} from "satoshifi-core/../federation/MainnetHeaderRelay.sol";
import {Testnet4HeaderRelay} from "satoshifi-core/../federation/Testnet4HeaderRelay.sol";
import {RegtestHeaderRelay} from "satoshifi-core/../federation/RegtestHeaderRelay.sol";
import {MainnetSPVGuard} from "satoshifi-core/../federation/MainnetSPVGuard.sol";

contract SelfReportedSPVNetworkFixture {
    function bitcoinGenesis() external pure returns (bytes32) { return BitcoinCustodyNetwork.MAINNET; }
    function testHelpersEnabled() external pure returns (bool) { return false; }
}

contract BitcoinCustodyNetworkTest is FixtureBase {
    address private mainnetGuard;
    address private testnet4Guard;
    address private mainnetZk;
    address private testnet4Zk;
    address private regtest;

    function setUp() public override {
        vm.chainId(31337);
        ZkBitcoinStateV2.Config memory main = config();
        main.programImageId = 0xa4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea;
        mainnetZk = address(deploy(main, 0).adapter());
        string memory checkpoint = cpFixture(0);
        MainnetHeaderRelay mainnetRelay = new MainnetHeaderRelay(vm.parseJsonBytes(checkpoint, ".raw"),
            uint64(vm.parseJsonUint(checkpoint, ".height")), uint256(vm.parseJsonBytes32(checkpoint, ".work")),
            vm.parseJsonBytesArray(checkpoint, ".prior"), 12);
        mainnetGuard = address(new MainnetSPVGuard(address(mainnetRelay), 7200));

        checkpoint = vm.readFile("fixtures/testnet4-state-v2.json");
        bytes memory raw = _parse(checkpoint, ".checkpoint.checkpoint_header");
        bytes[] memory prior = new bytes[](10);
        for (uint256 i; i < 10; ++i) {
            prior[i] = _parse(checkpoint, string.concat(".checkpoint.checkpoint_predecessors[", vm.toString(i), "]"));
        }
        uint64 height = uint64(vm.parseUint(vm.parseJsonString(checkpoint, ".checkpoint.checkpoint_height")));
        uint256 work = uint256(vm.parseBytes32(string.concat("0x", vm.parseJsonString(checkpoint, ".checkpoint.checkpoint_chainwork"))));
        Testnet4HeaderRelay testnet4Relay = new Testnet4HeaderRelay(raw, height, work, prior, 12);
        testnet4Guard = address(new MainnetSPVGuard(address(testnet4Relay), 7200));
        ZkBitcoinTestnet4StateV2.Config memory testnet = ZkBitcoinTestnet4StateV2.Config(bytes32(uint256(7)),
            0xa6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8,
            bytes32("PROOF_ONLY_V1"), 3600, 7200, 7200);
        ZkBitcoinTestnet4StateV2 zk = new ZkBitcoinTestnet4StateV2(testnet, raw, height, work, prior);
        testnet4Zk = address(zk.adapter());
        regtest = address(new RegtestHeaderRelay());
    }

    function _parse(string memory checkpoint, string memory field) private view returns (bytes memory) {
        return vm.parseBytes(string.concat("0x", vm.parseJsonString(checkpoint, field)));
    }

    function classify(address endpoint) external view returns (bytes32) {
        return BitcoinCustodyNetwork.genesis(endpoint);
    }

    function checkPair(address source, address target) external view {
        BitcoinCustodyNetwork.requireMatch(source, BitcoinCustodyNetwork.genesis(target));
    }

    function testBothBackendsOnSameBitcoinNetwork() public view {
        assertEq(this.classify(mainnetGuard), BitcoinCustodyNetwork.MAINNET);
        assertEq(this.classify(mainnetZk), BitcoinCustodyNetwork.MAINNET);
        assertEq(this.classify(testnet4Guard), BitcoinCustodyNetwork.TESTNET4);
        assertEq(this.classify(testnet4Zk), BitcoinCustodyNetwork.TESTNET4);
        assertEq(this.classify(regtest), BitcoinCustodyNetwork.REGTEST);
        this.checkPair(mainnetGuard, mainnetZk);
        this.checkPair(testnet4Zk, testnet4Guard);
    }

    function testDifferentNetworksCannotShareHandoverEvenOnSameEVM() public {
        vm.expectRevert(abi.encodeWithSelector(BitcoinCustodyNetwork.CustodyNetworkMismatch.selector,
            BitcoinCustodyNetwork.MAINNET, BitcoinCustodyNetwork.TESTNET4));
        this.checkPair(mainnetGuard, testnet4Zk);
    }

    function testSelfReportedNetworkAndWrongProgramAreUnrecognized() public {
        address reported = address(new SelfReportedSPVNetworkFixture());
        vm.expectRevert(BitcoinCustodyNetwork.UnsupportedCustodySPV.selector);
        this.classify(reported);
        address wrongProgram = address(deploy(config(), 0).adapter());
        vm.expectRevert(BitcoinCustodyNetwork.UnsupportedCustodySPV.selector);
        this.classify(wrongProgram);
    }
}
