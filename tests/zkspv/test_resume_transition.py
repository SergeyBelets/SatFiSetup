"""Independent V2 public statement encoding; never a consensus/proof substitute."""
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / 'rust/zkspv/resume-transition'

def framed(domain, *parts):
    return hashlib.sha256(b'SATOSHIFI_FORK_V1\0' + len(domain).to_bytes(4, 'big') + domain
        + b''.join(len(p).to_bytes(8, 'big') + p for p in parts)).digest()

def cases():
    fixture = json.loads((ROOT / 'web3/test/fixtures/transition-policy-v2.json').read_text())
    assert fixture['authentication'].startswith('NONE')
    return fixture['cases']

def decode(case):
    raw = bytes.fromhex(case['journal'][2:])
    schema = json.loads((HERE / 'journal-schema.json').read_text())
    assert len(raw) == 1216 and len(schema['words']) == 38
    result = {}
    for i, (name, kind) in enumerate(schema['words']):
        w = raw[i*32:(i+1)*32]
        if kind == 'bytes32':
            result[name] = w
        else:
            value = int.from_bytes(w, 'big')
            bits = {'address': 160, 'bool': 1}.get(kind)
            if bits is None:
                bits = int(kind.removeprefix('uint'))
            assert value < 2**bits
            result[name] = value
    for name in ['domain', 'bitcoin_network', 'time_domain']:
        assert result[name] == schema[name].encode().ljust(32, b'\0')
    return result

def test_both_v2_state_commitments_match_independent_python_hashing():
    assert len(cases()) == 6
    for case in cases():
        d = decode(case)
        for prefix in ['previous', 'next']:
            halt = framed(b'running')
            if prefix == 'next' and d['halted']:
                halt = framed(b'halt', d['halt_old_head'], d['halt_candidate'], d['halt_lca'], d['halt_depth'].to_bytes(8, 'big'))
            computed = framed(b'resume-state-v2', d['checkpoint_descriptor'], d['anchor_hash'], d['anchor_height'].to_bytes(8, 'big'),
                d[prefix+'_known_root'], d[prefix+'_known_log'], d[prefix+'_known_count'].to_bytes(8, 'big'),
                d[prefix+'_canonical_root'], d[prefix+'_head'], d[prefix+'_height'].to_bytes(8, 'big'),
                d[prefix+'_work'].to_bytes(32, 'big'), d[prefix+'_tip_time'].to_bytes(4, 'big'), halt)
            assert computed == d[prefix+'_state_commitment']
        assert d['next_known_count'] - d['previous_known_count'] == d['new_header_count']
        assert 0 < d['new_header_count'] <= d['processed_count'] <= d['submitted_count'] <= 64
        assert (d['next_work'] > d['previous_work']) == case['useful']
        assert bool(d['halted']) == case['halted']
        assert d['maximum_timestamp'] == case['maximum']

def test_input_and_journal_are_separate_bounded_versioned_formats():
    schema = json.loads((HERE / 'input-schema.json').read_text())
    assert schema['max_input_bytes'] == 402 + 2*64 + 5120 + 4 + 2275554
    assert schema['append_input_bytes'] == 402 + 2 + 80 + 4 + 31317
    assert schema['guest_execution'] == 'NOT_IMPLEMENTED'
    assert schema['real_proof'] == 'NOT_IMPLEMENTED'
    fixtures = {c['name']: decode(c) for c in cases()}
    for name in ['nonmonotonic', 'losing_branch', 'halt_prefix']:
        d = fixtures[name]
        assert d['maximum_timestamp'] > d['next_tip_time']
    side = fixtures['losing_branch']
    assert side['next_work'] == side['previous_work'] and side['next_head'] == side['previous_head']
    halt = fixtures['halt_prefix']
    assert halt['halt_position_one_based'] == halt['processed_count'] == 2
    assert halt['submitted_count'] == 3 and halt['halt_depth'] == 101

def test_native_pins_and_all_prior_components_are_retained():
    spec = importlib.util.spec_from_file_location('resume_transition_test', HERE / 'qualify.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.dependencies()) == 16
    _, original, corrected = module.prepare.checked_sources()
    native = module.native_source(original, corrected)
    assert (HERE / 'native_tests.rs').read_text() in native
    assert str(HERE / 'src/input.rs') in native and str(HERE / 'src/transition.rs') in native
    for name in ['resume-policy', 'transition-policy', 'checkpoint-policy', 'fork-policy', 'time-policy']:
        assert (HERE.parent / name / 'native_tests.rs').read_text() in native
