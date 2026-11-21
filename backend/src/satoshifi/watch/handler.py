# SPDX-License-Identifier: Apache-2.0
"""satoshifi-watch: scheduled checks of silence and deadlines (spec section 7)."""

import logging

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    # Checks are added in the watch layer; until then each run only records that it ran.
    log.info("watch tick")
    return {"checked": 0}
