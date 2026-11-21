// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/core/BitcoinFundingParser.sol";
import "../contracts/src/core/BitcoinTxParser.sol";

contract FundingParserHarness {
    function parse(bytes calldata raw, uint32 vout) external pure returns (BitcoinFundingParser.Parsed memory) {
        return BitcoinFundingParser.parse(raw, vout);
    }

    // ABI with bounded return data for actual transaction gas measurements.
    function inspect(bytes calldata raw, uint32 vout)
        external pure returns (bytes32 txid, bytes32 inputsHash, uint256 inputs, uint256 outputs,
            uint64 amount, bytes32 scriptHash)
    {
        BitcoinFundingParser.Parsed memory result = BitcoinFundingParser.parse(raw, vout);
        return (result.txid, keccak256(abi.encode(result.inputs)), result.inputs.length,
            result.outputCount, result.amount, keccak256(result.scriptPubKey));
    }

    function legacy(bytes calldata raw, uint32 vout) external pure returns (bytes32, uint64, bytes memory) {
        (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, vout);
        return (BitcoinTxParser.txId(raw), amount, script);
    }
}

contract BitcoinFundingParserTest is Test {
    FundingParserHarness internal parser;

    function setUp() public { parser = new FundingParserHarness(); }

    function _hex(string memory value) internal pure returns (bytes memory) {
        return vm.parseBytes(string.concat("0x", value));
    }

    function _fixture(uint256 index) internal {
        string[9] memory names = ["mixed_inputs_outputs", "outputs_33", "outputs_256", "outputs_257",
            "inputs_257", "max_inputs_2438", "max_outputs_11105", "script_65536", "stripped_100000"];
        string memory json = vm.readFile(string.concat("test/fixtures/funding-stripped-v1/", names[index], ".json"));
        bytes memory raw = vm.parseBytes(vm.readFile(vm.parseJsonString(json, ".raw_file")));
        uint32 target = uint32(vm.parseJsonUint(json, ".target_vout"));
        string memory expected = ".expected.";
        uint256 beforeGas = gasleft();
        BitcoinFundingParser.Parsed memory result = parser.parse(raw, target);
        uint256 used = beforeGas - gasleft();
        emit log_named_uint(names[index], used);
        assertEq(result.txid, bytes32(_hex(vm.parseJsonString(json, string.concat(expected, "raw_sha256d")))));
        assertEq(result.inputs.length, vm.parseJsonUint(json, string.concat(expected, "input_count")));
        assertEq(result.outputCount, vm.parseJsonUint(json, string.concat(expected, "output_count")));
        assertEq(result.amount, vm.parseJsonUint(json, string.concat(expected, "target_value")));
        assertEq(result.scriptPubKey, _hex(vm.parseJsonString(json, string.concat(expected, "target_script_hex"))));
        assertEq(keccak256(abi.encode(result.inputs)),
            bytes32(_hex(vm.parseJsonString(json, string.concat(expected, "inputs_abi_keccak256")))));
        assertEq(raw.length, vm.parseJsonUint(json, string.concat(expected, "stripped_bytes")));
        // Local execution regression only, excluding custody storage, SPV,
        // issuance, and calldata intrinsic/floor gas. Receipts measured separately.
        assertLt(used, 15_000_000);
    }

    function testMixedReference() public { _fixture(0); }
    function test33Outputs() public { _fixture(1); }
    function test256Outputs() public { _fixture(2); }
    function test257Outputs() public { _fixture(3); }
    function test257Inputs() public { _fixture(4); }
    function testMaximum2438Inputs() public { _fixture(5); }
    function testMaximum11105Outputs() public { _fixture(6); }
    function testCanonical65536ByteScript() public { _fixture(7); }
    function testExact100000ByteEnvelope() public { _fixture(8); }

    function _le(uint256 value, uint256 size) internal pure returns (bytes memory raw) {
        raw = new bytes(size);
        for (uint256 i; i < size; ++i) raw[i] = bytes1(uint8(value >> (8 * i)));
    }

    function _compact(uint256 value) internal pure returns (bytes memory) {
        if (value < 253) return _le(value, 1);
        if (value < 65536) return bytes.concat(hex"fd", _le(value, 2));
        return bytes.concat(hex"fe", _le(value, 4));
    }

    function _tx(uint256 inputs, uint256 outputs, bytes memory script, uint64 amount)
        internal pure returns (bytes memory raw)
    {
        raw = bytes.concat(hex"02000000", _compact(inputs));
        for (uint256 i; i < inputs; ++i) {
            raw = bytes.concat(raw, abi.encodePacked(bytes32(i + 1)), _le(i, 4), hex"00ffffffff");
        }
        raw = bytes.concat(raw, _compact(outputs));
        for (uint256 i; i < outputs; ++i) {
            raw = bytes.concat(raw, _le(amount, 8), _compact(script.length), script);
        }
        return bytes.concat(raw, hex"00000000");
    }

    function _replace(bytes memory raw, uint256 offset, uint256 count, bytes memory replacement)
        internal pure returns (bytes memory result)
    {
        result = new bytes(raw.length - count + replacement.length);
        for (uint256 i; i < offset; ++i) result[i] = raw[i];
        for (uint256 i; i < replacement.length; ++i) result[offset + i] = replacement[i];
        for (uint256 i = offset + count; i < raw.length; ++i) {
            result[i - count + replacement.length] = raw[i];
        }
    }

    function testFuzzMatchesLegacyForSharedEnvelope(uint8 vin, uint8 vout, uint64 amount, bytes32 script) public view {
        uint256 inputs = uint256(vin % 16) + 1;
        uint256 outputs = uint256(vout % 16) + 1;
        bytes memory raw = _tx(inputs, outputs, abi.encodePacked(script), amount);
        uint32 target = uint32(outputs - 1);
        BitcoinFundingParser.Parsed memory result = parser.parse(raw, target);
        (bytes32 txid, uint64 oldAmount, bytes memory oldScript) = parser.legacy(raw, target);
        assertEq(result.txid, txid);
        assertEq(result.amount, oldAmount);
        assertEq(result.scriptPubKey, oldScript);
        for (uint256 i; i < inputs; ++i) {
            assertEq(result.inputs[i].txidLE, bytes32(i + 1));
            assertEq(result.inputs[i].vout, i);
        }
    }

    function testEveryTruncationRejected() public {
        bytes memory raw = _tx(2, 3, hex"5120", 1000);
        for (uint256 length; length < raw.length; ++length) {
            bytes memory prefix = _replace(raw, length, raw.length - length, hex"");
            vm.expectRevert();
            parser.parse(prefix, 0);
        }
    }

    function testTrailingBytesRejected() public {
        bytes memory raw = bytes.concat(_tx(1, 1, hex"51", 1), hex"00");
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        parser.parse(raw, 0);
    }

    function testWitnessSerializationRejected() public {
        bytes memory stripped = _tx(1, 1, hex"51", 1);
        // A real witness stack: one one-byte item, before nLockTime.
        bytes memory raw = _replace(stripped, stripped.length - 4, 0, hex"010151");
        raw = _replace(raw, 4, 0, hex"0001");
        vm.expectRevert(BitcoinFundingParser.InvalidFundingCount.selector);
        parser.parse(raw, 0);
        // The existing witness-aware extractor agrees on txid when given
        // the full serialization, while the new path accepts its stripped form.
        (bytes32 expected,,) = parser.legacy(raw, 0);
        assertEq(parser.parse(stripped, 0).txid, expected);
    }

    function testSelectedLongScriptCopiedExactly() public view {
        bytes memory raw = vm.parseBytes(vm.readFile("test/fixtures/funding-stripped-v1/script_65536.hex"));
        BitcoinFundingParser.Parsed memory result = parser.parse(raw, 0);
        assertEq(result.amount, 1234);
        assertEq(result.scriptPubKey.length, 65536);
        assertEq(sha256(result.scriptPubKey), hex"8972076cb336eb71044adb1be09e761ec0070ceb9e833595dedce38fd1478c2f");
    }

    function testLongInputScriptPreservesNextSequenceAndOutput() public view {
        bytes memory raw = bytes.concat(hex"0200000001", abi.encodePacked(bytes32(uint256(0x1234))),
            hex"02000000fe00000100", new bytes(65536), hex"7856341201e803000000000000015104030201");
        BitcoinFundingParser.Parsed memory result = parser.parse(raw, 0);
        assertEq(result.inputs.length, 1);
        assertEq(result.inputs[0].txidLE, bytes32(uint256(0x1234)));
        assertEq(result.inputs[0].vout, 2);
        assertEq(result.amount, 1000);
        assertEq(result.scriptPubKey, hex"51");
        assertEq(result.txid, sha256(abi.encodePacked(sha256(raw))));
    }

    function testCompactSizeCannotReadLocktimeBytes() public {
        bytes memory raw = _tx(1, 2, hex"51", 1);
        // Last output's script length begins at 65; only a partial length
        // field remains before the four locktime bytes, which are not data.
        bytes[3] memory endings = [bytes(hex"fd00"), bytes(hex"fe000001"), bytes(hex"ff00000000010000")];
        for (uint256 i; i < endings.length; ++i) {
            bytes memory changed = _replace(raw, 65, raw.length - 65, bytes.concat(endings[i], hex"00000000"));
            vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
            parser.parse(changed, 0);
        }
    }

    function testHugeDeclaredScriptLengthRejected() public {
        bytes memory raw = _replace(_tx(1, 1, hex"51", 1), 55, 1, hex"ffffffffffffffffff");
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        parser.parse(raw, 0);
    }

    function testZeroVectorCountsRejected() public {
        bytes memory raw = _tx(1, 1, hex"51", 1);
        raw[4] = 0;
        vm.expectRevert(BitcoinFundingParser.InvalidFundingCount.selector);
        parser.parse(raw, 0);
        raw[4] = 0x01;
        raw[46] = 0;
        vm.expectRevert(BitcoinFundingParser.InvalidFundingCount.selector);
        parser.parse(raw, 0);
    }

    function testUnknownOutputRejected() public {
        bytes memory raw = _tx(1, 1, hex"51", 1);
        vm.expectRevert(BitcoinFundingParser.FundingOutputIndex.selector);
        parser.parse(raw, 1);
    }

    function testNonCanonicalCompactSizeAtEveryVectorAndScript() public {
        bytes memory raw = _tx(1, 1, hex"51", 1);
        uint256[4] memory positions = [uint256(4), 41, 46, 55];
        for (uint256 i; i < positions.length; ++i) {
            bytes memory changed = _replace(raw, positions[i], 1,
                bytes.concat(hex"fd", _le(uint8(raw[positions[i]]), 2)));
            vm.expectRevert(BitcoinFundingParser.NonCanonicalCompactSize.selector);
            parser.parse(changed, 0);
        }
        bytes memory fe = _replace(raw, 4, 1, hex"fe01000000");
        vm.expectRevert(BitcoinFundingParser.NonCanonicalCompactSize.selector);
        parser.parse(fe, 0);
        bytes memory ff = _replace(raw, 4, 1, hex"ff0100000000000000");
        vm.expectRevert(BitcoinFundingParser.NonCanonicalCompactSize.selector);
        parser.parse(ff, 0);
    }

    function testDeclaredCountBoundedBeforeAllocation() public {
        bytes memory raw = _replace(_tx(1, 1, hex"51", 1), 4, 1, hex"ffffffffffffffffff");
        vm.expectRevert(BitcoinFundingParser.InvalidFundingCount.selector);
        parser.parse(raw, 0);
    }

    function testAllInputsAndOutputsStructurallyChecked() public {
        // The first requested output is fine; a malformed later output still
        // invalidates the whole serialization. No early outputAt return.
        bytes memory raw = _tx(1, 2, hex"51", 1);
        raw[65] = 0x7f;
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        parser.parse(raw, 0);
        raw = _tx(2, 1, hex"51", 1);
        raw[82] = 0x7f;
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        parser.parse(raw, 0);
    }

    function testOutOfEnvelopeRejected() public {
        bytes memory raw = new bytes(100001);
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        parser.parse(raw, 0);
    }
}
