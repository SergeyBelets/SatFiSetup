"""Trust-anchor regressions use artificial public values and no user credentials."""

from types import SimpleNamespace

import pytest
from eth_abi import encode
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.bitcoin_trust import (
    checked_runtime,
    inspect_registry,
    pin_registry,
    require_registry,
)
from signer_agent.bitcoin_worker import sign_step, validate, plan_hash
from signer_agent.dkg_protocol import Manifest
from signer_agent.secure_state import SecureState


def address(index):
    return Web3.to_checksum_address("0x" + (index.to_bytes(20, "big")).hex())


@pytest.fixture
def graph(monkeypatch):
    names = {
        k: address(i)
        for i, k in enumerate(
            (
                "dkg",
                "batches",
                "authorization",
                "coordinator",
                "verifier",
                "escrow",
                "deposits",
                "relay",
            ),
            100,
        )
    }
    roster = (address(1), address(2), address(3))
    manifest = Manifest(
        31337,
        names["dkg"],
        b"a" * 32,
        1,
        "BTC_SPEND",
        roster,
        (b"1" * 32, b"2" * 32, b"3" * 32),
        2,
    )
    key = dict(result_hash=(b"r" * 32).hex(), result=dict(group="02" + "11" * 32))
    evm_id, evm_result, evm_group = b"e" * 32, b"v" * 32, b"g" * 32
    methods = {
        "dkg": {
            "getCompletedResult": lambda sid: (
                (
                    manifest.context,
                    bytes.fromhex(key["result_hash"]),
                    1,
                    Web3.keccak(text="BTC_SPEND"),
                    bytes.fromhex(key["result"]["group"][2:]),
                    roster,
                    2,
                )
                if sid == manifest.session_id
                else (
                    b"c" * 32,
                    evm_result,
                    1,
                    Web3.keccak(text="EVM_AUTH"),
                    evm_group,
                    roster,
                    2,
                )
            )
        },
        "batches": dict(
            feeReserve=address(0),
            dkg=names["dkg"],
            walletId=manifest.session_id,
            walletResult=bytes.fromhex(key["result_hash"]),
            authorization=names["authorization"],
            withdrawalEscrow=names["escrow"],
        ),
        "authorization": dict(
            coordinator=names["coordinator"],
            verifier=names["verifier"],
            registry=names["dkg"],
            dkgId=evm_id,
            resultHash=evm_result,
            groupPubkey=evm_group,
            groupPubkeyHash=Web3.keccak(evm_group),
            getParticipants=roster,
            threshold=2,
            participantsHash=Web3.keccak(encode(["address[]"], [roster])),
        ),
        "coordinator": dict(
            defaultVerifier=names["verifier"], dkgRegistry=names["dkg"]
        ),
        "verifier": {},
        "escrow": dict(
            batches=names["batches"],
            depositRegistry=names["deposits"],
            spv=names["relay"],
            depth=6,
            minP2WPKHSats=294,
            minP2TRSats=330,
        ),
        "deposits": dict(
            addressRegistry="0x" + "00" * 20,
            spendRecorder=names["escrow"],
            authorization=names["authorization"],
            spv=names["relay"],
            depth=6,
        ),
        "relay": dict(testHelpersEnabled=False),
    }
    kinds = dict(
        zip(
            names,
            (
                "DKGCoordinator",
                "BitcoinBatchRegistry",
                "DKGAuthorization",
                "FROSTCoordinator",
                "FROSTVerifier",
                "BitcoinWithdrawalEscrow",
                "BitcoinDepositRegistry",
                "RegtestHeaderRelay",
            ),
        )
    )
    codes = {
        names[k]: bytes.fromhex(
            artifact(kind)["deployedBytecode"]["object"].removeprefix("0x")
        )
        for k, kind in kinds.items()
    }

    class Block(dict):
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__

    block = Block(number=100, hash=Web3.keccak(text="review block"))

    # This RPC fixture tests runtime/getter relationships. Constructor provenance
    # itself is exercised separately with exact/malicious creation transactions.
    from signer_agent import contract_provenance

    monkeypatch.setattr(
        contract_provenance,
        "prove_creation",
        lambda w3, a, kind, args, latest: dict(
            version="1",
            method="direct",
            address=a,
            artifact=kind,
            block_number="100",
            block_hash=block.hash.hex(),
            transaction_hash=Web3.keccak(text=a).hex(),
        ),
    )

    class Functions:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, method):
            def fn(*args):
                value = methods[self.name][method]
                return SimpleNamespace(
                    call=lambda **kwargs: value(*args) if callable(value) else value
                )

            return fn

    def contract(address, abi):
        name = next(k for k, v in names.items() if v == address)
        return SimpleNamespace(address=address, functions=Functions(name))

    eth = SimpleNamespace(
        chain_id=31337,
        get_block=lambda _: block,
        get_code=lambda a, **kwargs: codes[a],
        contract=contract,
    )
    registry = SimpleNamespace(w3=SimpleNamespace(eth=eth))
    return SimpleNamespace(
        manifest=manifest,
        key=key,
        registry=registry,
        names=names,
        methods=methods,
        codes=codes,
        block=block,
    )


