"""GitHub Issues REST API test double (`api.github.com`).

Mirrors GitHub REST API version `2022-11-28`:
- Issues (`POST/GET /repos/{owner}/{repo}/issues`, `GET/PATCH /repos/{owner}/{repo}/issues/{number}`):
  https://docs.github.com/en/rest/issues/issues
- Issue comments (`POST/GET /repos/{owner}/{repo}/issues/{number}/comments`):
  https://docs.github.com/en/rest/issues/comments

Issue creation and comment creation are NOT idempotent in GitHub's REST API: retrying after a
lost acknowledgment creates a duplicate issue or duplicate comment.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from typing import Any

from ..boundary.invariants import Invariant, at_most_once
from ._util import json_response, next_seq, parse_params, parse_url
from .base import EffectInfo, Request, Response

_ISSUES_COL_RE = re.compile(r"^/repos/([^/]+/[^/]+)/issues$")
_ISSUE_ITEM_RE = re.compile(r"^/repos/([^/]+/[^/]+)/issues/(\d+)$")
_COMMENTS_COL_RE = re.compile(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/comments$")
_DOC_URL = "https://docs.github.com/rest/issues/issues"


class GitHubFake:
    """Faithful test double for GitHub issue create, comment, close, and read endpoints."""

    hosts: tuple[str, ...] = ("api.github.com",)
    api_version: str = "2022-11-28"

    def __init__(
        self,
        *,
        scope: Callable[[Request, dict[str, Any]], str] | None = None,
        repos: Collection[str] | None = None,
        hosts: tuple[str, ...] | None = None,
    ) -> None:
        self._scope_fn = scope
        self._repos = set(repos) if repos is not None else None
        if hosts is not None:
            self.hosts = tuple(hosts)

    def _resp(self, status: int, payload: Any) -> Response:
        return json_response(status, payload, headers={"X-GitHub-Api-Version": self.api_version})

    def _not_found(self) -> Response:
        return self._resp(404, {"message": "Not Found", "documentation_url": _DOC_URL})

    def _validation_failed(self, resource: str, field: str, code: str) -> Response:
        return self._resp(
            422,
            {
                "message": "Validation Failed",
                "errors": [{"resource": resource, "field": field, "code": code}],
                "documentation_url": _DOC_URL,
            },
        )

    def _repo_store(self, repo: str, state: dict) -> dict[str, Any] | None:
        if self._repos is not None and repo not in self._repos:
            return None
        repos = state.setdefault("repos", {})
        return repos.setdefault(repo, {"next_number": 1, "issues": {}, "comments": {}})

    def _classify(
        self, req: Request, params: dict[str, Any]
    ) -> tuple[str, str, str, bool, int] | None:
        method = req.method.upper()
        path, _ = parse_url(req.url)
        if method == "POST" and (m := _ISSUES_COL_RE.match(path)):
            repo = m.group(1)
            default_scope = f"{repo}:title:{params.get('title', '')}"
            return ("github.issue.create", default_scope, "repo:title", True, 201)
        if method == "POST" and (m := _COMMENTS_COL_RE.match(path)):
            repo, num = m.group(1), m.group(2)
            return ("github.issue.comment", f"{repo}#{num}", "repo#number", True, 201)
        if method == "PATCH" and (m := _ISSUE_ITEM_RE.match(path)):
            repo, num = m.group(1), m.group(2)
            op = "github.issue.close" if params.get("state") == "closed" else "github.issue.update"
            return (op, f"{repo}#{num}", "repo#number", False, 200)
        return None

    def describe(self, req: Request) -> EffectInfo | None:
        params = parse_params(req)
        info = self._classify(req, params)
        if info is None:
            return None
        op, default_scope, rule, irreversible, _ = info
        if self._scope_fn is not None:
            scope, scope_source, scope_rule = self._scope_fn(req, params), "explicit", None
        else:
            scope, scope_source, scope_rule = default_scope, "fake_default", rule
        return EffectInfo(op=op, scope=scope, key=None, irreversible=irreversible, landed=False,
                          scope_source=scope_source, scope_rule=scope_rule)

    def effect(self, req: Request, resp: Response) -> EffectInfo | None:
        params = parse_params(req)
        info = self._classify(req, params)
        if info is None:
            return None
        op, default_scope, rule, irreversible, ok_status = info
        if self._scope_fn is not None:
            scope, scope_source, scope_rule = self._scope_fn(req, params), "explicit", None
        else:
            scope, scope_source, scope_rule = default_scope, "fake_default", rule
        return EffectInfo(
            op=op,
            scope=scope,
            key=None,
            irreversible=irreversible,
            landed=resp.status == ok_status,
            scope_source=scope_source,
            scope_rule=scope_rule,
        )

    def handle(self, req: Request, state: dict) -> Response:
        method = req.method.upper()
        path, query = parse_url(req.url)
        params = parse_params(req)

        if m := _ISSUES_COL_RE.match(path):
            repo = m.group(1)
            store = self._repo_store(repo, state)
            if store is None:
                return self._not_found()
            if method == "POST":
                return self._create_issue(repo, store, params, state)
            if method == "GET":
                want = (query.get("state") or ["open"])[-1]
                items = [
                    i for i in store["issues"].values()
                    if want == "all" or i["state"] == want
                ]
                return self._resp(200, items)

        if m := _ISSUE_ITEM_RE.match(path):
            repo, num = m.group(1), m.group(2)
            store = self._repo_store(repo, state)
            if store is None or num not in store["issues"]:
                return self._not_found()
            if method == "GET":
                return self._resp(200, store["issues"][num])
            if method == "PATCH":
                return self._update_issue(store["issues"][num], params)

        if m := _COMMENTS_COL_RE.match(path):
            repo, num = m.group(1), m.group(2)
            store = self._repo_store(repo, state)
            if store is None or num not in store["issues"]:
                return self._not_found()
            if method == "POST":
                return self._create_comment(repo, num, store, params, state)
            if method == "GET":
                return self._resp(200, list(store["comments"].get(num, [])))

        return self._not_found()

    def _create_issue(
        self, repo: str, store: dict[str, Any], params: dict[str, Any], state: dict
    ) -> Response:
        title = params.get("title")
        if not isinstance(title, str) or not title.strip():
            return self._validation_failed("Issue", "title", "missing_field")
        num = int(store["next_number"])
        store["next_number"] = num + 1
        issue_id = next_seq(state, "_gh_issue_id")
        labels = [
            {"name": lbl} if isinstance(lbl, str) else dict(lbl)
            for lbl in (params.get("labels") or [])
        ]
        issue = {
            "id": issue_id,
            "number": num,
            "title": title,
            "body": params.get("body"),
            "state": "open",
            "state_reason": None,
            "labels": labels,
            "assignees": list(params.get("assignees") or []),
            "comments": 0,
            "url": f"https://api.github.com/repos/{repo}/issues/{num}",
            "html_url": f"https://github.com/{repo}/issues/{num}",
        }
        store["issues"][str(num)] = issue
        store["comments"].setdefault(str(num), [])
        return self._resp(201, issue)

    def _update_issue(self, issue: dict[str, Any], params: dict[str, Any]) -> Response:
        if "state" in params and params["state"] is not None:
            new_state = params["state"]
            if new_state not in ("open", "closed"):
                return self._validation_failed("Issue", "state", "invalid")
            issue["state"] = new_state
            if new_state == "closed":
                issue["state_reason"] = params.get("state_reason") or "completed"
            else:
                issue["state_reason"] = "reopened"
        if "title" in params and isinstance(params["title"], str):
            issue["title"] = params["title"]
        if "body" in params:
            issue["body"] = params["body"]
        if "labels" in params and isinstance(params["labels"], list):
            issue["labels"] = [
                {"name": lbl} if isinstance(lbl, str) else dict(lbl)
                for lbl in params["labels"]
            ]
        if "assignees" in params and isinstance(params["assignees"], list):
            issue["assignees"] = list(params["assignees"])
        return self._resp(200, issue)

    def _create_comment(
        self,
        repo: str,
        num: str,
        store: dict[str, Any],
        params: dict[str, Any],
        state: dict,
    ) -> Response:
        body = params.get("body")
        if not isinstance(body, str) or not body.strip():
            return self._validation_failed("IssueComment", "body", "missing_field")
        cid = next_seq(state, "_gh_comment_id")
        comment = {
            "id": cid,
            "body": body,
            "issue_url": f"https://api.github.com/repos/{repo}/issues/{num}",
            "url": f"https://api.github.com/repos/{repo}/issues/comments/{cid}",
            "html_url": f"https://github.com/{repo}/issues/{num}#issuecomment-{cid}",
        }
        store["comments"].setdefault(num, []).append(comment)
        store["issues"][num]["comments"] = len(store["comments"][num])
        return self._resp(201, comment)

    def invariants(self) -> list[Invariant]:
        """Default GitHub invariant: at most one issue created per configured title/scope."""
        return [at_most_once("github.issue.create")]
