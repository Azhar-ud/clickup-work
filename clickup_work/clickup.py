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

class ClickUpError(Exception):
    pass


@dataclass(frozen=True)
class Member:
    user_id: str
    username: str
    email: str


@dataclass(frozen=True)
class Comment:
    id: str
    text: str
    author: str  # display name
    created_ms: int | None  # epoch ms or None when the API returns junk


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
    space_name: str  # resolved via /team/{id}/space; used when folder is hidden
    locations: tuple[tuple[str, str], ...]  # additional (list_id, list_name) when ticket is in multiple lists (TMIL); excludes the home list
    tags: tuple[str, ...]  # ClickUp tag names — used to route tickets in shared folders
    task_type: str  # e.g. "Task", "Bug", "Feature" — used to pick branch prefix
    due_date: int | None = None  # epoch ms (UTC) or None if unset on the ticket
    time_estimate: int | None = None  # ms or None; ClickUp's Workload view ignores tickets without one
    start_date: int | None = None  # epoch ms (UTC) or None if unset
    space_id: str = ""  # used when calling space-scoped APIs (tags)
    assignees: tuple[Member, ...] = ()  # current assignees (id + username + email)


@dataclass(frozen=True)
class TimeEntry:
    """A single logged time entry on a task. ``id`` is required to edit/delete."""

    id: str
    duration_ms: int
    start_ms: int | None
    end_ms: int | None
    description: str
    user: str  # display name of who logged it


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
            # Trust include_closed=false to scope us to active work — that
            # filter respects custom workspace statuses (DEV ASSIGNED, QA
            # ASSIGNED, in review, blocked, …) by excluding only those whose
            # type is closed/cancelled. A hard-coded statuses[] list would
            # exclude every workspace that doesn't use the literal names
            # "to do" and "in progress".
            ("include_closed", "false"),
            ("order_by", "due_date"),
            ("reverse", "false"),
            # Tasks in Multiple Lists: when enabled in the workspace, this
            # populates the `locations` field on each task so we can show
            # "(also in: <list>)" in the picker. No-op when disabled.
            ("include_timl", "true"),
        ]
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
        # ClickUp task payloads only include space.id, not space.name. Pull the
        # id→name map once so folderless tickets can be labelled by Space (the
        # real organizational level when there's no folder in between).
        try:
            space_names = self.get_spaces(team_id)
        except ClickUpError:
            space_names = {}  # Non-fatal: folderless tickets fall back to "(no folder)".
        return [_to_task(t, space_names) for t in raw_tasks[:limit]]

    def get_spaces(self, team_id: str) -> dict[str, str]:
        """Return {space_id: space_name} for non-archived spaces in the workspace."""
        data = self._request(
            f"/team/{team_id}/space",
            params=[("archived", "false")],
        )
        out: dict[str, str] = {}
        for s in data.get("spaces") or []:
            sid = s.get("id")
            sname = s.get("name")
            if sid and sname:
                out[str(sid)] = str(sname)
        return out

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

    def set_time_estimate(self, task_id: str, estimate_ms: int) -> None:
        """Set the task's time estimate (milliseconds)."""
        if estimate_ms <= 0:
            raise ClickUpError("estimate must be a positive number of milliseconds")
        self._request(
            f"/task/{task_id}",
            method="PUT",
            json_body={"time_estimate": int(estimate_ms)},
        )

    def get_team_members(self, team_id: str) -> list[Member]:
        """Return active members of the workspace.

        ClickUp's /team endpoint embeds members on each team object, so this
        is one call regardless of workspace size.
        """
        data = self._request("/team")
        for t in data.get("teams") or []:
            if str(t.get("id")) == str(team_id):
                out: list[Member] = []
                for m in t.get("members") or []:
                    user = m.get("user") or {}
                    uid = user.get("id")
                    if uid is None:
                        continue
                    name = str(user.get("username", "")).strip()
                    if not name:
                        # Fall back to email local-part rather than empty rows.
                        email = str(user.get("email", "")).strip()
                        name = email.split("@", 1)[0] if email else f"user-{uid}"
                    out.append(
                        Member(
                            user_id=str(uid),
                            username=name,
                            email=str(user.get("email", "")).strip(),
                        )
                    )
                return out
        raise ClickUpError(
            f"team id {team_id} not found in /team response — "
            f"is team_id correct in config?"
        )

    def update_task_assignees(
        self,
        task_id: str,
        add_ids: list[str] | tuple[str, ...] = (),
        remove_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Add and/or remove assignees on a task in one PUT.

        ClickUp accepts integer user ids in the assignees.add / .rem lists.
        Empty lists are no-ops; we skip the call entirely in that case.
        """
        if not add_ids and not remove_ids:
            return
        body = {
            "assignees": {
                "add": [int(uid) for uid in add_ids],
                "rem": [int(uid) for uid in remove_ids],
            }
        }
        self._request(
            f"/task/{task_id}",
            method="PUT",
            json_body=body,
        )

    def update_task_fields(self, task_id: str, fields: dict[str, Any]) -> None:
        """Generic ``PUT /task/{id}`` for the writable scalar fields.

        Caller assembles the JSON body (e.g. ``{"name": "...", "priority": 2,
        "due_date": 1735689600000, "due_date_time": False}``). Empty mapping
        is a no-op so callers can build the dict from optional inputs without
        a dance.

        Pass ``None`` for ``due_date`` / ``start_date`` / ``priority`` to
        clear the field (ClickUp accepts JSON null on these).
        """
        if not fields:
            return
        self._request(
            f"/task/{task_id}",
            method="PUT",
            json_body=fields,
        )

    def add_task_tag(self, task_id: str, tag_name: str) -> None:
        """Add an existing space-scoped tag to a task.

        ClickUp returns 404 if the tag doesn't already exist in the task's
        space — we surface the error rather than auto-creating, to match the
        ClickUp UI behavior.
        """
        name = (tag_name or "").strip()
        if not name:
            raise ClickUpError("tag name is empty")
        self._request(
            f"/task/{task_id}/tag/{urllib.parse.quote(name)}",
            method="POST",
        )

    def remove_task_tag(self, task_id: str, tag_name: str) -> None:
        name = (tag_name or "").strip()
        if not name:
            raise ClickUpError("tag name is empty")
        self._request(
            f"/task/{task_id}/tag/{urllib.parse.quote(name)}",
            method="DELETE",
        )

    def get_space_tags(self, space_id: str) -> list[str]:
        """Return tag names defined in the given space, alphabetically sorted."""
        if not space_id:
            return []
        data = self._request(f"/space/{space_id}/tag")
        names = [
            str(t.get("name", "")).strip()
            for t in (data.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        return sorted({n for n in names if n}, key=str.lower)

    def get_task_time_entries(
        self, team_id: str, task_id: str
    ) -> list[TimeEntry]:
        """Return time entries logged on the task, newest first."""
        data = self._request(
            f"/team/{team_id}/time_entries",
            params=[("task_id", task_id)],
        )
        raw = data.get("data") or []
        out: list[TimeEntry] = []
        for e in raw:
            user = e.get("user") or {}
            author = (
                str(user.get("username") or "").strip()
                or str(user.get("email") or "").split("@", 1)[0]
                or "unknown"
            )
            out.append(
                TimeEntry(
                    id=str(e.get("id", "")),
                    duration_ms=int(e.get("duration", 0) or 0),
                    start_ms=_coerce_int(e.get("start")),
                    end_ms=_coerce_int(e.get("end")),
                    description=str(e.get("description") or "").strip(),
                    user=author,
                )
            )
        out.sort(key=lambda te: te.start_ms or 0, reverse=True)
        return out

    def update_time_entry(
        self,
        team_id: str,
        entry_id: str,
        fields: dict[str, Any],
    ) -> None:
        """PUT a partial update onto a time entry (duration, description, etc.)."""
        if not entry_id:
            raise ClickUpError("entry_id is required")
        if not fields:
            return
        self._request(
            f"/team/{team_id}/time_entries/{entry_id}",
            method="PUT",
            json_body=fields,
        )

    def delete_time_entry(self, team_id: str, entry_id: str) -> None:
        if not entry_id:
            raise ClickUpError("entry_id is required")
        self._request(
            f"/team/{team_id}/time_entries/{entry_id}",
            method="DELETE",
        )

    def get_subtasks(self, task_id: str) -> list[Task]:
        """Return immediate subtasks of a task.

        Uses ``GET /task/{id}?include_subtasks=true``, which embeds child
        tasks alongside the parent. Skips entries without an id (defensive
        — ClickUp occasionally returns thin records for archived rows).
        """
        if not task_id:
            raise ClickUpError("task_id is required")
        data = self._request(
            f"/task/{task_id}",
            params=[("include_subtasks", "true")],
        )
        raw = data.get("subtasks") or []
        return [_to_task(t) for t in raw if isinstance(t, dict) and t.get("id")]

    def create_subtask(self, list_id: str, parent_id: str, name: str) -> Task:
        """Create a subtask under ``parent_id`` in the parent's list."""
        title = (name or "").strip()
        if not title:
            raise ClickUpError("subtask name is empty")
        if not list_id or not parent_id:
            raise ClickUpError("list_id and parent_id are required")
        data = self._request(
            f"/list/{list_id}/task",
            method="POST",
            json_body={"name": title, "parent": parent_id},
        )
        return _to_task(data)

    def get_task_comments(self, task_id: str) -> list[Comment]:
        """List comments on a task, oldest first."""
        if not task_id:
            raise ClickUpError("task_id is required to fetch comments")
        data = self._request(f"/task/{task_id}/comment")
        raw = data.get("comments") or []
        out: list[Comment] = []
        for c in raw:
            text = str(c.get("comment_text") or "").strip()
            if not text:
                # ClickUp's rich-text "comment" field is a list of {text, ...}
                # blocks. Stitch them together so attachments-only comments
                # don't render blank.
                blocks = c.get("comment") or []
                text = "".join(
                    str(b.get("text", "")) for b in blocks if isinstance(b, dict)
                ).strip()
            user = c.get("user") or {}
            author = (
                str(user.get("username") or "").strip()
                or str(user.get("email") or "").split("@", 1)[0]
                or "unknown"
            )
            out.append(
                Comment(
                    id=str(c.get("id", "")),
                    text=text,
                    author=author,
                    created_ms=_coerce_int(c.get("date")),
                )
            )
        # ClickUp returns newest first; flip so the conversation reads naturally.
        out.sort(key=lambda c: c.created_ms or 0)
        return out

    def create_task_comment(self, task_id: str, text: str) -> None:
        """Post a comment on a task. Empty text is rejected client-side."""
        body = (text or "").strip()
        if not body:
            raise ClickUpError("comment text is empty")
        self._request(
            f"/task/{task_id}/comment",
            method="POST",
            json_body={"comment_text": body, "notify_all": False},
        )

    def add_time_entry(
        self,
        team_id: str,
        user_id: str,
        task_id: str,
        duration_ms: int,
        description: str = "",
    ) -> None:
        """Log a time entry against a task ending now."""
        if duration_ms <= 0:
            raise ClickUpError("duration must be a positive number of milliseconds")
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - int(duration_ms)
        body = {
            "description": description,
            "tid": task_id,
            "start": start_ms,
            "duration": int(duration_ms),
            "billable": False,
            "assignee": int(user_id),
        }
        self._request(
            f"/team/{team_id}/time_entries",
            method="POST",
            json_body=body,
        )


def _to_task(t: dict, space_names: dict[str, str] | None = None) -> Task:
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
    space_obj = t.get("space") or {}
    space_id = str(space_obj.get("id", ""))
    space_name = (space_names or {}).get(space_id, "")
    raw_tags = t.get("tags") or []
    tags = tuple(
        str(tg["name"]).strip()
        for tg in raw_tags
        if isinstance(tg, dict) and tg.get("name")
    )
    # Tasks in Multiple Lists: drop the home list (its id matches list.id) so
    # only *additional* locations show in the picker. Defensive about shape:
    # if the field is missing or wrong-typed, locations stays empty.
    home_list_id = str(list_obj.get("id", ""))
    raw_locations = t.get("locations") or []
    extra_locations: list[tuple[str, str]] = []
    for loc in raw_locations:
        if not isinstance(loc, dict):
            continue
        loc_id = str(loc.get("id", "")).strip()
        loc_name = str(loc.get("name", "")).strip()
        if not loc_id or not loc_name or loc_id == home_list_id:
            continue
        extra_locations.append((loc_id, loc_name))
    due_date = _coerce_int(t.get("due_date"))
    time_estimate = _coerce_int(t.get("time_estimate"))
    start_date = _coerce_int(t.get("start_date"))
    raw_assignees = t.get("assignees") or []
    assignees: list[Member] = []
    for a in raw_assignees:
        if not isinstance(a, dict):
            continue
        uid = a.get("id")
        if uid is None:
            continue
        username = str(a.get("username") or "").strip()
        email = str(a.get("email") or "").strip()
        if not username:
            username = email.split("@", 1)[0] if email else f"user-{uid}"
        assignees.append(Member(user_id=str(uid), username=username, email=email))
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
        space_name=space_name,
        locations=tuple(extra_locations),
        tags=tags,
        task_type=str(task_type),
        due_date=due_date,
        time_estimate=time_estimate,
        start_date=start_date,
        space_id=space_id,
        assignees=tuple(assignees),
    )


def _coerce_int(value: Any) -> int | None:
    """Cast a ClickUp millisecond field to int, tolerating str / None / empty."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
