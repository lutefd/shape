"""Google Calendar API v3 event writes and reads.

https://developers.google.com/workspace/calendar/api/v3/reference/events
https://developers.google.com/workspace/calendar/api/guides/version-resources
"""

import re
from datetime import date, datetime
from urllib.parse import unquote

from ._google import google_error
from ._util import header, json_response, next_seq, parse_bool, parse_params, parse_url
from ._write import WriteFake
from .base import Response

_ROUTE = re.compile(r"/calendar/v3/calendars/([^/]+)/events(?:/([^/]+))?")
_EVENT_ID = re.compile(r"[0-9a-v]{5,1024}")


def _date_key(value):
    if isinstance(value, dict):
        try:
            if "date" in value and "dateTime" not in value:
                return ("date", date.fromisoformat(value["date"]))
            if "dateTime" in value and "date" not in value:
                stamp = datetime.fromisoformat(value["dateTime"])
                if stamp.tzinfo is None and not value.get("timeZone"):
                    return None
                return ("dateTime", stamp)
        except (ValueError, TypeError):
            pass
    return None


def _valid_interval(event):
    start, end = _date_key(event.get("start")), _date_key(event.get("end"))
    if not start or not end or start[0] != end[0]:
        return False
    try:
        return start[1] < end[1]
    except TypeError:
        return False


class GoogleCalendarFake(WriteFake):
    hosts = ("www.googleapis.com", "calendar.googleapis.com")
    api_version = "Google Calendar API v3"
    once = ("calendar.events.insert",)

    def __init__(self, *, hosts=None, scope=None, primary="primary"):
        super().__init__(hosts=hosts, scope=scope)
        self.primary = primary

    def _route(self, path):
        route = _ROUTE.fullmatch(path)
        if route:
            calendar, event_id = route.groups()
            calendar = unquote(calendar)
            return self.primary if calendar == "primary" else calendar, event_id

    def describe(self, req):
        if not (route := self._route(parse_url(req.url)[0])):
            return None
        calendar, event_id = route
        params = parse_params(req)
        method = req.method.upper()
        if method == "POST" and event_id is None:
            supplied = params.get("id")
            supplied = supplied if isinstance(supplied, str) else None
            scope = supplied or f"{params.get('summary', '')}|{params.get('start', '')}"
            return self.write(req, "calendar.events.insert", f"{calendar}:{scope}",
                              "calendar|event_id or summary|start", key=supplied)
        if event_id and method in ("PATCH", "DELETE"):
            return self.write(req, f"calendar.events.{method.lower()}",
                              f"{calendar}:{event_id}", "calendar|event_id")

    def handle(self, req, state):
        path, query = parse_url(req.url)
        if not (route := self._route(path)):
            return google_error(404, "notFound", "Not Found")
        calendar, event_id = route
        method = req.method.upper()
        events = state.setdefault("calendars", {}).setdefault(calendar, {})
        params = parse_params(req)
        if event_id is None and method == "GET":
            show_deleted = parse_bool(query.get("showDeleted", ["false"])[0])
            items = [event for event in events.values()
                     if show_deleted or event["status"] != "cancelled"]
            return json_response(200, {"kind": "calendar#events", "items": items})
        if event_id is None and method == "POST":
            supplied = params.get("id")
            if supplied is not None and (not isinstance(supplied, str)
                                         or not _EVENT_ID.fullmatch(supplied)):
                return google_error(400, "invalid", "Invalid resource id value.")
            if supplied in events:
                return google_error(409, "duplicate", "The requested identifier already exists.")
            if not _valid_interval(params):
                return google_error(400, "required", "Missing or invalid start or end time.")
            seq = next_seq(state)
            if not supplied:
                while f"shape{seq:012d}" in events:
                    seq = next_seq(state)
                supplied = f"shape{seq:012d}"
            event = {**params, "kind": "calendar#event", "id": supplied,
                     "status": params.get("status", "confirmed"), "etag": f'"{seq}"',
                     "iCalUID": params.get("iCalUID", f"{supplied}@google.com"),
                     "sequence": params.get("sequence", 0)}
            events[supplied] = event
            return json_response(200, event, {"ETag": event["etag"]})
        event = events.get(event_id)
        if event is None:
            return google_error(404, "notFound", "Not Found")
        if method == "GET":
            return json_response(200, event, {"ETag": event["etag"]})
        if method not in ("PATCH", "DELETE"):
            return google_error(404, "notFound", "Not Found")
        match = header(req.headers, "If-Match")
        if match and match not in ("*", event["etag"]):
            return google_error(412, "conditionNotMet", "Precondition Failed")
        if event["status"] == "cancelled":
            return google_error(410, "deleted", "Resource has been deleted")
        if method == "DELETE":
            events[event_id] = {"id": event_id, "kind": "calendar#event", "status": "cancelled",
                                "etag": f'"{next_seq(state)}"'}
            return Response(204, {}, b"")
        updated = dict(event)
        for key, value in params.items():
            if key in ("id", "etag", "kind", "iCalUID"):
                continue
            if value is None:
                updated.pop(key, None)
            elif isinstance(value, dict) and isinstance(updated.get(key), dict):
                updated[key] = {**updated[key], **value}
            else:
                updated[key] = value
        if not _valid_interval(updated):
            return google_error(400, "invalid", "Invalid start or end time.")
        updated["etag"] = f'"{next_seq(state)}"'
        updated.setdefault("status", "confirmed")
        events[event_id] = updated
        return json_response(200, updated, {"ETag": updated["etag"]})
