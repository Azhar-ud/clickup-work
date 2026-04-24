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
    list_id: str  # needed to fetch the list's configured statuses
    folder_name: str  # "" for folderless lists (API reports folder.hidden=true)
    folder_id: str  # "" for folderless lists; used to route tickets to repos
    task_type: str  # e.g. "Task", "Bug", "Feature" — used to pick branch prefix


class ClickUp:
    def __init__(self, token: str):
        if not token:
            raise ClickUpError("CLICKUP_API_TOKEN is empty")
        self._token = token

    def _request(
        self,
        path: str,
        params: list[tuple[str, str]] | None = None,
        method: str = "GET",
        json_body: dict | None = None,
    ) -> Any:
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        headers = {
            "Authorization": self._token,
            "Accept": "application/json",
        }
        data_bytes: bytes | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(json_body).encode("utf-8")

        vlog(f"{method} {url}")
        if json_body is not None:
            vlog(f"  body: {json.dumps(json_body)}")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            method=method,
            headers=headers,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
                vlog(f"  → {resp.status} in {time.monotonic()-started:.2f}s ({len(body)} bytes)")
                return json.loads(body) if body else {}
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
        folder_ids: list[str] | None = None,
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
        # ClickUp's "project_ids" is the v2 API's name for folder ids.
        for fid in folder_ids or []:
            if fid:
                params.append(("project_ids[]", fid))

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

    def get_list_statuses(self, list_id: str) -> list[str]:
        """Return status names for the given list, in the workspace's order."""
        if not list_id:
            raise ClickUpError("list_id is required to fetch statuses")
        data = self._request(f"/list/{list_id}")
        raw = data.get("statuses") or []
        # Be defensive about ordering — sort by orderindex even though the API
        # typically returns them sorted already.
        raw_sorted = sorted(raw, key=lambda s: int(s.get("orderindex", 0)))
        return [str(s["status"]).strip() for s in raw_sorted if s.get("status")]

    def update_task_status(self, task_id: str, status: str) -> None:
        """Move a task to the given status. Raises ClickUpError on failure."""
        if not status:
            raise ClickUpError("status name is required")
        self._request(
            f"/task/{task_id}",
            method="PUT",
            json_body={"status": status},
        )


def _to_task(t: dict) -> Task:
    pr = t.get("priority")
    priority_name = pr.get("priority") if isinstance(pr, dict) else None
    task_type = (
        t.get("custom_type")
        or (t.get("custom_item") or {}).get("name")
        or "Task"
    )
    list_obj = t.get("list") or {}
    folder_obj = t.get("folder") or {}
    # Lists placed directly under a Space have a synthetic "hidden" folder —
    # treat those as having no folder for display/routing.
    if folder_obj.get("hidden"):
        folder_name = ""
        folder_id = ""
    else:
        folder_name = str(folder_obj.get("name", ""))
        folder_id = str(folder_obj.get("id", ""))
    return Task(
        id=str(t["id"]),
        name=str(t.get("name", "")).strip() or f"Task {t['id']}",
        description=str(t.get("description") or t.get("text_content") or "").strip(),
        url=str(t.get("url", "")),
        status=str((t.get("status") or {}).get("status", "")),
        priority=priority_name,
        list_name=str(list_obj.get("name", "")),
        list_id=str(list_obj.get("id", "")),
        folder_name=folder_name,
        folder_id=folder_id,
        task_type=str(task_type),
    )
