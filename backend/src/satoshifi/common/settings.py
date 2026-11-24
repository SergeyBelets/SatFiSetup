# SPDX-License-Identifier: Apache-2.0
"""Configuration: deploy-time values from the environment, runtime values and
secrets from SSM. Missing or malformed values raise; there are no defaults."""

import json
import os
from functools import lru_cache

import boto3

from .messages import require_checksum

RUNTIME_KEYS = {
    "operator_address": str,
    "chain_id": int,
    "report_period_s": int,
    "silence_s": int,
    "repeat_s": int,
    "clock_skew_s": int,
    "session_ttl_s": int,
    "max_participants": int,
}


class ConfigError(RuntimeError):
    pass


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError("environment variable %s is required" % name)
    return value


@lru_cache(maxsize=None)
def _ssm():
    return boto3.client("ssm")


@lru_cache(maxsize=None)
def ssm_value(name: str) -> str:
    return _ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


@lru_cache(maxsize=None)
def runtime() -> dict:
    raw = json.loads(ssm_value(env("SSM_CONFIG")))
    if not isinstance(raw, dict):
        raise ConfigError("runtime config must be a JSON object")
    missing = set(RUNTIME_KEYS) - set(raw)
    unknown = set(raw) - set(RUNTIME_KEYS)
    if missing or unknown:
        raise ConfigError("runtime config keys: missing %s, unknown %s" % (sorted(missing), sorted(unknown)))
    for key, kind in RUNTIME_KEYS.items():
        if not isinstance(raw[key], kind) or isinstance(raw[key], bool):
            raise ConfigError("runtime config %s must be %s" % (key, kind.__name__))
    require_checksum(raw["operator_address"], "operator_address")
    return raw


def session_secret() -> bytes:
    return ssm_value(env("SSM_SESSION_SECRET")).encode("utf-8")


def site_origin() -> str:
    origin = env("SITE_ORIGIN")
    if not origin.startswith("https://") or origin.count("/") != 2:
        raise ConfigError("SITE_ORIGIN must be https://<host>")
    return origin


def site_domain() -> str:
    return site_origin()[len("https://"):]


def telegram_token() -> str:
    return ssm_value(env("SSM_TELEGRAM_PREFIX") + "/bot_token")


def telegram_webhook_secret() -> str:
    return ssm_value(env("SSM_TELEGRAM_PREFIX") + "/webhook_secret")


@lru_cache(maxsize=None)
def operator_chat_id() -> int:
    chats = json.loads(ssm_value(env("SSM_TELEGRAM_PREFIX") + "/chats"))
    key = env("OPERATOR_CHAT_KEY")
    if key not in chats or not isinstance(chats[key], int):
        raise ConfigError("chat %s is missing in telegram chats" % key)
    return chats[key]
