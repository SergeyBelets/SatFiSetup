// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/BitcoinCustodyTransfer.sol";
import {RegtestHeaderRelay} from "../contracts/src/federation/RegtestHeaderRelay.sol";
import {BitcoinWithdrawalFeeVerifier} from "../contracts/src/federation/BitcoinWithdrawalFeeVerifier.sol";

// Explicit unit boundaries: real contracts/accounting, synthetic signatures/SPV.
// Native FROST and real Bitcoin acceptance are exercised separately in tests/e2e.
contract CustodyAuthFixture {
    function hashOperation(address target, bytes4 method, bytes32 params, uint256 nonce) external pure returns(bytes32) {
        return keccak256(abi.encode(target, method, params, nonce));
    }
    function consumeOperation(bytes32) external pure returns(bool) { return true; }
    function isOperationApproved(bytes32,address) external pure returns(bool) { return true; }
}
contract CustodyValidatorFixture {
    function meetsMinimumCustodians(bytes32) external pure returns(bool) { return true; }
}
contract CustodyDkgFixture {
    bytes32 private immutable group;
    constructor(bytes32 key) { group = key; }
    function getCompletedResult(bytes32) external view returns(bytes32,bytes32,uint256,bytes32,bytes memory,address[] memory,uint256) {
        return (bytes32(0),bytes32(0),1,keccak256("BTC_SPEND"),abi.encodePacked(group),new address[](0),2);
    }
}
contract CustodyBatchFixture {
    address public immutable dkg;
    bytes32 public constant walletId = bytes32(uint256(1));
    bytes32 public constant walletResult = bytes32(uint256(2));
    address public constant recoveryController = address(0);
    BitcoinWithdrawalFeeVerifier public immutable withdrawalFeeVerifier = new BitcoinWithdrawalFeeVerifier();
    address public immutable authorization = address(new CustodyAuthFixture());
    address public withdrawalEscrow;
    address public feeReserve;
    BitcoinBatchRegistry.State private state;
    bytes32[] private inputs;
    mapping(bytes32 => bytes32) public reservations;
    mapping(bytes32 => bytes32) public replacementOf;
    constructor(bytes32 group) { dkg = address(new CustodyDkgFixture(group)); }
    function bindEscrow(address target) external { withdrawalEscrow = target; }
    function configure(bytes32 input, BitcoinBatchRegistry.State next) external {
        delete inputs; inputs.push(input); state = next;
    }
    function configure(bytes32[] calldata orderedInputs, BitcoinBatchRegistry.State next) external {
        inputs = orderedInputs;
        state = next;
    }
    function batchState(bytes32) external view returns(BitcoinBatchRegistry.State) { return state; }
    function batchWalletResult(bytes32) external pure returns (bytes32) { return walletResult; }
    function getInputs(bytes32) external view returns(bytes32[] memory) { return inputs; }
}
contract CustodySPVFixture is Test {
    RegtestHeaderRelay public immutable relay = new RegtestHeaderRelay();
    uint64 private confirmations = 100;
    bool private canonical = true;

    constructor() {
        _mockStatus();
        vm.mockCall(address(relay), abi.encodeWithSelector(IPaymentSPV.checkTxInclusion.selector), abi.encode(true));
    }

    function setDepth(uint64 value) external { confirmations = value; _mockStatus(); }
    function setCanonical(bool value) external { canonical = value; _mockStatus(); }

    function _mockStatus() private {
        vm.mockCall(address(relay), abi.encodeWithSelector(IPaymentSPV.getBlockStatus.selector),
            abi.encode(canonical, confirmations));
    }
}

