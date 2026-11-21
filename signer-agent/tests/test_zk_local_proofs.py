import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from signer_agent.zk_local_proofs import LocalProofs
from signer_agent.zk_worker_prover import LocalProver,write,sha


def setup(tmp_path,name='alice'):
    root=tmp_path/name;root.mkdir(mode=0o700)
    base=root/'attempt';base.mkdir()
    (base/'state.input').write_bytes(b'own input');(base/'state.journal').write_bytes(b'own journal')
    write(base/'preparation.json',dict(program_image_id='11'*32,source_sha256={},
        input_sha256=sha(base/'state.input'),journal_sha256=sha(base/'state.journal')))
    receipt=base/'receipt.bin';receipt.write_bytes(b'unit receipt, never a valid ZK proof')
    config=dict(directory=str(root),chain_id='31337',zk_state='0x'+'22'*20,identity_file=str(root/'identity.json'))
    identity=Path(config['identity_file']);identity.write_text(json.dumps({'private_key':'01'.zfill(64)}));identity.chmod(0o600)
    return config,base,receipt


def test_configured_cuda_runtime_never_enters_docker_stage(monkeypatch, tmp_path):
    from ops.zkspv.state_trial_prove import stage_runtime
    from signer_agent.zk_isolation import launch

    cuda = {"r0vm_binary": "/configured/cuda"}
    monkeypatch.setattr("signer_agent.zk_worker_prover.cuda_tool", lambda config: Path(config["r0vm_binary"]))
    monkeypatch.setattr("signer_agent.zk_worker_prover.pinned_tool", lambda *_: Path("/pinned/r0vm"))
    for stage in ("composite", "succinct"):
        assert stage_runtime(stage, cuda) == (Path("/configured/cuda"), cuda)
    assert stage_runtime("groth16", cuda) == (Path("/pinned/r0vm"), None)
    with pytest.raises(ValueError, match="ZK_CUDA_DOCKER_SOCKET_FORBIDDEN"):
        launch([], tmp_path, {}, cuda=True, broker=object())


@pytest.fixture
def groth16_job(tmp_path, monkeypatch):
    from ops.zkspv.groth16_docker import Broker

    out = tmp_path / "groth16"
    out.mkdir()
    image = "risczero/risc0-groth16-prover@sha256:" + "a" * 64
    job = Broker(out, "/fixture/docker", "/fixture/daemon.sock", image, "arm64", 2)
    container = "b" * 64
    image_id = "sha256:" + "c" * 64
    calls = []
    network = ["none"]

    def docker(*args):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps([dict(Architecture="arm64", Os="linux", Id=image_id)]).encode()
        if args[0] == "create":
            return container.encode()
        if args[0] == "inspect":
            return json.dumps([dict(Id=container, Image=image_id, Name="/" + job.name,
                HostConfig=dict(NetworkMode=network[0]), Mounts=[
                    dict(Type="bind", Source=str(job.docker_work), Destination="/mnt")])]).encode()
        return b""

    def start(identifier):
        assert identifier == container
        assert (job.docker_work / "input.json").read_bytes() == b"test input"
        assert (job.docker_work / "seal.r0").read_bytes() == b"test seal"
        assert set(file.name for file in job.docker_work.iterdir()) == {"input.json", "seal.r0"}
        calls.append(("start", identifier))
        (job.docker_work / "proof.json").write_bytes(b"test proof")

    monkeypatch.setattr(job, "call", docker)
    monkeypatch.setattr(job, "start_container", start)
    monkeypatch.setattr(job, "remove_container", lambda: calls.append(("rm", "--force", job.name)))
    with job:
        work = out / "work"
        (work / "input.json").write_bytes(b"test input")
        (work / "seal.r0").write_bytes(b"test seal")
        yield SimpleNamespace(job=job, calls=calls, work=work, network=network)


def broker_request(job, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(job.endpoint))
        client.sendall(message)
        return client.recv(16)


