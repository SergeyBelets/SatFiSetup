// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinDepositRegistry.sol";

interface IReserveMintAdmissionBinding {
    function pool() external view returns (address);
    function authorization() external view returns (address);
}

interface IReserveTransferBinding {
    function mining() external view returns (address);
    function isSourceFunding(bytes calldata raw) external view returns (bool);
}

interface IReserveWithdrawalRecovery {
    function recovery() external view returns (address);
}

interface IReserveRecoveryPolicy {
    function paused() external view returns (bool);
}

interface IReservePool {
    function shareToken() external view returns (address);
    function withdrawalEscrow() external view returns (address);
    function bitcoinReserve() external view returns (address);
    function mintFromReserve(address recipient, uint256 amount, bool selfWrap) external returns (uint256);
    function previewDirectDepositFee(uint256 amount) external view returns (uint256);
    function mintFromDirectDeposit(address recipient, uint256 amount, uint256 fixedFee) external returns (uint256);
    function lockCustodyQuote(uint256 id, address owner, address recipient, uint256 amount, uint256 minimum)
        external returns (uint256);
    function cancelCustodyQuote(uint256 id) external;
    function mintFromCustodyQuote(uint256 id, address recipient, uint256 amount) external returns (uint256);
}

/// @notice Records Bitcoin backing and issues the confederation's S without an intermediate token.
contract ConfederationBitcoinReserve is ReentrancyGuard {
    uint256 private constant SAT = 1e10;
    uint64 private constant MAX_MONEY = 21_000_000 * 1e8;
    BitcoinDepositRegistry public immutable deposits;
    BitcoinWithdrawalEscrow public immutable escrow;
    IReservePool public immutable confederationPool;
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
    mapping(bytes32 => uint256) private directDepositFeesPlusOne;
    error DirectDepositQuoteRequired();
    error InvalidDirectDepositQuote();
    error BitcoinRecoveryPaused();
    event Minted(bytes32 indexed txid, uint32 indexed vout, uint64 satoshis);
    event MintAdmissionBound(address indexed admission);
    event MintNonceExpired(uint256 indexed nonce);
    event CustodyTransferBound(address indexed gateway);
    event CustodyTransferMinted(bytes32 indexed claim, bytes32 indexed txid, uint32 vout, uint64 satoshis);
    event DirectDepositQuoted(bytes32 indexed txid, uint32 indexed vout, address indexed recipient,
        uint64 satoshis, uint256 fee, uint256 net);

    function shareToken() external view returns (address) { return confederationPool.shareToken(); }

    function payoutScriptHash() external view returns (bytes32) { return keccak256(payoutScript); }

    function rotatePayoutScript(bytes32 successorKey) external {
        require(msg.sender == IReserveWithdrawalRecovery(address(escrow)).recovery(), "recovery binding");
        bytes memory script = abi.encodePacked(hex"5120", successorKey);
        require(keccak256(script) == deposits.poolScripts(address(this)), "wallet registration");
        payoutScript = script;
    }

    function _requireNewFunding() private view {
        address recovery = IReserveWithdrawalRecovery(address(escrow)).recovery();
        if (recovery != address(0) && IReserveRecoveryPolicy(recovery).paused()) revert BitcoinRecoveryPaused();
    }

    function getSetCustodyTransferOperationHash(address gateway) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.setCustodyTransfer.selector,
            keccak256(abi.encode(gateway)), 0);
    }

    function setCustodyTransfer(address gateway, bytes32 op) external nonReentrant {
        require(custodyTransfer == address(0) && nonce == 0 && gateway.code.length > 0
            && IReserveTransferBinding(gateway).mining() == address(this), "transfer binding");
        require(op == getSetCustodyTransferOperationHash(gateway), "hash");
        authorization.consumeOperation(op);
        custodyTransfer = gateway;
        emit CustodyTransferBound(gateway);
    }

    function lockCustodyQuote(uint256 id, address owner, address recipient, uint256 amount, uint256 minimum)
        external nonReentrant returns (uint256)
    {
        _requireTransferGateway();
        _requireNewFunding();
        return confederationPool.lockCustodyQuote(id, owner, recipient, amount, minimum);
    }

    function cancelCustodyQuote(uint256 id) external nonReentrant {
        _requireTransferGateway();
        confederationPool.cancelCustodyQuote(id);
    }

    function _requireTransferGateway() private view {
        require(msg.sender == custodyTransfer && msg.sender != address(0), "transfer gateway only");
    }

    function mintFromCustodyTransfer(bytes calldata raw, uint32 vout, bytes32 claim,
        bytes32 blockHash, bytes32[] calldata siblings, uint256 index,
        uint256[] calldata ids, address[] calldata recipients, uint256[] calldata amounts)
        external nonReentrant returns (uint256[] memory shares)
    {
        _requireTransferGateway();
        require(recipients.length > 0 && recipients.length <= 32 && recipients.length == amounts.length
            && recipients.length == ids.length, "distribution");
        uint256 total;
        for (uint256 i; i < amounts.length; ++i) total += amounts[i];
        require(total > 0 && total % 1e10 == 0 && total / 1e10 <= type(uint64).max, "transfer amount");
        deposits.consumeTransfer(raw, vout, uint64(total / 1e10), claim, blockHash, siblings, index);
        shares = new uint256[](amounts.length);
        for (uint256 i; i < amounts.length; ++i) {
            shares[i] = confederationPool.mintFromCustodyQuote(ids[i], recipients[i], amounts[i]);
        }
        emit CustodyTransferMinted(claim, BitcoinTxParser.txId(raw), vout, uint64(total / 1e10));
    }
    event AddressMinted(bytes32 indexed intentId, bytes32 indexed txid, uint32 indexed vout,
        address recipient, uint64 satoshis, bytes32 validationProvenance);

    constructor(
        string memory id,
        address depositRegistry,
        address withdrawalEscrow,
        address pool
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
        confederationPool = IReservePool(pool);
        require(confederationPool.withdrawalEscrow() == withdrawalEscrow, "reserve pool binding");
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
            IReserveMintAdmissionBinding(admission).pool() == address(this)
                && IReserveMintAdmissionBinding(admission).authorization() == address(authorization),
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

    /// @notice The recipient locks terms for one exact Bitcoin output before paying.
    /// The quote does not expire because that transaction can confirm later.
    function lockDirectDepositQuote(bytes32 txid, uint32 vout, uint64 satoshis, uint256 minimumShares)
        external nonReentrant returns (uint256 fee, uint256 net)
    {
        _requireNewFunding();
        if (txid == 0 || satoshis == 0 || satoshis > MAX_MONEY || minimumShares == 0
            || confederationPool.bitcoinReserve() != address(this)
            || keccak256(payoutScript) != deposits.poolScripts(address(this))) revert InvalidDirectDepositQuote();
        uint256 gross = uint256(satoshis) * SAT;
        bytes32 key = keccak256(abi.encode(txid, vout, msg.sender, satoshis));
        uint256 stored = directDepositFeesPlusOne[key];
        fee = stored == 0 ? confederationPool.previewDirectDepositFee(gross) : stored - 1;
        if (fee >= gross || gross - fee < minimumShares) revert InvalidDirectDepositQuote();
        net = gross - fee;
        if (stored == 0) {
            if (deposits.consumed(keccak256(abi.encode(txid, vout)))) revert InvalidDirectDepositQuote();
            directDepositFeesPlusOne[key] = fee + 1;
            emit DirectDepositQuoted(txid, vout, msg.sender, satoshis, fee, net);
        }
    }

    function directDepositQuote(bytes32 txid, uint32 vout, address recipient, uint64 satoshis)
        public view returns (uint256 fee, uint256 net)
    {
        uint256 stored = directDepositFeesPlusOne[keccak256(abi.encode(txid, vout, recipient, satoshis))];
        if (stored != 0) {
            fee = stored - 1;
            net = uint256(satoshis) * SAT - fee;
        }
    }

    function _quotedFee(bytes32 txid, uint32 vout, address recipient, uint64 satoshis) private view returns (uint256) {
        (uint256 fee, uint256 net) = directDepositQuote(txid, vout, recipient, satoshis);
        if (net == 0) revert DirectDepositQuoteRequired();
        return fee;
    }

    function _checkDepositAllocation(bytes calldata raw, uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        private view returns (bool)
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
        if (coinbase && !marked) return false;
        require(recipients.length == 1 && amounts.length == 1, "single deposit recipient");
        require(matched, "deposit allocation commitment");
        return true;
    }

    function _checkExternalFunding(bytes calldata raw) private view {
        if (custodyTransfer != address(0)) require(
            !IReserveTransferBinding(custodyTransfer).isSourceFunding(raw), "use custody transfer");
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
        bool quoted = _checkDepositAllocation(raw, vout, recipients, amounts);
        if (!quoted) _requireNewFunding();
        bytes32 txid = BitcoinTxParser.txId(raw);
        authorization.consumeOperation(op);
        nonce++;
        uint64 sats = deposits.consume(raw, vout, blockHash, siblings, index);
        uint256 gross = uint256(sats) * SAT;
        if (quoted) {
            require(amounts[0] == gross, "distribution amount");
            confederationPool.mintFromDirectDeposit(recipients[0], gross, _quotedFee(txid, vout, recipients[0], sats));
        } else {
            uint256 total;
            for (uint256 i; i < amounts.length; i++) {
                require(recipients[i] != address(0), "recipient");
                total += amounts[i];
                confederationPool.mintFromReserve(recipients[i], amounts[i], false);
            }
            require(total == gross, "distribution amount");
        }
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
        uint256 amount = uint256(sats) * SAT;
        uint256 fee = _quotedFee(claim.txid, claim.vout, claim.recipient, sats);
        confederationPool.mintFromDirectDeposit(claim.recipient, amount, fee);
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
