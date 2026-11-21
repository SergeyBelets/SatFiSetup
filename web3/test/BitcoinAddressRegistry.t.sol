// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinMintAdmission.t.sol";
import "./EscrowBoundary.t.sol";
import "../contracts/src/federation/BitcoinAddressRegistry.sol";

// UNIT BOUNDARIES: completed DKG records, FROST approvals and SPV are fixtures.
// Real derivation arithmetic, authorization target/one-use checks, registry,
// pool token minting, backing and status state machines execute normally.
contract AddressDkgFixture {
    bytes32 public constant AUTH_ID = keccak256("address fixture EVM epoch");
    bytes32 public constant BTC_ID = keccak256("address fixture BTC epoch");
    bytes32 public constant AUTH_RESULT = keccak256("address fixture EVM result");
    bytes32 public constant BTC_RESULT = keccak256("address fixture BTC result");
    bytes32 public constant BASE_X = 0x5a82597fe64c2c1b2ed8bec38257a704d289064ce8eb4452392c9ca8a7593fb7;
    bytes32 public constant AUTH_X = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;
    address[] private members;
    constructor(address[] memory roster) { members = roster; }
    function isSessionParticipant(bytes32 id,address who) external view returns(bool) {
        if(id != BTC_ID && id != AUTH_ID)return false;
        for(uint256 i;i<members.length;++i)if(members[i] == who)return true;
        return false;
    }
    function getCompletedResult(bytes32 id) external view returns
        (bytes32, bytes32, uint64, bytes32, bytes memory, address[] memory, uint256)
    {
        require(id == AUTH_ID || id == BTC_ID, "fixture DKG id");
        bool btc = id == BTC_ID;
        return (id, btc ? BTC_RESULT : AUTH_RESULT, 9, btc ? keccak256("BTC_SPEND") : keccak256("EVM_AUTH"),
            abi.encodePacked(btc ? BASE_X : AUTH_X), members, 2);
    }
    function getPublicKeySharePoint(bytes32 id, address) external pure returns (uint256, uint256) {
        require(id == AUTH_ID, "fixture EVM epoch");
        return (uint256(AUTH_X), 0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8);
    }
}

// TEST ONLY. This wrapper supplies a synthetic qualification flag while
// delegating every derivation to the real, unqualified arithmetic candidate.
// It is not deployment code or evidence of independent verifier qualification.
contract TestQualifiedDepositDerivation {
    bool public constant QUALIFIED = true;
    bytes32 public constant PROFILE = keccak256("BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1");
    BitcoinDepositDerivation public immutable candidate;
    constructor(BitcoinDepositDerivation impl) { candidate = impl; }
    function derive(bytes calldata intent, bytes calldata group) external view
        returns (BitcoinDepositDerivation.Descriptor memory) { return candidate.derive(intent, group); }
}

