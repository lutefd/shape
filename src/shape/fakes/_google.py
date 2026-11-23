"""Google JSON API error responses."""

from ._util import json_response


def google_error(code, reason, message):
    status = {
        400: "INVALID_ARGUMENT", 404: "NOT_FOUND", 409: "ALREADY_EXISTS",
        410: "GONE", 412: "FAILED_PRECONDITION",
    }
    return json_response(code, {"error": {
        "code": code, "message": message, "status": status[code],
        "errors": [{"domain": "global", "reason": reason, "message": message}],
    }})