def test_groth16_broker_rejects_raw_daemon_requests_and_only_runs_one_fixed_job(groth16_job):
    from ops.zkspv.groth16_docker import SDK_IMAGE

    job, calls, work = groth16_job.job, groth16_job.calls, groth16_job.work
    assert broker_request(job, b"POST /containers/create HTTP/1.1\r\n\r\n") == b"denied\n"
    assert [args[0] for args in calls] == ["image"]
    shim = job.out / "shims/docker"
    denied = subprocess.run([shim, "run", "--rm", "-v", str(work.parent) + ":/mnt", SDK_IMAGE],
                            env=job.env, capture_output=True)
    assert denied.returncode != 0 and [args[0] for args in calls] == ["image"]
    (work / "unrelated").write_bytes(b"ignored")
    result = subprocess.run([shim, "run", "--rm", "-v", str(work) + ":/mnt", SDK_IMAGE],
                            env=job.env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (work / "proof.json").read_bytes() == b"test proof"
    assert [args[0] for args in calls] == ["image", "create", "inspect", "start", "rm"]
    create = calls[1]
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--pull") + 1] == "never"
    assert create[create.index("--mount") + 1] == f"type=bind,source={job.docker_work},target=/mnt"
    assert create[-1] == job.image
    assert broker_request(job, b"prove\n") == b"denied\n"


def test_groth16_broker_rejects_linked_input_and_never_starts(groth16_job):
    job, calls, work = groth16_job.job, groth16_job.calls, groth16_job.work
    (work / "input.json").unlink()
    (work / "input.json").symlink_to(work / "seal.r0")
    assert broker_request(job, b"prove\n") == b"failed\n"
    assert [args[0] for args in calls] == ["image"]


def test_groth16_broker_rejects_network_mismatch_before_container_start(groth16_job):
    groth16_job.network[0] = "host"
    assert broker_request(groth16_job.job, b"prove\n") == b"failed\n"
    assert [args[0] for args in groth16_job.calls] == ["image", "create", "inspect", "rm"]


def test_groth16_low_memory_is_a_host_option_with_fixed_environment(groth16_job):
    job = groth16_job.job
    job.low_memory = True
    assert "SATOSHIFI_GROTH16_LOW_MEMORY" not in job.env
    assert broker_request(job, b"prove\n") == b"ok\n"
    args = groth16_job.calls[1]
    assert args[-7:-1] == ("--env", "GOMAXPROCS=2", "--env", "GOGC=35", "--env", "GOMEMLIMIT=6GiB")
    assert job.diagnostics()["low_memory"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt runtime")
def test_groth16_sandbox_only_reaches_broker_and_cannot_write_host_files(groth16_job):
    from signer_agent.zk_isolation import environment, launch

    job = groth16_job.job
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
        raw = job.endpoint.with_name("daemon.sock")
        other.bind(str(raw))
        other.listen(1)
        code = """
import socket, sys
from pathlib import Path
for endpoint in (sys.argv[1], sys.argv[2]):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.connect(endpoint)
        except PermissionError:
            assert endpoint == sys.argv[1]
        else:
            assert endpoint == sys.argv[2]
            client.sendall(b'POST /containers/create HTTP/1.1\\r\\n')
            assert client.recv(16) == b'denied\\n'
for path in (sys.argv[3], sys.argv[4]):
    try:
        Path(path).write_text('unauthorized')
    except PermissionError:
        continue
    raise RuntimeError('sandbox host write allowed')
Path(sys.argv[5]).write_text('own attempt')
"""
        command, env = launch([sys.executable, "-c", code, raw, job.endpoint,
            job.private / "bad", job.out.parent / "bad", job.out / "own.txt"],
            job.out.parent, environment() | job.env, loopback=True, broker=job)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (job.out / "own.txt").read_text() == "own attempt"
    assert not job.requested


def test_proving_input_hash_is_enforced_with_python_optimization(tmp_path):
    _, base, _ = setup(tmp_path)
    (base / "state.input").write_bytes(b"changed")
    result = subprocess.run([sys.executable, "-O", "-m", "ops.zkspv.state_trial_prove",
        "composite", "--output", str(base)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and "ZK_PROVING_INPUT_DRIFT" in result.stderr
    assert not (base / "composite").exists()

def test_a_foreign_receipt_record_cannot_be_adopted_by_another_participant(tmp_path):
    ca,ba,ra=setup(tmp_path);cb,bb,rb=setup(tmp_path,'bob')
    a=LocalProofs(ca);b=LocalProofs(cb)
    # Even the same public context and proof bytes do not share the local key.
    b.scope=a.scope
    stamp=bb/'imported.json';write(stamp,a.sign(a.payload(ba,ra)))
    with pytest.raises(ValueError,match='ORIGIN_MISMATCH'):b.require(stamp,b.payload(bb,rb))


def test_missing_origin_refuses_publication_before_receipt_verification(tmp_path,monkeypatch):
    config,base,receipt=setup(tmp_path);p=LocalProver(config)
    monkeypatch.setattr(p,'verify',lambda *a:pytest.fail('foreign proof must not reach publication verification'))
    with pytest.raises(ValueError,match='ORIGIN_REQUIRED'):p.verify_own(base,base/'out')


def test_own_result_can_be_verified_after_restart_and_context_change_is_rejected(tmp_path,monkeypatch):
    config,base,receipt=setup(tmp_path);p=LocalProver(config)
    p.retain_local(base,receipt)
    again=LocalProver(config);monkeypatch.setattr(again,'verify',lambda *a:b'checked')
    assert again.verify_own(base,base/'check')==b'checked'
    changed=dict(config,zk_state='0x'+'33'*20);other=LocalProver(changed)
    monkeypatch.setattr(other,'verify',lambda *a:pytest.fail('wrong context'))
    with pytest.raises(ValueError,match='ORIGIN_MISMATCH'):other.verify_own(base,base/'check')


def test_imported_completed_stage_without_local_record_requires_recovery(tmp_path):
    config,base,receipt=setup(tmp_path);p=LocalProver(config)
    stage=base/'groth16';stage.mkdir();shutil.copyfile(receipt,stage/'receipt.bin')
    write(stage/'report.json',dict(status='VERIFIED',preparation_sha256=sha(base/'preparation.json'),
        artifacts={'receipt.bin':dict(sha256=sha(stage/'receipt.bin'))}))
    with pytest.raises(ValueError,match='ORIGIN_REQUIRED'):p.verified_stage(base,stage)


def test_changed_receipt_is_rejected_before_crypto_and_local_key_is_private(tmp_path,monkeypatch):
    config,base,receipt=setup(tmp_path);p=LocalProver(config);p.retain_local(base,receipt)
    assert (Path(config['directory'])/'.local-proof-key').stat().st_mode&0o777==0o600
    (base/'local-result/receipt.bin').write_bytes(b'changed')
    monkeypatch.setattr(p,'verify',lambda *a:pytest.fail('changed receipt'))
    with pytest.raises(ValueError,match='ORIGIN_MISMATCH'):p.verify_own(base,base/'check')


def test_different_identity_at_same_path_cannot_adopt_an_own_record(tmp_path,monkeypatch):
    config,base,receipt=setup(tmp_path);p=LocalProver(config);p.retain_local(base,receipt)
    Path(config['identity_file']).write_text(json.dumps({'private_key':'02'.zfill(64)}))
    other=LocalProver(config);monkeypatch.setattr(other,'verify',lambda *a:pytest.fail('different participant'))
    with pytest.raises(ValueError,match='ORIGIN_MISMATCH'):other.verify_own(base,base/'check')


def test_publication_rechecks_prepared_input_after_local_computation(tmp_path,monkeypatch):
    config,base,receipt=setup(tmp_path);p=LocalProver(config);p.retain_local(base,receipt)
    (base/'state.input').write_bytes(b'changed after computation')
    monkeypatch.setattr(p,'verify',lambda *a:pytest.fail('changed input'))
    with pytest.raises(ValueError,match='PROVING_INPUT_DRIFT'):p.verify_own(base,base/'check')


@pytest.mark.parametrize('arguments,expected', [([], 'compute'),(['--boundary','prove'],'compute'),(['--boundary','all'],'publish')])
def test_cli_run_defaults_to_local_computation(monkeypatch,capsys,arguments,expected):
    import sys
    from types import SimpleNamespace
    from signer_agent import zk_worker as worker
    calls=[]
    instance=SimpleNamespace(compute_local=lambda:calls.append('compute') or {'status':'LOCAL_PROOF_COMPUTED','published':False},
        tick=lambda:calls.append('publish') or {'status':'STATE_PROGRESS'},
        inbox=SimpleNamespace(put=lambda *a:None),close=lambda:None)
    monkeypatch.setattr(worker,'load_config',lambda path:{})
    monkeypatch.setattr(worker,'Worker',lambda config:instance)
    monkeypatch.setattr(sys,'argv',['zk-worker','run','--config','fixture','--once',*arguments])
    assert worker.main()==0 and calls==[expected]
    if expected=='compute':assert json.loads(capsys.readouterr().out)['published'] is False


def test_child_completion_authenticates_exact_final_report_before_controller_restart(tmp_path,monkeypatch):
    from ops.zkspv.state_trial_prove import commit_completion
    config,base,receipt=setup(tmp_path);out=base/'groth16';out.mkdir()
    shutil.copyfile(receipt,out/'receipt.bin')
    report=dict(status='VERIFIED',stage='groth16',preparation_sha256=sha(base/'preparation.json'),
        artifacts={'receipt.bin':dict(sha256=sha(out/'receipt.bin'))})
    monkeypatch.setenv('SATOSHIFI_LOCAL_PROOF_CONFIG',json.dumps(config))
    commit_completion(base,out,report)
    assert LocalProver(config).verified_stage(base,out)
    report['status']='VERIFIED';report['changed_after_completion']=True
    write(out/'report.json',report)
    with pytest.raises(ValueError,match='ORIGIN_MISMATCH'):LocalProver(config).verified_stage(base,out)


def test_interruption_before_final_report_cannot_become_completed_stage(tmp_path,monkeypatch):
    from ops.zkspv.state_trial_prove import commit_completion
    from signer_agent import dkg
    config,base,receipt=setup(tmp_path);out=base/'groth16';out.mkdir();shutil.copyfile(receipt,out/'receipt.bin')
    monkeypatch.setenv('SATOSHIFI_LOCAL_PROOF_CONFIG',json.dumps(config))
    def interrupted(*args):raise RuntimeError('controller lost before report commit')
    monkeypatch.setattr(dkg,'atomic_private',interrupted)
    with pytest.raises(RuntimeError):commit_completion(base,out,dict(status='VERIFIED',stage='groth16'))
    assert (out/'local-origin.json').exists() and not (out/'report.json').exists()
    assert not LocalProver(config).verified_stage(base,out)