contract BitcoinCustodyTransferTest is Test {
    uint256 private constant SAT = 1e10;
    bytes32 private constant BATCH = bytes32(uint256(17));
    address private constant ALICE = address(0xA11CE);
    address private constant BOB = address(0xB0B);
    struct Graph {
        CustodyBatchFixture batches;
        CustodySPVFixture spv;
        BitcoinWithdrawalEscrow escrow;
        BitcoinDepositRegistry deposits;
        MiningPoolV1 mining;
    }
    Graph private source;
    Graph private destination;
    ConfederationMultiPool private pool;
    BitcoinCustodyTransfer private gateway;
    bytes private funding;

    function graph(bytes32 group) private returns(Graph memory g) {
        g.batches = new CustodyBatchFixture(group);
        g.spv = new CustodySPVFixture();
        g.escrow = new BitcoinWithdrawalEscrow(address(g.batches), address(g.spv.relay()), 6, 294, 330);
        g.batches.bindEscrow(address(g.escrow));
        g.deposits = new BitcoinDepositRegistry(address(g.spv.relay()), g.batches.authorization(), 6, address(g.escrow));
        g.escrow.setDepositRegistry(address(g.deposits), g.escrow.getSetDepositRegistryOperationHash(address(g.deposits)));
        g.mining = new MiningPoolV1("custody", address(g.deposits), address(g.escrow), "MP", "MP");
        bytes memory script = g.mining.payoutScript();
        g.deposits.register(address(g.mining), script, g.deposits.getRegisterOperationHash(address(g.mining), script));
    }

    function setUp() public {
        source = graph(bytes32(uint256(11)));
        destination = graph(bytes32(uint256(22)));
        address validator = address(new CustodyValidatorFixture());
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        ConfederationToken share = new ConfederationToken("Custodian Bitcoin", "cBTC", predicted);
        pool = new ConfederationMultiPool("custodian-conf", destination.batches.authorization(),
            validator, address(share));
        pool.setWithdrawalEscrow(address(destination.escrow), pool.getSetWithdrawalEscrowOperationHash(address(destination.escrow)));
        destination.escrow.requireLocalCustody(address(pool), destination.escrow.getRequireLocalCustodyOperationHash(address(pool)));
        pool.addPool(address(destination.mining.mpToken()), "local", pool.getAddPoolOperationHash(address(destination.mining.mpToken()), "local"));
        BitcoinCustodyTransfer.Source[] memory sources = new BitcoinCustodyTransfer.Source[](1);
        sources[0] = descriptor(source);
        gateway = new BitcoinCustodyTransfer(address(destination.mining), address(pool), sources);
        destination.mining.setCustodyTransfer(address(gateway), destination.mining.getSetCustodyTransferOperationHash(address(gateway)));
        funding = transaction(bytes32(0), type(uint32).max, 2000, source.mining.payoutScript(), new bytes(0));
        address[] memory recipients = new address[](1); recipients[0] = address(this);
        uint256[] memory amounts = new uint256[](1); amounts[0] = 2000 * SAT;
        source.mining.mintFromProof(funding, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts,
            source.mining.getMintOperationHash(txid(funding), 0, recipients, amounts));
        source.mining.mpToken().approve(address(gateway), type(uint256).max);
    }

    function descriptor(Graph memory g) private view returns(BitcoinCustodyTransfer.Source memory) {
        return BitcoinCustodyTransfer.Source(address(g.escrow), address(g.mining.mpToken()), address(g.escrow).codehash,
            address(g.mining.mpToken()).codehash, address(g.mining).codehash, address(g.deposits).codehash, address(g.batches).codehash);
    }
    function little(uint256 value, uint256 length) private pure returns(bytes memory b) {
        b = new bytes(length); for(uint256 i; i < length; ++i) b[i] = bytes1(uint8(value >> (8 * i)));
    }
    function transaction(bytes32 prev, uint32 vout, uint64 amount, bytes memory script, bytes memory extra)
        private pure returns(bytes memory)
    {
        return abi.encodePacked(hex"0200000001", prev, little(vout,4), hex"00ffffffff",
            extra.length == 0 ? hex"01" : hex"02", little(amount,8), bytes1(uint8(script.length)), script, extra, hex"00000000");
    }
    function txid(bytes memory raw) private pure returns(bytes32) { return sha256(abi.encodePacked(sha256(raw))); }
    function ids(uint256 one) private pure returns(uint256[] memory a) { a = new uint256[](1); a[0] = one; }
    function pay(uint256[] memory transfers_, uint64 sats) private returns(bytes memory raw) {
        bytes32[] memory keys = new bytes32[](transfers_.length);
        for(uint256 i; i < keys.length; ++i) keys[i] = source.escrow.requestKey(address(gateway), transfers_[i]);
        uint256 remaining = 2000 - sats;
        bytes memory change = remaining == 0 ? new bytes(0) : abi.encodePacked(little(remaining,8),hex"22",source.mining.payoutScript());
        raw = transaction(txid(funding), 0, sats, destination.mining.payoutScript(), change);
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid(funding)),uint32(0)));
        source.batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        source.escrow.bind(BATCH, txid(raw), keys, source.escrow.getBindOperationHash(BATCH, txid(raw), keys));
        source.batches.configure(input, BitcoinBatchRegistry.State.Signing);
        source.escrow.settle(BATCH, raw, bytes32(uint256(1)), new bytes32[](0), 0);
    }
    function complete(uint256[] memory transfers_, bytes memory raw) private {
        gateway.finalize(transfers_, BATCH, raw, 0, bytes32(uint256(1)), new bytes32[](0), 0);
    }

    function testNoSharesUntilExactSourcePaymentAndDestinationProof() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1000 * SAT, ALICE);
        assertEq(pool.shareToken().balanceOf(ALICE), 0);
        assertEq(source.mining.mpToken().balanceOf(address(source.escrow)), 1000 * SAT);
        bytes memory raw = pay(ids(id), 1000);
        assertEq(source.mining.mpToken().totalSupply(), 1000 * SAT);
        assertEq(source.deposits.totalBackingSats(), 1000);
        destination.spv.setDepth(5);
        vm.expectRevert("transfer proof"); complete(ids(id), raw);
        assertEq(destination.mining.mpToken().totalSupply(), 0);
        destination.spv.setDepth(6);
        complete(ids(id), raw);
        assertEq(pool.shareToken().balanceOf(ALICE), 1000 * SAT);
        assertEq(pool.userPoolBalances(ALICE, address(destination.mining.mpToken())), 1000 * SAT);
        assertEq(destination.mining.mpToken().balanceOf(address(pool)), 1000 * SAT);
        assertEq(destination.deposits.totalBackingSats(), 1000);
        assertEq(source.deposits.totalBackingSats() + destination.deposits.totalBackingSats(), 2000);
        vm.expectRevert("transfer state"); complete(ids(id), raw);
    }
    function testWaitingCanCancelOnlyOwnerAndReturnsOriginalMP() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        vm.prank(ALICE); vm.expectRevert("transfer owner/state"); gateway.cancel(id);
        gateway.cancel(id);
        assertEq(source.mining.mpToken().balanceOf(address(this)), 2000 * SAT);
        assertEq(pool.shareToken().totalSupply(), 0);
        vm.expectRevert("transfer owner/state"); gateway.cancel(id);
    }
    function testNoTimeoutRefundAfterSourceSigningStarts() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        bytes32[] memory keys = new bytes32[](1); keys[0] = source.escrow.requestKey(address(gateway), id);
        source.batches.configure(bytes32(uint256(1)), BitcoinBatchRegistry.State.Reserved);
        source.escrow.bind(BATCH, bytes32(uint256(1)), keys, source.escrow.getBindOperationHash(BATCH, bytes32(uint256(1)), keys));
        source.batches.configure(bytes32(uint256(1)), BitcoinBatchRegistry.State.Signing);
        vm.warp(block.timestamp + 365 days);
        vm.expectRevert("signature may exist"); gateway.cancel(id);
        assertEq(pool.shareToken().totalSupply(), 0);
    }
    function testSourcePaymentRequiredEvenWithDestinationSPV() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        bytes memory raw = transaction(txid(funding), 0, 1000, destination.mining.payoutScript(), new bytes(0));
        vm.expectRevert("source payment not settled"); complete(ids(id), raw);
        assertEq(destination.mining.mpToken().totalSupply(), 0);
    }
    function testAggregatedOutputRequiresEveryClaimInOneAtomicAcceptance() public {
        uint256 a = gateway.request(0, 1000 * SAT, 1, ALICE);
        uint256 b = gateway.request(0, 1000 * SAT, 1, BOB);
        uint256[] memory both = new uint256[](2); both[0] = a; both[1] = b;
        bytes memory raw = pay(both, 2000);
        vm.expectRevert("transfer output"); complete(ids(a), raw);
        assertEq(pool.shareToken().totalSupply(), 0);
        complete(both, raw);
        assertEq(pool.shareToken().balanceOf(ALICE), 1000 * SAT);
        assertEq(pool.shareToken().balanceOf(BOB), 1000 * SAT);
        assertEq(destination.deposits.totalBackingSats(), 2000);
        assertEq(source.deposits.totalBackingSats(), 0);
    }
    function testRecipientAndSlippageCannotBeChangedByFinalizer() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1000 * SAT, ALICE);
        bytes memory raw = pay(ids(id), 1000);
        vm.prank(BOB); vm.expectRevert("transfer owner/state"); gateway.setMinimumShares(id, 1);
        vm.prank(BOB); complete(ids(id), raw);
        assertEq(pool.shareToken().balanceOf(BOB), 0);
        assertEq(pool.shareToken().balanceOf(ALICE), 1000 * SAT);
    }
    function testReorgBlocksAcceptanceWithoutMinting() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        bytes memory raw = pay(ids(id), 1000);
        destination.spv.setCanonical(false);
        vm.expectRevert("transfer proof"); complete(ids(id), raw);
        assertEq(pool.shareToken().totalSupply(), 0);
        destination.spv.setCanonical(true);
        complete(ids(id), raw);
    }
    function testForeignMPRejectedAndMintGatewayCannotBeCalledDirectly() public {
        address token = address(source.mining.mpToken());
        bytes32 op = pool.getAddPoolOperationHash(token, "foreign");
        vm.expectRevert("foreign Bitcoin custody");
        pool.addPool(token, "foreign", op);
        vm.expectRevert("transfer gateway only");
        destination.mining.mintFromCustodyTransfer(funding, 0, 1000, bytes32(uint256(1)), bytes32(uint256(1)), new bytes32[](0), 0);
    }
    function testSourceTransactionCannotMintAgainThroughOrdinaryAllocation() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        bytes memory raw = pay(ids(id), 1000);
        assertTrue(gateway.isSourceFunding(raw));
        address[] memory recipients = new address[](1); recipients[0] = BOB;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 1000 * SAT;
        bytes32 op = destination.mining.getMintOperationHash(txid(raw), 0, recipients, amounts);
        vm.expectRevert("use custody transfer");
        destination.mining.mintFromProof(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts,
            op);
        complete(ids(id), raw);
        assertEq(pool.shareToken().balanceOf(ALICE), 1000 * SAT);
    }
    function testDuplicateIdAndWrongOutputCannotIncreaseBacking() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        bytes memory raw = pay(ids(id), 1000);
        uint256[] memory twice = new uint256[](2); twice[0] = id; twice[1] = id;
        vm.expectRevert("transfer state/source"); complete(twice, raw);
        vm.expectRevert("transfer output");
        gateway.finalize(ids(id), BATCH, raw, 1, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(destination.deposits.totalBackingSats(), 0);
        complete(ids(id), raw);
    }

    function testPermissionlessHandoverDoesNotInvalidatePendingOrdinaryMintNonce() public {
        uint256 id = gateway.request(0, 1000 * SAT, 1, ALICE);
        address[] memory recipients = new address[](1); recipients[0] = BOB;
        uint256[] memory amounts = new uint256[](1); amounts[0] = 600 * SAT;
        bytes memory data = destination.mining.getDepositData(0, recipients, amounts);
        bytes memory marker = abi.encodePacked(hex"00000000000000003a6a38", data);
        bytes memory ordinary = transaction(bytes32(uint256(999)), 0, 600, destination.mining.payoutScript(), marker);
        bytes32 approved = destination.mining.getMintOperationHash(txid(ordinary), 0, recipients, amounts);
        uint256 beforeNonce = destination.mining.nonce();
        bytes memory raw = pay(ids(id), 1000);
        complete(ids(id), raw);
        assertEq(destination.mining.nonce(), beforeNonce);
        destination.mining.mintFromProof(ordinary, 0, bytes32(uint256(1)), new bytes32[](0), 0,
            recipients, amounts, approved);
        assertEq(destination.mining.nonce(), beforeNonce + 1);
        assertEq(destination.mining.mpToken().balanceOf(BOB), 600 * SAT);
        assertEq(destination.deposits.totalBackingSats(), 1600);
    }
}
