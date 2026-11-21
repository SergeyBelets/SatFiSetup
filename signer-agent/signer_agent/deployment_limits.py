"""EIP-170 runtime and EIP-3860 complete initcode gates (bytes, not hex digits)."""

MAX_RUNTIME = 24_576
MAX_INITCODE = 49_152


def check_portable_metadata(metadata):
    """Reject host paths that change Solidity's metadata hash between clones."""
    import re

    settings = metadata.get("settings", {})
    paths = list(metadata.get("sources", {})) + list(
        settings.get("compilationTarget", {})
    )
    for mapping in settings.get("remappings", []):
        paths.extend(mapping.split("="))
    # Remapping context may precede the import prefix, e.g. /checkout/lib/:forge/.
    if any(re.search(r"(^|:)(/|[A-Za-z]:[\\/])", path) for path in paths):
        raise ValueError("NONPORTABLE_BUILD_METADATA: rebuild with relative remappings")


def byte_length(value):
    if isinstance(value, str):
        try:
            return len(bytes.fromhex(value.removeprefix("0x")))
        except ValueError:
            raise ValueError("UNLINKED_OR_INVALID_BYTECODE") from None
    return len(value)


def check_deployment(runtime, initcode):
    runtime_size, initcode_size = byte_length(runtime), byte_length(initcode)
    if runtime_size > MAX_RUNTIME:
        raise ValueError(f"EIP170_RUNTIME_TOO_LARGE: {runtime_size} > {MAX_RUNTIME}")
    if initcode_size > MAX_INITCODE:
        raise ValueError(
            f"EIP3860_INITCODE_TOO_LARGE: {initcode_size} > {MAX_INITCODE}"
        )
    return dict(
        runtime_bytes=runtime_size,
        initcode_bytes=initcode_size,
        runtime_margin=MAX_RUNTIME - runtime_size,
        initcode_margin=MAX_INITCODE - initcode_size,
    )