def test_legacy_signer_cannot_enroll_generation_bound_graph(graph):
    graph.methods["deposits"]["addressRegistry"] = graph.names["relay"]
    with pytest.raises(ValueError, match="BTC_ADDRESS_SIGNING_PROFILE_REQUIRED"):
        inspect_registry(graph.manifest, graph.key, graph.registry, graph.names["batches"])


def test_unpinned_bitcoin_plan_rejected_before_rpc_or_nonce(
    tmp_path, monkeypatch, graph
):
    from signer_agent import bitcoin_worker

    monkeypatch.setattr(
        bitcoin_worker,
        "validate",
        lambda *args: pytest.fail("untrusted plan reached validation"),
    )
    state = SecureState(str(tmp_path / "state"), "11" * 32)
    with pytest.raises(ValueError, match="BTC_REGISTRY_NOT_PINNED"):
        sign_step(
            "btc-commit",
            graph.manifest,
            graph.key,
            dict(batch_registry=graph.names["batches"]),
            graph.registry,
            state,
            None,
        )
    assert not state.path.exists()


def test_self_reported_approval_cannot_cross_signing_boundary(tmp_path, graph):
    """Reproduce the former bypass, then prove it stops before commitment now.

    The RPC fixture supplies a real-looking UTXO and an arbitrary contract that
    lies about approval. No real node/wallet or production secrets are involved.
    """
    from decimal import Decimal
    from signer_agent import crypto

    script = "5120" + graph.key["result"]["group"][2:]
    plan = dict(
        network="regtest",
        wallet_script=script,
        batch_registry=graph.names["batches"],
        batch_id="bb" * 32,
        deadline="200",
        withdrawal_refs=[],
        fee="1000",
        max_fee="2000",
        inputs=[dict(outpoint="aa" * 32 + ":0", amount="10000", script=script)],
        outputs=[dict(amount="9000", script="0014" + "22" * 20)],
    )
    built = crypto.call(
        "bitcoin_build",
        **{k: plan[k] for k in ("inputs", "outputs", "wallet_script", "fee", "max_fee")}
    )
    h = plan_hash(graph.manifest, plan, built)
    root = bytes.fromhex(plan["batch_id"])
    graph.methods["batches"].update(
        batches=lambda _: (h, bytes.fromhex(graph.key["result_hash"]), 200, 2),
        reservationRoot=lambda _: root,
        replacementOf=lambda _: bytes(32),
        reservations=lambda _: root,
    )
    graph.block.timestamp = 100
    graph.codes[graph.names["batches"]] = (
        b"\x00"  # No reviewed registry implementation.
    )
    bitcoin = SimpleNamespace(
        check_network=lambda: None,
        call=lambda *args: dict(
            scriptPubKey=dict(hex=script),
            value=Decimal("0.00010000"),
            confirmations=6,
            coinbase=False,
        ),
    )
    # The historical validation stage alone trusted these self-reported getters.
    assert validate(graph.manifest, graph.key, plan, graph.registry, bitcoin)[1] == h
    state = SecureState(str(tmp_path / "state"), "11" * 32)
    with pytest.raises(ValueError, match="BTC_REGISTRY_NOT_PINNED"):
        sign_step(
            "btc-commit",
            graph.manifest,
            graph.key,
            plan,
            graph.registry,
            state,
            bitcoin,
        )
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        inspect_registry(
            graph.manifest, graph.key, graph.registry, plan["batch_registry"]
        )
    assert not state.path.exists()


