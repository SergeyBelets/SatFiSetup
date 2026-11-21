# SPDX-License-Identifier: Apache-2.0
"""Outgoing notifications: Telegram Bot API over HTTPS and Amazon SES."""

import json
import logging
import urllib.request
from functools import lru_cache

import boto3

from . import settings

log = logging.getLogger()

TELEGRAM_LIMIT = 4000


class NotifyError(RuntimeError):
    pass


def telegram(chat_id: int, text: str) -> None:
    payload = json.dumps({"chat_id": chat_id, "text": text[:TELEGRAM_LIMIT],
                          "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % settings.telegram_token(),
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        # The URL contains the token: report only the exception type.
        raise NotifyError("telegram request failed: %s" % type(exc).__name__) from None
    if not result.get("ok"):
        raise NotifyError("telegram rejected message: %s" % result.get("description"))


@lru_cache(maxsize=None)
def _ses():
    return boto3.client("sesv2")


def operator_email(subject: str, body: str) -> None:
    _ses().send_email(
        FromEmailAddress=settings.env("SES_SENDER"),
        Destination={"ToAddresses": [settings.env("OPERATOR_EMAIL")]},
        Content={"Simple": {"Subject": {"Data": subject[:200], "Charset": "UTF-8"},
                            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}},
    )


def line(label: str, verdict: str, reason: str, action: str) -> str:
    """T-06: one line — participant label, verdict, reason, action."""
    parts = [label, verdict]
    if reason:
        parts.append(reason)
    if action:
        parts.append("→ " + action)
    return " · ".join(parts)
