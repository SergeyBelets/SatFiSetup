// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinDepositRegistry.sol";

interface IBitcoinMintAdmissionBinding {
    function pool() external view returns (address);
    function authorization() external view returns (address);
}

interface IBitcoinCustodyTransferBinding {
    function mining() external view returns (address);
    function isSourceFunding(bytes calldata raw) external view returns (bool);
}

/// @notice Proof-only MP issuance. No owner mint or discretionary redemption confirmation.
contract MiningPoolV1 is ReentrancyGuard {
    BitcoinDepositRegistry public immutable deposits;
    BitcoinWithdrawalEscrow public immutable escrow;
    PoolMpToken public immutable mpToken;
    FROSTAuthorization public immutable authorization;
    string public poolId;
    bytes public payoutScript;
    uint256 public nonce;
    /// @notice One-time federation-approved admission shared by standard mint clients.
    address public mintAdmission;
    address public custodyTransfer;
    /// @notice Exact successful allocation, for unambiguous retry/recovery of a lost receipt.
    mapping(bytes32 => bytes32) public mintedAllocations;
    mapping(bytes32 => bytes32) public mintedAddressIntents;
    mapping(bytes32 => uint64) public mintedAddressSats;
    event Minted(bytes32 indexed txid, uint32 indexed vout, uint64 satoshis);
    event MintAdmissionBound(address indexed admission);
    event MintNonceExpired(uint256 indexed nonce);
    event CustodyTransferBound(address indexed gateway);
    event CustodyTransferMinted(bytes32 indexed claim, bytes32 indexed txid, uint32 vout, uint64 satoshis);

    function belongsToCustody(address token, address escrow_, address registry_) external view returns (bool) {
        return address(mpToken) == token && address(escrow) == escrow_ && address(deposits) == registry_
            && deposits.poolScripts(address(this)) == keccak256(payoutScript);
    }

    function getSetCustodyTransferOperationHash(address gateway) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.setCustodyTransfer.selector,
            keccak256(abi.encode(gateway)), 0);
    }

    function setCustodyTransfer(address gateway, bytes32 op) external nonReentrant {
        require(custodyTransfer == address(0) && nonce == 0 && gateway.code.length > 0
            && IBitcoinCustodyTransferBinding(gateway).mining() == address(this), "transfer binding");
        require(op == getSetCustodyTransferOperationHash(gateway), "hash");
        authorization.consumeOperation(op);
        custodyTransfer = gateway;
        emit CustodyTransferBound(gateway);
    }

    function mintFromCustodyTransfer(bytes calldata raw, uint32 vout, uint64 sats, bytes32 claim,
        bytes32 blockHash, bytes32[] calldata siblings, uint256 index) external nonReentrant
    {
        require(msg.sender == custodyTransfer && msg.sender != address(0), "transfer gateway only");
        deposits.consumeTransfer(raw, vout, sats, claim, blockHash, siblings, index);
        // A permissionless completed handover has its own consumed claim/output.
        // It must not retire a concurrently approved ordinary mint admission slot.
        mpToken.mint(msg.sender, uint256(sats) * 1e10);
        emit CustodyTransferMinted(claim, BitcoinTxParser.txId(raw), vout, sats);
    }
    event AddressMinted(bytes32 indexed intentId, bytes32 indexed txid, uint32 indexed vout,
        address recipient, uint64 satoshis, bytes32 validationProvenance);

    constructor(
        string memory id,
        address depositRegistry,
        address withdrawalEscrow,
        string memory name,
        string memory symbol
    ) {
        require(bytes(id).length > 0, "pool ID");
        poolId = id;
        deposits = BitcoinDepositRegistry(depositRegistry);
        escrow = BitcoinWithdrawalEscrow(withdrawalEscrow);
        authorization = escrow.batches().authorization();
        require(
            address(deposits.authorization()) == address(authorization)
                && address(deposits.spv()) == address(escrow.spv()),
            "binding"
        );
        (,,,, bytes memory group,,) = escrow.batches().dkg().getCompletedResult(escrow.batches().walletId());
        bytes32 x;
        assembly { x := mload(add(group, 32)) }
        // Registry exposes the canonical even-Y x-only group (32 bytes).
        require(group.length == 32, "canonical group");
        payoutScript = abi.encodePacked(hex"5120", x);
        mpToken = new PoolMpToken(name, symbol, address(this), false);
    }

    function getMintOperationHash(bytes32 txid, uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this), this.mintFromProof.selector, keccak256(abi.encode(txid, vout, recipients, amounts)), nonce
        );
    }

    function mintProofVersion() external view returns (uint8) {
        return address(deposits.addressRegistry()) == address(0) ? 1 : 2;
    }

    function mintExpiryVersion() external pure returns (uint8) { return 1; }

    function getCustodyMintOperationHash(bytes32 txid, uint32 vout, address[] calldata recipients,
        uint256[] calldata amounts, uint256 custodyGeneration) public view returns (bytes32)
    {
        IBitcoinAddressRegistry registry = deposits.addressRegistry();
        require(address(registry) != address(0), "custody mode required");
        return authorization.hashOperation(address(this), this.mintFromProofV2.selector,
            keccak256(abi.encode(keccak256("SATOHSI_CUSTODY_MINT_V2"), address(registry),
                registry.custodyScope(), txid, vout, recipients, amounts, custodyGeneration)), nonce);
    }

    function getAddressMintOperationHash(IBitcoinAddressRegistry.MintClaim calldata claim)
        public view returns (bytes32)
    {
        IBitcoinAddressRegistry registry = deposits.addressRegistry();
        require(address(registry) != address(0), "address mode required");
        return authorization.hashOperation(address(this), this.mintFromAddressProof.selector,
            keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_MINT_V1"), address(registry),
                registry.custodyScope(), claim)), nonce);
    }

    function getSetMintAdmissionOperationHash(address admission) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setMintAdmission.selector, keccak256(abi.encode(admission)), 0
        );
    }

    /// @dev Uses a separate selector and one-time nonce; the legacy mint nonce is unchanged.
    function setMintAdmission(address admission, bytes32 op) external nonReentrant {
        require(mintAdmission == address(0) && admission.code.length > 0, "admission binding");
        require(
            IBitcoinMintAdmissionBinding(admission).pool() == address(this)
                && IBitcoinMintAdmissionBinding(admission).authorization() == address(authorization),
            "admission context"
        );
        require(op == getSetMintAdmissionOperationHash(admission), "hash");
        authorization.consumeOperation(op);
        mintAdmission = admission;
        emit MintAdmissionBound(admission);
    }

    /// @dev The immutable admission verifies the exact current slot and its deadline.
    function expireMintNonce(uint256 expectedNonce) external nonReentrant {
        require(msg.sender == mintAdmission && nonce == expectedNonce, "mint expiry");
        nonce = expectedNonce + 1;
        emit MintNonceExpired(expectedNonce);
    }

    /// @notice Allocation hash included alongside the recipient in a zero-value OP_RETURN.
    /// @dev A deposit cannot be redirected by changing the later FROST allocation.
    function getDepositCommitment(uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        public view returns (bytes32)
    {
        return keccak256(abi.encode(keccak256("SATOHSI_DEPOSIT_ALLOCATION_V1"), block.chainid,
            address(this), vout, recipients, amounts));
    }

    /// @notice 56-byte OP_RETURN payload: "SMP1" || EVM recipient || allocation hash.
    function getDepositData(uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        public view returns (bytes memory)
    {
        require(recipients.length == 1 && amounts.length == 1, "single deposit recipient");
        return abi.encodePacked(hex"534d5031", recipients[0], getDepositCommitment(vout, recipients, amounts));
    }

    function _checkDepositAllocation(bytes calldata raw, uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        private view
    {
        _checkExternalFunding(raw);
        (bytes32[] memory prev, uint32[] memory prevouts) = BitcoinTxParser.parseAllInputs(raw);
        bool coinbase = prev.length == 1 && prev[0] == 0 && prevouts[0] == type(uint32).max;
        bool marked;
        bool matched;
        bytes32 expected;
        if (recipients.length == 1 && amounts.length == 1) {
            expected = keccak256(abi.encodePacked(hex"6a38", getDepositData(vout, recipients, amounts)));
        } else if (!coinbase) revert("single deposit recipient");
        uint256 count = BitcoinTxParser.outputCount(raw);
        require(count <= 32, "deposit outputs");
        for (uint32 i; i < count; ++i) {
            (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, i);
            if (_hasDepositMarker(script)) {
                marked = true;
                if (amount == 0 && expected != 0 && keccak256(script) == expected) matched = true;
            }
        }
        // Only an unmarked coinbase uses the threshold's reward allocation policy.
        // A present SMP1 recipient must never silently fall back to that policy.
        if (coinbase && !marked) return;
        require(recipients.length == 1 && amounts.length == 1, "single deposit recipient");
        require(matched, "deposit allocation commitment");
    }

    function _checkExternalFunding(bytes calldata raw) private view {
        if (custodyTransfer != address(0)) require(
            !IBitcoinCustodyTransferBinding(custodyTransfer).isSourceFunding(raw), "use custody transfer");
    }

    function _hasDepositMarker(bytes memory script) private pure returns (bool) {
        if (script.length < 2 || script[0] != 0x6a) return false;
        uint8 push = uint8(script[1]);
        uint256 offset;
        if (push > 0 && push <= 75) offset = 2;
        else if (push == 0x4c) offset = 3;
        else if (push == 0x4d) offset = 4;
        else if (push == 0x4e) offset = 6;
        else return false;
        // Recognize even malformed/nonminimal pushes, then require exact canonical
        // bytes above. Otherwise malformed marked rewards could bypass the binding.
        return script.length >= offset + 4 && script[offset] == 0x53 && script[offset+1] == 0x4d
            && script[offset+2] == 0x50 && script[offset+3] == 0x31;
    }

    function mintFromProof(
        bytes calldata raw,
        uint32 vout,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index,
        address[] calldata recipients,
        uint256[] calldata amounts,
        bytes32 op
    ) external nonReentrant {
        require(address(deposits.addressRegistry()) == address(0), "use generation-bound mint");
        require(op == getMintOperationHash(BitcoinTxParser.txId(raw), vout, recipients, amounts), "hash");
        _mintFromProof(raw, vout, blockHash, siblings, index, recipients, amounts, op);
    }

    /// @notice SMP1/coinbase rules are unchanged; current custody generation is explicit in authorization.
    function mintFromProofV2(bytes calldata raw, uint32 vout, bytes32 blockHash, bytes32[] calldata siblings,
        uint256 index, address[] calldata recipients, uint256[] calldata amounts, uint256 custodyGeneration,
        bytes32 op) external nonReentrant
    {
        IBitcoinAddressRegistry registry = deposits.addressRegistry();
        require(address(registry) != address(0), "custody mode required");
        registry.requireCustody(custodyGeneration);
        require(op == getCustodyMintOperationHash(BitcoinTxParser.txId(raw), vout, recipients,
            amounts, custodyGeneration), "hash");
        _mintFromProof(raw, vout, blockHash, siblings, index, recipients, amounts, op);
    }

    function _mintFromProof(bytes calldata raw, uint32 vout, bytes32 blockHash, bytes32[] calldata siblings,
        uint256 index, address[] calldata recipients, uint256[] calldata amounts, bytes32 op) private
    {
        require(recipients.length > 0 && recipients.length <= 64 && recipients.length == amounts.length, "distribution");
        require(keccak256(payoutScript) == deposits.poolScripts(address(this)), "wallet registration");
        _checkDepositAllocation(raw, vout, recipients, amounts);
        bytes32 txid = BitcoinTxParser.txId(raw);
        authorization.consumeOperation(op);
        nonce++;
        uint64 sats = deposits.consume(raw, vout, blockHash, siblings, index);
        uint256 total;
        for (uint256 i; i < amounts.length; i++) {
            require(recipients[i] != address(0), "recipient");
            total += amounts[i];
            mpToken.mint(recipients[i], amounts[i]);
        }
        require(total == uint256(sats) * 1e10, "distribution amount");
        mintedAllocations[keccak256(abi.encode(txid, vout))] = keccak256(abi.encode(recipients, amounts));
        emit Minted(txid, vout, sats);
    }

    function mintFromAddressProof(bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings,
        uint256 index, IBitcoinAddressRegistry.MintClaim calldata claim, bytes32 op) external nonReentrant
    {
        _checkExternalFunding(raw);
        require(op == getAddressMintOperationHash(claim), "hash");
        authorization.consumeOperation(op);
        ++nonce;
        uint64 sats = deposits.consumeAddress(raw, blockHash, siblings, index, claim);
        uint256 amount = uint256(sats) * 1e10;
        mpToken.mint(claim.recipient, amount);
        address[] memory recipients = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        recipients[0] = claim.recipient;
        amounts[0] = amount;
        bytes32 outpoint = keccak256(abi.encode(claim.txid, claim.vout));
        mintedAllocations[outpoint] = keccak256(abi.encode(recipients, amounts));
        mintedAddressIntents[outpoint] = claim.intentId;
        mintedAddressSats[outpoint] = sats;
        emit Minted(claim.txid, claim.vout, sats);
        emit AddressMinted(claim.intentId, claim.txid, claim.vout, claim.recipient, sats, claim.validationProvenance);
    }
}
