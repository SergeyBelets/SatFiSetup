"""Historical bytes must match old pins; missing tools cannot qualify new code."""
import hashlib,json
from pathlib import Path
import pytest
from ops.zkspv import historical_sources as h


def store(tmp_path,monkeypatch):
    root=tmp_path/'current';root.mkdir();archive=tmp_path/'retained';archive.mkdir()
    monkeypatch.setattr(h,'ROOT',root);monkeypatch.setattr(h,'STORE',archive)
    return root,archive


def test_exact_retained_source_is_authenticated_without_changing_current_file(tmp_path,monkeypatch):
    root,archive=store(tmp_path,monkeypatch);(root/'X.sol').write_text('new source')
    raw=b'original source';digest=hashlib.sha256(raw).hexdigest();(archive/digest).write_bytes(raw)
    (archive/'manifest.json').write_text(json.dumps(dict(files={'X.sol':dict(sha256=digest,file=digest)},missing_tools={})))
    result=h.audit({'X.sol':digest})
    assert result['status']=='HISTORICAL_RETAINED' and result['historical_reproduction_complete']
    assert not result['current_source_qualification'] and (root/'X.sol').read_text()=='new source'
    (archive/digest).write_text('edited history')
    with pytest.raises(ValueError,match='CORRUPT'):h.audit({'X.sol':digest})


def test_missing_ordinary_source_cannot_use_the_missing_tool_exception(tmp_path,monkeypatch):
    root,archive=store(tmp_path,monkeypatch);digest='11'*32
    (archive/'manifest.json').write_text(json.dumps(dict(files={},missing_tools={'missing.sol':digest})))
    with pytest.raises(ValueError,match='UNAVAILABLE'):h.audit({'missing.sol':digest})


def test_exact_declared_missing_old_executable_is_an_explicit_reproduction_gap(tmp_path,monkeypatch):
    root,archive=store(tmp_path,monkeypatch);name=next(iter(h.TOOLS));digest='22'*32
    (archive/'manifest.json').write_text(json.dumps(dict(files={},missing_tools={name:digest})))
    result=h.audit({name:digest})
    assert result['status']=='HISTORICAL_TOOL_UNAVAILABLE' and result['missing_tools']=={name:digest}
    assert not result['historical_reproduction_complete'] and not result['production_ready']
    with pytest.raises(ValueError,match='UNAVAILABLE'):h.audit({name:'33'*32})


def test_historical_path_cannot_read_outside_the_recorded_repository(tmp_path,monkeypatch):
    store(tmp_path,monkeypatch)
    for name in ('../outside','/etc/passwd'):
        with pytest.raises(ValueError,match='SOURCE_PATH'):h.resolve(name,'44'*32)
