from types import SimpleNamespace
import pytest
from web3 import Web3
from signer_agent import callback_admission as admission


def address(index):
    return Web3.to_checksum_address(f"0x{index:040x}")


def environment(monkeypatch, confederation=False):
    names = {name: address(index) for index, name in enumerate(
        ["escrow", "registry", "batches", "auth", "origin", "share_origin", "pool", "share", "frost", "mp", "owner", "validator"], 1)}
    zero = address(0)
    block_reads, origins = [], []
    code = {value: name.encode() for name, value in names.items()}
    data = {
        "escrow": dict(callbackPoolRegistry=names["registry"], batches=names["batches"], isCallbackPoolAdmitted=True),
        "batches": dict(authorization=names["auth"]),
        "registry": dict(escrow=names["escrow"], authorization=names["auth"],
            simpleDeployer=zero if confederation else names["origin"],
            confederationDeployer=names["origin"] if confederation else zero, validateWithdrawal=None),
        "origin": dict(shareTokenDeployer=names["share_origin"], frostDeployer=names["frost"],
            deployedPoolCodehash=Web3.keccak(code[names["pool"]])),
        "share_origin": dict(deployedTokenCodehash=Web3.keccak(code[names["share"]])),
        "share": {"confederationPool" if confederation else "multiPool": names["pool"]},
        "pool": dict(shareToken=names["share"], getFrostMultisig=names["auth"], withdrawalEscrow=names["escrow"],
            custodianValidator=names["validator"],
            getWithdrawal=[names["owner"], names["mp"], 1200, 1000, b"script", b"", False, 1, b"x"*32],
            withdrawalCancelled=False, withdrawalPrepared=True, pendingUserPoolShares=1200, escrowedMP=1000),
        "validator": dict(frostMultisig=names["auth"]),
    }
    class Functions:
        def __init__(self, name): self.name = name
        def __getattr__(self, method):
            def invoke(*args):
                def call(**kwargs):
                    block_reads.append(kwargs["block_identifier"])
                    return data[self.name][method]
                return SimpleNamespace(call=call)
            return invoke
    contracts = {value: SimpleNamespace(address=value, functions=Functions(name), abi=[]) for name, value in names.items()}
    w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda address, **_:contracts[address],
        get_code=lambda address, **_:code[address],
        get_block=lambda _:SimpleNamespace(number=123, hash=b"canonical")))
    for contract in contracts.values(): contract.w3 = w3
    monkeypatch.setattr(admission, "artifact", lambda kind:dict(abi=[]))
    monkeypatch.setattr(admission, "checked_runtime", lambda *args:None)
    monkeypatch.setattr(admission, "prove_creation", lambda *args:origins.append(args[1:]))
    request = [names["pool"], 7, names["mp"], names["owner"], 1000, b"script", bytes(32), 1]
    return SimpleNamespace(escrow=contracts[names["escrow"]], request=request, data=data,
        names=names, reads=block_reads, origins=origins)


@pytest.mark.parametrize("pool_deployer,share_deployer", [
    ("SimpleMultiPoolDeployer", "MultiPoolShareTokenDeployer"),
    ("MultiPoolDeployer", "ConfederationTokenDeployer"),
    ("ConfederationPoolV2Deployer", "ConfederationShareV2Deployer"),
])
def test_authentic_callback_has_exact_source_and_one_snapshot(monkeypatch, pool_deployer, share_deployer):
    confederation = pool_deployer != "SimpleMultiPoolDeployer"
    env = environment(monkeypatch, confederation)
    if pool_deployer == "ConfederationPoolV2Deployer":
        env.escrow.abi = [{"name": "defaultMinerFeeLimitSats"}]
    admission.validate_callback(env.escrow, env.request, 123)
    assert set(env.reads) == {123}
    expected = ["CallbackPoolRegistry", pool_deployer, share_deployer]
    if confederation:
        expected.append("CustodianValidator")
    assert [row[1] for row in env.origins] == expected


def test_confederation_callback_requires_authentic_bound_custodian(monkeypatch):
    env = environment(monkeypatch, confederation=True)
    env.data["validator"]["frostMultisig"] = address(99)
    with pytest.raises(ValueError, match="BTC_CALLBACK_CUSTODIAN_MISMATCH"):
        admission.validate_callback(env.escrow, env.request, 123)


def test_local_bootstrap_latest_is_pinned_before_constructor_provenance(monkeypatch):
    env = environment(monkeypatch)
    admission.review_callback_configuration(env.escrow, env.names["registry"], "latest")
    assert set(env.reads) == {123}
    assert all(row[-1] == 123 for row in env.origins)


def test_arbitrary_pool_cannot_pass_using_matching_getters(monkeypatch):
    env = environment(monkeypatch)
    env.data["origin"]["deployedPoolCodehash"] = bytes(32)
    with pytest.raises(ValueError, match="BTC_CALLBACK_NOT_ADMITTED"):
        admission.validate_callback(env.escrow, env.request, 123)


def test_forged_registry_or_leaf_runtime_stops_before_callback(monkeypatch):
    env = environment(monkeypatch)
    def reject(*args):
        raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
    monkeypatch.setattr(admission, "checked_runtime", reject)
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        admission.validate_callback(env.escrow, env.request, 123)
    assert env.origins == []


@pytest.mark.parametrize("field,value", [("callbackPoolRegistry", address(0)), ("isCallbackPoolAdmitted", False)])
def test_canonical_admission_required(monkeypatch, field, value):
    env = environment(monkeypatch); env.data["escrow"][field] = value
    with pytest.raises(ValueError, match="BTC_CALLBACK_"):
        admission.validate_callback(env.escrow, env.request, 123)


@pytest.mark.parametrize("index,value", [(0, address(99)), (1, address(99)), (2, 999), (3, 999), (4, b"attacker"), (6, True)])
def test_callback_matches_exact_owner_token_amount_script_and_state(monkeypatch, index, value):
    env = environment(monkeypatch); env.data["pool"]["getWithdrawal"][index] = value
    with pytest.raises(ValueError, match="BTC_CALLBACK_REQUEST_MISMATCH"):
        admission.validate_callback(env.escrow, env.request, 123)


def test_zero_pool_treasury_has_no_external_callback():
    admission.validate_callback(SimpleNamespace(), [address(0), 7, address(0), address(1), 1000, b"script"], 123)
