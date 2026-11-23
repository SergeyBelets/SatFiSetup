"""Build/check a fresh POSIX participant workstation (Linux, WSL2 or macOS).

Uses stdlib before dependencies exist. Run with the intended venv Python.
No node, private key, invitation or transaction is created by build/check.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'signer-agent'))
from signer_agent.platform_support import capabilities


def version(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0] if result.returncode == 0 and lines else None


def check(role="signer", state_dir=None):
    c = capabilities()
    missing = []
    if not c["signer"]: missing.append(c["reason"])
    if sys.version_info < (3,11): missing.append("Python >=3.11 required")
    tools = {}
    for name in ("git", "cargo", "rustup", "forge"):
        path = shutil.which(name)
        tools[name] = dict(path=path, version=version([path, "--version"]) if path else None)
        if not path: missing.append("tool:"+name)
        elif not tools[name]['version']: missing.append('tool-version:'+name)
    if tools["forge"]["version"] and "1.8.5" not in tools["forge"]["version"]:
        missing.append("Foundry v1.8.5 required")
    if tools['rustup']['path']:
        pinned = version([tools['rustup']['path'], 'run', '1.89.0', 'rustc', '--version'])
        tools['rustc-1.89.0'] = dict(version=pinned)
        if not pinned or not pinned.startswith('rustc 1.89.0 '): missing.append('toolchain:Rust 1.89.0 required')
    for name in ("web3", "coincurve", "cryptography"):
        if importlib.util.find_spec(name) is None: missing.append("python:"+name)
    binary = Path(os.environ.get('SATOHSI_CRYPTO_BIN', ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    native_frost_sha256 = None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        missing.append("build:native FROST")
    else:
        from signer_agent.crypto import checked_binary
        try:
            with checked_binary(required=True) as (_, _, native_frost_sha256):
                pass
        except (OSError, ValueError) as error:
            from signer_agent.participant_errors import participant_exception_code
            missing.append('build:native-frost-pin:' + participant_exception_code(error))
    for name in ('DKGCoordinator', 'ULRStaking', 'ULRCustodianSnapshots', 'ULRConfederationTreasury',
                 'ULRConfederationVoting', 'BitcoinKeyFamilyRegistry', 'BitcoinAddressRegistry'):
        if not (ROOT/'web3/contracts/out'/f'{name}.sol'/f'{name}.json').is_file():
            missing.append('build:artifact:'+name)
    prover=None
    if role == "prover":
        if not c['local_prover_supported']:missing.append('prover:requires macOS arm64 or Linux/WSL2 x86-64')
        else:
            from signer_agent.zk_prover_platform import check as check_prover
            try:prover=check_prover()
            except (ValueError,OSError,subprocess.SubprocessError) as error:
                from signer_agent.participant_errors import participant_exception_code
                missing.append('prover:'+participant_exception_code(error))
    if state_dir:
        original = Path(state_dir).expanduser()
        p = original.resolve()
        if str(p).startswith('/mnt/') and c['wsl']: missing.append("state:use WSL Linux filesystem, not /mnt/<drive>")
        if original.is_symlink() or (p.exists() and (not p.is_dir() or p.stat().st_mode & 0o077 or (hasattr(os,'getuid') and p.stat().st_uid!=os.getuid()))):
            missing.append("state:owned directory mode 0700 required")
    sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for base,pattern in [('signer-agent/signer_agent','*.py'),('frost-crypto/src','*.rs'),('web3/contracts/src','*.sol')]
        for p in sorted((ROOT/base).rglob(pattern))}
    for name in ('frost-crypto/Cargo.lock','frost-crypto/Cargo.toml','web3/foundry.toml',
                 'rust/zkspv/state-adapter/foundry.toml','signer-agent/requirements.lock','signer-agent/pyproject.toml'):
        sources[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    source_hash=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(version="1", capabilities=c, role=role, status="BLOCKED" if missing else "READY_TO_CONFIGURE",
        missing=missing, tools=tools, native_frost=str(binary),
        native_frost_sha256=native_frost_sha256,
        protocol_source_sha256=source_hash,
        prover=prover,
        qualification=False, peers="Every participant builds the same reviewed commit; endpoints and private state stay local")


def build():
    if not capabilities()['signer']: raise ValueError('PLATFORM_USE_WSL2')
    if sys.prefix == sys.base_prefix: raise ValueError('VENV_REQUIRED')
    if sys.version_info < (3,11): raise ValueError('PYTHON_3_11_REQUIRED')
    for name in ('cargo','rustup','forge'):
        if not shutil.which(name): raise ValueError('MISSING_TOOL:'+name)
    if '1.8.5' not in (version(['forge','--version']) or ''): raise ValueError('FOUNDRY_1_8_5_REQUIRED')
    commands = [
        [sys.executable, '-m', 'pip', 'install', '--force-reinstall', '--require-hashes',
         '--only-binary', ':all:', '-r', 'signer-agent/requirements.lock'],
        [sys.executable,'-m','pip','install','--no-deps','--no-build-isolation','-e','./signer-agent'],
        ['forge','build','--root','web3'],
        ['forge','build','--root','rust/zkspv/state-adapter'],
        [sys.executable,'ops/generate_abis.py','--check','--zk'],
        [sys.executable,'ops/generate_fee_runtime_hashes.py','--check'],
        [sys.executable,'ops/check_bitcoin_custody_spv_pins.py'],
        [sys.executable,'ops/check_contract_sizes.py','--check'],
    ]
    for command in commands: subprocess.run(command, cwd=ROOT, check=True)
    from signer_agent.crypto import DEFAULT_BINARY, pin_local_build
    DEFAULT_BINARY.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".frost-build-", dir=DEFAULT_BINARY.parent.parent) as target:
        subprocess.run(['cargo', '+1.89.0', 'build', '--locked', '--manifest-path', 'frost-crypto/Cargo.toml',
                        '--target-dir', target], cwd=ROOT, check=True)
        os.replace(Path(target) / 'debug' / DEFAULT_BINARY.name, DEFAULT_BINARY)
        pin_local_build()


def identity(path, expected=None, generate=False):
    if not capabilities()['signer']: raise ValueError('PLATFORM_USE_WSL2')
    import getpass
    from eth_account import Account
    from web3 import Web3
    from signer_agent.dkg import atomic_private
    if not generate and not Web3.is_checksum_address(expected): raise ValueError('CHECKSUM_ADDRESS_REQUIRED')
    if generate and expected is not None:raise ValueError('GENERATE_ADDRESS_CONFLICT')
    p=Path(path).expanduser()
    if p.exists(): raise ValueError('OUTPUT_EXISTS')
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.parent.is_symlink() or p.parent.stat().st_mode & 0o077 or p.parent.stat().st_uid!=os.getuid(): raise ValueError('PRIVATE_DIRECTORY_REQUIRED')
    a=Account.create() if generate else Account.from_key(getpass.getpass('Your EVM private key (hidden): ').strip())
    if expected is not None and a.address!=expected: raise ValueError('IDENTITY_ADDRESS_MISMATCH')
    atomic_private(p, json.dumps(dict(private_key=a.key.hex())).encode())
    return dict(status='IDENTITY_SAVED',address=a.address,path=str(p.resolve()))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('check','build','identity'))
    p.add_argument('--role',choices=('signer','operator','prover'),default='signer')
    p.add_argument('--state-dir',type=Path)
    p.add_argument('--identity-file',type=Path)
    p.add_argument('--address')
    p.add_argument('--generate',action='store_true',help='Create a new local EVM key; output only its public address')
    a=p.parse_args(argv)
    try:
        if a.action=='identity':
            if not a.identity_file or not (a.address or a.generate):p.error('identity requires --identity-file and --address or --generate')
            report=identity(a.identity_file,a.address,a.generate)
        else:
            if a.action=='build':build()
            report=check(a.role,a.state_dir)
        print(json.dumps(report,indent=2));return int(report.get('status')=='BLOCKED')
    except (ValueError,OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        # Do not print private key parse errors or remote installation responses.
        print(json.dumps(dict(status='ERROR',code=type(e).__name__,action=a.action)));return 2


if __name__=='__main__':raise SystemExit(main())