def test_getter_compatible_counterfeit_registry_cannot_be_pinned(graph):
    graph.codes[graph.names["batches"]] = b"\x60\x00\x60\x00\xf3"
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        inspect_registry(
            graph.manifest, graph.key, graph.registry, graph.names["batches"]
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "counterfeit_auth",
        "counterfeit_verifier",
        "weak_threshold",
        "other_roster",
        "other_registry",
    ],
)
def test_trust_verifies_authorization_graph_not_only_batch_runtime(graph, mutation):
    if mutation == "counterfeit_auth":
        graph.codes[graph.names["authorization"]] = b"\x00"
    elif mutation == "counterfeit_verifier":
        graph.codes[graph.names["verifier"]] = b"\x00"
    elif mutation == "weak_threshold":
        graph.methods["authorization"]["threshold"] = 1
    elif mutation == "other_roster":
        graph.methods["authorization"]["getParticipants"] = [address(9)]
    else:
        graph.methods["coordinator"]["dkgRegistry"] = address(9)
    with pytest.raises(
        ValueError, match="BTC_(CONTRACT_BUILD|AUTHORIZATION_GRAPH)_MISMATCH"
    ):
        inspect_registry(
            graph.manifest, graph.key, graph.registry, graph.names["batches"]
        )


def test_review_pin_repeat_and_refuse_address_or_branch_change(tmp_path, graph):
    state = SecureState(str(tmp_path / "state"), "11" * 32)
    args = (graph.manifest, graph.key, graph.registry)
    reviewed = inspect_registry(*args, graph.names["batches"])
    with pytest.raises(ValueError, match="BTC_REGISTRY_FINGERPRINT_MISMATCH"):
        pin_registry(*args, state, graph.names["batches"], "00" * 32)
    for _ in range(2):
        assert (
            pin_registry(*args, state, graph.names["batches"], reviewed["fingerprint"])[
                "status"
            ]
            == "BTC_REGISTRY_PINNED"
        )
    require_registry(*args, state, graph.names["batches"])
    with pytest.raises(ValueError, match="BTC_REGISTRY_NOT_TRUSTED"):
        require_registry(*args, state, address(999))
    graph.block.hash = Web3.keccak(text="different branch")
    with pytest.raises(ValueError, match="BTC_REGISTRY_ANCHOR_REORG"):
        require_registry(*args, state, graph.names["batches"])


def test_inconsistent_copies_of_same_immutable_rejected(graph):
    compiled = artifact("DKGAuthorization")["deployedBytecode"]
    positions = next(p for p in compiled["immutableReferences"].values() if len(p) > 1)
    code = bytearray(graph.codes[graph.names["authorization"]])
    code[positions[0]["start"] + 31] ^= 1
    graph.codes[graph.names["authorization"]] = bytes(code)
    with pytest.raises(ValueError, match="BTC_CONTRACT_IMMUTABLE_MISMATCH"):
        checked_runtime(
            graph.registry.w3, graph.names["authorization"], "DKGAuthorization", 100
        )


def test_legacy_pin_requires_provenance_and_can_be_upgraded(tmp_path, graph):
    state = SecureState(str(tmp_path / "state"), "11" * 32)
    args = (graph.manifest, graph.key, graph.registry)
    reviewed = inspect_registry(*args, graph.names["batches"])
    old = {name: value for name, value in reviewed.items() if name != "provenance"}
    state.put("btc-registry:" + graph.manifest.context.hex(), old)
    with pytest.raises(ValueError, match="BTC_REGISTRY_PROVENANCE_REQUIRED"):
        require_registry(*args, state, graph.names["batches"])
    pin_registry(*args, state, graph.names["batches"], reviewed["fingerprint"])
    assert require_registry(*args, state, graph.names["batches"])["provenance"]


