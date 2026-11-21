import json

import pytest
from eth_account import Account
from signer_agent.dao_client import (
    units,
    display,
    day_number,
    claim_days,
    account_at,
    DAOClient,
)
from signer_agent.dao_cli import main, parser, transaction


@pytest.mark.parametrize(
    "bad", ["1e18", "-1", "NaN", "1,25", "01", " 1", "1.0000001", "0"]
)
def test_exact_amounts_reject_ambiguous_or_truncated_values(bad):
    with pytest.raises(ValueError):
        units(bad, 6)


def test_amounts_do_not_round_large_balances():
    text = "9007199254740993.123456789012345678"
    assert display(units(text, 18), 18) == text
    assert units("0", 18, allow_zero=True) == 0
    assert units("0.012", 4) == 120
    with pytest.raises(ValueError):
        units(str(2**256), 18)


def test_claim_dates_are_unique_bounded_and_sorted():
    assert claim_days(["2026-10-17", "2026-10-16"]) == [20742, 20743]
    with pytest.raises(ValueError):
        claim_days(["2026-10-17", str(day_number("2026-10-17"))])
    with pytest.raises(ValueError):
        claim_days([str(i) for i in range(33)])
    with pytest.raises(ValueError):
        day_number("1969-12-31")


def test_identity_permissions_and_symlinks(tmp_path):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"private_key": "01".zfill(64)}))
    identity.chmod(0o644)
    with pytest.raises(ValueError, match="PRIVATE_FILE_PERMISSIONS"):
        account_at(identity)
    identity.chmod(0o600)
    assert (
        account_at(identity).address
        == Account.from_key(bytes.fromhex("01".zfill(64))).address
    )
    link = tmp_path / "link.json"
    link.symlink_to(identity)
    with pytest.raises(OSError):
        account_at(link)


def test_cli_default_is_simulation_and_parses_explicit_rate():
    a = parser().parse_args(
        ["propose-rate", "--percent", "0.012", "--custodian", "0x" + "01" * 20]
    )
    assert not a.send
    assert transaction(None, a, "unused") == (
        "controller",
        "propose",
        [0, 120, "0x" + "01" * 20],
    )


def test_cli_never_emits_provider_credentials_on_error(monkeypatch, capsys):
    def fail(*args):
        raise ConnectionError("https://secret:password@provider/api-key")

    monkeypatch.setattr(DAOClient, "load", fail)
    assert main(["status"]) == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "ERROR" and result["code"] == "ConnectionError"
    assert result["message"] and result["next_action"]
    assert "secret" not in output


def test_unsupported_evm_chain_rejected_before_rpc():
    with pytest.raises(ValueError, match="INTEGRATION_NETWORK_NOT_ALLOWED"):
        DAOClient("http://unused", "10")


def test_mainnet_dao_requires_finalized_rpc(monkeypatch):
    from types import SimpleNamespace
    from web3.datastructures import AttributeDict
    from signer_agent import dao_client
    reads = []
    def block(tag):
        reads.append(tag)
        if tag == "finalized":
            raise RuntimeError("provider does not support finalized")
        return AttributeDict(dict(number=100, hash=b"b" * 32))
    factory = lambda *a, **kw: SimpleNamespace(eth=SimpleNamespace(chain_id=1, get_block=block))
    factory.HTTPProvider = lambda *a, **kw: None
    monkeypatch.setattr(dao_client, "Web3", factory)
    with pytest.raises(ValueError, match="ETHEREUM_FINALITY_UNAVAILABLE"):
        DAOClient("http://unused", "1")
    assert reads == ["latest", "finalized"]


def test_empty_config_fails_closed(tmp_path, capsys):
    config = tmp_path / "dao.json"
    config.write_text("{}")
    assert main(["status", "--config", str(config)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "DAO_CONFIG_SCHEMA"


def test_old_config_requires_local_provenance_initialization(tmp_path):
    config = tmp_path / "old.json"
    config.write_text('{"version":"1"}')
    with pytest.raises(ValueError, match="DAO_CONFIG_PROVENANCE_REQUIRED_RUN_INIT"):
        DAOClient.load("http://unused", config)


def test_initial_factory_requires_reviewed_runtime_before_reading_getters():
    from types import SimpleNamespace

    target = "0x" + "11" * 20
    client = DAOClient.__new__(DAOClient)
    client.block = SimpleNamespace(number=1)
    client.w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda *a, **kw: b"\x00"))
    client.contract = lambda *a: SimpleNamespace(address=target)
    with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD_MISMATCH"):
        client.graph(target, "test")


def test_unverified_dao_creation_cannot_publish_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from signer_agent import dao_client

    output = tmp_path / "dao.json"
    output.write_text("previous verified config")
    client = DAOClient.__new__(DAOClient)
    client.block = SimpleNamespace(number=1)
    client.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337))
    client.graph = lambda *a: {
        "token": dict(address="0x" + "11" * 20, artifact="CustodianDAOToken")
    }

    def fail(*args):
        raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")

    monkeypatch.setattr(dao_client, "prove_creation", fail)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        client.init_config("0x" + "22" * 20, "test", output, 1)
    assert output.read_text() == "previous verified config"


@pytest.mark.parametrize("matches", [False, True])
def test_dao_discovery_binds_no_lp_treasury_to_invitation(monkeypatch, matches):
    from types import SimpleNamespace
    from signer_agent import onchain_transport
    from signer_agent.dao_cli import discover

    target, treasury, registry, authorization = ("0x" + x * 40 for x in "1234")
    sid = b"s" * 32
    dao = ["0x" + "5" * 40] * 7
    intent = dict(artifact="ConfederationFactory", hash_method="getRegisterCustodianDAOOperationHash",
                  chain_id="31337", hash_args=["friends", dao], target=target)
    board = SimpleNamespace(events=SimpleNamespace(IntentPublished=lambda: SimpleNamespace(
        get_logs=lambda **kw: [dict(args=dict(session=sid))])))
    invite = dict(confederation_id="friends", treasury=treasury,
                  sessions=dict(conf_ceremony=sid.hex()))
    monkeypatch.setattr(onchain_transport, "invitation", lambda *a: (board, invite))
    monkeypatch.setattr("signer_agent.board_intents.intent_rows", lambda *a: [dict(session="1", intent=intent)])
    answers = dict(registryBlock=1, intents=json.dumps(intent).encode(),
                   treasury=treasury if matches else authorization,
                   getCustodianDAO=dao, coordinator=target, dkgRegistry=registry,
                   registry=registry, dkgId=sid)
    client = SimpleNamespace(
        w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337)), block=SimpleNamespace(number=1),
        read=lambda contract, method, *args: answers[method], graph=lambda *a: None,
        contract=lambda kind, address: SimpleNamespace(address=address),
        contracts=dict(authorization=SimpleNamespace(address=authorization)),
    )
    if matches:
        assert discover(client, target) == (target, "friends")
    else:
        with pytest.raises(ValueError, match="DAO_BOARD_TREASURY_MISMATCH"):
            discover(client, target)
