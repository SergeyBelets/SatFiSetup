#!/usr/bin/env python3
"""Retain V1 history encoding and implementation verbatim; no consensus rewrite."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
FORK=HERE.parent/'fork-policy'
spec=importlib.util.spec_from_file_location('resume_extract',HERE.parent/'tools/prepare_arithmetic_probe.py')
extract=importlib.util.module_from_spec(spec); spec.loader.exec_module(extract)
def sha(data): return hashlib.sha256(data).hexdigest()
def generated():
    report=json.loads((FORK/'report.json').read_text())
    for name in ['src/lib.rs','src/history.rs']:
        path=FORK/name
        if sha(path.read_bytes())!=report['source_sha256'][str(path.relative_to(ROOT))]: raise ValueError('V1 reference changed')
    source=(FORK/'src/lib.rs').read_text()
    text='''// SPDX-License-Identifier: MIT
// Generated from the qualified V1 source; function bodies are unchanged.
use bitcoin::{block::Header, consensus::deserialize, hashes::Hash as _};
use super::history::{digest, Hash};
'''
    text+='\n\n'.join(extract.function(source,name) for name in ['parse','scope','leaf','canonical_root'])
    text+='''
pub(crate) fn header(raw: &[u8]) -> Option<(Header, [u8; 80])> { parse(raw) }
pub(crate) fn scoped(anchor: Hash, height: u64) -> Hash { scope(anchor, height) }
pub(crate) fn history_leaf(scope: Hash, height: u64, header: &Header) -> Hash { leaf(scope, height, header) }
pub(crate) fn canonical(scope: Hash, head: Hash, height: u64, tree: Hash) -> Hash { canonical_root(scope, head, height, tree) }
'''
    history=(FORK/'src/history.rs').read_text()
    manifest=dict(schema='satoshifi.resume-reuse.v2',classification='NEW_INTEGRATION',
        reused='V1 persistent history bytes and four encoding/parsing function bodies unchanged',
        new_critical_code=['256-bit authenticated known-context index','V2 state commitment','exact LCA witnesses','bounded stateless transitions','archive witness construction'],
        source_sha256={str((FORK/name).relative_to(ROOT)):sha((FORK/name).read_bytes()) for name in ['src/lib.rs','src/history.rs']},
        generated_sha256={'src/history.rs':sha(history.encode()),'src/bindings.rs':sha(text.encode())})
    return [(HERE/'src/history.rs',history),(HERE/'src/bindings.rs',text),(HERE/'source-map.json',json.dumps(manifest,indent=2)+'\n')]
def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--check',action='store_true'); args=parser.parse_args()
    for path,text in generated():
        if args.check:
            if path.read_text()!=text: raise ValueError('Generated source mismatch: '+str(path))
        else: path.write_text(text)
    print('V1 history reuse matches exact pinned bytes')
if __name__=='__main__': main()
