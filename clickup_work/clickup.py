from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from clickup_work.log import vlog

BASE_URL = "https://api.clickup.com/api/v2"

# ClickUp standard "open" statuses. Each workspace can customize, so we match
# case-insensitively downstream. These are the names sent as query filters.
OPEN_STATUSES = ("to do", "in progress")


class ClickUpError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    description: str
    url: str
    status: str
    priority: str | None  # "urgent" | "high" | "normal" | "low" | None
    list_name: str
    task_type: str  # e.g. "Task", "Bug", "Feature" — used to pick branch prefix


class ClickUp:
    def __init__(self, token: str):
        if not token:
            raise ClickUpError("CLICKUP_API_TOKEN is empty")
        self._token = token

    def _request(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        vlog(f"GET {url}")
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": self._token,
                "Accept": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                vlog(f"  → {resp.status} in {time.monotonic()-started:.2f}s ({len(body)} bytes)")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise ClickUpError(
                    "ClickUp rejected the token (401). "
                    "Check CLICKUP_API_TOKEN — generate one at "
                    "ClickUp → Settings → Apps → API Token."
                ) from None
            raise ClickUpError(f"ClickUp API {e.code}: {body[:300]}") from None
        except urllib.error.URLError as e:
            raise ClickUpError(f"network error talking to ClickUp: {e.reason}") from None

    def get_user_id(self) -> str:
        data = self._request("/user")
        user = data.get("user") or {}
        uid = user.get("id")
        if uid is None:
            raise ClickUpError("unexpected /user response: no user.id")
        return str(uid)

    def get_first_team_id(self) -> str:
        data = self._request("/team")
        teams = data.get("teams") or []
        if not teams:
            raise ClickUpError("no teams/workspaces visible to this token")
        if len(teams) > 1:
            names = ", ".join(t.get("name", "?") for t in teams)
            print(
                f"[clickup-work] multiple teams visible ({names}); "
                f"using the first. Set team_id in config to override."
            )
        return str(teams[0]["id"])

    def get_open_tasks(
        self,
        team_id: str,
        user_id: str,
        list_id: str = "",
        limit: int = 25,
    ) -> list[Task]:
        params: list[tuple[str, str]] = [
            ("assignees[]", user_id),
            ("include_closed", "false"),
            ("order_by", "due_date"),
            ("reverse", "false"),
        ]
        for s in OPEN_STATUSES:
            params.append(("statuses[]", s))
        if list_id:
            params.append(("list_ids[]", list_id))

        data = self._request(f"/team/{team_id}/task", params=params)
        raw_tasks = data.get("tasks") or []

        # Belt-and-suspenders client-side sort in case the API ignores order_by.
        # ClickUp priority ids: 1=urgent, 2=high, 3=normal, 4=low, None=unset.
        def sort_key(t: dict) -> tuple[int, int]:
            pr = t.get("priority")
            pr_id = int(pr.get("id", 5)) if isinstance(pr, dict) else 5
            due = t.get("due_date")
            due_ms = int(due) if due else 2**62
            return (pr_id, due_ms)

        raw_tasks.sort(key=sort_key)
        return [_to_task(t) for t in raw_tasks[:limit]]


def _to_task(t: dict) -> Task:
    pr = t.get("priority")
    priority_name = pr.get("priority") if isinstance(pr, dict) else None
    task_type = (
        t.get("custom_type")
        or (t.get("custom_item") or {}).get("name")
        or "Task"
    )
    return Task(
        id=str(t["id"]),
        name=str(t.get("name", "")).strip() or f"Task {t['id']}",
        description=str(t.get("description") or t.get("text_content") or "").strip(),
        url=str(t.get("url", "")),
        status=str((t.get("status") or {}).get("status", "")),
        priority=priority_name,
        list_name=str((t.get("list") or {}).get("name", "")),
        task_type=str(task_type),
    )
