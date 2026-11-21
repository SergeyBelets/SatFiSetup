// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/core/BitcoinDepositDerivation.sol";

contract DepositDerivationHarness is BitcoinDepositDerivation {
    function addTweak(bytes32 base, uint256 tweak) external view returns (bytes32, bool) {
        return _addTweak(base, tweak);
    }
}

contract BitcoinDepositDerivationTest is Test {
    DepositDerivationHarness internal verifier;
    string internal fixture;

    function setUp() public {
        verifier = new DepositDerivationHarness();
        fixture = vm.readFile("test/fixtures/deposit-derivation-v1.json");
    }
    function _bytes(string memory path) internal view returns (bytes memory) {
        return vm.parseBytes(string.concat("0x", vm.parseJsonString(fixture, path)));
    }
    function _vector(uint256 index) internal returns (uint256 used) {
        string memory prefix = string.concat(".vectors[", vm.toString(index), "].");
        bytes memory intent = _bytes(string.concat(prefix, "intent"));
        bytes memory base = _bytes(string.concat(prefix, "base_group"));
        uint256 beforeGas = gasleft();
        BitcoinDepositDerivation.Descriptor memory d = verifier.derive(intent, base);
        used = beforeGas - gasleft();
        assertEq(d.intentId, bytes32(_bytes(string.concat(prefix, "intent_id"))));
        assertEq(d.merkleRoot, bytes32(_bytes(string.concat(prefix, "merkle_root"))));
        assertEq(d.tweak, bytes32(_bytes(string.concat(prefix, "tweak"))));
        assertEq(d.outputKey, bytes32(_bytes(string.concat(prefix, "output_key"))));
        assertEq(d.bindingCommitment, bytes32(_bytes(string.concat(prefix, "binding_commitment"))));
        assertEq(d.walletContext, bytes32(_bytes(string.concat(prefix, "wallet_context"))));
        assertEq(d.outputParityOdd, vm.parseJsonBool(fixture, string.concat(prefix, "output_parity_odd")));
        assertTrue(verifier.verify(intent, base, d.outputKey, d.outputParityOdd, d.bindingCommitment));
        assertFalse(verifier.QUALIFIED());
    }
    function testNativeVectorsBothParities() public {
        uint256 maximum;
        for (uint256 i; i < 16; ++i) {
            uint256 used = _vector(i);
            if (used > maximum) maximum = used;
        }
        emit log_named_uint("maximum local derive execution gas (no registry/issuance)", maximum);
        assertLt(maximum, 5_000_000);
    }
    function testEveryIntentFieldAndParityAreBound() public {
        bytes memory intent = _bytes(".vectors[0].intent");
        bytes memory base = _bytes(".vectors[0].base_group");
        BitcoinDepositDerivation.Descriptor memory d = verifier.derive(intent, base);
        uint256[14] memory offsets = [uint256(30), 62, 94, 114, 134, 154, 186, 206, 238, 270, 278, 298, 318, 350];
        for (uint256 i; i < offsets.length; ++i) {
            intent[offsets[i]] ^= 0x80;
            assertFalse(verifier.verify(intent, base, d.outputKey, d.outputParityOdd, d.bindingCommitment));
            intent[offsets[i]] ^= 0x80;
        }
        assertFalse(verifier.verify(intent, base, d.outputKey, !d.outputParityOdd, d.bindingCommitment));
        assertFalse(verifier.verify(intent, base, d.outputKey, d.outputParityOdd, bytes32(uint256(1))));
        assertFalse(verifier.verify(intent, base, bytes32(uint256(1)), d.outputParityOdd, d.bindingCommitment));
    }
    function testMalformedIntentAndBaseRejected() public {
        bytes memory intent = _bytes(".vectors[0].intent");
        bytes memory base = _bytes(".vectors[0].base_group");
        vm.expectRevert(BitcoinDepositDerivation.InvalidIntent.selector);
        verifier.derive(bytes.concat(intent, hex"00"), base);
        intent[26] = 0x01;
        vm.expectRevert(BitcoinDepositDerivation.InvalidIntent.selector);
        verifier.derive(intent, base);
        intent[26] = 0;
        for (uint256 i = 298; i < 318; ++i) intent[i] = 0;
        vm.expectRevert(BitcoinDepositDerivation.InvalidIntent.selector);
        verifier.derive(intent, base);
        intent = _bytes(".vectors[0].intent");
        base[0] = 0x03;
        vm.expectRevert(BitcoinDepositDerivation.InvalidBase.selector);
        verifier.derive(intent, base);
        vm.expectRevert(BitcoinDepositDerivation.InvalidBase.selector);
        verifier.derive(intent, abi.encodePacked(hex"02", bytes32(type(uint256).max)));
    }
    function testStrictScalarInfinityAndBaseAliasRejected() public {
        bytes32 generator = bytes32(Secp256k1.GX);
        vm.expectRevert(BitcoinDepositDerivation.InvalidTweak.selector);
        verifier.addTweak(generator, Secp256k1.N);
        vm.expectRevert(BitcoinDepositDerivation.InvalidTweak.selector);
        verifier.addTweak(generator, type(uint256).max);
        vm.expectRevert(BitcoinDepositDerivation.InvalidDerivedKey.selector);
        verifier.addTweak(generator, Secp256k1.N - 1);
        vm.expectRevert(BitcoinDepositDerivation.InvalidDerivedKey.selector);
        verifier.addTweak(generator, 0);
        vm.expectRevert(BitcoinDepositDerivation.InvalidDerivedKey.selector);
        verifier.addTweak(generator, Secp256k1.N - 2);
    }
}
