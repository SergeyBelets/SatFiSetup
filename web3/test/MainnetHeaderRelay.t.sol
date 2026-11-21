// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";
import "../contracts/src/federation/RegtestHeaderRelay.sol";

contract MainnetHeaderRelayTest is MainnetFixtureBase {
    MainnetHeaderRelay internal relay;

    function setUp() public {
        loadMainnetFixture();
        relay = newRelay(100);
    }

    function testRealMainnetAcrossTwoRetargetsEveryHashBitsAndChainwork() public {
        // Cache fixture arrays in memory: avoid copying the entire storage fixture on every iteration.
        bytes memory headers = realHeaders;
        bytes memory expected = expectedRecords;
        for (uint64 height = CHECKPOINT; height <= LAST; ++height) {
            // Every header is an independent transaction in use. Refill only the test driver's gas
            // periodically; each metered relay call retains an explicit one-million-gas ceiling.
            if ((height - CHECKPOINT) % 128 == 0) vm.resetGasMetering();
            uint256 index = height - FIRST;
            bytes memory raw = slice(headers, index * 80, 80);
            bytes32 hash = digest(raw);
            if (height > CHECKPOINT) {
                if (height % 2016 == 0) {
                    bytes memory badBits = slice(headers, index * 80, 80);
                    uint32 previousBits = expectedBits(height - 1);
                    write32(badBits, 72, previousBits);
                    vm.expectRevert(
                        abi.encodeWithSelector(
                            MainnetHeaderRelay.InvalidDifficultyBits.selector, previousBits, expectedBits(height)
                        )
                    );
                    relay.addBlockHeader(badBits);
                }
                relay.addBlockHeader{gas: 1000000}(raw);
            }
            bytes32 expectedHash;
            uint256 work;
            uint32 bits;
            assembly ("memory-safe") {
                let at := add(add(expected, 32), mul(index, 68))
                expectedHash := mload(at)
                work := mload(add(at, 32))
                bits := shr(224, mload(add(at, 64)))
            }
            (,, uint256 storedWork, uint64 storedHeight,, uint32 storedBits) = relay.blocks(hash);
            assertEq(BitcoinTxParser.flipBytes32(hash), expectedHash);
            assertEq(storedWork, work);
            assertEq(storedHeight, height);
            assertEq(storedBits, bits);
        }
        assertEq(relay.getMainchainHead(), wireHash(LAST));
        assertEq(relay.getMainchainHeight(), LAST);
        (bool canonical, uint64 confirmations) = relay.getBlockStatus(wireHash(CHECKPOINT));
        assertTrue(canonical);
        assertEq(confirmations, 4033);
        assertEq(relay.height2hash(CHECKPOINT + 2016), wireHash(CHECKPOINT + 2016));
        assertFalse(relay.halted());
    }

    function testCheckpointWorkIncludesCheckpointExactlyOnce() public view {
        (,, uint256 work, uint64 height,,) = relay.blocks(wireHash(CHECKPOINT));
        assertEq(work, expectedWork(CHECKPOINT));
        assertEq(height, CHECKPOINT);
        (bool canonical, uint64 confirmations) = relay.getBlockStatus(wireHash(CHECKPOINT));
        assertTrue(canonical);
        assertEq(confirmations, 1);
    }

    function testCheckpointRejectsInvalidHeightWorkHeaderAndLimit() public {
        bytes memory raw = headerAt(CHECKPOINT);
        bytes[] memory prior = checkpointAncestors();
        vm.expectRevert(MainnetHeaderRelay.InvalidCheckpoint.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT + 1, expectedWork(CHECKPOINT), prior, 100);
        vm.expectRevert(MainnetHeaderRelay.InvalidCheckpoint.selector);
        new MainnetHeaderRelay(raw, 0, expectedWork(CHECKPOINT), prior, 100);
        vm.expectRevert(MainnetHeaderRelay.InvalidCheckpointWork.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, 1, prior, 100);
        vm.expectRevert(MainnetHeaderRelay.InvalidReorgLimit.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), prior, 0);
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderLength.selector);
        new MainnetHeaderRelay(new bytes(79), CHECKPOINT, expectedWork(CHECKPOINT), prior, 100);
        raw[79] = bytes1(uint8(raw[79]) ^ 1);
        vm.expectRevert(MainnetHeaderRelay.InvalidProofOfWork.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), prior, 100);
    }

    function testCheckpointRejectsBrokenAncestorChainAndWrongCount() public {
        bytes memory raw = headerAt(CHECKPOINT);
        bytes[] memory prior = checkpointAncestors();
        vm.expectRevert(MainnetHeaderRelay.InvalidAncestors.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), new bytes[](9), 100);
        prior[4][79] = bytes1(uint8(prior[4][79]) ^ 1);
        vm.expectRevert(MainnetHeaderRelay.InvalidAncestors.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), prior, 100);
        prior = checkpointAncestors();
        prior[9][79] = bytes1(uint8(prior[9][79]) ^ 1);
        vm.expectRevert(MainnetHeaderRelay.InvalidAncestors.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), prior, 100);
        prior = checkpointAncestors();
        prior[0] = new bytes(81);
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderLength.selector);
        new MainnetHeaderRelay(raw, CHECKPOINT, expectedWork(CHECKPOINT), prior, 100);
    }

    function testCheckpointAncestorsCannotReceiveSPVConfirmationsOrBecomeParents() public {
        bytes32 hash = wireHash(CHECKPOINT - 1);
        (bool canonical, uint64 count) = relay.getBlockStatus(hash);
        assertFalse(canonical);
        assertEq(count, 0);
        assertFalse(relay.checkTxInclusion(hash, bytes32(0), new bytes32[](0), 0));
        assertEq(relay.height2hash(CHECKPOINT - 1), bytes32(0));
        bytes memory raw = headerAt(CHECKPOINT);
        raw[79] = bytes1(uint8(raw[79]) ^ 1);
        vm.expectRevert(abi.encodeWithSelector(MainnetHeaderRelay.PrevNotFound.selector, hash));
        relay.addBlockHeader(raw);
    }

    function testNoHelpersNoAdministratorAndSepoliaDeploymentAllowed() public {
        assertFalse(relay.testHelpersEnabled());
        (bool ok,) = address(relay).call(abi.encodeWithSignature("addBlockWithHash(bytes32,uint64)", bytes32(0), 1));
        assertFalse(ok);
        (ok,) = address(relay).call(abi.encodeWithSignature("spvAdmin()"));
        assertFalse(ok);
        vm.chainId(11155111);
        MainnetHeaderRelay sepolia = newRelay(100);
        assertFalse(sepolia.testHelpersEnabled());
        assertEq(sepolia.POW_LIMIT(), relay.POW_LIMIT());
    }

    function testDuplicateAndBadLengthDoNotChangeState() public {
        bytes32 checkpoint = relay.getMainchainHead();
        vm.expectRevert(abi.encodeWithSelector(MainnetHeaderRelay.BlockAlreadyExists.selector, checkpoint));
        relay.addBlockHeader(headerAt(CHECKPOINT));
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderLength.selector);
        relay.addBlockHeader(new bytes(81));
        relay.addBlockHeader(headerAt(CHECKPOINT + 1));
        bytes32 child = relay.getMainchainHead();
        vm.expectRevert(abi.encodeWithSelector(MainnetHeaderRelay.BlockAlreadyExists.selector, child));
        relay.addBlockHeader(headerAt(CHECKPOINT + 1));
        (,, uint256 work,,,) = relay.blocks(child);
        assertEq(work, expectedWork(CHECKPOINT + 1));
        assertEq(relay.getMainchainHead(), child);
    }

    function testBatchOverlapAndExactRetryDoNotAddWorkOrEvents() public {
        relay.addBlockHeader(headerAt(CHECKPOINT + 1));
        bytes[] memory batch = new bytes[](3);
        for (uint64 i; i < 3; ++i) batch[i] = headerAt(CHECKPOINT + i);
        vm.recordLogs();
        relay.addBlockHeaders(batch);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs.length, 2); // One new header and one selected head.
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT + 2));
        (,, uint256 work,,,) = relay.blocks(wireHash(CHECKPOINT + 2));
        assertEq(work, expectedWork(CHECKPOINT + 2));
        vm.recordLogs();
        relay.addBlockHeaders(batch);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT + 2));
        (,, uint256 afterWork,,,) = relay.blocks(wireHash(CHECKPOINT + 2));
        assertEq(afterWork, work);
        assertEq(relay.height2hash(CHECKPOINT + 1), wireHash(CHECKPOINT + 1));
    }

    function testBatchKnownPrefixDoesNotHideInvalidUnknownOrMalformedHeader() public {
        bytes[] memory batch = new bytes[](3);
        for (uint64 i; i < 3; ++i) batch[i] = headerAt(CHECKPOINT + i);
        batch[2][79] = bytes1(uint8(batch[2][79]) ^ 1);
        vm.expectRevert(MainnetHeaderRelay.InvalidProofOfWork.selector);
        relay.addBlockHeaders(batch);
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT));
        (,,, uint64 height,,) = relay.blocks(wireHash(CHECKPOINT + 1));
        assertEq(height, 0);
        batch[2] = new bytes(81);
        vm.expectRevert(MainnetHeaderRelay.InvalidHeaderLength.selector);
        relay.addBlockHeaders(batch);
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT));
    }

    function testRejectsWrongBitsPoWUnknownParentAndMainnetLimit() public {
        bytes memory raw = headerAt(CHECKPOINT + 1);
        write32(raw, 72, 0x1d00ffff);
        vm.expectRevert(
            abi.encodeWithSelector(
                MainnetHeaderRelay.InvalidDifficultyBits.selector, uint32(0x1d00ffff), expectedBits(CHECKPOINT)
            )
        );
        relay.addBlockHeader(raw);
        raw = headerAt(CHECKPOINT + 1);
        write32(raw, 72, 0x207fffff);
        vm.expectRevert(MainnetHeaderRelay.TargetAboveMainnetLimit.selector);
        relay.addBlockHeader(raw);
        raw = headerAt(CHECKPOINT + 1);
        write32(raw, 72, 0x1d80ffff);
        vm.expectRevert(abi.encodeWithSelector(TargetsHelper.InvalidCompactTarget.selector, uint32(0x1d80ffff)));
        relay.addBlockHeader(raw);
        raw = headerAt(CHECKPOINT + 1);
        raw[79] = bytes1(uint8(raw[79]) ^ 1);
        bytes32 invalidHash = digest(raw);
        vm.expectRevert(MainnetHeaderRelay.InvalidProofOfWork.selector);
        relay.addBlockHeader(raw);
        (,, uint256 work, uint64 h,,) = relay.blocks(invalidHash);
        assertEq(work, 0);
        assertEq(h, 0);
        raw = headerAt(CHECKPOINT + 2);
        vm.expectRevert(abi.encodeWithSelector(MainnetHeaderRelay.PrevNotFound.selector, wireHash(CHECKPOINT + 1)));
        relay.addBlockHeader(raw);
        assertEq(relay.getMainchainHead(), wireHash(CHECKPOINT));
        assertFalse(relay.halted());
    }

    function testTimeEqualsMedianRejectedAndFutureBoundaryInclusive() public {
        MainnetRelayHarness h = newHarness(100);
        uint32 median = h.medianTimePast(wireHash(CHECKPOINT));
        assertEq(median, 1262150214); // Independently exported Bitcoin Core MTP.
        bytes memory raw = headerAt(CHECKPOINT + 1);
        write32(raw, 68, median);
        vm.expectRevert(MainnetHeaderRelay.InvalidMedianTime.selector);
        relay.addBlockHeader(raw);
        h.validateTime(median + 1, wireHash(CHECKPOINT));
        raw = headerAt(CHECKPOINT + 1);
        vm.warp(timestamp(raw) - 7201);
        vm.expectRevert(MainnetHeaderRelay.TooFarInFuture.selector);
        relay.addBlockHeader(raw);
        vm.warp(timestamp(raw) - 7200);
        relay.addBlockHeader(raw);
        assertEq(relay.getMainchainHead(), digest(raw));
    }

    function testChainworkOverflowRevertsWithoutPartialHeader() public {
        MainnetHeaderRelay full =
            new MainnetHeaderRelay(headerAt(CHECKPOINT), CHECKPOINT, type(uint256).max, checkpointAncestors(), 100);
        bytes memory raw = headerAt(CHECKPOINT + 1);
        vm.expectRevert(stdError.arithmeticError);
        full.addBlockHeader(raw);
        (,, uint256 work, uint64 height,,) = full.blocks(digest(raw));
        assertEq(work, 0);
        assertEq(height, 0);
        assertFalse(full.halted());
    }

    function testPoWComparisonAcceptsEqualityAndNeighborsInWireOrder() public {
        MainnetRelayHarness h = newHarness(100);
        uint256 target = TargetsHelper.bitsToTarget(0x1d00d86a);
        assertTrue(h.hashMeetsTarget(BitcoinTxParser.flipBytes32(bytes32(target - 1)), target));
        assertTrue(h.hashMeetsTarget(BitcoinTxParser.flipBytes32(bytes32(target)), target));
        assertFalse(h.hashMeetsTarget(BitcoinTxParser.flipBytes32(bytes32(target + 1)), target));
        assertTrue(h.hashMeetsTarget(wireHash(CHECKPOINT), target));
        assertFalse(h.hashMeetsTarget(BitcoinTxParser.flipBytes32(wireHash(CHECKPOINT)), target));
    }

    function testMerkleProofLengthIndexAndByteOrder() public {
        (, bytes32 root,,,,) = relay.blocks(wireHash(CHECKPOINT));
        assertTrue(relay.checkTxInclusion(wireHash(CHECKPOINT), root, new bytes32[](0), 0));
        assertFalse(relay.checkTxInclusion(wireHash(CHECKPOINT), root, new bytes32[](0), 1));
        assertFalse(relay.checkTxInclusion(wireHash(CHECKPOINT), root, new bytes32[](33), 0));
        assertFalse(
            relay.checkTxInclusion(wireHash(CHECKPOINT), BitcoinTxParser.flipBytes32(root), new bytes32[](0), 0)
        );
        assertFalse(relay.checkTxInclusion(bytes32(uint256(1)), root, new bytes32[](0), 0));
        MainnetRelayHarness h = newHarness(100);
        bytes32 leaf = sha256("transaction");
        bytes32 sibling = sha256("sibling");
        bytes32 parent = digest(abi.encodePacked(sibling, leaf));
        bytes32 blockHash = h.syntheticChild(wireHash(CHECKPOINT), 1, parent, 1300000000);
        bytes32[] memory proof = new bytes32[](1);
        proof[0] = sibling;
        assertTrue(h.checkTxInclusion(blockHash, leaf, proof, 1));
        assertFalse(h.checkTxInclusion(blockHash, leaf, proof, 0));
        assertFalse(h.checkTxInclusion(blockHash, leaf, proof, 3));
        proof[0] = BitcoinTxParser.flipBytes32(sibling);
        assertFalse(h.checkTxInclusion(blockHash, leaf, proof, 1));
    }

    function testReorgExactly100AcceptedAfterTieAndLessWorkPreserveHead() public {
        MainnetRelayHarness h = newHarness(100);
        bytes32 checkpoint = h.checkpointHash();
        bytes32 oldHead = checkpoint;
        for (uint32 i; i < 100; ++i) {
            oldHead = h.syntheticChild(oldHead, 1, bytes32(0), 1300000000 + i);
        }
        bytes32 fork = checkpoint;
        for (uint32 i; i < 100; ++i) {
            fork = h.syntheticChild(fork, 1, bytes32(uint256(1)), 1300001000 + i);
            assertEq(h.getMainchainHead(), oldHead);
            assertFalse(h.halted());
        }
        fork = h.syntheticChild(fork, 1, bytes32(uint256(1)), 1300002000);
        assertEq(h.getMainchainHead(), fork);
        assertFalse(h.halted());
        (bool canonical, uint64 count) = h.getBlockStatus(oldHead);
        assertFalse(canonical);
        assertEq(count, 0);
        (canonical, count) = h.getBlockStatus(checkpoint);
        assertTrue(canonical);
        assertEq(count, 102);
        assertEq(h.height2hash(CHECKPOINT + 100), h.ancestor(fork, CHECKPOINT + 100));
    }

    function testReorg101PersistsHaltAndDiagnosticsButDisablesAllProofs() public {
        MainnetRelayHarness h = newHarness(100);
        bytes32 checkpoint = h.checkpointHash();
        bytes32 oldHead = checkpoint;
        for (uint32 i; i < 101; ++i) {
            oldHead = h.syntheticChild(oldHead, 1, bytes32(0), 1300000000 + i);
        }
        bytes32 fork = checkpoint;
        for (uint32 i; i < 101; ++i) {
            fork = h.syntheticChild(fork, 1, bytes32(uint256(1)), 1300001000 + i);
        }
        assertFalse(h.halted());
        assertEq(h.getMainchainHead(), oldHead);
        vm.recordLogs();
        bytes32 candidate = h.syntheticChild(fork, 1, bytes32(uint256(1)), 1300002000);
        assertTrue(h.halted());
        assertEq(h.getMainchainHead(), oldHead);
        assertEq(h.getMainchainHeight(), CHECKPOINT + 101);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(logs.length, 1);
        assertEq(logs[0].topics[0], keccak256("RelayHalted(bytes32,bytes32,uint64,bytes32)"));
        assertEq(logs[0].topics[1], oldHead);
        assertEq(logs[0].topics[2], candidate);
        (uint64 depth, bytes32 reason) = abi.decode(logs[0].data, (uint64, bytes32));
        assertEq(depth, 101);
        assertEq(reason, keccak256("MAX_REORG_DEPTH_EXCEEDED"));
        assertEq(h.height2hash(CHECKPOINT + 101), oldHead);
        (bool canonical, uint64 count) = h.getBlockStatus(checkpoint);
        assertFalse(canonical);
        assertEq(count, 0);
        (canonical, count) = h.getBlockStatus(oldHead);
        assertFalse(canonical);
        assertEq(count, 0);
        (, bytes32 root,,,,) = h.blocks(checkpoint);
        assertFalse(h.checkTxInclusion(checkpoint, root, new bytes32[](0), 0));
        vm.expectRevert(MainnetHeaderRelay.RelayIsHalted.selector);
        h.addBlockHeader(headerAt(CHECKPOINT + 1));
        bytes[] memory known = new bytes[](1);
        known[0] = headerAt(CHECKPOINT);
        vm.expectRevert(MainnetHeaderRelay.RelayIsHalted.selector);
        h.addBlockHeaders(known);
    }

    function testShorterHeavierChainRemovesOldTailHeightMapping() public {
        MainnetRelayHarness h = newHarness(100);
        bytes32 oldHead = h.checkpointHash();
        for (uint32 i; i < 100; ++i) {
            oldHead = h.syntheticChild(oldHead, 1, bytes32(0), 1300000000 + i);
        }
        bytes32 shorter = h.syntheticChild(h.checkpointHash(), 101, bytes32(uint256(1)), 1300001000);
        assertEq(h.getMainchainHead(), shorter);
        assertEq(h.getMainchainHeight(), CHECKPOINT + 1);
        assertEq(h.height2hash(CHECKPOINT + 2), bytes32(0));
        assertEq(h.height2hash(CHECKPOINT + 100), bytes32(0));
        (bool canonical, uint64 count) = h.getBlockStatus(oldHead);
        assertFalse(canonical);
        assertEq(count, 0);
        assertFalse(h.halted());
    }

    function testHaltCostBoundedFor8192BlockCompetingTail() public {
        MainnetRelayHarness h = newHarness(100);
        bytes32 oldHead = h.checkpointHash();
        for (uint32 i; i < 101; ++i) {
            oldHead = h.syntheticChild(oldHead, 100000, bytes32(0), 1300000000 + i);
        }
        bytes32 fork = h.checkpointHash();
        for (uint32 i; i < 8192; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            fork = h.syntheticChild(fork, 1, bytes32(uint256(1)), 1300100000 + i);
        }
        assertFalse(h.halted());
        assertEq(h.getMainchainHead(), oldHead);
        uint256 beforeGas = gasleft();
        h.syntheticChild(fork, 11000000, bytes32(uint256(1)), 1300200000);
        uint256 used = beforeGas - gasleft();
        emit log_named_uint("bounded halt including header storage gas", used);
        assertLt(used, 1000000);
        assertTrue(h.halted());
        assertEq(h.getMainchainHead(), oldHead);
    }

    function testBranchSpecificMedianAndRetargetAcrossPeriod() public {
        MainnetRelayHarness h = newHarness(100);
        bytes32 canonical = h.checkpointHash();
        bytes32 fork = canonical;
        uint32 baseTime = timestamp(headerAt(CHECKPOINT));
        for (uint32 i = 1; i <= 2015; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            canonical = h.syntheticChild(canonical, 100, bytes32(0), baseTime + i * 600);
        }
        for (uint32 i = 1; i <= 2015; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            fork = h.syntheticChild(fork, 1, bytes32(uint256(1)), baseTime + i * 500);
        }
        uint256 target = TargetsHelper.bitsToTarget(expectedBits(CHECKPOINT));
        uint32 mainExpected =
            TargetsHelper.targetToBits(TargetsHelper.retargetWithLimit(target, 2015 * 600, h.POW_LIMIT()));
        uint32 forkExpected =
            TargetsHelper.targetToBits(TargetsHelper.retargetWithLimit(target, 2015 * 500, h.POW_LIMIT()));
        assertTrue(mainExpected != forkExpected);
        assertEq(h.expectedBits(canonical, CHECKPOINT + 2016), mainExpected);
        assertEq(h.expectedBits(fork, CHECKPOINT + 2016), forkExpected);
        assertEq(h.medianTimePast(canonical), baseTime + 2010 * 600);
        assertEq(h.medianTimePast(fork), baseTime + 2010 * 500);
        h.validateTime(baseTime + 2010 * 500 + 1, fork);
        vm.expectRevert(MainnetHeaderRelay.InvalidMedianTime.selector);
        h.validateTime(baseTime + 2010 * 500 + 1, canonical);
    }

    function testRegtestConsumerStillAcceptsItsGenesisAndExactWorkTwo() public {
        vm.chainId(31337);
        RegtestHeaderRelay regtest = new RegtestHeaderRelay();
        bytes memory raw =
            hex"0100000000000000000000000000000000000000000000000000000000000000000000003ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4adae5494dffff7f2002000000";
        regtest.addBlockHeader(raw);
        assertEq(regtest.getMainchainHead(), hex"06226e46111a0b59caaf126043eb5bbf28c34f3a5e332a1fc7b2b73cf188910f");
        (,,,,,,, uint256 work) = regtest.blocks(digest(raw));
        assertEq(work, 2);
    }
}
