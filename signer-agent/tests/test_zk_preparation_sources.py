"""Keep historical qualification immutable while freezing a new operational attempt."""
from pathlib import Path
import pytest
from signer_agent import zk_worker_prover as m
from signer_agent.zk_spv import MAINNET_IMAGE


@pytest.fixture
def sources(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'ROOT',tmp_path)
    from signer_agent import zk_profiles, zk_prover_platform
    monkeypatch.setattr(zk_profiles,'ROOT',tmp_path)
    guest=tmp_path/'rust/guest.rs';guest.parent.mkdir();guest.write_text('pinned guest')
    evm=tmp_path/'web3/parser.sol';evm.parent.mkdir();evm.write_text('current parser')
    program=tmp_path/'rust/zkspv/guest-integration/evidence/mainnet-transition.r0bf'
    program.parent.mkdir(parents=True);program.write_bytes(b'pinned program')
    (program.parent.parent/'report.json').write_text('{}')
    (tmp_path/'config').mkdir();(tmp_path/'config/zk-prover-3.1.0.json').write_text('{}')
    monkeypatch.setattr(zk_prover_platform,'release_sources',lambda:{'config/zk-prover-3.1.0.json':None})
    cuda_patch=tmp_path/'ops/zkspv/r0vm-3.0.3-segment-limit.patch'
    cuda_patch.parent.mkdir(parents=True);cuda_patch.write_text('pinned CUDA segment patch')
    report=dict(program_image_id=MAINNET_IMAGE, source_sha256={'rust/guest.rs':m.sha(guest),'web3/parser.sol':'historical parser'},
        evidence_sha256={program.name:m.sha(program)})
    return report,guest,evm,program


def test_current_non_guest_revision_is_frozen_without_rewriting_history(sources):
    report,guest,evm,program=sources
    actual=m.preparation_sources(report)
    assert actual['web3/parser.sol']==m.sha(evm)
    assert report['source_sha256']['web3/parser.sol']=='historical parser'
    assert actual['rust/guest.rs']==report['source_sha256']['rust/guest.rs']
    evm.write_text('changed after preparation')
    assert actual['web3/parser.sol']!=m.sha(evm)


@pytest.mark.parametrize('kind',['source','program','image'])
def test_guest_drift_never_gets_a_new_implicit_pin(sources,kind):
    report,guest,evm,program=sources
    if kind=='source':guest.write_text('changed')
    if kind=='program':program.write_bytes(b'changed')
    if kind=='image':report['program_image_id']='ff'*32
    with pytest.raises(ValueError,match='ZK_GUEST_'):m.preparation_sources(report)


def test_tool_override_keeps_exact_executable_hash(tmp_path,monkeypatch):
    tool=tmp_path/'r0vm';tool.write_bytes(b'unit tool')
    tool.chmod(0o755)
    from signer_agent import zk_prover_platform
    trusted=m.sha(tool)
    monkeypatch.setattr(zk_prover_platform,'profile',lambda:dict(platform='darwin-arm64',tools={'r0vm':trusted}))
    monkeypatch.setenv('SATOHSI_R0VM',str(tool))
    assert m.pinned_tool('r0vm',trusted)==tool
    tool.write_bytes(b'changed')
    with pytest.raises(ValueError,match='TOOL_DRIFT'):m.pinned_tool('r0vm',trusted)
