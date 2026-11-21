"""Authenticate original evidence bytes without rebinding them to current code.

The retired DKG helper binary is a tool, not a source file. If it is unavailable,
that limitation is returned explicitly and full historical reproduction is false.
No missing source file is excused and no stored proof is relabelled as current.
"""
import json
from pathlib import Path
from .contour import ROOT,sha

STORE=ROOT/'ops/artifacts/zkspv/historical-sources'
TOOLS={'frost-crypto/target/debug/satohsi-frost-crypto'}


def resolve(name,expected):
    path=Path(name)
    if path.is_absolute() or '..' in path.parts:raise ValueError('HISTORICAL_SOURCE_PATH')
    current=ROOT/path
    if current.is_file() and not current.is_symlink() and sha(current)==expected:return current
    manifest=json.loads((STORE/'manifest.json').read_text())
    entry=manifest.get('versions', {}).get(name, {}).get(expected) or manifest['files'].get(name)
    if (not entry or entry['sha256']!=expected or entry['file']!=expected
            or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected)):
        raise ValueError('HISTORICAL_SOURCE_UNAVAILABLE:'+name)
    stored=STORE/expected
    if stored.is_symlink() or not stored.is_file() or sha(stored)!=expected:raise ValueError('HISTORICAL_SOURCE_CORRUPT:'+name)
    return stored


def audit(files):
    current=[];retained=[];missing={}
    for name,h in files.items():
        try:p=resolve(name,h)
        except ValueError:
            # Only a separately declared, exact old executable pin may be missing.
            m=json.loads((STORE/'manifest.json').read_text())
            if name not in TOOLS or m['missing_tools'].get(name)!=h:raise
            missing[name]=h;continue
        (current if p==ROOT/name else retained).append(name)
    return dict(status='HISTORICAL_TOOL_UNAVAILABLE' if missing else 'HISTORICAL_RETAINED' if retained else 'CURRENT',
        current_sources=current,retained_sources=retained,missing_tools=missing,
        historical_reproduction_complete=not missing,current_source_qualification=False,production_ready=False)
