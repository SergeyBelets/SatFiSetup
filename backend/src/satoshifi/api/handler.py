# SPDX-License-Identifier: Apache-2.0
"""satoshifi-api: single Lambda behind the HTTP API (spec section 5)."""

import logging

from satoshifi.api import monitoring
from satoshifi.common.http import ApiError, error

log = logging.getLogger()
log.setLevel(logging.INFO)

# routeKey -> callable(event) -> response. Filled in layer by layer.
ROUTES = {}
ROUTES.update(monitoring.ROUTES)


def handler(event, context):
    route = event.get("routeKey", "")
    request_id = getattr(context, "aws_request_id", "")
    func = ROUTES.get(route)
    if func is None:
        log.info("route not implemented: %s", route)
        return error("NOT_IMPLEMENTED", route)
    try:
        return func(event)
    except ApiError as exc:
        log.info("api error %s on %s: %s", exc.code, route, exc.message)
        return error(exc.code, exc.message)
    except Exception:
        log.exception("internal error on %s request_id=%s", route, request_id)
        return error("INTERNAL", request_id=request_id)
