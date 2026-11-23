"""Jira Cloud v3 issue creation, comments, and workflow transitions.

https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/
"""

import re
from urllib.parse import urlsplit

from ._util import json_response, next_seq, parse_params, parse_url
from ._write import WriteFake
from .base import Response

_ROUTE = re.compile(r"/rest/api/3/issue(?:/([^/]+)(?:/(comment|transitions)(?:/([^/]+))?)?)?")


def _error(status, message="", **fields):
    return json_response(status, {"errorMessages": [message] if message else [], "errors": fields})


class JiraFake(WriteFake):
    hosts = ("jira.example.com",)
    api_version = "Jira Cloud REST v3"
    once = ("jira.issue.create", "jira.comment.create")

    def __init__(self, *, hosts=None, scope=None, projects=None, transitions=None):
        super().__init__(hosts=hosts, scope=scope)
        self.projects = dict(projects) if projects is not None else None
        self.transitions = dict(transitions) if transitions is not None else {
            "11": "To Do", "21": "In Progress", "31": "Done",
        }

    def describe(self, req):
        route = _ROUTE.fullmatch(parse_url(req.url)[0])
        if not route or req.method.upper() != "POST":
            return None
        issue, action, item = route.groups()
        if item:
            return None
        params = parse_params(req)
        if not issue:
            fields = params.get("fields")
            fields = fields if isinstance(fields, dict) else {}
            return self.write(req, "jira.issue.create",
                              f"{fields.get('project', '')}:{fields.get('summary', '')}",
                              "project|summary")
        if action == "comment":
            return self.write(req, "jira.comment.create", f"{issue}:{params.get('body', '')}",
                              "issue_id|body")
        if action == "transitions":
            return self.write(req, "jira.issue.transition",
                              f"{issue}:{params.get('transition', '')}", "issue_id|transition")

    def _create(self, issues, state, params, base):
        fields = params.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        project = fields.get("project")
        project = project if isinstance(project, dict) else {}
        key = project.get("key")
        if key is None and self.projects is not None:
            key = next((name for name, ident in self.projects.items()
                        if ident == project.get("id")), None)
        if not isinstance(key, str) or not key or self.projects is not None and key not in self.projects:
            return _error(400, project="Specify a valid project ID or key")
        summary = fields.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return _error(400, summary="You must specify a summary of the issue.")
        issue_type = fields.get("issuetype")
        if not isinstance(issue_type, dict) or not (issue_type.get("id") or issue_type.get("name")):
            return _error(400, issuetype="Specify an issue type")
        number = next_seq(state.setdefault("project_numbers", {}), key)
        ident = str(10000 + next_seq(state, "issue_seq"))
        reference = {"id": ident, "key": f"{key}-{number}", "self": f"{base}/issue/{ident}"}
        first = next(iter(self.transitions), "11")
        issue = {**reference, "fields": {**fields, "status": {
            "id": first, "name": self.transitions.get(first, "To Do"),
        }}, "comments": {}}
        issues[ident] = issue
        return json_response(201, reference)

    def handle(self, req, state):
        route = _ROUTE.fullmatch(parse_url(req.url)[0])
        if route is None:
            return _error(404, "Not Found")
        ident, action, item = route.groups()
        method = req.method.upper()
        params = parse_params(req)
        url = urlsplit(req.url)
        base = f"{url.scheme}://{url.netloc}/rest/api/3"
        issues = state.setdefault("issues", {})
        if ident is None and method == "POST":
            return self._create(issues, state, params, base)
        issue = issues.get(ident)
        if issue is None:
            issue = next((record for record in issues.values() if record["key"] == ident), None)
        if issue is None:
            return _error(404, "Issue does not exist or you do not have permission to see it.")
        if action is None and method == "GET":
            return json_response(200, {key: value for key, value in issue.items()
                                       if key != "comments"})
        if action == "comment":
            comments = issue["comments"]
            if method == "GET":
                if item is not None:
                    return json_response(200, comments[item]) if item in comments else _error(
                        404, "Comment does not exist")
                return json_response(200, {"startAt": 0, "maxResults": len(comments),
                                           "total": len(comments),
                                           "comments": list(comments.values())})
            if method == "POST" and item is None:
                body = params.get("body")
                if not isinstance(body, dict) or body.get("type") != "doc" or body.get("version") != 1:
                    return _error(400, "Comment body must be in Atlassian Document Format")
                comment_id = str(10000 + next_seq(state, "comment_seq"))
                comment = {"id": comment_id, "body": body,
                           "self": f"{issue['self']}/comment/{comment_id}",
                           "created": "2024-01-01T00:00:00.000+0000",
                           "updated": "2024-01-01T00:00:00.000+0000"}
                comments[comment_id] = comment
                return json_response(201, comment)
        if action == "transitions" and item is None:
            if method == "GET":
                return json_response(200, {"transitions": [
                    {"id": code, "name": name, "to": {"id": code, "name": name}}
                    for code, name in self.transitions.items()
                ]})
            if method == "POST":
                transition = params.get("transition")
                code = transition.get("id") if isinstance(transition, dict) else None
                if (not isinstance(code, (str, int)) or str(code) not in self.transitions):
                    return _error(400, "Transition id is not valid for this issue.")
                issue["fields"]["status"] = {"id": str(code), "name": self.transitions[str(code)]}
                return Response(204, {}, b"")
        return _error(404, "Not Found")
