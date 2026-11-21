"""Build the receipt tools locally with the pinned RISC Zero release."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from signer_agent.zk_prover_platform import (
    RELEASE_CRATE, RELEASE_TOOLS, file_sha256, release_profile, release_sources,
)


def prepare(archive, output, *, offline=False):
    profile = release_profile()
    runtime = profile['runtime']
    if file_sha256(archive) != runtime['archive_sha256']:
        raise ValueError('ZK_RISC0_RELEASE_ARCHIVE_DRIFT')
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('ZK_RISC0_RELEASE_NEW_DIRECTORY_REQUIRED')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sources = release_sources()
    with tempfile.TemporaryDirectory(prefix='.risc0-release-', dir=output.parent) as folder:
        base = Path(folder)
        tools = base / 'tools'
        tools.mkdir(mode=0o700)
        with tarfile.open(archive, 'r:gz') as package:
            member = package.getmember('r0vm')
            if not member.isfile() or not 0 < member.size <= 128 * 1024 * 1024:
                raise ValueError('ZK_RISC0_RELEASE_ARCHIVE_CONTENTS')
            with package.extractfile(member) as source, (tools / 'r0vm').open('xb') as destination:
                shutil.copyfileobj(source, destination)
        if file_sha256(tools / 'r0vm') != runtime['r0vm_sha256']:
            raise ValueError('ZK_RISC0_RELEASE_RUNTIME_DRIFT')
        (tools / 'r0vm').chmod(0o700)
        target = base / 'target'
        command = ['rustup', 'run', profile['rust'], 'cargo', 'build', '--locked',
                   '--manifest-path', str(RELEASE_CRATE / 'Cargo.toml'), '--target-dir', str(target)]
        if offline:
            command.append('--offline')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('RUST', 'CARGO_BUILD_', 'CARGO_ENCODED_'))}
        if 'RUSTUP_HOME' in os.environ:
            env['RUSTUP_HOME'] = os.environ['RUSTUP_HOME']
        subprocess.run(command, env=env, check=True)
        for name in RELEASE_TOOLS[1:]:
            shutil.copyfile(target / 'debug' / name, tools / name)
            (tools / name).chmod(0o700)
        if sources != release_sources():
            raise ValueError('ZK_RISC0_RELEASE_BUILD_DRIFT')
        build = dict(sdk=profile['sdk'], platform=profile['platform'], sources=sources,
                     tools={name: file_sha256(tools / name) for name in RELEASE_TOOLS})
        (tools / 'build.json').write_text(json.dumps(build, indent=2) + '\n')
        tools.rename(output)
    return dict(status='RISC0_RELEASE_TOOLS_BUILT', sdk=profile['sdk'], platform=profile['platform'],
                directory=str(output), container_image=profile['container_image'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.output, offline=args.offline), indent=2))


if __name__ == '__main__':
    main()
