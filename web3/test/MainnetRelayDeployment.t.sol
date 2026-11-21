// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";
import {DeployMainnetHeaderRelay} from "../script/DeployMainnetHeaderRelay.s.sol";
import {MainnetSPVGuard} from "../contracts/src/federation/MainnetSPVGuard.sol";

contract MainnetRelayDeploymentTest is MainnetFixtureBase {
    function setUp() public { loadMainnetFixture(); }

    function _environment() private {
        vm.chainId(1);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.setEnv("PRIVATE_KEY", "1");
        vm.setEnv("BTC_CHECKPOINT_HEADER", vm.toString(headerAt(CHECKPOINT)));
        vm.setEnv("BTC_CHECKPOINT_HEIGHT", vm.toString(CHECKPOINT));
        vm.setEnv("BTC_CHECKPOINT_CHAINWORK", vm.toString(expectedWork(CHECKPOINT)));
        string memory prior;
        for (uint64 i; i < 10; ++i) {
            prior = string.concat(prior, i == 0 ? "" : ",", vm.toString(headerAt(FIRST + i)));
        }
        vm.setEnv("BTC_CHECKPOINT_PREDECESSORS", prior);
        vm.setEnv("MAX_REORG_DEPTH", "100");
        vm.setEnv("MAX_TIP_AGE_SECONDS", "86400");
    }

    function testEthereumMainnetScriptDeploysRawRelayAndMandatoryConsumerGuard() public {
        _environment();
        DeployMainnetHeaderRelay script = new DeployMainnetHeaderRelay();
        MainnetHeaderRelay relay = script.run();
        MainnetSPVGuard guard = script.guard();
        assertEq(address(guard.relay()), address(relay));
        assertEq(guard.maxTipAgeSeconds(), 86400);
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT));
        assertFalse(guard.isFresh()); // Historical checkpoint must be caught up before accepting money.
        (bool accepted, uint64 confirmations) = guard.getBlockStatus(wireHash(CHECKPOINT));
        assertFalse(accepted);
        assertEq(confirmations, 0);
    }

    function testMainnetScriptRejectsChainAndUnboundedPolicyBeforeBroadcast() public {
        _environment();
        DeployMainnetHeaderRelay script = new DeployMainnetHeaderRelay();
        uint64 nonce = vm.getNonce(vm.addr(1));
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.expectRevert("chain mismatch"); script.run();
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.setEnv("MAX_REORG_DEPTH", "145");
        vm.expectRevert("checkpoint ancestors or reorg limit"); script.run();
        vm.setEnv("MAX_REORG_DEPTH", "100");
        vm.setEnv("MAX_TIP_AGE_SECONDS", "86401");
        vm.expectRevert("tip age configuration"); script.run();
        assertEq(vm.getNonce(vm.addr(1)), nonce);
    }
}
