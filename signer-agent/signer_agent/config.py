import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv




def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    rpc_url: str = field(repr=False)
    sender: str
    private_key: str = field(repr=False)
    coordinator_address: str
    coordinator_abi_path: str
    signing_share_hex: str = field(repr=False)
    state_key: str = field(repr=False)
    state_path: str
    deployment_block: int
    scan_chunk_size: int
    confirmations: int
    poll_interval: int
    receipt_timeout: int
    gas_limit: int
    max_fee_gwei: Optional[float]
    priority_fee_gwei: float
    auto_sign: bool
    auto_finalize: bool
    nonce_ledger: Optional[str] = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        project_dir = Path(__file__).resolve().parents[1]
        signing_share = os.getenv("FROST_SIGNING_SHARE_HEX", "")
        if not signing_share:
            legacy = os.getenv("FROST_SECRET_SHARE", "")
            if len(legacy.removeprefix("0x")) == 64:
                signing_share = legacy

        max_fee = os.getenv("MAX_FEE_GWEI")
        return cls(
            rpc_url=os.getenv("EVM_RPC_URL", ""),
            sender=os.getenv("EVM_SENDER", ""),
            private_key=os.getenv("EVM_PRIVATE_KEY", ""),
            coordinator_address=os.getenv("FROST_COORDINATOR_ADDRESS", ""),
            coordinator_abi_path=os.getenv(
                "FROST_ABI_PATH",
                str(project_dir / "contracts" / "FROSTCoordinator.json"),
            ),
            signing_share_hex=signing_share,
            state_key=os.getenv("SIGNER_STATE_KEY", ""),
            state_path=os.getenv("SIGNER_STATE_PATH", str(project_dir / ".state" / "signer-state.enc")),
            deployment_block=int(os.getenv("FROST_DEPLOYMENT_BLOCK", "-1")),
            scan_chunk_size=int(os.getenv("FROST_SCAN_CHUNK_SIZE", "5000")),
            confirmations=int(os.getenv("EVM_CONFIRMATIONS", "1")),
            poll_interval=int(os.getenv("SIGNER_POLL_INTERVAL", "30")),
            receipt_timeout=int(os.getenv("EVM_RECEIPT_TIMEOUT", "180")),
            gas_limit=int(os.getenv("GAS_LIMIT", "0")),
            max_fee_gwei=float(max_fee) if max_fee else None,
            priority_fee_gwei=float(os.getenv("PRIORITY_FEE_GWEI", "2")),
            auto_sign=_env_bool("AUTO_SIGN"),
            auto_finalize=_env_bool("AUTO_FINALIZE"),
            nonce_ledger=os.getenv("SIGNER_NONCE_LEDGER") or None,
        )

    def validate(self, require_signing: bool = True) -> None:
        missing = []
        for name, value in (
            ("EVM_RPC_URL", self.rpc_url),
            ("EVM_SENDER", self.sender),
            ("EVM_PRIVATE_KEY", self.private_key),
            ("FROST_COORDINATOR_ADDRESS", self.coordinator_address),
            ("FROST_ABI_PATH", self.coordinator_abi_path),
        ):
            if not value:
                missing.append(name)
        if require_signing:
            if not self.signing_share_hex:
                missing.append("FROST_SIGNING_SHARE_HEX")
            if not self.state_key:
                missing.append("SIGNER_STATE_KEY")
            elif len(self.state_key.removeprefix("0x")) < 64:
                missing.append("SIGNER_STATE_KEY (must contain at least 32 bytes)")
        if self.deployment_block < 0:
            missing.append("FROST_DEPLOYMENT_BLOCK")
        if self.coordinator_abi_path and not Path(self.coordinator_abi_path).expanduser().is_file():
            missing.append("FROST_ABI_PATH (file not found)")
        if missing:
            raise ValueError("missing configuration: %s" % ", ".join(missing))
        if self.scan_chunk_size <= 0:
            raise ValueError("FROST_SCAN_CHUNK_SIZE must be positive")
        if self.confirmations <= 0:
            raise ValueError("EVM_CONFIRMATIONS must be positive")