contract BitcoinAddressRegistryTest is Test {
    MintAdmissionAuthorizationFixture internal auth;
    AddressDkgFixture internal dkg;
    BitcoinBatchRegistry internal batches;
    EscrowProofFixture internal proof;
    BitcoinWithdrawalEscrow internal escrow;
    BitcoinDepositRegistry internal common;
    MiningPoolV1 internal pool;
    BitcoinAddressRegistry internal addresses;
    bytes32 internal constant GENESIS = keccak256("explicit test Bitcoin genesis");
    bytes32 internal constant SERVICE = keccak256("ordinary no-code recipient fixture policy");
    address internal constant REQUESTER = address(0x1234);
    address internal constant RECIPIENT = address(0x5678);

    function _approve(bytes32 op, address target) internal { auth.fixtureApprove(op, target); }
    function setUp() public virtual { _deploy(true); }
    function _deploy(bool syntheticQualified) internal {
        address[] memory members = new address[](3);
        members[0] = address(0xa1); members[1] = address(0xa2); members[2] = address(0xa3);
        dkg = new AddressDkgFixture(members);
        FROSTCoordinator coordinator = FROSTCoordinator(deployCode("FROSTCoordinator.sol:FROSTCoordinator",
            abi.encode(address(new MintAdmissionVerifierFixture()))));
        coordinator.bindDKGRegistry(address(dkg));
        auth = MintAdmissionAuthorizationFixture(deployCode("BitcoinMintAdmission.t.sol:MintAdmissionAuthorizationFixture",
            abi.encode(address(coordinator), dkg.AUTH_ID(), abi.encodePacked(dkg.AUTH_X()), members)));
        batches = BitcoinBatchRegistry(deployCode("BitcoinBatchRegistry.sol:BitcoinBatchRegistry",
            abi.encode(address(auth), address(dkg), dkg.BTC_ID())));
        proof = new EscrowProofFixture();
        escrow = BitcoinWithdrawalEscrow(deployCode("BitcoinWithdrawalEscrow.sol:BitcoinWithdrawalEscrow",
            abi.encode(address(batches), address(proof), uint32(6), uint256(294), uint256(330))));
        bytes32 op = batches.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(op, address(batches)); batches.setWithdrawalEscrow(address(escrow), op);
        common = BitcoinDepositRegistry(deployCode("BitcoinDepositRegistry.sol:BitcoinDepositRegistry",
            abi.encode(address(proof), address(auth), uint32(6), address(escrow))));
        op = escrow.getSetDepositRegistryOperationHash(address(common));
        _approve(op, address(escrow)); escrow.setDepositRegistry(address(common), op);
        BitcoinDepositDerivation candidate = new BitcoinDepositDerivation();
        address verifier = syntheticQualified ? address(new TestQualifiedDepositDerivation(candidate)) : address(candidate);
        addresses = BitcoinAddressRegistry(deployCode("BitcoinAddressRegistry.sol:BitcoinAddressRegistry",
            abi.encode(address(common), GENESIS, verifier, verifier.codehash,
                keccak256("TEST ONLY source pin"), keccak256("TEST ONLY qualification"), SERVICE)));
        BitcoinKeyFamilyRegistry family = BitcoinKeyFamilyRegistry(deployCode("BitcoinKeyFamilyRegistry.sol:BitcoinKeyFamilyRegistry",abi.encode(address(auth))));
        op=family.getEnrollOperationHash(address(addresses));_approve(op,address(family));family.enroll(address(addresses),op);
        op=addresses.getBindFamilyRegistryOperationHash(address(family));_approve(op,address(addresses));addresses.bindFamilyRegistry(address(family),op);
        op = common.getSetAddressRegistryOperationHash(address(addresses));
        _approve(op, address(common)); common.setAddressRegistry(address(addresses), op);
        pool = MiningPoolV1(deployCode("MiningPoolV1.sol:MiningPoolV1",
            abi.encode("address-unit", address(common), address(escrow), "MP", "MP")));
        op = common.getRegisterOperationHash(address(pool), pool.payoutScript());
        _approve(op, address(common)); common.register(address(pool), pool.payoutScript(), op);
        if (syntheticQualified) _status(1, 0, false);
    }
    function _change(uint8 scope, uint8 next, bool close) internal view returns (BitcoinAddressRegistry.StatusChange memory) {
        return BitcoinAddressRegistry.StatusChange(scope,
            scope == 0 ? uint8(addresses.custodyState()) : uint8(addresses.derivationState()),
            scope == 0 ? addresses.custodyGeneration() : addresses.derivationGeneration(),
            scope == 0 ? addresses.custodyStatusNonce() : addresses.derivationStatusNonce(), next, close,
            uint64(block.timestamp + 3600), BitcoinAddressRegistry.IncidentReport(
                keccak256("fixture incident"), keccak256("fixture reason"), keccak256("fixture evidence"), 0, 0, 0));
    }
    function _status(uint8 scope, uint8 next, bool close) internal {
        BitcoinAddressRegistry.StatusChange memory c = _change(scope, next, close);
        bytes32 op = addresses.getStatusOperationHash(c);
        _approve(op, address(addresses)); addresses.setStatus(c, op);
    }
    function _intent(uint256 requestNonce, address recipient) internal view returns (bytes memory) {
        return abi.encodePacked("SATOHSI_DEPOSIT_INTENT_V1\x00", uint16(1), uint16(1), GENESIS,
            uint256(block.chainid), address(addresses), address(pool), address(pool.mpToken()), addresses.graphId(),
            address(dkg), dkg.BTC_ID(), dkg.BTC_RESULT(), uint64(9), REQUESTER, recipient, bytes32(requestNonce), SERVICE);
    }
    function _register(uint256 requestNonce) internal returns (bytes32 id) {
        bytes[] memory raw = new bytes[](1); raw[0] = _intent(requestNonce, RECIPIENT);
        _registerBatch(raw);
        return sha256(raw[0]);
    }
    function testRegistrationHelperAndFrozenPythonVector() public {
        bytes[] memory raw = new bytes[](1); raw[0] = _intent(7, RECIPIENT);
        bytes32 actualParams = keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_REGISTER_V1"),
            addresses.graphId(), addresses.derivationScope(), raw, uint256(3), uint256(5), uint64(2000000000)));
        assertEq(addresses.getRegisterOperationHash(raw, 3, 5, 2000000000, 9),
            auth.hashOperation(address(addresses), addresses.registerBatch.selector, actualParams, 9));
        string memory data = vm.readFile("test/fixtures/address-registration-operations-v1.json");
        raw[0] = vm.parseBytes(string.concat("0x", vm.parseJsonString(data, ".intents[0]")));
        bytes32 params = keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_REGISTER_V1"),
            vm.parseJsonBytes32(data, ".graph_id"), vm.parseJsonBytes32(data, ".derivation_scope"),
            raw, uint256(3), uint256(5), uint64(2000000000)));
        assertEq(params, vm.parseJsonBytes32(data, ".params_hash"));
        address frozenAuth = vm.parseJsonAddress(data, ".authorization");
        vm.etch(frozenAuth, address(auth).code); vm.chainId(vm.parseJsonUint(data, ".chain_id"));
        assertEq(FROSTAuthorization(frozenAuth).hashOperation(vm.parseJsonAddress(data, ".registry"),
            addresses.registerBatch.selector, params, 9), vm.parseJsonBytes32(data, ".operation_hash"));
        assertEq(keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_REQUEST_COMMITMENT_V2"),
            vm.parseJsonBytes32(data, ".request.domain"), sha256(raw[0]),
            vm.parseJsonBytes32(data, ".request.opening"), vm.parseJsonAddress(data, ".request.requester"),
            vm.parseJsonAddress(data, ".request.service"))), vm.parseJsonBytes32(data, ".request.request_id"));
        bytes32[] memory ids = new bytes32[](1); ids[0] = sha256(raw[0]);
        assertEq(keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_REGISTRATION_BATCH_V1"),
            vm.parseJsonAddress(data, ".registry"), ids)), vm.parseJsonBytes32(data, ".batch_id"));
    }
    function _registerBatch(bytes[] memory raw) internal returns (bytes32 op) {
        uint256 cg = addresses.custodyGeneration(); uint256 dg = addresses.derivationGeneration();
        uint64 deadline = uint64(block.timestamp + 3600); uint256 sequence = addresses.registrationNonce();
        op = addresses.getRegisterOperationHash(raw, cg, dg, deadline, sequence);
        _approve(op, address(addresses)); addresses.registerBatch(raw, cg, dg, deadline, sequence, op);
    }
    function _script(bytes32 id) internal view returns (bytes memory) {
        return abi.encodePacked(hex"5120", addresses.getBinding(id).descriptor.outputKey);
    }
    function _le(uint256 n, uint256 size) internal pure returns (bytes memory raw) {
        raw = new bytes(size); for (uint256 i; i < size; ++i) raw[i] = bytes1(uint8(n >> (8 * i)));
    }
    function _compact(uint256 n) internal pure returns (bytes memory) {
        return n < 253 ? _le(n, 1) : bytes.concat(hex"fd", _le(n, 2));
    }
    function _funding(uint256 inputs, uint256 outputs, uint32 target, bytes memory script,
        bytes32 prev, bool coinbase) internal pure returns (bytes memory raw)
    {
        raw = bytes.concat(hex"02000000", _compact(inputs));
        for (uint256 i; i < inputs; ++i)
            raw = bytes.concat(raw, abi.encodePacked(coinbase ? bytes32(0) : prev),
                _le(coinbase ? type(uint32).max : i, 4), hex"00ffffffff");
        raw = bytes.concat(raw, _compact(outputs));
        for (uint256 i; i < outputs; ++i) {
            // Foreign malformed SMP1 markers do not select the address output's recipient.
            bytes memory s = i == target ? script : bytes(hex"6a04534d5031");
            raw = bytes.concat(raw, _le(i == target ? 1000 : 0, 8), _compact(s.length), s);
        }
        return bytes.concat(raw, hex"00000000");
    }
    function _txid(bytes memory raw) internal pure returns (bytes32) { return sha256(abi.encodePacked(sha256(raw))); }
    function _claim(bytes32 id, bytes memory raw, uint32 vout) internal view returns (IBitcoinAddressRegistry.MintClaim memory) {
        BitcoinAddressRegistry.Binding memory b = addresses.getBinding(id);
        return IBitcoinAddressRegistry.MintClaim(id, b.descriptor.bindingCommitment, _txid(raw), vout,
            b.recipient, 1000, addresses.custodyGeneration(), addresses.derivationGeneration(), b.validationProvenance);
    }
    function _mint(bytes memory raw, IBitcoinAddressRegistry.MintClaim memory c) internal {
        bytes32 op = pool.getAddressMintOperationHash(c); _approve(op, address(pool));
        pool.mintFromAddressProof(raw, bytes32(uint256(1)), new bytes32[](0), 0, c, op);
    }
    function _mintFailure(bytes memory raw, IBitcoinAddressRegistry.MintClaim memory c, bytes memory reason) internal {
        uint256 nonce = pool.nonce(); uint256 supply = pool.mpToken().totalSupply();
        bytes32 op = pool.getAddressMintOperationHash(c); _approve(op, address(pool));
        vm.expectRevert(reason);
        pool.mintFromAddressProof(raw, bytes32(uint256(1)), new bytes32[](0), 0, c, op);
        assertEq(pool.nonce(), nonce); assertEq(pool.mpToken().totalSupply(), supply);
        assertTrue(auth.approvedOperations(op)); assertFalse(auth.executedOperations(op));
    }

    function testRegistersImmutableBindingAndMintsExactRecipient() public {
        bytes32 id = _register(1); BitcoinAddressRegistry.Binding memory b = addresses.getBinding(id);
        assertEq(b.intent, _intent(1, RECIPIENT)); assertEq(b.pool, address(pool));
        assertEq(common.scriptPools(b.scriptHash), address(pool)); assertEq(common.addressIntents(b.scriptHash), id);
        bytes memory raw = _funding(1, 2, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0); _mint(raw, c);
        bytes32 key = keccak256(abi.encode(c.txid, c.vout));
        assertTrue(common.consumed(key)); assertEq(common.backingSats(key), 1000);
        assertEq(pool.mpToken().balanceOf(RECIPIENT), 1000 * 1e10); assertEq(pool.nonce(), 1);
        address[] memory recipients = new address[](1); recipients[0] = RECIPIENT;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
        assertEq(pool.mintedAllocations(key), keccak256(abi.encode(recipients, amounts)));
        assertEq(pool.mintedAddressIntents(key), id);
        assertEq(pool.mintedAddressSats(key), 1000);
        _mintFailure(raw, c, "outpoint consumed or change");
    }
    function testWrongRecipientAmountOrProvenanceCannotConsumeApprovalOrBacking() public {
        bytes32 id = _register(1); bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0);
        c.recipient = address(0x9876); _mintFailure(raw, c, "address binding");
        c = _claim(id, raw, 0); ++c.satoshis; _mintFailure(raw, c, "address funding output");
        c = _claim(id, raw, 0); c.validationProvenance = bytes32(uint256(1)); _mintFailure(raw, c, "address binding");
        assertFalse(common.consumed(keccak256(abi.encode(_txid(raw), uint32(0)))));
    }
    function testRegistrationBatchIsAtomicAndExactReceiptSurvivesPause() public {
        bytes[] memory raw = new bytes[](2); raw[0] = _intent(1, RECIPIENT); raw[1] = _intent(1, address(0x9999));
        uint64 deadline = uint64(block.timestamp + 3600); uint256 cg = addresses.custodyGeneration();
        uint256 dg = addresses.derivationGeneration();
        bytes32 op = addresses.getRegisterOperationHash(raw, cg, dg, deadline, 0); _approve(op, address(addresses));
        vm.expectRevert("request nonce bound"); addresses.registerBatch(raw, cg, dg, deadline, 0, op);
        assertEq(addresses.getBinding(sha256(raw[0])).scriptHash, 0); assertEq(addresses.registrationNonce(), 0);
        assertFalse(auth.executedOperations(op));
        raw = new bytes[](1); raw[0] = _intent(1, RECIPIENT);
        op = addresses.getRegisterOperationHash(raw, cg, dg, deadline, 0); _approve(op, address(addresses));
        addresses.registerBatch(raw, cg, dg, deadline, 0, op);
        BitcoinAddressRegistry.Binding memory b = addresses.getBinding(sha256(raw[0]));
        _status(0, 2, false); vm.warp(deadline + 1);
        addresses.registerBatch(raw, cg, dg, deadline, 0, op);
        assertEq(addresses.getBinding(sha256(raw[0])).validationProvenance, b.validationProvenance);
        assertEq(addresses.registrationNonce(), 1);
    }
    function testBoundedEightAddressBatchAndGlobalScriptNamespace() public {
        bytes[] memory raw = new bytes[](8);
        for (uint256 i; i < 8; ++i) raw[i] = _intent(i + 1, RECIPIENT);
        uint64 deadline = uint64(block.timestamp + 3600);
        bytes32 op = addresses.getRegisterOperationHash(raw, 1, 2, deadline, 0); _approve(op, address(addresses));
        uint256 beforeGas = gasleft(); addresses.registerBatch(raw, 1, 2, deadline, 0, op);
        uint256 used = beforeGas - gasleft();
        emit log_named_uint("local eight address registration execution gas", used); assertLt(used, 12_000_000);
        bytes32 id = sha256(raw[0]); bytes32 scriptHash = addresses.getBinding(id).scriptHash;
        bytes memory derivedScript = _script(id);
        op = common.getRegisterOperationHash(address(this), derivedScript); _approve(op, address(common));
        vm.expectRevert("registered"); common.register(address(this), derivedScript, op);
        vm.expectRevert("address registrar"); common.registerAddressScript(scriptHash, address(pool), id);
        bytes32 baseHash = keccak256(pool.payoutScript());
        vm.prank(address(addresses)); vm.expectRevert("script registered");
        common.registerAddressScript(baseHash, address(pool), id);
        bytes[] memory tooMany = new bytes[](9); for (uint256 i; i < 9; ++i) tooMany[i] = _intent(i + 10, RECIPIENT);
        op = addresses.getRegisterOperationHash(tooMany, 1, 2, uint64(block.timestamp + 3600), 1);
        vm.expectRevert("registration count"); addresses.registerBatch(tooMany, 1, 2, uint64(block.timestamp + 3600), 1, op);
    }
    function testCustodyAndDerivationGenerationsFencePreviouslyApprovedMint() public {
        bytes32 id = _register(1); bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0);
        _status(0, 2, false); _mintFailure(raw, c, "custody fence");
        _status(0, 0, false); _mintFailure(raw, c, "custody fence");
        c = _claim(id, raw, 0); _status(1, 1, false); _mintFailure(raw, c, "derivation fence");
        _status(1, 0, false); _mintFailure(raw, c, "derivation fence");
        _mint(raw, _claim(id, raw, 0));
    }
    function testDrainingStopsNewIssuanceButServesOldBindingsAndCannotReopen() public {
        bytes32 id = _register(1); _status(0, 1, false); assertTrue(addresses.issuanceClosed());
        bytes[] memory next = new bytes[](1); next[0] = _intent(2, RECIPIENT);
        bytes32 op = addresses.getRegisterOperationHash(next, 2, 2, uint64(block.timestamp + 3600), 1);
        _approve(op, address(addresses)); vm.expectRevert("issuance closed");
        addresses.registerBatch(next, 2, 2, uint64(block.timestamp + 3600), 1, op);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false); _mint(raw, _claim(id, raw, 0));
        _status(0, 2, false);
        BitcoinAddressRegistry.StatusChange memory c = _change(0, 0, false);
        op = addresses.getStatusOperationHash(c); _approve(op, address(addresses));
        vm.expectRevert("issuance irreversible"); addresses.setStatus(c, op);
        _status(0, 1, false);
    }
    function testStatusReceiptsIndependentNonceAndTerminalStates() public {
        BitcoinAddressRegistry.StatusChange memory c = _change(0, 2, false);
        bytes32 op = addresses.getStatusOperationHash(c); _approve(op, address(addresses));
        addresses.setStatus(c, op); assertEq(addresses.custodyGeneration(), 2);
        assertEq(pool.nonce(), 0); assertEq(addresses.registrationNonce(), 0);
        addresses.setStatus(c, op); assertEq(addresses.custodyGeneration(), 2);
        _status(0, 3, false);
        c = _change(0, 0, false); op = addresses.getStatusOperationHash(c); _approve(op, address(addresses));
        vm.expectRevert("custody terminal"); addresses.setStatus(c, op);
        _status(1, 2, false);
        c = _change(1, 0, false); op = addresses.getStatusOperationHash(c); _approve(op, address(addresses));
        vm.expectRevert("derivation terminal"); addresses.setStatus(c, op);
    }
    function testUnqualifiedRealCandidateCannotActivateEvenWithApproval() public {
        _deploy(false);
        BitcoinAddressRegistry.StatusChange memory c = _change(1, 0, false);
        bytes32 op = addresses.getStatusOperationHash(c); _approve(op, address(addresses));
        vm.expectRevert("verifier unqualified"); addresses.setStatus(c, op);
        assertEq(addresses.derivationGeneration(), 1); assertFalse(auth.executedOperations(op));
    }
    function test257InputsAnd257OutputsForeignMarkersAndCoinbaseMaturity() public {
        bytes32 id = _register(1); bytes memory raw = _funding(257, 257, 256, _script(id), bytes32(uint256(99)), false);
        proof.setDepth(5); _mintFailure(raw, _claim(id, raw, 256), "deposit proof"); proof.setDepth(6);
        _mint(raw, _claim(id, raw, 256));
        raw = _funding(1, 1, 0, _script(id), 0, true); proof.setDepth(99);
        _mintFailure(raw, _claim(id, raw, 0), "deposit proof"); proof.setDepth(100); _mint(raw, _claim(id, raw, 0));
    }
    function testFrozenAddressAndCustodyOperationVectorsMatchClient() public {
        string memory data = vm.readFile("test/fixtures/address-mint-operations-v1.json");
        address registry = vm.parseJsonAddress(data, ".registry");
        bytes32 scope = vm.parseJsonBytes32(data, ".custody_scope");
        vm.mockCall(address(common), abi.encodeWithSignature("addressRegistry()"), abi.encode(registry));
        vm.mockCall(registry, abi.encodeWithSignature("custodyScope()"), abi.encode(scope));
        IBitcoinAddressRegistry.MintClaim memory c = IBitcoinAddressRegistry.MintClaim(
            vm.parseJsonBytes32(data, ".claim[0]"), vm.parseJsonBytes32(data, ".claim[1]"),
            vm.parseJsonBytes32(data, ".claim[2]"), uint32(vm.parseJsonUint(data, ".claim[3]")),
            vm.parseJsonAddress(data, ".claim[4]"), uint64(vm.parseJsonUint(data, ".claim[5]")),
            vm.parseJsonUint(data, ".claim[6]"), vm.parseJsonUint(data, ".claim[7]"),
            vm.parseJsonBytes32(data, ".claim[8]"));
        bytes32 params = vm.parseJsonBytes32(data, ".address.params_hash");
        assertEq(pool.getAddressMintOperationHash(c), auth.hashOperation(address(pool),
            pool.mintFromAddressProof.selector, params, pool.nonce()));
        address[] memory recipients = new address[](1); recipients[0] = c.recipient;
        uint256[] memory amounts = new uint256[](1); amounts[0] = uint256(c.satoshis) * 1e10;
        assertEq(pool.getCustodyMintOperationHash(c.txid, c.vout, recipients, amounts, c.custodyGeneration),
            auth.hashOperation(address(pool), pool.mintFromProofV2.selector,
                vm.parseJsonBytes32(data, ".custody.params_hash"), pool.nonce()));
        address frozenAuth = vm.parseJsonAddress(data, ".authorization");
        vm.etch(frozenAuth, address(auth).code); vm.chainId(vm.parseJsonUint(data, ".chain_id"));
        FROSTAuthorization frozen = FROSTAuthorization(frozenAuth);
        address target = vm.parseJsonAddress(data, ".pool"); uint256 nonce = vm.parseJsonUint(data, ".nonce");
        assertEq(frozen.hashOperation(target, pool.mintFromAddressProof.selector, params, nonce),
            vm.parseJsonBytes32(data, ".address.operation_hash"));
        assertEq(frozen.hashOperation(target, pool.mintFromProofV2.selector,
            vm.parseJsonBytes32(data, ".custody.params_hash"), nonce),
            vm.parseJsonBytes32(data, ".custody.operation_hash"));
    }
    function testInternalChangeAndReservedInputsCannotBecomeNewBacking() public {
        bytes32 id = _register(1); bytes32 changeId = _register(2);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false); _mint(raw, _claim(id, raw, 0));
        bytes memory change = _funding(1, 1, 0, _script(changeId), _txid(raw), false);
        vm.prank(address(escrow)); common.recordSpend(change, 0);
        _mintFailure(change, _claim(changeId, change, 0), "outpoint consumed or change");
        bytes32 changeKey = keccak256(abi.encode(_txid(change), uint32(0)));
        assertTrue(common.walletOutput(changeKey)); assertEq(common.backingSats(changeKey), 1000);
        bytes32[] memory inputs = new bytes32[](1);
        inputs[0] = keccak256(abi.encode(BitcoinTxParser.flipBytes32(bytes32(uint256(123))), uint32(0)));
        bytes32 batch = keccak256("reserved fixture batch"); bytes32 plan = keccak256("reserved fixture plan");
        bytes32 op = batches.operationHash(batch, plan, inputs, uint64(block.timestamp + 3600));
        _approve(op, address(batches));
        if(batches.feeReserve() != address(0)) {
            vm.expectRevert("fee unprepared plan");batches.reserve(batch, plan, inputs, uint64(block.timestamp + 3600), op);
            assertEq(batches.reservations(inputs[0]),0);return;
        }
        batches.reserve(batch, plan, inputs, uint64(block.timestamp + 3600), op);
        bytes memory own = _funding(1, 1, 0, _script(id), bytes32(uint256(123)), false);
        _mintFailure(own, _claim(id, own, 0), "wallet input reserved for spend");
    }
    function testLegacySelectorCannotBypassFenceAndCustodyV2RetainsSmp1Rules() public {
        address[] memory recipients = new address[](1); recipients[0] = RECIPIENT;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
        bytes memory raw = abi.encodePacked(hex"0200000001", bytes32(uint256(99)), hex"0000000000ffffffff02",
            _le(1000, 8), hex"22", pool.payoutScript(), bytes8(0), hex"3a6a38", pool.getDepositData(0, recipients, amounts), hex"00000000");
        bytes32 op = pool.getMintOperationHash(_txid(raw), 0, recipients, amounts); _approve(op, address(pool));
        vm.expectRevert("use generation-bound mint");
        pool.mintFromProof(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, op);
        _status(1, 1, false); // Derivation suspension does not suspend independent SMP1.
        uint256 old = addresses.custodyGeneration();
        op = pool.getCustodyMintOperationHash(_txid(raw), 0, recipients, amounts, old); _approve(op, address(pool));
        _status(0, 2, false); _status(0, 0, false);
        vm.expectRevert("custody fence");
        pool.mintFromProofV2(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, old, op);
        uint256 current = addresses.custodyGeneration();
        op = pool.getCustodyMintOperationHash(_txid(raw), 0, recipients, amounts, current); _approve(op, address(pool));
        pool.mintFromProofV2(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, current, op);
        assertEq(pool.mpToken().balanceOf(RECIPIENT), 1000 * 1e10);
    }

    function testIntentContextAndRecipientPolicyCannotBeOverriddenByApproval() public {
        bytes[] memory raw = new bytes[](1);
        uint256[9] memory offsets = [uint256(30), 62, 94, 154, 186, 206, 238, 270, 350];
        uint64 deadline = uint64(block.timestamp + 3600);
        for (uint256 i; i < offsets.length; ++i) {
            raw[0] = _intent(1, RECIPIENT); raw[0][offsets[i]] ^= 0x01;
            bytes32 op = addresses.getRegisterOperationHash(raw, 1, 2, deadline, 0); _approve(op, address(addresses));
            vm.expectRevert("intent context"); addresses.registerBatch(raw, 1, 2, deadline, 0, op);
            assertFalse(auth.executedOperations(op));
        }
        address[3] memory sinks = [address(0), address(pool.mpToken()), address(common)];
        for (uint256 i; i < sinks.length; ++i) {
            raw[0] = _intent(1, sinks[i]);
            bytes32 op = addresses.getRegisterOperationHash(raw, 1, 2, deadline, 0); _approve(op, address(addresses));
            vm.expectRevert(i == 0 ? bytes("recipient profile") : bytes("recipient sink")); addresses.registerBatch(raw, 1, 2, deadline, 0, op);
        }
        assertEq(addresses.registrationNonce(), 0);
    }
    function testStaleUnpauseDeadlineAndUnauthorizedStatusCannotChangeGeneration() public {
        _status(0, 2, false);
        BitcoinAddressRegistry.StatusChange memory stale = _change(0, 0, false);
        bytes32 op = addresses.getStatusOperationHash(stale); _approve(op, address(addresses));
        _status(0, 2, false); // New incident invalidates the earlier unpause.
        vm.expectRevert("custody transition"); addresses.setStatus(stale, op);
        BitcoinAddressRegistry.StatusChange memory c = _change(0, 0, false);
        op = addresses.getStatusOperationHash(c);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector); addresses.setStatus(c, op);
        _approve(op, address(addresses)); vm.warp(uint256(c.deadline) + 1);
        vm.expectRevert("status report"); addresses.setStatus(c, op);
        assertEq(addresses.custodyGeneration(), 3); assertEq(addresses.custodyStatusNonce(), 2);
        assertEq(pool.nonce(), 0);
    }
    function testVerifierRuntimeChangeQuarantinesCachedBinding() public {
        bytes32 id = _register(1);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        vm.etch(address(addresses.verifier()), hex"00");
        _mintFailure(raw, _claim(id, raw, 0), "verifier unqualified");
        _status(1, 1, false); // Suspension remains available when the verifier is unavailable.
        assertEq(addresses.getBinding(id).recipient, RECIPIENT);
    }
    function testExactScriptSelectsModeAndConflictingSmp1CannotRedirectAddress() public {
        bytes32 id = _register(1);
        address[] memory recipients = new address[](1); recipients[0] = address(0x9999);
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
        bytes memory raw = abi.encodePacked(hex"0200000001", bytes32(uint256(99)), hex"0000000000ffffffff02",
            _le(1000, 8), hex"22", _script(id), bytes8(0), hex"3a6a38",
            pool.getDepositData(0, recipients, amounts), hex"00000000");
        bytes32 op = pool.getCustodyMintOperationHash(_txid(raw), 0, recipients, amounts, 1); _approve(op, address(pool));
        vm.expectRevert("deposit output");
        pool.mintFromProofV2(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, 1, op);
        assertEq(pool.nonce(), 0); assertFalse(auth.executedOperations(op));
        _mint(raw, _claim(id, raw, 0));
        assertEq(pool.mpToken().balanceOf(RECIPIENT), 1000 * 1e10);
        assertEq(pool.mpToken().balanceOf(address(0x9999)), 0);
    }
    function testMalformedFundingRollsBackConsumedAndAuthorization() public {
        bytes32 id = _register(1);
        bytes memory raw = bytes.concat(_funding(1, 1, 0, _script(id), bytes32(uint256(99)), false), hex"00");
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0);
        bytes32 op = pool.getAddressMintOperationHash(c); _approve(op, address(pool));
        vm.expectRevert(BitcoinFundingParser.InvalidFundingLength.selector);
        pool.mintFromAddressProof(raw, bytes32(uint256(1)), new bytes32[](0), 0, c, op);
        assertEq(pool.nonce(), 0); assertEq(pool.mpToken().totalSupply(), 0);
        assertFalse(common.consumed(keccak256(abi.encode(c.txid, uint32(0)))));
        assertFalse(auth.executedOperations(op));
    }

    function _admission() internal returns (BitcoinMintAdmission a) {
        a = BitcoinMintAdmission(deployCode("BitcoinMintAdmission.sol:BitcoinMintAdmission", abi.encode(address(pool), address(0xa1))));
        bytes32 op = pool.getSetMintAdmissionOperationHash(address(a)); _approve(op, address(pool));
        pool.setMintAdmission(address(a), op);
    }
    function testAddressAdmissionAdoptsExactClaimAndSharesNonceSlotWithCustodyMint() public {
        BitcoinMintAdmission a = _admission(); bytes32 id = _register(1);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0); uint64 deadline = uint64(block.timestamp + 3600);
        vm.prank(address(0xa1)); uint256 session = a.openAddressMint(c, deadline, 0);
        vm.prank(address(0xa2)); assertEq(a.openAddressMint(c, deadline, 0), session);
        assertTrue(a.active(session)); assertEq(a.slotSession(0), session); assertEq(pool.nonce(), 0);
        bytes32 claimId = a.getClaimId(c.txid, c.vout); assertEq(uint8(a.claimModes(claimId)), 2);
        assertEq(abi.encode(a.getAttemptAddressClaim(session)), abi.encode(c));
        address[] memory recipients = new address[](1); recipients[0] = RECIPIENT;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
        vm.prank(address(0xa1)); vm.expectRevert("slot busy");
        a.openCustodyMint(bytes32(uint256(123)), 0, recipients, amounts, 1, deadline, 0);
        vm.prank(address(0xa1)); vm.expectRevert("generation admission required");
        a.openMint(bytes32(uint256(123)), 0, recipients, amounts, deadline, 0);
        c.recipient = address(0x9876);
        vm.prank(address(0xa1)); vm.expectRevert("address binding"); a.openAddressMint(c, deadline, 0);
        assertEq(a.getAddressClaim(claimId).recipient, RECIPIENT);
    }
    function testApprovedStaleAddressAttemptRetiresAndRetriesSameEconomicsAndNonce() public {
        BitcoinMintAdmission a = _admission(); bytes32 id = _register(1);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0); uint64 deadline = uint64(block.timestamp + 3600);
        vm.prank(address(0xa1)); uint256 first = a.openAddressMint(c, deadline, 0);
        bytes32 oldOp = pool.getAddressMintOperationHash(c); _approve(oldOp, address(pool));
        vm.prank(address(0xa1)); vm.expectRevert("executable authorization"); a.retire(first);
        _status(0, 2, false); assertFalse(a.active(first));
        bytes32 claimId = a.getClaimId(c.txid, c.vout);
        vm.prank(address(0xa1)); vm.expectRevert("custody fence"); a.retryGenerationMint(claimId, 0, first, 2, 2);
        _status(0, 0, false);
        vm.prank(address(0xa1)); uint256 second = a.retryGenerationMint(claimId, 0, first, 3, 2);
        assertTrue(a.active(second)); assertFalse(a.active(first)); assertEq(pool.nonce(), 0);
        (,,,, bool retired) = a.attempts(first); assertTrue(retired);
        assertTrue(auth.approvedOperations(oldOp)); assertFalse(auth.executedOperations(oldOp));
        assertEq(a.getAddressClaim(claimId).custodyGeneration, 1);
        IBitcoinAddressRegistry.MintClaim memory fresh = a.getAttemptAddressClaim(second);
        assertEq(fresh.custodyGeneration, 3); assertEq(fresh.recipient, c.recipient);
        assertEq(fresh.validationProvenance, c.validationProvenance); assertEq(fresh.satoshis, c.satoshis);
        (,,,, uint64 originalDeadline, uint256 count,) = a.getClaim(claimId);
        assertEq(originalDeadline, deadline); assertEq(count, 2);
        vm.expectRevert("custody fence");
        pool.mintFromAddressProof(raw, bytes32(uint256(1)), new bytes32[](0), 0, c, oldOp);
        _mint(raw, fresh); assertEq(pool.mintedAddressSats(keccak256(abi.encode(c.txid, c.vout))), 1000);
    }
    function testDerivationFenceReleasesSlotWithoutInvalidatingIndependentCustodyClaim() public {
        BitcoinMintAdmission a = _admission(); bytes32 id = _register(1);
        bytes memory raw = _funding(1, 1, 0, _script(id), bytes32(uint256(99)), false);
        IBitcoinAddressRegistry.MintClaim memory c = _claim(id, raw, 0); uint64 deadline = uint64(block.timestamp + 3600);
        vm.prank(address(0xa1)); uint256 first = a.openAddressMint(c, deadline, 0);
        _approve(pool.getAddressMintOperationHash(c), address(pool)); _status(1, 1, false);
        vm.prank(address(0xa2)); a.retire(first);
        address[] memory recipients = new address[](1); recipients[0] = RECIPIENT;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
        vm.prank(address(0xa1)); uint256 custody = a.openCustodyMint(bytes32(uint256(123)), 0, recipients, amounts, 1, deadline, 0);
        assertTrue(a.active(custody)); assertFalse(a.active(first));
        _status(0, 2, false); assertFalse(a.active(custody));
        vm.prank(address(0xa2)); a.retire(custody); assertEq(a.slotSession(0), 0);
    }
}
