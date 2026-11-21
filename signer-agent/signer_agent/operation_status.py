"""Recognize a consumed authorization before nonce-dependent hash helpers run."""
from hexbytes import HexBytes
from web3 import Web3
from eth_utils.abi import function_abi_to_4byte_selector

from .bootstrap import artifact, encode_args
from .bitcoin_trust import checked_runtime


def _dao_receipt(w3, target, auth, intent, op, at, target_birth):
    """Bind the consumed digest to the original selector and arguments, including after later changes."""
    helper = intent["hash_method"]
    inputs = next(entry["inputs"] for entry in target.abi
                  if entry.get("type") == "function" and entry.get("name") == helper)
    args = encode_args(inputs, intent["hash_args"])
    if helper in ("getAddCustodianOperationHash", "getRemoveCustodianOperationHash"):
        method = "addCustodian" if helper == "getAddCustodianOperationHash" else "removeCustodian"
        abi = next(entry for entry in target.abi if entry.get("name") == method)
        expected = Web3.keccak(w3.codec.encode(["bytes4", "address"],
                                               [function_abi_to_4byte_selector(abi), args[0]]))
        if bytes(target.functions.operationReceipts(op).call(block_identifier=at)) != expected:
            raise ValueError("ULR_CUSTODIAN_OPERATION_RECEIPT")
        return

    # A helper that is still reproducible needs no historical lookup.
    digest = target.functions.getApproveTariffOperationHash(*args)
    if bytes(digest.call(block_identifier=at)) == op:
        return

    # The SDK executes approveTariff directly. Bind the exact canonical call
    # even when several operations consumed target nonces in the same block.
    from .evm_log_paging import log_pages
    topic = Web3.keccak(text="OperationExecuted(bytes32,address)")
    topics = [Web3.to_hex(topic), Web3.to_hex(op), Web3.to_hex(bytes(12) + bytes(HexBytes(target.address)))]
    for page in log_pages(w3, target_birth, at, topics=topics, address=auth.address):
        for event in page:
            height = event["blockNumber"]
            if (event.get("removed") or not target_birth <= height <= at or event["address"] != auth.address
                    or [HexBytes(value) for value in event["topics"]] != [HexBytes(value) for value in topics]):
                raise ValueError("POLICY_MISMATCH")
            block = w3.eth.get_block(height)
            tx = w3.eth.get_transaction(event["transactionHash"])
            receipt = w3.eth.get_transaction_receipt(event["transactionHash"])
            index = tx["transactionIndex"]
            if (event["blockHash"] != block.hash or tx["blockHash"] != block.hash or receipt["blockHash"] != block.hash
                    or tx["hash"] != event["transactionHash"] or receipt["transactionHash"] != tx["hash"]
                    or tx["blockNumber"] != height or receipt["blockNumber"] != height
                    or not 0 <= index < len(block.transactions) or block.transactions[index] != tx["hash"]
                    or receipt["transactionIndex"] != index or event["transactionIndex"] != index or receipt["status"] != 1
                    or not any(log["address"] == auth.address and log["topics"] == event["topics"]
                               and log["logIndex"] == event["logIndex"] for log in receipt["logs"])):
                raise ValueError("POLICY_MISMATCH")
            if tx["to"] == target.address:
                expected = target.encode_abi("approveTariff", args=[*args, op])
                if HexBytes(tx["input"]) != HexBytes(expected):
                    raise ValueError("OPERATION_PARAMETERS_MISMATCH")
            elif height <= target_birth or bytes(digest.call(block_identifier=height - 1)) != op:
                # Indirect calls have no outer approveTariff calldata; require
                # the historical helper to reproduce the same signed intent.
                raise ValueError("OPERATION_PARAMETERS_MISMATCH")
            return
    raise ValueError("OPERATION_PARAMETERS_MISMATCH")


