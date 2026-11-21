from pathlib import Path
from types import SimpleNamespace

import pytest
from ops import audit_launch_readiness as audit


def test_duplicate_config_keys_cannot_hide_an_overridden_policy(tmp_path):
    config=tmp_path/'config.json';config.write_text('{"version":"1","version":"2"}')
    with pytest.raises(ValueError,match='DUPLICATE_JSON_KEY'):audit.read_json(config)


def test_documented_send_commands_are_checked_only_with_help(tmp_path,monkeypatch):
    doc=tmp_path/'run.md';doc.write_text('```sh\nfrost-signer ulr-treasury stake \\\n  --rpc "$RPC" --send\n```\n')
    monkeypatch.setattr(audit,'ROOT',tmp_path)
    commands=[]
    def help_only(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0,stdout='usage: --rpc --send --help')
    monkeypatch.setattr(audit.subprocess,'run',help_only)
    result=audit.check_cli(Path('/not-real/frost-signer'),['run.md'])
    assert result['examples']==1
    assert commands==[['/not-real/frost-signer','ulr-treasury','stake','--help']]


def test_unknown_runbook_option_is_reported(tmp_path,monkeypatch):
    (tmp_path/'run.md').write_text('```sh\nfrost-signer setup invite --missing-option value\n```')
    monkeypatch.setattr(audit,'ROOT',tmp_path)
    monkeypatch.setattr(audit.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='usage: --help'))
    with pytest.raises(ValueError,match='UNDOCUMENTED_CLI_FLAG'):audit.check_cli(Path('frost-signer'),['run.md'])


def test_profile_doctor_help_selects_the_profile_route_without_reading_it():
    body='```sh\nfrost-signer doctor --profile profile.json --state state.sqlite --json\n```'
    prefix,flags=list(audit.cli_examples(body))[0]
    assert prefix==['doctor','--profile','__audit_help_only__']
    assert flags==['--json','--profile','--state']


def test_abi_notation_inside_code_is_not_a_markdown_link(tmp_path,monkeypatch):
    (tmp_path/'run.md').write_text('ABI: `address[](recipients), uint256[](amounts)`')
    monkeypatch.setattr(audit,'ROOT',tmp_path)
    assert audit.check_links(['run.md'],tmp_path/'out')['checked']==0


def test_dkg_forwarding_wrapper_checks_base_options_without_running_an_action():
    body='```sh\nbtc() {\n  frost-signer dkg "$@" --rpc "$RPC" --board "$BOARD"\n}\n```'
    prefix,flags=list(audit.cli_examples(body))[0]
    assert prefix==['dkg','verify']
    assert flags==['--board','--rpc']


def test_nested_cli_route_is_preserved_for_help():
    body='```bash\nfrost-signer bitcoin continuity handover prepare --source-context "$OLD"\n```'
    prefix,flags=list(audit.cli_examples(body))[0]
    assert prefix==['bitcoin','continuity','handover','prepare']
    assert flags==['--source-context']
