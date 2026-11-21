"""Recognize a consumed authorization before nonce-dependent hash helpers run."""
from .bootstrap import artifact
from .bitcoin_trust import checked_runtime


def verify_executed_session(auth, coordinator, target, op, session, number):
    """Bind an executed operation to either its winning or an exact repeat session."""
    from .coordinator import Session

    def read(contract, method, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=number)

    if (read(auth, "registry") != read(coordinator, "dkgRegistry")
            or bytes(read(auth, "dkgId")) != bytes(read(coordinator, "sessionDKGId", session))
            or bytes(read(auth, "resultHash")) != bytes(read(coordinator, "sessionDKGResult", session))):
        raise ValueError("POLICY_MISMATCH")
    if read(auth, "consumedSessions", session):
        result = read(coordinator, "getAuthorizationResult", session)
        if (bytes(result[1]) != op or not result[2] or result[4] != target or result[6] < result[5]):
            raise ValueError("POLICY_MISMATCH")
        return
    # A concurrent proposal may have the same authorization but never finalize.
    duplicate = Session.from_contract(read(coordinator, "getSession", session))
    if (duplicate.session_id != session or not duplicate.message_bound
            or duplicate.message_hash != op or duplicate.origin_contract != target
            or not duplicate.enforce_shares_check or int(duplicate.verifier_override, 16) != 0
            or duplicate.threshold != read(auth, "threshold")
            or duplicate.group_pubkey != bytes(read(auth, "groupPubkey"))):
        raise ValueError("POLICY_MISMATCH")


def executed_operation(w3, target, kind, intent, number, *, session=None, coordinator=None, operator=None):
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
        "MiningPoolV1": "authorization", "ConfederationBitcoinReserve": "authorization", "BitcoinMintAdmission": "authorization",
        "SPVServicePaymentRegistry": "authorization", "BitcoinAddressRegistry": "authorization", "BitcoinFeeReserve": "authorization",
        "CustodianDAODecisionBoard": "authorization"}
    if kind in direct:
        address = read(target, direct[kind])
    elif kind == "DKGAuthorization":
        address = target.address
    elif kind in ("BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2"):
        address = read(contract(read(target, "batches"), "BitcoinBatchRegistry"), "authorization")
    elif kind == "ConfederationNFTPass":
        address = read(contract(read(target, "income"), "ConfederationDAOIncome"), "authorization")
    elif kind in ("FederationFactory", "ConfederationFactory", "GovernanceRegistry"):
        factory = contract(read(target, "factory"), "FederationFactory") if kind == "GovernanceRegistry" else target
        conf = kind == "ConfederationFactory"
        address = read(factory, "getConfederation" if conf else "getFederation", intent["hash_args"][0])[7 if conf else 4]
    else:
        return False
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
        prove_creation(w3, target.address, kind, None, number)
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
        verify_executed_session(auth, c, target.address, op, session, number)
    return True
