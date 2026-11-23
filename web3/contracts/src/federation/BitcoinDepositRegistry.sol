// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinWithdrawalEscrow.sol";
import "./IBitcoinAddressRegistry.sol";
import "../core/BitcoinFundingParser.sol";
import "./WithdrawalFeeTypes.sol";

interface IDepositRecoveryBinding {
    function recovery() external view returns (address);
}

/// @notice Common outpoint consumption for all deposit/reward modes in this deployment.
contract BitcoinDepositRegistry {
    IPaymentSPV public immutable spv;
    FROSTAuthorization public immutable authorization;
    uint32 public immutable depth;
    uint256 public nonce;
    address public immutable spendRecorder;
    mapping(bytes32 => bool) public walletOutput;
    mapping(bytes32 => uint256) public backingSats;
    struct OutputValue { uint64 sats; bytes script; bool known; }
    mapping(bytes32 => OutputValue) private outputValues;
    mapping(bytes32 => BackingOutpoint) public valueLocation;
    mapping(bytes32 => bool) public custodyTransactions;
    mapping(bytes32 => bytes32) public feeScripts;
    function getValue(bytes32 key) external view returns(OutputValue memory) { return outputValues[key]; }
    function _value(bytes32 txid,uint32 vout,uint64 sats,bytes memory script) private {
        require(sats <= 2100000000000000,"value range");bytes32 key=keccak256(abi.encode(txid,vout));
        OutputValue storage old=outputValues[key];
        if(old.known)require(old.sats == sats && keccak256(old.script) == keccak256(script),"value conflict");
        else { outputValues[key]=OutputValue(sats,script,true);valueLocation[key]=BackingOutpoint(txid,vout); }
    }
    /// @notice Exact immutable prevout bytes, not a proof of current unspentness.
    function cacheValue(bytes calldata raw,uint32 vout) external returns(bytes32 key) {
        BitcoinFundingParser.Parsed memory p=BitcoinFundingParser.parse(raw,vout);
        _value(p.txid,vout,p.amount,p.scriptPubKey);return keccak256(abi.encode(p.txid,vout));
    }
    function recordFamily(bytes32 txid) external {
        require(msg.sender == spendRecorder,"family recorder");custodyTransactions[txid]=true;
    }
    function _feeFunding() private view returns(address) {
        address c=BitcoinWithdrawalEscrow(spendRecorder).withdrawalFees();
        return c == address(0) ? address(0) : IWithdrawalFeeCredits(c).funding();
    }
    function registerFeeScript(bytes32 scriptHash,bytes32 binding) external {
        require(msg.sender == _feeFunding() && msg.sender != address(0),"fee registrar");
        require(scriptPools[scriptHash] == address(0) && feeScripts[scriptHash] == 0 && binding != 0,"fee namespace");
        feeScripts[scriptHash]=binding;
    }
    function markUserFee(bytes calldata raw,uint32 vout) external {
        require(msg.sender == _feeFunding() && msg.sender != address(0),"fee funding recorder");
        BitcoinFundingParser.Parsed memory p=BitcoinFundingParser.parse(raw,vout);bytes32 key=keccak256(abi.encode(p.txid,vout));
        require(!consumed[key] && !walletOutput[key] && backingSats[key] == 0 && feeScripts[keccak256(p.scriptPubKey)] != 0,"fee output used");
        _value(p.txid,vout,p.amount,p.scriptPubKey);walletOutput[key]=true;
    }
    mapping(bytes32 => bytes32) public backingScripts;
    mapping(bytes32 => uint256) public backingByScript;
    mapping(address => bytes32) public poolScripts;
    mapping(address => bytes32) public retiredPoolScripts;
    mapping(bytes32 => address) public scriptPools;
    mapping(bytes32 => bool) public consumed;
    IBitcoinAddressRegistry public addressRegistry;
    mapping(bytes32 => bytes32) public addressIntents;
    uint256 public totalBackingSats;
    mapping(bytes32 => bool) public transferClaims;
    struct BackingOutpoint { bytes32 txid; uint32 vout; }
    BackingOutpoint[] private backingOutpoints;
    mapping(bytes32 => uint256) private backingIndex;
    event AddressRegistryBound(address indexed registry);
    event RecoveryScriptRegistered(address indexed pool, bytes32 indexed scriptHash);

    constructor(address relay, address auth, uint32 confirmations, address escrow) {
        require(escrow.code.length > 0, "escrow");
        spendRecorder = escrow;
        spv = IPaymentSPV(relay);
        authorization = FROSTAuthorization(auth);
        depth = confirmations;
        require(confirmations >= 6 && !spv.testHelpersEnabled(), "proof policy");
    }

    function getRegisterOperationHash(address pool, bytes calldata script) public view returns (bytes32) {
        return
            authorization.hashOperation(
                address(this), this.register.selector, keccak256(abi.encode(pool, script)), nonce
            );
    }

    function register(address pool, bytes calldata script, bytes32 op) external {
        bytes32 h = keccak256(script);
        require(pool.code.length > 0 && poolScripts[pool] == 0 && scriptPools[h] == address(0) && feeScripts[h] == 0, "registered");
        require(op == getRegisterOperationHash(pool, script), "hash");
        authorization.consumeOperation(op);
        nonce++;
        poolScripts[pool] = h;
        scriptPools[h] = pool;
    }

    /// @dev Reserve both the change-only successor and its separate sponsor namespace.
    function registerRecoveryScript(address pool, bytes32 scriptHash, bytes32 sponsorHash) external {
        require(msg.sender == spendRecorder && poolScripts[pool] != 0 && scriptHash != 0
            && sponsorHash != 0 && sponsorHash != scriptHash
            && scriptPools[scriptHash] == address(0) && feeScripts[scriptHash] == 0
            && scriptPools[sponsorHash] == address(0) && feeScripts[sponsorHash] == 0, "recovery script");
        scriptPools[scriptHash] = pool;
        feeScripts[sponsorHash] = scriptHash;
        emit RecoveryScriptRegistered(pool, scriptHash);
    }

    /// @dev The old namespace remains reserved for late paid obligations and historical change.
    function activateRecoveryScript(address pool, bytes32 source, bytes32 successor) external {
        require(msg.sender == IDepositRecoveryBinding(spendRecorder).recovery()
            && source != 0 && source != successor && poolScripts[pool] == source && retiredPoolScripts[pool] == 0
            && scriptPools[successor] == pool && backingByScript[source] == 0, "recovery activation");
        retiredPoolScripts[pool] = source;
        poolScripts[pool] = successor;
    }

    function getSetAddressRegistryOperationHash(address registry) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.setAddressRegistry.selector,
            keccak256(abi.encode(registry)), 0);
    }
    /// @notice New deployment only: no base registration/issuance may predate the common fence.
    function setAddressRegistry(address registry, bytes32 op) external {
        require(address(addressRegistry) == address(0) && nonce == 0 && registry.code.length > 0, "address mode fixed");
        IBitcoinAddressRegistry candidate = IBitcoinAddressRegistry(registry);
        require(candidate.deposits() == address(this) && candidate.authorization() == address(authorization)
            && candidate.escrow() == spendRecorder, "address mode graph");
        require(op == getSetAddressRegistryOperationHash(registry), "hash");
        authorization.consumeOperation(op);
        addressRegistry = candidate;
        ++nonce;
        emit AddressRegistryBound(registry);
    }

    /// @dev One namespace with legacy base scripts; only the fixed validated registry may add derived scripts.
    function registerAddressScript(bytes32 scriptHash, address pool, bytes32 intentId) external {
        require(msg.sender == address(addressRegistry) && msg.sender != address(0), "address registrar");
        require(scriptHash != 0 && intentId != 0 && poolScripts[pool] != 0 && feeScripts[scriptHash] == 0, "address script");
        if (scriptPools[scriptHash] != address(0)) {
            require(scriptPools[scriptHash] == pool && addressIntents[scriptHash] == intentId, "script registered");
            return;
        }
        scriptPools[scriptHash] = pool;
        addressIntents[scriptHash] = intentId;
    }
    function backingCount() external view returns (uint256) { return backingOutpoints.length; }
    function backingAt(uint256 i) external view returns (bytes32 txid, uint32 vout, uint256 sats) {
        BackingOutpoint memory b = backingOutpoints[i];
        return (b.txid, b.vout, backingSats[keccak256(abi.encode(b.txid,b.vout))]);
    }
    function _backing(bytes32 txid, uint32 vout, uint256 amount, bytes32 scriptHash) private {
        bytes32 key = keccak256(abi.encode(txid,vout)); uint256 old = backingSats[key];
        totalBackingSats = totalBackingSats - old + amount;
        if (old != 0) backingByScript[backingScripts[key]] -= old;
        if (amount != 0) backingByScript[scriptHash] += amount;
        if (old == 0 && amount != 0) {
            backingOutpoints.push(BackingOutpoint(txid,vout)); backingIndex[key] = backingOutpoints.length;
        } else if (old != 0 && amount == 0) {
            uint256 index = backingIndex[key]-1; BackingOutpoint memory last = backingOutpoints[backingOutpoints.length-1];
            backingOutpoints[index] = last; backingIndex[keccak256(abi.encode(last.txid,last.vout))] = index+1;
            backingOutpoints.pop(); delete backingIndex[key];
        }
        backingSats[key] = amount;
        if(amount == 0)delete backingScripts[key]; else backingScripts[key]=scriptHash;
    }
    function markFeeOutput(bytes32 txid, uint32 vout) external {
        address fee = BitcoinWithdrawalEscrow(spendRecorder).batches().feeReserve();
        require(msg.sender == fee && fee != address(0), "fee recorder");
        bytes32 key = keccak256(abi.encode(txid,vout));
        require(!consumed[key] && !walletOutput[key] && backingSats[key] == 0, "fee output used");
        walletOutput[key] = true;
    }

    /// @notice Move outstanding principal to verified wallet change; miner fees cannot consume MP backing.
    function recordSpend(bytes calldata raw, uint256 burnedSats) external {
        _recordSpend(raw, burnedSats, false, 0);
    }

    /// @notice Record a user-funded spend using the wallet outputs frozen by its escrow at bind.
    function recordSpendWithChange(bytes calldata raw, uint256 burnedSats, uint32 changeOutputs) external {
        _recordSpend(raw, burnedSats, true, changeOutputs);
    }

    function _recordSpend(bytes calldata raw, uint256 burnedSats, bool fixedChange, uint32 changeOutputs) private {
        require(msg.sender == spendRecorder, "recorder");
        (bytes32[] memory prev, uint32[] memory vouts) = BitcoinTxParser.parseAllInputs(raw);
        uint256 backing;
        for (uint256 i; i < prev.length; i++) {
            bytes32 inputKey = keccak256(abi.encode(prev[i], vouts[i]));
            backing += backingSats[inputKey];
            _backing(prev[i],vouts[i],0,0);
            walletOutput[inputKey] = true; // Includes unminted treasury inputs: spent outputs can never mint later.
        }
        require(backing >= burnedSats, "principal inputs missing");
        uint256 remaining = backing - burnedSats;
        bytes32 txid = BitcoinTxParser.txId(raw);
        uint256 count = BitcoinTxParser.outputCount(raw);
        require(count <= 32 && uint256(changeOutputs) >> count == 0, "outputs");
        for (uint32 i; i < count; i++) {
            (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, i);
            _value(txid,i,amount,script);
            bytes32 scriptHash = keccak256(script);
            if (fixedChange ? changeOutputs & (uint32(1) << i) != 0 : scriptPools[scriptHash] != address(0)) {
                bytes32 outputKey = keccak256(abi.encode(txid, i));
                walletOutput[outputKey] = true;
                uint256 assigned = amount < remaining ? amount : remaining;
                _backing(txid,i,assigned,scriptHash);
                remaining -= assigned;
            }
        }
        require(remaining == 0, "miner fee consumes principal");
    }

    function consume(bytes calldata raw, uint32 vout, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        external
        returns (uint64 amount)
    {
        // The status-enabled graph uses the explicit generation-bound pool entry.
        // Registry calls also enforce the CURRENT fence, independent of caller behavior.
        if (address(addressRegistry) != address(0)) addressRegistry.requireCustody(addressRegistry.custodyGeneration());
        require(
            poolScripts[msg.sender] != 0 && raw.length <= 100000 && siblings.length <= 32
                && index >> siblings.length == 0,
            "pool or bounds"
        );
        bytes32 txid = BitcoinTxParser.txId(raw);
        bytes32 key = keccak256(abi.encode(txid, vout));
        require(!consumed[key] && !walletOutput[key], "outpoint consumed or change");
        bytes32 reservation = keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid), vout));
        BitcoinBatchRegistry batch = BitcoinWithdrawalEscrow(spendRecorder).batches();
        require(
            batch.reservations(reservation) == 0,
            "outpoint reserved for spend"
        );
        (bool canonical, uint64 confirmations) = spv.getBlockStatus(blockHash);
        (bytes32[] memory prev, uint32[] memory outputs) = BitcoinTxParser.parseAllInputs(raw);
        for (uint256 i; i < prev.length; i++) {
            require(
                !consumed[keccak256(abi.encode(prev[i], outputs[i]))]
                    && !walletOutput[keccak256(abi.encode(prev[i], outputs[i]))],
                "wallet spend is not new backing"
            );
            require(batch.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]), outputs[i]))) == 0,
                "wallet input reserved for spend");
        }
        bool coinbase = prev.length == 1 && prev[0] == 0 && outputs[0] == type(uint32).max;
        require(
            canonical && confirmations >= (coinbase ? 100 : depth)
                && spv.checkTxInclusion(blockHash, txid, siblings, index),
            "deposit proof"
        );
        bytes memory script;
        (amount, script) = BitcoinTxParser.outputAt(raw, vout);
        bytes32 scriptHash = keccak256(script);
        require(amount > 0 && (scriptHash == poolScripts[msg.sender]
            || !coinbase && scriptHash == retiredPoolScripts[msg.sender]), "deposit output");
        consumed[key] = true;
        _backing(txid,vout,amount,scriptHash);
        _value(txid,vout,amount,script);
    }

    /// @notice Accept backing whose original MP was burned by a pinned source escrow.
    /// @dev Only a registered MiningPoolV1's fixed transfer gateway can trigger its
    /// mint path. An aggregated output must be accepted in full in one call, so
    /// it cannot be spent while other owners' claims still await acceptance.
    function consumeTransfer(bytes calldata raw, uint32 vout, uint64 sats, bytes32 claim,
        bytes32 blockHash, bytes32[] calldata siblings, uint256 index) external
    {
        require(poolScripts[msg.sender] != 0 && claim != 0 && !transferClaims[claim] && sats > 0,
            "transfer claim");
        if (address(addressRegistry) != address(0)) addressRegistry.requireCustody(addressRegistry.custodyGeneration());
        require(raw.length <= 100000 && siblings.length <= 32 && index >> siblings.length == 0, "transfer bounds");
        bytes32 txid = BitcoinTxParser.txId(raw);
        bytes32 key = keccak256(abi.encode(txid, vout));
        BitcoinBatchRegistry batch = BitcoinWithdrawalEscrow(spendRecorder).batches();
        require(!walletOutput[key] && !consumed[key]
            && batch.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid), vout))) == 0,
            "transfer output used");
        (bool canonical, uint64 confirmations) = spv.getBlockStatus(blockHash);
        require(canonical && confirmations >= depth && spv.checkTxInclusion(blockHash, txid, siblings, index),
            "transfer proof");
        (bytes32[] memory prev, uint32[] memory outputs) = BitcoinTxParser.parseAllInputs(raw);
        require(!(prev.length == 1 && prev[0] == 0 && outputs[0] == type(uint32).max), "transfer coinbase");
        for (uint256 i; i < prev.length; ++i) {
            bytes32 inputKey = keccak256(abi.encode(prev[i], outputs[i]));
            require(!consumed[inputKey] && !walletOutput[inputKey]
                && batch.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]), outputs[i]))) == 0,
                "transfer recycles backing");
        }
        (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, vout);
        bytes32 scriptHash = keccak256(script);
        require((scriptHash == poolScripts[msg.sender] || scriptHash == retiredPoolScripts[msg.sender]) && sats == amount,
            "transfer output");
        consumed[key] = true;
        transferClaims[claim] = true;
        _backing(txid, vout, sats, scriptHash);
        _value(txid, vout, amount, script);
    }

    function consumeAddress(bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings, uint256 index,
        IBitcoinAddressRegistry.MintClaim calldata claim) external returns (uint64 amount)
    {
        require(address(addressRegistry) != address(0) && poolScripts[msg.sender] != 0
            && siblings.length <= 32 && index >> siblings.length == 0, "address pool or bounds");
        bytes32 scriptHash = addressRegistry.validateMint(msg.sender, claim);
        BitcoinFundingParser.Parsed memory parsed = BitcoinFundingParser.parse(raw, claim.vout);
        require(parsed.txid == claim.txid && parsed.amount == claim.satoshis && parsed.amount > 0
            && keccak256(parsed.scriptPubKey) == scriptHash && addressIntents[scriptHash] == claim.intentId,
            "address funding output");
        bytes32 key = keccak256(abi.encode(parsed.txid, claim.vout));
        require(!consumed[key] && !walletOutput[key], "outpoint consumed or change");
        BitcoinBatchRegistry batch = BitcoinWithdrawalEscrow(spendRecorder).batches();
        require(batch.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(parsed.txid), claim.vout))) == 0,
            "outpoint reserved for spend");
        for (uint256 i; i < parsed.inputs.length; ++i) {
            BitcoinFundingParser.Input memory input = parsed.inputs[i];
            bytes32 previous = keccak256(abi.encode(input.txidLE, input.vout));
            require(!consumed[previous] && !walletOutput[previous], "wallet spend is not new backing");
            require(batch.reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(input.txidLE), input.vout))) == 0,
                "wallet input reserved for spend");
        }
        bool coinbase = parsed.inputs.length == 1 && parsed.inputs[0].txidLE == 0
            && parsed.inputs[0].vout == type(uint32).max;
        (bool canonical, uint64 confirmations) = spv.getBlockStatus(blockHash);
        require(canonical && confirmations >= (coinbase ? 100 : depth)
            && spv.checkTxInclusion(blockHash, parsed.txid, siblings, index), "deposit proof");
        consumed[key] = true;
        amount = parsed.amount;
        _backing(parsed.txid,claim.vout,amount,scriptHash);
        _value(parsed.txid,claim.vout,amount,parsed.scriptPubKey);
    }
}
