// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "./SimpleSPVContract.sol";
import "../tokens/PoolMpToken.sol";
import "./IDistributionScheme.sol";
import "../libs/BTCScriptConstructor.sol";
import "./BitcoinRedemptionTxBuilder.sol";

/**
 * @title SimpleMiningPoolWithCalculator
 * @notice Mining pool с поддержкой калькуляторов распределения
 * @dev Demo версия с ManualPercentageCalculator или ManualDirectCalculator
 * @dev Includes BTC address mapping, unsigned tx construction, and SPV burn verification
 */
contract SimpleMiningPoolWithCalculator is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ======== CONSTANTS ========
    /// @notice Multiplier to convert satoshis (8 decimals) to token units (18 decimals)
    uint256 public constant SAT_TO_TOKEN_MULTIPLIER = 1e10;
    uint256 public constant RESERVATION_CANCEL_DELAY = 7 days;

    SimpleSPVContract public immutable spvContract;
    BitcoinRedemptionTxBuilder public immutable redemptionTxBuilder;
    PoolMpToken public mpToken;
    IDistributionScheme public calculator;

    /// @notice Custom SPV confirmation depth (0 = use default)
    uint32 public confirmationDepth;

    string public poolId;
    bytes public payoutScript;

    struct UTXO {
        bytes32 txid;
        uint32 vout;
        uint64 amountSat;
        bytes32 blockHash;
        bool isDistributed;
        bool isReserved;
        bool isSpent;
        bytes32 reservationHash;
    }

    struct RedemptionRequest {
        address requester;
        uint64 amountSat;
        bytes btcScript;
        bool confirmed;
        uint256 timestamp;
        bytes32 burnTxid; // Bitcoin transaction ID that fulfilled this redemption
        bool spvVerified; // Whether the burn was verified via SPV
        bool isReserved; // Whether pool UTXOs have been reserved for this request
        bytes32 reservationHash; // Reservation that must be settled by the BTC transaction
    }

    mapping(bytes32 => UTXO) public utxos;
    mapping(uint256 => RedemptionRequest) public redemptions;
    mapping(uint256 => bool) public redemptionApproved;
    mapping(uint256 => bool) public redemptionRefunded;
    uint256 public nextRedemptionId;
    uint64 public availableReserveSat;
    uint64 public reservedForRedemptionsSat;
    uint64 public spentReserveSat;
    mapping(bytes32 => uint64) public reservedInputByReservation;
    mapping(bytes32 => uint64) public reservedPayoutByReservation;
    mapping(bytes32 => uint256) public reservedRequestCountByReservation;
    mapping(bytes32 => uint256) public reservationCreatedAt;
    mapping(bytes32 => bool) public reservationSettled;
    mapping(bytes32 => bool) public reservationCancelled;
    mapping(bytes32 => uint256[]) private reservedRedemptionIds;

    // BTC address mapping for reward distribution fallback
    // ethAddress => btcScript (for users who didn't provide merkle proof)
    mapping(address => bytes) public ethToBtcScript;

    // Client UUID tracking (optional, for reference)
    mapping(address => string) public ethToClientUuid;

    event RewardRegistered(bytes32 indexed utxoKey, bytes32 indexed txid, uint64 amount);
    event RewardDistributed(bytes32 indexed utxoKey, uint256 totalAmount, uint256 recipients);
    event RedemptionRequested(uint256 indexed redemptionId, address indexed requester, uint64 amount);
    event RedemptionConfirmed(uint256 indexed redemptionId, bool approved);
    event RedemptionRefunded(uint256 indexed redemptionId, address indexed requester, uint256 tokenAmount);
    event CalculatorUpdated(address indexed oldCalculator, address indexed newCalculator);
    event BtcAddressMapped(address indexed ethAddress, bytes btcScript, string clientUuid);
    event RedemptionTxConstructed(uint256 indexed redemptionId, bytes32 sighash);
    event BurnVerifiedViaSPV(uint256 indexed redemptionId, bytes32 indexed burnTxid, bytes32 blockHash);
    event RedemptionReserveCommitted(
        bytes32 indexed reservationHash, uint64 inputAmount, uint64 payoutAmount, uint64 minerFeeSat
    );
    event RedemptionReservationReleased(bytes32 indexed reservationHash, uint64 inputAmount, uint64 payoutAmount);
    event ReservedUTXOSpent(bytes32 indexed reservationHash, bytes32 indexed txid, uint32 vout, uint64 amountSat);

    error BlockNotMature();
    error UTXONotFound();
    error AlreadyDistributed();
    error InvalidCalculator();

    constructor(address _spv, string memory _poolId, bytes memory _payoutScript, uint32 _confirmationDepth)
        Ownable(msg.sender)
    {
        spvContract = SimpleSPVContract(_spv);
        redemptionTxBuilder = new BitcoinRedemptionTxBuilder();
        poolId = _poolId;
        payoutScript = _payoutScript;
        confirmationDepth = _confirmationDepth;

        // SPV admin must register this pool after deployment.
    }

    // ======== Configuration ========

    function setMpToken(address _mpToken) external onlyOwner {
        require(_mpToken != address(0), "Invalid token");
        mpToken = PoolMpToken(_mpToken);
    }

    function setCalculator(address _calculator) external onlyOwner {
        require(_calculator != address(0), "Invalid calculator");
        address oldCalculator = address(calculator);
        calculator = IDistributionScheme(_calculator);
        emit CalculatorUpdated(oldCalculator, _calculator);
    }

    /**
     * @notice Map ETH address to BTC script for fallback redemption
     * @param ethAddress Ethereum address
     * @param btcScript Bitcoin script (P2PKH, P2WPKH, etc.)
     * @param clientUuid Optional client UUID for tracking
     */
    function mapBtcAddress(address ethAddress, bytes calldata btcScript, string calldata clientUuid)
        external
        onlyOwner
    {
        _mapBtcAddress(ethAddress, btcScript, clientUuid);
    }

    function _mapBtcAddress(address ethAddress, bytes calldata btcScript, string calldata clientUuid) internal {
        require(ethAddress != address(0), "Invalid ETH address");
        require(btcScript.length > 0, "Invalid BTC script");

        ethToBtcScript[ethAddress] = btcScript;
        if (bytes(clientUuid).length > 0) {
            ethToClientUuid[ethAddress] = clientUuid;
        }

        emit BtcAddressMapped(ethAddress, btcScript, clientUuid);
    }

    /**
     * @notice Batch map multiple ETH addresses to BTC scripts
     * @param ethAddresses Array of Ethereum addresses
     * @param btcScripts Array of Bitcoin scripts
     * @param clientUuids Array of client UUIDs
     */
    function batchMapBtcAddresses(
        address[] calldata ethAddresses,
        bytes[] calldata btcScripts,
        string[] calldata clientUuids
    ) external onlyOwner {
        require(
            ethAddresses.length == btcScripts.length && ethAddresses.length == clientUuids.length,
            "Array length mismatch"
        );

        for (uint256 i = 0; i < ethAddresses.length; i++) {
            _mapBtcAddress(ethAddresses[i], btcScripts[i], clientUuids[i]);
        }
    }

    /**
     * @notice Get BTC script for an ETH address (fallback if not provided)
     * @param ethAddress Ethereum address
     * @return BTC script or empty if not mapped
     */
    function getBtcScript(address ethAddress) external view returns (bytes memory) {
        return ethToBtcScript[ethAddress];
    }

    // ======== Reward Registration ========

    function registerReward(
        bytes32 txid,
        uint32 vout,
        uint64 amountSat,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index
    ) external onlyOwner returns (bytes32 utxoKey) {
        if (!spvContract.isMatureWithDepth(blockHash, confirmationDepth)) revert BlockNotMature();
        require(spvContract.checkTxInclusion(blockHash, txid, siblings, index), "TX not in block");

        utxoKey = keccak256(abi.encodePacked(txid, vout));
        require(utxos[utxoKey].amountSat == 0, "UTXO already registered");

        utxos[utxoKey] = UTXO({
            txid: txid,
            vout: vout,
            amountSat: amountSat,
            blockHash: blockHash,
            isDistributed: false,
            isReserved: false,
            isSpent: false,
            reservationHash: bytes32(0)
        });
        availableReserveSat += amountSat;

        // Register this UTXO in SPV for source verification
        spvContract.registerPoolUTXO(txid, vout);

        emit RewardRegistered(utxoKey, txid, amountSat);
    }

    // ======== Reward Distribution (с калькулятором) ========

    function distributeRewardWithCalculator(
        bytes32 utxoKey,
        IDistributionScheme.WorkerData[] memory workers,
        IDistributionScheme.SchemeParams memory params
    ) external onlyOwner nonReentrant {
        if (address(calculator) == address(0)) revert InvalidCalculator();

        UTXO storage utxo = utxos[utxoKey];
        if (utxo.amountSat == 0) revert UTXONotFound();
        if (utxo.isDistributed) revert AlreadyDistributed();

        // Расчет распределения через калькулятор
        (IDistributionScheme.DistributionResult[] memory results, uint256 distributedAmount, uint256 remainder) =
            calculator.calculate(utxo.amountSat, workers, params);

        // Mint MP tokens (convert satoshis to token units)
        for (uint256 i = 0; i < results.length; i++) {
            if (results[i].amount > 0) {
                mpToken.mint(results[i].recipient, results[i].amount * SAT_TO_TOKEN_MULTIPLIER);
            }
        }

        utxo.isDistributed = true;
        emit RewardDistributed(utxoKey, distributedAmount + remainder, results.length);
    }

    // ======== Redemption ========

    /**
     * @notice Request redemption with optional BTC script
     * @param amountSat Amount to redeem in satoshis
     * @param btcScript Bitcoin script (or empty to use mapped address)
     */
    function requestRedemption(uint64 amountSat, bytes calldata btcScript)
        external
        nonReentrant
        returns (uint256 redemptionId)
    {
        require(amountSat > 0, "Invalid amount");

        // Use provided script or fallback to mapped address
        bytes memory finalScript = btcScript;
        if (btcScript.length == 0) {
            finalScript = ethToBtcScript[msg.sender];
            require(finalScript.length > 0, "No BTC address mapped");
        }

        uint256 tokenAmount = uint256(amountSat) * SAT_TO_TOKEN_MULTIPLIER;
        IERC20(address(mpToken)).safeTransferFrom(msg.sender, address(this), tokenAmount);

        redemptionId = nextRedemptionId++;
        redemptions[redemptionId] = RedemptionRequest({
            requester: msg.sender,
            amountSat: amountSat,
            btcScript: finalScript,
            confirmed: false,
            timestamp: block.timestamp,
            burnTxid: bytes32(0),
            spvVerified: false,
            isReserved: false,
            reservationHash: bytes32(0)
        });

        emit RedemptionRequested(redemptionId, msg.sender, amountSat);
    }

    /**
     * @notice Construct unsigned Bitcoin transaction for redemption
     * @param redemptionIds Array of redemption request IDs to include
     * @param poolUTXOs Array of pool UTXOs to spend
     * @param changeScript Change address script
     * @param minerFeeSat Explicit Bitcoin miner fee paid by the selected inputs
     * @return unsignedTx Unsigned transaction data with sighash
     * @return encodedTx Encoded transaction bytes
     * @return reservationHash Hash of the reserved redemption plan
     */
    function constructRedemptionTx(
        uint256[] calldata redemptionIds,
        BTCScriptConstructor.UTXOInput[] memory poolUTXOs,
        bytes memory changeScript,
        uint64 minerFeeSat
    )
        external
        onlyOwner
        returns (BTCScriptConstructor.UnsignedTx memory unsignedTx, bytes memory encodedTx, bytes32 reservationHash)
    {
        require(redemptionIds.length > 0, "No redemptions");
        require(poolUTXOs.length > 0, "No inputs");
        require(minerFeeSat > 0, "Fee required");

        // Build outputs from redemption requests
        BTCScriptConstructor.TxOutput[] memory outputs = new BTCScriptConstructor.TxOutput[](redemptionIds.length);
        uint64 payoutAmount = 0;

        for (uint256 i = 0; i < redemptionIds.length; i++) {
            RedemptionRequest storage req = redemptions[redemptionIds[i]];
            require(req.requester != address(0), "Invalid redemption ID");
            require(!req.confirmed, "Already confirmed");
            require(!req.isReserved, "Redemption already reserved");

            outputs[i] = BTCScriptConstructor.TxOutput({amount: req.amountSat, scriptPubKey: req.btcScript});
            payoutAmount += req.amountSat;
        }

        uint64 inputAmount = 0;
        for (uint256 i = 0; i < poolUTXOs.length; i++) {
            bytes32 utxoKey = keccak256(abi.encodePacked(poolUTXOs[i].txid, poolUTXOs[i].vout));
            UTXO storage utxo = utxos[utxoKey];
            require(utxo.amountSat > 0, "Unknown pool UTXO");
            require(!utxo.isReserved, "UTXO already reserved");
            require(!utxo.isSpent, "UTXO already spent");
            require(utxo.amountSat == poolUTXOs[i].amount, "UTXO amount mismatch");
            inputAmount += utxo.amountSat;
        }

        require(inputAmount >= payoutAmount + minerFeeSat, "Insufficient reserved input");
        require(availableReserveSat >= inputAmount, "Insufficient available reserve");

        reservationHash = keccak256(
            abi.encode(
                block.chainid,
                address(this),
                redemptionIds,
                _hashPoolInputs(poolUTXOs),
                payoutAmount,
                minerFeeSat,
                changeScript
            )
        );
        require(
            reservedInputByReservation[reservationHash] == 0 && !reservationSettled[reservationHash]
                && !reservationCancelled[reservationHash],
            "Reservation exists"
        );

        (unsignedTx, encodedTx) =
            redemptionTxBuilder.constructRedemptionTx(poolUTXOs, outputs, changeScript, minerFeeSat);

        availableReserveSat -= inputAmount;
        reservedForRedemptionsSat += inputAmount;
        reservedInputByReservation[reservationHash] = inputAmount;
        reservedPayoutByReservation[reservationHash] = payoutAmount;
        reservedRequestCountByReservation[reservationHash] = redemptionIds.length;
        reservationCreatedAt[reservationHash] = block.timestamp;

        for (uint256 i = 0; i < poolUTXOs.length; i++) {
            bytes32 utxoKey = keccak256(abi.encodePacked(poolUTXOs[i].txid, poolUTXOs[i].vout));
            UTXO storage utxo = utxos[utxoKey];
            utxo.isReserved = true;
            utxo.reservationHash = reservationHash;
        }

        for (uint256 i = 0; i < redemptionIds.length; i++) {
            RedemptionRequest storage req = redemptions[redemptionIds[i]];
            req.isReserved = true;
            req.reservationHash = reservationHash;
            reservedRedemptionIds[reservationHash].push(redemptionIds[i]);
            if (unsignedTx.sighashes.length > 0) {
                emit RedemptionTxConstructed(redemptionIds[i], unsignedTx.sighashes[0]);
            }
        }

        emit RedemptionReserveCommitted(reservationHash, inputAmount, payoutAmount, minerFeeSat);

        return (unsignedTx, encodedTx, reservationHash);
    }

    function confirmRedemption(uint256 redemptionId, bool approved) external onlyOwner nonReentrant {
        RedemptionRequest storage request = redemptions[redemptionId];
        require(request.requester != address(0), "Request not found");
        require(!request.confirmed, "Already confirmed");
        require(!request.spvVerified, "Already verified");
        require(!request.isReserved, "Reserved requires SPV");

        request.confirmed = true;
        redemptionApproved[redemptionId] = approved;
        if (approved) {
            _burnEscrowedRedemption(redemptionId, request);
        } else {
            _refundEscrowedRedemption(redemptionId, request);
        }
        emit RedemptionConfirmed(redemptionId, approved);
    }

    /**
     * @notice Verify a burn transaction via SPV with source wallet verification
     * @param redemptionId The redemption request ID
     * @param burnTxid Transaction ID of the burn transaction
     * @param blockHash Block hash containing the transaction
     * @param siblings Merkle proof siblings
     * @param index Transaction index in the block
     * @param rawTx Raw Bitcoin transaction bytes (for source verification)
     */
    function verifyBurnViaSPV(
        uint256 redemptionId,
        bytes32 burnTxid,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index,
        bytes calldata rawTx
    ) external onlyOwner nonReentrant {
        RedemptionRequest storage request = redemptions[redemptionId];
        require(request.requester != address(0), "Request not found");
        require(!request.confirmed, "Already confirmed");
        require(!request.spvVerified, "Already verified");
        require(request.isReserved, "Redemption not reserved");
        require(!reservationCancelled[request.reservationHash], "Reservation cancelled");

        // Verify block is mature
        if (!spvContract.isMatureWithDepth(blockHash, confirmationDepth)) revert BlockNotMature();

        _verifyBurnTransaction(request, burnTxid, blockHash, siblings, index, rawTx);
        _finalizeVerifiedRedemption(redemptionId, request, burnTxid, blockHash, rawTx);
    }

    /**
     * @notice Batch verify multiple burns via SPV
     * @param redemptionIds Array of redemption IDs
     * @param burnTxids Array of burn transaction IDs
     * @param blockHash Block containing all transactions (or array if different blocks)
     * @param siblings Array of merkle proofs
     * @param indices Transaction indices
     * @param rawTxs Raw Bitcoin transactions for source and output verification
     */
    function batchVerifyBurnsViaSPV(
        uint256[] calldata redemptionIds,
        bytes32[] calldata burnTxids,
        bytes32 blockHash,
        bytes32[][] calldata siblings,
        uint256[] calldata indices,
        bytes[] calldata rawTxs
    ) external onlyOwner nonReentrant {
        require(
            redemptionIds.length == burnTxids.length && redemptionIds.length == siblings.length
                && redemptionIds.length == indices.length && redemptionIds.length == rawTxs.length,
            "Array length mismatch"
        );

        // Verify block is mature
        if (!spvContract.isMatureWithDepth(blockHash, confirmationDepth)) revert BlockNotMature();

        for (uint256 i = 0; i < redemptionIds.length; i++) {
            RedemptionRequest storage request = redemptions[redemptionIds[i]];
            require(request.requester != address(0), "Request not found");
            require(!request.confirmed, "Already confirmed");
            require(!request.spvVerified, "Already verified");
            require(request.isReserved, "Redemption not reserved");
            require(!reservationCancelled[request.reservationHash], "Reservation cancelled");

            _verifyBurnTransaction(request, burnTxids[i], blockHash, siblings[i], indices[i], rawTxs[i]);
            _finalizeVerifiedRedemption(redemptionIds[i], request, burnTxids[i], blockHash, rawTxs[i]);
        }
    }

    function _verifyBurnTransaction(
        RedemptionRequest storage request,
        bytes32 burnTxid,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index,
        bytes calldata rawTx
    ) internal view {
        require(spvContract.checkTxInclusion(blockHash, burnTxid, siblings, index), "TX not in block");
        require(redemptionTxBuilder.txIdMatches(rawTx, burnTxid), "TXID mismatch");
        require(spvContract.verifyTxSource(rawTx), "Transaction not from pool wallet");
        require(
            redemptionTxBuilder.sumOutputsToScript(rawTx, request.btcScript) >= request.amountSat,
            "Redemption output mismatch"
        );
    }

    function _finalizeVerifiedRedemption(
        uint256 redemptionId,
        RedemptionRequest storage request,
        bytes32 burnTxid,
        bytes32 blockHash,
        bytes calldata rawTx
    ) internal {
        _settleReservedInputs(rawTx, request.reservationHash);
        _burnEscrowedRedemption(redemptionId, request);

        request.burnTxid = burnTxid;
        request.spvVerified = true;
        redemptionApproved[redemptionId] = true;
        request.confirmed = true;

        emit BurnVerifiedViaSPV(redemptionId, burnTxid, blockHash);
    }

    function releaseExpiredReservation(
        bytes32 reservationHash,
        uint256[] calldata redemptionIds,
        BTCScriptConstructor.UTXOInput[] calldata poolUTXOs
    ) external onlyOwner nonReentrant {
        require(reservationHash != bytes32(0), "Invalid reservation");
        require(!reservationSettled[reservationHash], "Reservation settled");
        require(!reservationCancelled[reservationHash], "Reservation cancelled");
        require(reservedInputByReservation[reservationHash] > 0, "Reservation not found");
        require(
            block.timestamp >= reservationCreatedAt[reservationHash] + RESERVATION_CANCEL_DELAY,
            "Cancel delay not reached"
        );
        require(redemptionIds.length == reservedRequestCountByReservation[reservationHash], "Request set mismatch");
        uint256[] storage storedRedemptionIds = reservedRedemptionIds[reservationHash];
        require(redemptionIds.length == storedRedemptionIds.length, "Stored request set mismatch");
        for (uint256 i = 0; i < redemptionIds.length; i++) {
            require(redemptionIds[i] == storedRedemptionIds[i], "Request order mismatch");
        }

        uint64 releasedInput = 0;
        for (uint256 i = 0; i < poolUTXOs.length; i++) {
            bytes32 utxoKey = keccak256(abi.encodePacked(poolUTXOs[i].txid, poolUTXOs[i].vout));
            UTXO storage utxo = utxos[utxoKey];
            if (utxo.reservationHash == reservationHash) {
                require(utxo.isReserved, "UTXO not reserved");
                require(!utxo.isSpent, "UTXO already spent");
                releasedInput += utxo.amountSat;
                utxo.isReserved = false;
                utxo.reservationHash = bytes32(0);
            }
        }

        uint64 reservedInput = reservedInputByReservation[reservationHash];
        require(releasedInput == reservedInput, "Reserved input set mismatch");

        uint64 releasedPayout = 0;
        for (uint256 i = 0; i < redemptionIds.length; i++) {
            RedemptionRequest storage request = redemptions[redemptionIds[i]];
            require(request.requester != address(0), "Request not found");
            require(request.reservationHash == reservationHash, "Redemption set mismatch");
            require(request.isReserved, "Redemption not reserved");
            require(!request.confirmed, "Already confirmed");
            require(!request.spvVerified, "Already verified");

            releasedPayout += request.amountSat;
            request.isReserved = false;
            request.reservationHash = bytes32(0);
            request.confirmed = true;
            _refundEscrowedRedemption(redemptionIds[i], request);
            emit RedemptionConfirmed(redemptionIds[i], false);
        }

        require(releasedPayout == reservedPayoutByReservation[reservationHash], "Reserved payout mismatch");

        reservedForRedemptionsSat -= reservedInput;
        availableReserveSat += reservedInput;
        reservationCancelled[reservationHash] = true;

        delete reservedInputByReservation[reservationHash];
        delete reservedPayoutByReservation[reservationHash];
        delete reservedRequestCountByReservation[reservationHash];
        delete reservationCreatedAt[reservationHash];
        delete reservedRedemptionIds[reservationHash];

        emit RedemptionReservationReleased(reservationHash, releasedInput, releasedPayout);
    }

    function _settleReservedInputs(bytes calldata rawTx, bytes32 reservationHash) internal {
        require(reservationHash != bytes32(0), "Invalid reservation");
        require(!reservationCancelled[reservationHash], "Reservation cancelled");
        if (reservationSettled[reservationHash]) return;

        (bytes32[] memory prevTxIdsLE, uint32[] memory vouts) = redemptionTxBuilder.parseAllInputsLE(rawTx);
        bool spentReservedInput = false;
        uint64 matchedInputAmount = 0;

        for (uint256 i = 0; i < prevTxIdsLE.length; i++) {
            bytes32 prevTxIdLE = prevTxIdsLE[i];
            bytes32 utxoKey = keccak256(abi.encodePacked(prevTxIdLE, vouts[i]));
            UTXO storage utxo = utxos[utxoKey];

            if (utxo.reservationHash == reservationHash) {
                require(utxo.isReserved, "UTXO not reserved");
                if (!utxo.isSpent) {
                    matchedInputAmount += utxo.amountSat;
                    utxo.isSpent = true;
                    spvContract.markPoolUTXOSpent(prevTxIdLE, vouts[i]);
                    emit ReservedUTXOSpent(reservationHash, prevTxIdLE, vouts[i], utxo.amountSat);
                }
                spentReservedInput = true;
            }
        }

        require(spentReservedInput, "Reserved input not spent");

        uint64 reservedInput = reservedInputByReservation[reservationHash];
        require(reservedInput > 0, "Reservation not found");
        require(matchedInputAmount == reservedInput, "Reserved input set mismatch");
        _verifyReservationPayouts(rawTx, reservationHash);
        reservedForRedemptionsSat -= reservedInput;
        spentReserveSat += reservedInput;
        reservationSettled[reservationHash] = true;
    }

    function _verifyReservationPayouts(bytes calldata rawTx, bytes32 reservationHash) internal view {
        uint256[] storage redemptionIds = reservedRedemptionIds[reservationHash];
        require(
            redemptionIds.length == reservedRequestCountByReservation[reservationHash],
            "Reservation request set missing"
        );

        uint64 totalPayout = 0;
        for (uint256 i = 0; i < redemptionIds.length; i++) {
            RedemptionRequest storage request = redemptions[redemptionIds[i]];
            require(request.reservationHash == reservationHash, "Redemption set mismatch");
            require(
                redemptionTxBuilder.sumOutputsToScript(rawTx, request.btcScript) >= request.amountSat,
                "Reservation output mismatch"
            );
            totalPayout += request.amountSat;
        }

        require(totalPayout == reservedPayoutByReservation[reservationHash], "Reserved payout mismatch");
    }

    function _hashPoolInputs(BTCScriptConstructor.UTXOInput[] memory poolUTXOs) internal pure returns (bytes32) {
        bytes32[] memory inputHashes = new bytes32[](poolUTXOs.length);
        for (uint256 i = 0; i < poolUTXOs.length; i++) {
            inputHashes[i] = keccak256(
                abi.encode(
                    poolUTXOs[i].txid,
                    poolUTXOs[i].vout,
                    poolUTXOs[i].amount,
                    poolUTXOs[i].scriptPubKey,
                    poolUTXOs[i].scriptCode,
                    poolUTXOs[i].sequence
                )
            );
        }
        return keccak256(abi.encode(inputHashes));
    }

    function _burnEscrowedRedemption(uint256 redemptionId, RedemptionRequest storage request) internal {
        require(!redemptionRefunded[redemptionId], "Redemption refunded");
        uint256 tokenAmount = uint256(request.amountSat) * SAT_TO_TOKEN_MULTIPLIER;
        mpToken.burn(address(this), tokenAmount);
    }

    function _refundEscrowedRedemption(uint256 redemptionId, RedemptionRequest storage request) internal {
        require(!redemptionRefunded[redemptionId], "Redemption refunded");
        redemptionRefunded[redemptionId] = true;
        redemptionApproved[redemptionId] = false;
        uint256 tokenAmount = uint256(request.amountSat) * SAT_TO_TOKEN_MULTIPLIER;
        IERC20(address(mpToken)).safeTransfer(request.requester, tokenAmount);
        emit RedemptionRefunded(redemptionId, request.requester, tokenAmount);
    }

    /**
     * @notice Check if a redemption has been verified via SPV
     * @param redemptionId Redemption request ID
     * @return verified Whether SPV verification passed
     * @return burnTxid Bitcoin transaction ID (if verified)
     */
    function getRedemptionVerificationStatus(uint256 redemptionId)
        external
        view
        returns (bool verified, bytes32 burnTxid)
    {
        RedemptionRequest storage request = redemptions[redemptionId];
        return (request.spvVerified, request.burnTxid);
    }

    function getReservedRedemptionIds(bytes32 reservationHash) external view returns (uint256[] memory) {
        return reservedRedemptionIds[reservationHash];
    }

    // ======== ADMIN VIEWS ========

    /**
     * @notice Get list of all pending redemptions
     * @return pendingIds Array of redemption IDs that are not yet confirmed
     * @return pendingRedemptions Array of corresponding redemption details
     */
    function getPendingRedemptions()
        external
        view
        returns (uint256[] memory pendingIds, RedemptionRequest[] memory pendingRedemptions)
    {
        // First pass: count pending redemptions
        uint256 pendingCount = 0;
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].amountSat > 0 && !redemptions[i].confirmed) {
                pendingCount++;
            }
        }

        // Second pass: collect pending redemptions
        pendingIds = new uint256[](pendingCount);
        pendingRedemptions = new RedemptionRequest[](pendingCount);

        uint256 index = 0;
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].amountSat > 0 && !redemptions[i].confirmed) {
                pendingIds[index] = i;
                pendingRedemptions[index] = redemptions[i];
                index++;
            }
        }
    }

    /**
     * @notice Get detailed information about a specific redemption request
     * @param redemptionId The redemption ID to query
     * @return redemption The full redemption details
     * @return requesterBalance Current MP token balance of the requester
     * @return timeSinceRequest Time elapsed since request in seconds
     * @return isSpvVerified Whether the burn was verified via SPV
     */
    function getRedemptionDetails(uint256 redemptionId)
        external
        view
        returns (
            RedemptionRequest memory redemption,
            uint256 requesterBalance,
            uint256 timeSinceRequest,
            bool isSpvVerified
        )
    {
        redemption = redemptions[redemptionId];
        require(redemption.amountSat > 0, "Redemption not found");

        requesterBalance = address(mpToken) != address(0) ? mpToken.balanceOf(redemption.requester) : 0;
        timeSinceRequest = block.timestamp - redemption.timestamp;
        isSpvVerified = redemption.spvVerified;
    }

    /**
     * @notice Get statistics about redemptions
     * @return totalRequests Total number of redemption requests
     * @return pendingCount Number of pending redemptions
     * @return confirmedCount Number of confirmed redemptions
     * @return spvVerifiedCount Number of SPV-verified redemptions
     * @return totalPendingAmount Total amount in pending redemptions (satoshis)
     * @return totalConfirmedAmount Total amount in confirmed redemptions (satoshis)
     */
    function getRedemptionStats()
        external
        view
        returns (
            uint256 totalRequests,
            uint256 pendingCount,
            uint256 confirmedCount,
            uint256 spvVerifiedCount,
            uint256 totalPendingAmount,
            uint256 totalConfirmedAmount
        )
    {
        totalRequests = nextRedemptionId;

        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].amountSat > 0) {
                if (redemptions[i].spvVerified) {
                    spvVerifiedCount++;
                }
                if (redemptions[i].confirmed) {
                    confirmedCount++;
                    totalConfirmedAmount += redemptions[i].amountSat;
                } else {
                    pendingCount++;
                    totalPendingAmount += redemptions[i].amountSat;
                }
            }
        }
    }

    /**
     * @notice Get all reward addresses and their current balances
     * @dev This is a simplified version. For complete history, use events indexing.
     * @return count Number of addresses with mapped BTC scripts
     */
    function getRewardAddressesCount() external pure returns (uint256 count) {
        // Note: Without tracking all addresses in an array, we can't enumerate them
        // This would require additional state variables to track all mapped addresses
        // For now, return 0 as a placeholder
        return 0;
    }

    /**
     * @notice Get all paid rewards for a specific address
     * @param recipient The address to query
     * @return currentBalance Current MP token balance
     * @return totalRedeemed Total amount redeemed by this address
     * @return btcScript Mapped BTC script for this address
     * @return clientUuid Client UUID if set
     */
    function getAddressRewards(address recipient)
        external
        view
        returns (uint256 currentBalance, uint256 totalRedeemed, bytes memory btcScript, string memory clientUuid)
    {
        currentBalance = address(mpToken) != address(0) ? mpToken.balanceOf(recipient) : 0;

        // Calculate total redeemed by this address
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].requester == recipient) {
                totalRedeemed += redemptions[i].amountSat;
            }
        }

        btcScript = ethToBtcScript[recipient];
        clientUuid = ethToClientUuid[recipient];
    }

    /**
     * @notice Get paginated list of redemptions for better UI performance
     * @param offset Starting index
     * @param limit Maximum number of results
     * @return ids Array of redemption IDs
     * @return redeems Array of redemption details
     * @return total Total number of redemptions
     */
    function getRedemptionsPaginated(uint256 offset, uint256 limit)
        external
        view
        returns (uint256[] memory ids, RedemptionRequest[] memory redeems, uint256 total)
    {
        total = nextRedemptionId;

        if (offset >= total) {
            return (new uint256[](0), new RedemptionRequest[](0), total);
        }

        uint256 end = offset + limit;
        if (end > total) {
            end = total;
        }

        uint256 resultCount = end - offset;
        ids = new uint256[](resultCount);
        redeems = new RedemptionRequest[](resultCount);

        for (uint256 i = 0; i < resultCount; i++) {
            uint256 redemptionId = offset + i;
            ids[i] = redemptionId;
            redeems[i] = redemptions[redemptionId];
        }
    }

    /**
     * @notice Get all redemptions by a specific requester
     * @param requester Address of the requester
     * @return ids Array of redemption IDs
     * @return redeems Array of redemption details
     */
    function getRedemptionsByRequester(address requester)
        external
        view
        returns (uint256[] memory ids, RedemptionRequest[] memory redeems)
    {
        // First pass: count redemptions by this requester
        uint256 count = 0;
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].requester == requester && redemptions[i].amountSat > 0) {
                count++;
            }
        }

        // Second pass: collect redemptions
        ids = new uint256[](count);
        redeems = new RedemptionRequest[](count);

        uint256 index = 0;
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].requester == requester && redemptions[i].amountSat > 0) {
                ids[index] = i;
                redeems[index] = redemptions[i];
                index++;
            }
        }
    }

    /**
     * @notice Get summary of pool state for admin dashboard
     * @return totalUTXOs Total number of registered UTXOs
     * @return distributedUTXOs Number of distributed UTXOs
     * @return totalRedemptions Total redemption requests
     * @return pendingRedemptions Number of pending redemptions
     * @return mpTokenAddress Address of the MP token contract
     * @return calculatorAddress Address of the distribution calculator
     */
    function getPoolSummary()
        external
        view
        returns (
            uint256 totalUTXOs,
            uint256 distributedUTXOs,
            uint256 totalRedemptions,
            uint256 pendingRedemptions,
            address mpTokenAddress,
            address calculatorAddress
        )
    {
        totalRedemptions = nextRedemptionId;

        // Count pending redemptions
        for (uint256 i = 0; i < nextRedemptionId; i++) {
            if (redemptions[i].amountSat > 0 && !redemptions[i].confirmed) {
                pendingRedemptions++;
            }
        }

        mpTokenAddress = address(mpToken);
        calculatorAddress = address(calculator);

        // Note: UTXO counting would require tracking all registered UTXOs
        // For now, return 0 as we don't have a counter in the current implementation
        totalUTXOs = 0;
        distributedUTXOs = 0;
    }
}
