# SPDX-License-Identifier: Apache-2.0
"""satoshifi-watch: scheduled silence check (spec 7, T-04)."""

import logging
import time

from botocore.exceptions import ClientError

from satoshifi.common import notify, settings, store

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    now = int(time.time())
    silence = settings.runtime()["silence_s"]
    checked = alerted = 0
    for dep in store.scan_all(store.deployments()):
        if dep.get("status") != "ACTIVE":
            continue
        dep_id = dep["deployment_id"]
        chat = dep.get("participants_chat_id")
        chat = int(chat) if chat is not None else settings.operator_chat_id()
        rows = store.query_all(store.reports(), KeyConditionExpression="deployment_id = :d",
                               ExpressionAttributeValues={":d": dep_id})
        for row in rows:
            checked += 1
            received = int(row["received_at"])
            if row.get("silence_alerted") or now - received <= silence:
                continue
            try:
                store.reports().update_item(
                    Key={"deployment_id": dep_id, "work_address": row["work_address"]},
                    UpdateExpression="SET silence_alerted = :t",
                    ConditionExpression="received_at = :r AND silence_alerted = :f",
                    ExpressionAttributeValues={":t": True, ":f": False, ":r": received})
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    continue  # a new report arrived or another run already alerted
                raise
            message = notify.line(row.get("label", row["work_address"]), "МОЛЧИТ",
                                  "нет отчёта %d мин" % ((now - received) // 60),
                                  "проверить службу участника")
            try:
                notify.telegram(chat, message)
                alerted += 1
            except notify.NotifyError as exc:
                log.error("telegram delivery failed for %s: %s", dep_id, exc)
    log.info("watch checked=%d alerted=%d", checked, alerted)
    return {"checked": checked, "alerted": alerted}
