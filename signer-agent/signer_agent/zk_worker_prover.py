"""Pinned local proving stages and independently verified receipt cache.

The qualified runner denies IP networking, and its Groth16 container uses
network=none. No remote proving fallback or development receipts are accepted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
MAX_STAGE_TIMEOUT_SECONDS = 86400


def validate_cuda(config):
    if (not isinstance(config, dict) or set(config) != {"r0vm_binary", "r0vm_sha256", "segment_limit_po2"}
            or not all(isinstance(value, str) for value in config.values())
            or not Path(config["r0vm_binary"]).is_absolute()
            or not re.fullmatch(r"[0-9a-f]{64}", config["r0vm_sha256"])
            or config["segment_limit_po2"] not in {str(n) for n in range(16, 22)}):
        raise ValueError("ZK_CUDA_CONFIG")


def cuda_tool(config):
    import platform
    validate_cuda(config)
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("ZK_CUDA_PLATFORM_REQUIRED")
    path = Path(config["r0vm_binary"]).resolve()
    if not path.is_file() or not os.access(path, os.X_OK) or sha(path) != config["r0vm_sha256"]:
        raise ValueError("ZK_CUDA_RUNTIME_PIN")
    return path

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text())


def pinned_tool(name, expected=None):
    """Locate the same qualified executable; a path override never changes its pin."""
    from .zk_prover_platform import release_tool, tool
    return release_tool(name) if expected is None else tool(name,expected)


def preparation_sources(guest):
    """Freeze this attempt separately from the historical qualification inventory.

    The RISC-V program and its Rust source inventory retain their original pins.
    EVM/parser/SDK revisions are independently checked by runtime provenance and
    frozen here; recording them does not requalify the guest or deployment.
    """
    from .zk_profiles import profile
    selected=profile({'network':guest.get('bitcoin_network','mainnet')})
    if guest['program_image_id'] != selected['image']: raise ValueError('ZK_GUEST_IMAGE_DRIFT')
    for name, expected in guest['source_sha256'].items():
        if name.startswith('rust/') and not name.startswith('rust/zkspv/state-adapter/') and sha(ROOT/name) != expected:
            raise ValueError('ZK_GUEST_SOURCE_DRIFT:'+name)
    program = selected['program']
    if sha(program) != guest['evidence_sha256'][program.name]: raise ValueError('ZK_GUEST_PROGRAM_DRIFT')
    paths = {ROOT/name for name in guest['source_sha256']}
    paths.update((ROOT/'signer-agent/signer_agent').glob('*.py'))
    paths.update((ROOT/'ops/zkspv').glob('*.py'))
    paths.update((program, selected['report']))
    from .zk_prover_platform import release_sources
    paths.update(ROOT/name for name in release_sources())
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}

def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class LocalProver:
    def __init__(self, config, lock_fd=None):
        self.config, self.lock_fd = config, lock_fd

    def origins(self):
        from .zk_local_proofs import LocalProofs
        return LocalProofs(self.config)

    def verified_stage(self, base, folder):
        report = folder / "report.json"
        if not report.exists() or read(report).get("status") != "VERIFIED": return False
        r = read(report)
        if r["preparation_sha256"] != sha(base / "preparation.json"):
            raise ValueError("ZK_STAGE_PREPARATION_DRIFT")
        if not r.get("artifacts") or "receipt.bin" not in r["artifacts"]:
            raise ValueError("ZK_STAGE_ARTIFACT_MISSING")
        for name, item in r["artifacts"].items():
            if sha(folder / name) != item["sha256"]: raise ValueError("ZK_STAGE_ARTIFACT_DRIFT")
        origin=self.origins()
        origin.require(folder/'local-origin.json',origin.payload(base,folder/'receipt.bin',stage=folder.name,report=report))
        return True

    def retain_local(self,base,receipt):
        """Only called after generating/verifying our stage or authenticating our cache."""
        folder=base/'local-result';folder.mkdir(exist_ok=True)
        shutil.copyfile(receipt,folder/'receipt.bin')
        origin=self.origins()
        write(folder/'origin.json',origin.sign(origin.payload(base,folder/'receipt.bin')))

    def verify_own(self,base,output):
        self.checked_preparation(base)
        receipt=base/'local-result/receipt.bin';origin=self.origins()
        if not receipt.is_file():raise ValueError('ZK_LOCAL_PROOF_ORIGIN_REQUIRED')
        origin.require(base/'local-result/origin.json',origin.payload(base,receipt))
        return self.verify(base,receipt,output)

    def checked_preparation(self,base):
        prep=read(base/'preparation.json')
        for name,digest in prep['source_sha256'].items():
            if sha(ROOT/name)!=digest:raise ValueError('ZK_PROVING_SOURCE_DRIFT')
        if sha(base/'state.input')!=prep['input_sha256'] or sha(base/'state.journal')!=prep['journal_sha256']:
            raise ValueError('ZK_PROVING_INPUT_DRIFT')
        return prep

    def preflight(self, base):
        """Fail before STARK if the final local wrapper/image is unavailable."""
        for name in ("r0vm", "satoshifi-receipt-bridge", "wrap_groth16"):
            pinned_tool(name)
        from .zk_prover_platform import release_profile
        from .zk_isolation import docker_endpoint
        p=release_profile();image=p['container_image'];binary,socket=docker_endpoint()
        docker_config = base / "preflight-docker-config"; docker_config.mkdir(exist_ok=True)
        result = subprocess.run([str(binary), "--config", str(docker_config), "--host", "unix://"+str(socket),
            "image", "inspect", image], capture_output=True, text=True, timeout=30)
        if result.returncode: raise ValueError("ZK_LOCAL_PROVER_IMAGE_REQUIRED")
        inspected = json.loads(result.stdout)[0]
        if inspected["Architecture"] != p['docker_arch'] or inspected["Os"] != "linux": raise ValueError("ZK_LOCAL_PROVER_IMAGE_MISMATCH")
        from .zk_isolation import launch,environment,probe_code
        for loopback in (False,True):
            command,env=launch([sys.executable,'-c',probe_code(loopback)],base,environment(),loopback=loopback)
            subprocess.run(command,env=env,capture_output=True,timeout=30,check=True)
        write(base / "prover-preflight.json", dict(image=image, image_id=inspected["Id"], remote_proving=False))

    def verify(self, base, receipt, output):
        # Same pinned local tools as the qualified stage runner; resolving these
        # does not depend on the participant's current working directory.
        from .zk_profiles import profile
        selected=profile(self.config.get('bitcoin_policy',{'network':'mainnet'}))
        guest = read(selected['report'])
        BRIDGE = pinned_tool("satoshifi-receipt-bridge")
        program = selected['program']
        if sha(program) != guest["evidence_sha256"][program.name]:
            raise ValueError("ZK_PROVER_TOOL_DRIFT")
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="receipt-check-", dir=base) as temporary:
            result = Path(temporary) / "result"  # The pinned inspector requires a new directory.
            with (output / "verify.log").open("w") as log:
                from .zk_isolation import launch,environment
                command,env=launch([BRIDGE,'inspect',program,base/'state.journal',receipt,result],base,environment(),
                    read_only=[BRIDGE,program,receipt],lock_fd=self.lock_fd)
                subprocess.run(command,env=env,pass_fds=() if self.lock_fd is None else (self.lock_fd,),
                    stdout=log, stderr=subprocess.STDOUT, timeout=180, check=True)
            verified = read(result / "receipt.json")
            seal = (result / "seal.bin").read_bytes()
        if (verified.get("status") != "VERIFIED" or verified.get("receipt_kind") != "groth16"
                or verified.get("image_id") != selected['image'] or verified.get("dev_mode_disabled") is not True
                or verified.get("journal_sha256") != sha(base / "state.journal")
                or verified.get("receipt_sha256") != sha(receipt)):
            raise ValueError("ZK_RECEIPT_NOT_VERIFIED")
        if len(seal) != 260: raise ValueError("ZK_SEAL_LENGTH")
        from .zk_spv import VERIFIER_SELECTOR
        if seal[:4] != VERIFIER_SELECTOR:
            raise ValueError("ZK_VERIFIER_3_1_REQUIRED")
        write(output / "receipt.json", verified)
        (output / "seal.bin").write_bytes(seal)
        return seal

    def prove(self, base, *, use_cache=True, max_new_stages=None):
        if max_new_stages is not None and (type(max_new_stages) is not int or not 1<=max_new_stages<=3):
            raise ValueError('ZK_PROVER_STAGE_BOUND')
        if sys.platform not in ('darwin','linux'):raise ValueError('ZK_LOCAL_RUNNER_PLATFORM_NOT_SUPPORTED')
        prep = self.checked_preparation(base)
        origin=self.origins()
        cache = Path(self.config["directory"]) / "receipt-cache/local-v1"
        cache.mkdir(parents=True,exist_ok=True)
        # Legacy/imported caches are deliberately outside this namespace.
        from .zk_local_proofs import canonical
        key=hashlib.sha256(canonical(dict(scope=origin.scope,image=prep['program_image_id'],
            journal=prep['journal_sha256'],input=prep['input_sha256'],sources=prep['source_sha256']))).hexdigest()
        cached = cache / (key + ".bin")
        if use_cache and cached.exists():
            origin.require(cached.with_suffix('.origin.json'),origin.payload(base,cached))
            seal = self.verify(base, cached, base / "cache-verified")
            self.retain_local(base,cached)
            write(base / "proof-result.json", dict(cache_hit=True, locally_computed=True, receipt_sha256=sha(cached), key=key))
            return seal
        self.preflight(base)
        started=0
        for stage in ("composite", "succinct", "groth16"):
            folder = base / stage; report = folder / "report.json"
            running = base / (stage + ".running.json")
            if self.verified_stage(base, folder):
                if running.exists(): running.unlink()
                continue
            if running.exists():
                # An interrupted stage may still own a prover process. Do not
                # spend resources again without its completion evidence.
                if not report.exists() or read(report).get("status") != "INCOMPLETE":
                    raise ValueError("ZK_STAGE_RECOVERY_REQUIRED")
                running.unlink()  # Runner wrote this only after waiting/cleanup.
            if folder.exists():
                if not report.exists() or read(report).get("status") != "INCOMPLETE":
                    raise ValueError("ZK_FAILED_STAGE_REVIEW_REQUIRED")
                failures = list(base.glob(stage + ".failed-*"))
                folder.rename(base / (stage + ".failed-" + str(len(failures) + 1)))
            if len(list(base.glob(stage + ".failed-*"))) > int(self.config["max_stage_retries"]):
                raise ValueError("ZK_STAGE_RETRY_BUDGET")
            if shutil.disk_usage(base).free < int(self.config["minimum_free_bytes"]):
                raise ValueError("ZK_PROVER_DISK_BUDGET")
            write(running, dict(stage=stage, preparation_sha256=sha(base / "preparation.json")))
            env=dict(os.environ)
            env['SATOSHIFI_LOCAL_PROOF_CONFIG']=json.dumps({k:self.config.get(k) for k in ('directory','chain_id','zk_state','identity_file')})
            env.pop("SATOSHIFI_LOCAL_PROOF_CUDA", None)
            if self.config.get("cuda"):
                env["SATOSHIFI_LOCAL_PROOF_CUDA"] = json.dumps(self.config["cuda"])
            env.pop('SATOSHIFI_PROVER_LOCK_FD',None)
            if self.lock_fd is not None:env['SATOSHIFI_PROVER_LOCK_FD']=str(self.lock_fd)
            with (base / (stage + ".log")).open("w") as log:
                result = subprocess.run([sys.executable, "-m", "ops.zkspv.state_trial_prove", stage,
                    "--output", str(base), "--timeout", self.config["stage_timeout_seconds"]], cwd=ROOT,
                    stdout=log, stderr=subprocess.STDOUT,env=env,
                    pass_fds=() if self.lock_fd is None else (self.lock_fd,))
            if not report.exists(): raise ValueError("ZK_STAGE_RECOVERY_REQUIRED")
            running.unlink()
            if result.returncode:
                if read(report).get("status") == "INCOMPLETE":
                    failures = list(base.glob(stage + ".failed-*"))
                    folder.rename(base / (stage + ".failed-" + str(len(failures) + 1)))
                raise ValueError("ZK_PROVER_STAGE_FAILED:" + stage)
            if not self.verified_stage(base,folder):raise ValueError('ZK_STAGE_NOT_VERIFIED')
            started+=1
            if max_new_stages is not None and started>=max_new_stages and stage!='groth16':return None
        receipt = base / "groth16/receipt.bin"
        seal = self.verify(base, receipt, base / "final-verified")
        temporary = cached.with_suffix(".tmp")
        shutil.copyfile(receipt, temporary); os.replace(temporary, cached)
        write(cached.with_suffix('.origin.json'),origin.sign(origin.payload(base,cached)))
        self.retain_local(base,receipt)
        write(base / "proof-result.json", dict(cache_hit=False, locally_computed=True, receipt_sha256=sha(cached), key=key))
        return seal
