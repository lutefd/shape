"""Subset of Linear's GraphQL issue and comment API.

https://linear.app/developers/graphql
https://linear.app/developers/sdk-errors
"""

import json
import re
from uuid import UUID

from ._util import json_response, next_seq, parse_params, parse_url, response_json
from ._write import WriteFake

_TOKEN = re.compile(r'#[^\n]*|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|[_A-Za-z]\w*|[^\s,]')
_FIELDS = {"issueCreate", "commentCreate", "issue", "comment"}


def _selection(tokens):
    fields = []
    while tokens and tokens[-1] != "}":
        name = tokens.pop()
        alias = name
        if tokens and tokens[-1] == ":":
            tokens.pop()
            name = tokens.pop()
        args = []
        if tokens and tokens[-1] == "(":
            tokens.pop()
            while tokens[-1] != ")":
                args.append(tokens.pop())
            tokens.pop()
        children = []
        if tokens and tokens[-1] == "{":
            tokens.pop()
            children = _selection(tokens)
        if not re.fullmatch(r"[_A-Za-z]\w*", name):
            raise ValueError("invalid selection")
        fields.append((alias, name, args, children))
    if not tokens or not fields:
        raise ValueError("missing selection")
    tokens.pop()
    return fields


def _value(tokens, variables):
    token = tokens.pop()
    if token == "{":
        fields = {}
        while tokens[-1] != "}":
            key = tokens.pop()
            if tokens.pop() != ":":
                raise ValueError("invalid input")
            fields[key] = _value(tokens, variables)
        tokens.pop()
        return fields
    if token == "[":
        items = []
        while tokens[-1] != "]":
            items.append(_value(tokens, variables))
        tokens.pop()
        return items
    if token == "$":
        return variables.get(tokens.pop())
    if token.startswith('"') or token[0].isdigit() or token in ("true", "false", "null"):
        return json.loads(token)
    return token


def _operation(req):
    params = parse_params(req)
    query = params.get("query")
    variables = params.get("variables") or {}
    if not isinstance(query, str) or not isinstance(variables, dict):
        return None
    tokens = [token for token in _TOKEN.findall(query) if not token.startswith("#")][::-1]
    try:
        while tokens.pop() != "{":
            pass
        [(alias, name, args, children)] = _selection(tokens)
        arg, colon, *args = args
        if colon != ":" or name not in _FIELDS or arg not in ("input", "id"):
            return None
        return alias, name, _value(args[::-1], variables), children
    except (IndexError, ValueError):
        return None


def _project(record, selection):
    return {
        alias: _project(record[name], children) if children and isinstance(record.get(name), dict)
        else record.get(name)
        for alias, name, _, children in selection
    }


def _error(message, code="BAD_USER_INPUT", status=400, field=None):
    error = {"message": message, "extensions": {"code": code, "type": "invalid input"}}
    if field:
        error["path"] = [field]
    return json_response(status, {"errors": [error], **({"data": None} if field else {})})


class LinearFake(WriteFake):
    hosts = ("api.linear.app",)
    api_version = "Linear GraphQL (unversioned)"
    once = ("linear.issueCreate", "linear.commentCreate")

    def __init__(self, *, hosts=None, scope=None, teams=None):
        super().__init__(hosts=hosts, scope=scope)
        self.teams = None if teams is None else dict(teams)

    def describe(self, req):
        if req.method.upper() != "POST" or parse_url(req.url)[0] != "/graphql":
            return None
        operation = _operation(req)
        if not operation:
            return None
        _, name, value, _ = operation
        if not isinstance(value, dict):
            return None
        if name == "issueCreate":
            return self.write(req, "linear.issueCreate",
                              f"{value.get('teamId', '')}:{value.get('title', '')}",
                              "team_id|title", params=value)
        if name == "commentCreate":
            return self.write(req, "linear.commentCreate",
                              f"{value.get('issueId', '')}:{value.get('body', '')}",
                              "issue_id|body", params=value)

    def handle(self, req, state):
        if req.method.upper() != "POST" or parse_url(req.url)[0] != "/graphql":
            return _error("Not Found", "NOT_FOUND", 404)
        operation = _operation(req)
        if operation is None:
            return _error("Unsupported or invalid GraphQL operation", "GRAPHQL_VALIDATION_FAILED")
        alias, name, value, selection = operation
        issues = state.setdefault("issues", {})
        comments = state.setdefault("comments", {})
        if name in ("issue", "comment"):
            if not isinstance(value, str):
                return _error("Expected an ID", field=alias)
            store = issues if name == "issue" else comments
            record = store.get(value)
            if record is None and name == "issue":
                record = next((i for i in issues.values() if i["identifier"] == value), None)
            if record is None:
                return _error("Entity not found", "ENTITY_NOT_FOUND", 200, alias)
            return json_response(200, {"data": {alias: _project(record, selection)}})
        if not isinstance(value, dict):
            return _error("Expected input object", field=alias)
        if name == "issueCreate":
            title, team = value.get("title"), value.get("teamId")
            if not isinstance(title, str) or not title.strip() or not isinstance(team, str):
                return _error("title and teamId are required", field=alias)
            if self.teams is not None and team not in self.teams:
                return _error("Team not found", "ENTITY_NOT_FOUND", 200, alias)
            key = self.teams[team] if self.teams is not None else "SHAPE"
            number = next_seq(state.setdefault("issue_numbers", {}), key)
            ident = str(UUID(int=next_seq(state)))
            issue = {"id": ident, "identifier": f"{key}-{number}", "title": title,
                     "description": value.get("description"), "number": number,
                     "url": f"https://linear.app/shape/issue/{key}-{number}",
                     "team": {"id": team, "key": key}, "state": {"id": value.get("stateId")}}
            issues[ident] = issue
            result = {"success": True, "issue": issue}
        else:
            ident, body = value.get("issueId"), value.get("body")
            if not isinstance(ident, str) or not isinstance(body, str) or not body.strip():
                return _error("issueId and body are required", field=alias)
            issue = issues.get(ident)
            if issue is None:
                return _error("Issue not found", "ENTITY_NOT_FOUND", 200, alias)
            ident = str(UUID(int=next_seq(state)))
            comment = {"id": ident, "body": body, "issue": issue,
                       "url": f"{issue['url']}#comment-{ident}"}
            comments[ident] = comment
            result = {"success": True, "comment": comment}
        return json_response(200, {"data": {alias: _project(result, selection)}})

    def effect(self, req, resp):
        if "errors" in (response_json(resp) or {}):
            return super().effect(req, _error("Mutation rejected"))
        return super().effect(req, resp)
