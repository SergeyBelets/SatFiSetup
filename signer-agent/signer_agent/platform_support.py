"""Capabilities of this release; OS support is distinct from prover qualification."""
import platform
import sys


def capabilities(system=None, release=None, machine=None):
    system = system or platform.system()
    release = release or platform.release()
    machine = machine or platform.machine()
    posix = system in ("Linux", "Darwin")
    wsl = system == "Linux" and "microsoft" in release.lower()
    prover=(system=='Darwin' and machine.lower() in ('arm64','aarch64')) or (system=='Linux' and machine.lower() in ('x86_64','amd64'))
    return dict(system=system, machine=machine, wsl=wsl,
        signer=posix, relay=posix, bootstrap=posix,
        qualified_local_prover=system == "Darwin" and machine.lower() in ("arm64", "aarch64"),
        local_prover_supported=prover,
        native_windows=False,
        reason=None if posix else "PLATFORM_USE_WSL2: run the Linux build inside WSL2 Ubuntu; native Windows locks/permissions are unsupported")


def entrypoint():
    import json
    c = capabilities()
    if not c["signer"]:
        print(json.dumps(dict(status="ERROR", code="PLATFORM_USE_WSL2", details=c)))
        return 2
    from .cli import main
    return main()


def require_participant_prover(invite):
    """New ZK participants must be able to compute, not borrow a peer's proof."""
    from .zk_spv import is_zk
    if is_zk(invite.get('bitcoin_policy',{})) and not capabilities()['local_prover_supported']:
        raise ValueError('ZK_PARTICIPANT_LOCAL_PROVER_REQUIRED')