def test_failed_creation_proof_does_not_pin_registry(tmp_path, graph, monkeypatch):
    from signer_agent import contract_provenance

    def fail(*args):
        raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")

    monkeypatch.setattr(contract_provenance, "prove_creation", fail)
    state = SecureState(str(tmp_path / "state"), "11" * 32)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        pin_registry(
            graph.manifest,
            graph.key,
            graph.registry,
            state,
            graph.names["batches"],
            "00" * 32,
        )
    assert state.get("btc-registry:" + graph.manifest.context.hex()) is None


def test_withdrawal_minimums_are_reviewed_and_bound_to_constructor(graph, monkeypatch):
    from signer_agent import contract_provenance

    original = contract_provenance.prove_creation
    calls = {}

    def capture(w3, target, kind, args, latest):
        calls[kind] = args
        return original(w3, target, kind, args, latest)

    monkeypatch.setattr(contract_provenance, "prove_creation", capture)
    graph.methods["escrow"].update(minP2WPKHSats=500, minP2TRSats=700)
    reviewed = inspect_registry(
        graph.manifest, graph.key, graph.registry, graph.names["batches"]
    )
    assert reviewed["graph"]["withdrawal_policy"] == {
        "min_p2wpkh_sats": "500", "min_p2tr_sats": "700"
    }
    assert calls["BitcoinWithdrawalEscrow"] == [
        graph.names["batches"], graph.names["relay"], 6, 500, 700
    ]


@pytest.mark.parametrize("minimum,confirmations", [("294", "6"), ("295", "6"), ("294", "7")])
def test_board_bitcoin_discovery_requires_enrolled_withdrawal_policy(graph, monkeypatch, minimum, confirmations):
    import json
    from signer_agent import onchain_transport
    from signer_agent.bitcoin_trust import discover_registry

    op = "11" * 32
    intent = dict(
        artifact="BitcoinBatchRegistry", hash_method="getSetWithdrawalEscrowOperationHash",
        chain_id="31337", target=graph.names["batches"],
        hash_args=[graph.names["escrow"]], operation_hash=op,
    )
    board = SimpleNamespace(address=address(999),
        functions=SimpleNamespace(
            registry=lambda: SimpleNamespace(call=lambda: graph.names["dkg"]),
            registryBlock=lambda: SimpleNamespace(call=lambda: 1),
            intents=lambda _: SimpleNamespace(call=lambda **kw: json.dumps(intent).encode()),
            intentCount=lambda: SimpleNamespace(call=lambda **kw: 1),
            intentSessions=lambda _: SimpleNamespace(call=lambda **kw: 1),
        ),
        events=SimpleNamespace(IntentPublished=lambda: SimpleNamespace(
            get_logs=lambda **kw: [dict(args=dict(session=b"s" * 32))])),
    )
    invite = dict(version="3", bitcoin_network="regtest", sessions=dict(btc_ceremony=graph.manifest.session_id.hex(),
                  conf_ceremony=(b"e" * 32).hex()),
                  min_p2wpkh_sats=minimum, min_p2tr_sats="330", bitcoin_confirmations=confirmations)
    monkeypatch.setattr(onchain_transport, "invitation", lambda *a: (board, invite))
    graph.methods["authorization"].update(
        operationTargets=lambda _: graph.names["batches"], executedOperations=lambda _: True
    )
    args = (graph.manifest, graph.key, graph.registry, address(999))
    if minimum == "294" and confirmations == "6":
        assert discover_registry(*args) == graph.names["batches"]
    else:
        error = "BTC_BOARD_WITHDRAWAL_POLICY_MISMATCH" if minimum != "294" else "BTC_BOARD_CONFIRMATIONS_MISMATCH"
        with pytest.raises(ValueError, match=error):
            discover_registry(*args)
