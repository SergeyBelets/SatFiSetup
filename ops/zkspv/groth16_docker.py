"""One offline Groth16 container per local attempt, managed outside the sandbox."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import uuid

SDK_IMAGE = "risczero/risc0-groth16-prover:v2025-04-03.1"
INPUT_LIMIT = 32 * 1024 * 1024
PROOF_LIMIT = 64 * 1024


def read_file(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("ZK_GROTH16_JOB_FILE")
        value = stream.read(limit + 1)
        if not value or len(value) > limit:
            raise ValueError("ZK_GROTH16_JOB_FILE")
        return value


def create_args(name, work, image, *, low_memory=False, arch=None):
    args = ["create", "--rm", "--name", name, "--network", "none", "--pull", "never",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=bind,source={work},target=/mnt"]
    if arch:
        args += ["--platform", "linux/" + arch]
    if low_memory:
        args += ["--env", "GOMAXPROCS=2", "--env", "GOGC=35", "--env", "GOMEMLIMIT=6GiB"]
    return args + [image]


class Broker:
    def __init__(self, out, binary, daemon, image, arch, timeout, *, sdk_image=SDK_IMAGE):
        self.out, self.binary, self.daemon = Path(out), Path(binary), Path(daemon)
        self.image, self.arch, self.timeout = image, arch, timeout
        self.sdk_image = sdk_image
        self.low_memory = os.environ.get("SATOSHIFI_GROTH16_LOW_MEMORY") == "1"
        self.name = "satoshifi-groth16-" + uuid.uuid4().hex
        self.stop = Event()
        self.requested = False
        self.observed = {}
        self.phase = "request"
        self.failure = None
        self.container_exit_code = None

    def __enter__(self):
        if (not self.binary.is_absolute() or not self.daemon.is_absolute()
                or not re.fullmatch(r"risczero/risc0-groth16-prover@sha256:[0-9a-f]{64}", self.image)):
            raise ValueError("ZK_GROTH16_BROKER_CONFIG")
        with ExitStack() as setup:
            root = Path(setup.enter_context(TemporaryDirectory(
                prefix="sat-g16-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp")))
            if root.is_relative_to(self.out.parent.resolve()):
                raise ValueError("ZK_GROTH16_BROKER_CONFIG")
            self.endpoint = root / "broker.sock"
            self.private = root / "docker"
            self.private.mkdir(mode=0o700)
            config = self.private / "config"
            config.mkdir(mode=0o700)
            self.docker_work = self.private / "work"
            self.docker_work.mkdir(mode=0o700)
            self.docker_work.chmod(0o777)
            self.docker = [str(self.binary), "--config", str(config), "--host", "unix://" + str(self.daemon)]
            self.docker_env = {"PATH": "/usr/bin:/bin"}
            try:
                pin = json.loads(self.call("image", "inspect", self.image))[0]
            except ValueError:
                raise ValueError("ZK_LOCAL_PROVER_IMAGE_REQUIRED") from None
            if pin["Architecture"] != self.arch or pin["Os"] != "linux":
                raise ValueError("ZK_LOCAL_PROVER_IMAGE_MISMATCH")
            self.image_id = pin["Id"]

            work = self.out / "work"
            work.mkdir(mode=0o700)
            self.work_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            setup.callback(os.close, self.work_fd)
            shim = self.out / "shims" / "docker"
            shim.parent.mkdir(mode=0o700)
            source = Path(__file__).resolve()
            shim.write_text(f"#!{Path(sys.executable).resolve()} -I\n"
                            f"exec(compile(open({str(source)!r}, 'rb').read(), {str(source)!r}, 'exec'))\n")
            shim.chmod(0o700)
            self.read_only = (source, shim)
            temporary = self.out / "tmp"
            temporary.mkdir(mode=0o700)
            self.env = {"PATH": str(shim.parent) + ":/usr/bin:/bin",
                        "SATOSHIFI_GROTH16_BROKER": str(self.endpoint), "RISC0_WORK_DIR": str(work),
                        "SATOSHIFI_GROTH16_SDK_IMAGE": self.sdk_image,
                        "TMPDIR": str(temporary)}
            self.listener = setup.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            self.listener.bind(str(self.endpoint))
            self.endpoint.chmod(0o600)
            self.listener.listen(4)
            self.listener.settimeout(0.2)
            self.thread = Thread(target=self.serve, daemon=True)
            self.thread.start()
            self.resources = setup.pop_all()
        return self

    def __exit__(self, *_):
        self.stop.set()
        try:
            self.thread.join()
        finally:
            self.resources.close()

    def diagnostics(self):
        return dict(requested=self.requested, failure=self.failure,
                    container_exit_code=self.container_exit_code, low_memory=self.low_memory)

    def call(self, *args):
        result = subprocess.run(self.docker + list(args), env=self.docker_env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode != 0:
            raise ValueError("ZK_GROTH16_DOCKER_COMMAND")
        return result.stdout

    def serve(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(2)
                with client.makefile("rb") as stream:
                    try:
                        allowed = stream.readline(16) == b"prove\n" and not self.requested
                        if allowed:
                            self.requested = True
                            self.prove()
                        client.sendall(b"ok\n" if allowed else b"denied\n")
                    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
                        if self.requested and not self.observed:
                            self.failure = self.phase
                        try:
                            client.sendall(b"failed\n")
                        except OSError:
                            pass

    def prove(self):
        self.phase = "input"
        for name in ("input.json", "seal.r0"):
            (self.docker_work / name).write_bytes(read_file(self.work_fd, name, INPUT_LIMIT))
            (self.docker_work / name).chmod(0o644)
        if self.stop.is_set():
            raise ValueError("ZK_GROTH16_DOCKER_COMMAND")
        try:
            self.phase = "create"
            args = create_args(self.name, self.docker_work, self.image, low_memory=self.low_memory, arch=self.arch)
            container = self.call(*args).decode().strip()
            if self.stop.is_set() or not re.fullmatch(r"[0-9a-f]{64}", container):
                raise ValueError("ZK_LOCAL_PROVER_CONTAINER_MISMATCH")
            self.phase = "inspect"
            info = json.loads(self.call("inspect", container))[0]
            mounts = info["Mounts"]
            if (info["Id"] != container or info["Image"] != self.image_id
                    or info["Name"] != "/" + self.name or info["HostConfig"]["NetworkMode"] != "none"
                    or len(mounts) != 1 or mounts[0]["Type"] != "bind"
                    or mounts[0]["Source"] != str(self.docker_work) or mounts[0]["Destination"] != "/mnt"):
                raise ValueError("ZK_LOCAL_PROVER_CONTAINER_MISMATCH")
            self.phase = "start"
            self.start_container(container)
            self.phase = "output"
            fd = os.open(self.docker_work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                proof = read_file(fd, "proof.json", PROOF_LIMIT)
            finally:
                os.close(fd)
            fd = os.open("proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.work_fd)
            with os.fdopen(fd, "wb") as output:
                output.write(proof)
        finally:
            try:
                self.remove_container()
            except (OSError, ValueError, subprocess.SubprocessError):
                self.phase = "cleanup"
                raise
        self.observed.update(image=self.image_id, network_mode="none")

    def remove_container(self):
        result = subprocess.run(self.docker + ["rm", "--force", self.name], env=self.docker_env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        if result.returncode and b"No such container" not in result.stderr:
            raise ValueError("ZK_GROTH16_DOCKER_COMMAND")

    def start_container(self, container):
        with subprocess.Popen(self.docker + ["start", "--attach", container], env=self.docker_env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            deadline = monotonic() + self.timeout
            while process.poll() is None:
                if self.stop.wait(0.2) or monotonic() >= deadline:
                    process.kill()
                    break
            self.container_exit_code = process.wait()
            if self.container_exit_code != 0:
                raise ValueError("ZK_GROTH16_DOCKER_COMMAND")


def main():
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("Docker (local Groth16 broker)")
        return 0
    work = os.environ["RISC0_WORK_DIR"]
    if arguments != ["run", "--rm", "-v", work + ":/mnt", os.environ.get("SATOSHIFI_GROTH16_SDK_IMAGE", SDK_IMAGE)]:
        raise ValueError("Unexpected SDK Docker command")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(os.environ["SATOSHIFI_GROTH16_BROKER"])
        client.sendall(b"prove\n")
        with client.makefile("rb") as stream:
            if stream.readline(16) != b"ok\n":
                raise ValueError("ZK_GROTH16_BROKER_FAILED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