def executed_operation(w3, target, kind, intent, number, *, session=None, coordinator=None, operator=None,
                       dao_authorization=None):
    if kind in ('WithdrawalFeePayments','WithdrawalFeeFunding'):
        from .withdrawal_fee_policy import review
        return review(w3,intent,None,number,session=session,executable=False)['complete']
    def read(contract, method, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=number)
    def contract(address, name):
        return w3.eth.contract(address=address, abi=artifact(name)["abi"])
    direct = {"SimpleMultiPool": "frostMultisig", "ConfederationMultiPool": "frostMultisig",
        "ConfederationPoolV2": "frostMultisig", "ConfederationDAOIncome": "authorization",
        "FederationFeeManager": "frostMultisig", "CustodianValidator": "frostMultisig",
        "BitcoinBatchRegistry": "authorization", "BitcoinDepositRegistry": "authorization",
        "CustodianDAOIncomeVault": "authorization", "CustodianDAODailySettlement": "authorization",
        "CustodianDAOFeeRouter": "authorization", "PoolFeePipeline": "authorization",
        "MiningPoolV1": "authorization", "BitcoinMintAdmission": "authorization",
        "SPVServicePaymentRegistry": "authorization", "BitcoinAddressRegistry": "authorization", "BitcoinFeeReserve": "authorization",
        "CustodianDAODecisionBoard": "authorization"}
    if kind in direct:
        address = read(target, direct[kind])
    elif kind == "ConfederationNFTPass":
        if dao_authorization is None or session is None:
            return False
        address = read(contract(read(target, "income"), "ConfederationDAOIncome"), "authorization")
    elif kind == "DKGAuthorization":
        address = target.address
    elif kind in ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2"):
        address = read(contract(read(target, "batches"), "BitcoinBatchRegistry"), "authorization")
    elif kind in ("FederationFactory", "ConfederationFactory", "GovernanceRegistry"):
        factory = contract(read(target, "factory"), "FederationFactory") if kind == "GovernanceRegistry" else target
        conf = kind == "ConfederationFactory"
        address = read(factory, "getConfederation" if conf else "getFederation", intent["hash_args"][0])[7 if conf else 4]
    else:
        return False
    if dao_authorization is not None and address != dao_authorization:
        raise ValueError("CONFEDERATION_DAO_BINDING")
    auth = contract(address, "DKGAuthorization")
    op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
    if not read(auth, "executedOperations", op):
        return False
    if kind == "BitcoinAddressRegistry":
        from .address_registration import review
        registration = review(w3, intent, None, number, session=session, executable=False)
        receipt='statusReceipts' if intent.get('hash_method')=='getStatusOperationHash' else 'registrationReceipts'
        if not read(target, receipt, op) or not registration["complete"]:
            raise ValueError("INCIDENT_STATUS_RECEIPT" if receipt=='statusReceipts' else "ADDRESS_REGISTRATION_POSTCONDITION")
    if kind == "BitcoinFeeReserve":
        from .btc_fee_policy import review
        if not review(w3,intent,None,number,session=session,executable=False)["complete"]:
            raise ValueError("BTC_FEE_RECEIPT_MISMATCH")
    from .contract_provenance import prove_creation
    checked_runtime(w3, auth.address, "DKGAuthorization", number)
    prove_creation(w3, auth.address, "DKGAuthorization", None, number)
    checked_runtime(w3, target.address, kind, number)
    if kind in ("SimpleMultiPool", "ConfederationMultiPool", "ConfederationPoolV2"):
        from .ethereum_intents import _prove_pool
        if operator is None:
            raise ValueError("ETHEREUM_BOOTSTRAP_ORIGIN")
        _prove_pool(w3, target, kind, operator, number)
    else:
        target_origin = prove_creation(w3, target.address, kind, None, number)
    coordinator = coordinator or intent.get("coordinator")
    if (read(auth, "operationTargets", op) != target.address
            or read(auth, "coordinator") != coordinator):
        raise ValueError("POLICY_MISMATCH")
    if session is not None:
        c = contract(coordinator, "FROSTCoordinator")
        verifier = read(c, "defaultVerifier")
        prove_creation(w3, verifier, "FROSTVerifier", [], number)
        prove_creation(w3, c.address, "FROSTCoordinator", [verifier], number)
        prove_creation(w3, read(c, "dkgRegistry"), "DKGCoordinator", [], number)
        result = read(c, "getAuthorizationResult", session)
        if (not read(auth, "consumedSessions", session) or bytes(result[1]) != op
                or not result[2] or result[4] != target.address or result[6] < result[5]
                or read(auth, "registry") != read(c, "dkgRegistry")
                or bytes(read(auth, "dkgId")) != bytes(read(c, "sessionDKGId", session))
                or bytes(read(auth, "resultHash")) != bytes(read(c, "sessionDKGResult", session))):
            raise ValueError("POLICY_MISMATCH")
    if dao_authorization is not None and kind in ("CustodianValidator", "ConfederationNFTPass"):
        _dao_receipt(w3, target, auth, intent, op, number,
                     int(target_origin["block_number"]) if kind == "ConfederationNFTPass" else None)
    return True
