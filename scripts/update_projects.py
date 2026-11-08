#!/usr/bin/env python3
"""Refresh the public-project index in the GitHub profile README."""

from __future__ import annotations

import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OWNER = "ZhuangHT-1201"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
START_MARKER = "<!-- PROJECTS:START -->"
END_MARKER = "<!-- PROJECTS:END -->"
VISIBLE_PROJECTS = 8
ACTIVITY_WINDOW_DAYS = 90
ACTIVITY_BATCH_SIZE = 8
MAX_COMMITS_PER_REPOSITORY = 100
RECENCY_HALF_LIFE_DAYS = 14.0
MAX_COMMIT_SCORE = 25.0
MAX_CHANGE_SCORE = 35.0
CHANGE_SCORE_CEILING = 100_000.0

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
META_PATH = ROOT / "scripts" / "project_meta.json"


def github_get(path: str, accept: str = "application/vnd.github+json") -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "ZhuangHT-1201-profile-project-index",
        "X-GitHub-Api-Version": API_VERSION,
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(f"{API_ROOT}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return b""
        raise


def github_graphql(query: str, variables: dict[str, str]) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "ZhuangHT-1201-profile-project-index",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{API_ROOT}/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())

    if payload.get("errors") and not payload.get("data"):
        message = payload["errors"][0].get("message", "Unknown GraphQL error")
        raise ValueError(message)
    if payload.get("errors"):
        print("Warning: some repository activity could not be loaded.", file=sys.stderr)
    return payload.get("data") or {}


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_owner_node_id() -> str:
    payload = json.loads(github_get(f"/users/{OWNER}"))
    node_id = payload.get("node_id")
    if not node_id:
        raise ValueError(f"Could not resolve the GitHub node ID for {OWNER}.")
    return node_id


def build_activity_query(repository_count: int) -> str:
    variable_definitions = [
        "$owner: String!",
        "$authorId: ID!",
        "$since: GitTimestamp!",
    ]
    repository_fields: list[str] = []

    for index in range(repository_count):
        repository_variable = f"$repo{index}"
        variable_definitions.append(f"{repository_variable}: String!")
        repository_fields.append(
            f"""
            repo{index}: repository(owner: $owner, name: {repository_variable}) {{
              defaultBranchRef {{
                target {{
                  ... on Commit {{
                    history(
                      first: {MAX_COMMITS_PER_REPOSITORY}
                      since: $since
                      author: {{id: $authorId}}
                    ) {{
                      totalCount
                      nodes {{ committedDate additions deletions }}
                    }}
                  }}
                }}
              }}
            }}
            """
        )

    return (
        f"query({', '.join(variable_definitions)}) {{ "
        + "\n".join(repository_fields)
        + " }"
    )


def load_recent_activity(repositories: list[dict], now: datetime) -> dict[str, dict]:
    author_id = load_owner_node_id()
    since = (now - timedelta(days=ACTIVITY_WINDOW_DAYS)).isoformat().replace(
        "+00:00", "Z"
    )
    activity: dict[str, dict] = {}

    for start in range(0, len(repositories), ACTIVITY_BATCH_SIZE):
        batch = repositories[start : start + ACTIVITY_BATCH_SIZE]
        variables = {
            "owner": OWNER,
            "authorId": author_id,
            "since": since,
        }
        for index, repository in enumerate(batch):
            variables[f"repo{index}"] = repository["name"]

        try:
            data = github_graphql(build_activity_query(len(batch)), variables)
        except (urllib.error.URLError, ValueError) as error:
            print(
                f"Warning: recent activity unavailable for one repository batch: {error}",
                file=sys.stderr,
            )
            continue

        for index, repository in enumerate(batch):
            repository_data = data.get(f"repo{index}") or {}
            default_branch = repository_data.get("defaultBranchRef") or {}
            target = default_branch.get("target") or {}
            history = target.get("history") or {}
            commits = history.get("nodes") or []
            activity[repository["name"]] = {
                "commits": int(history.get("totalCount") or 0),
                "changes": sum(
                    int(commit.get("additions") or 0)
                    + int(commit.get("deletions") or 0)
                    for commit in commits
                ),
            }

    return activity


def activity_score(repository: dict, activity: dict, now: datetime) -> float:
    # Recent pushes dominate; authored commits and churn elevate heavier work.
    pushed_at = parse_timestamp(repository["pushed_at"])
    age_days = max(0.0, (now - pushed_at).total_seconds() / 86_400)
    recency_score = 100.0 * (0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))

    commits = activity.get("commits", 0)
    commit_score = MAX_COMMIT_SCORE * min(
        1.0,
        math.log1p(commits) / math.log1p(MAX_COMMITS_PER_REPOSITORY),
    )

    changes = activity.get("changes", 0)
    change_score = MAX_CHANGE_SCORE * min(
        1.0,
        math.log1p(changes) / math.log1p(CHANGE_SCORE_CEILING),
    )
    return recency_score + commit_score + change_score


def rank_repositories(repositories: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    activity = load_recent_activity(repositories, now)

    def sort_key(repository: dict) -> tuple:
        pushed_at = parse_timestamp(repository["pushed_at"])
        created_at = parse_timestamp(repository["created_at"])
        return (
            activity_score(repository, activity.get(repository["name"], {}), now),
            pushed_at,
            created_at,
            repository["name"].casefold(),
        )

    return sorted(repositories, key=sort_key, reverse=True)


def load_repositories() -> list[dict]:
    repositories: list[dict] = []
    for page in range(1, 11):
        payload = github_get(
            f"/users/{OWNER}/repos"
            f"?type=owner&sort=created&direction=desc&per_page=100&page={page}"
        )
        batch = json.loads(payload or b"[]")
        repositories.extend(batch)
        if len(batch) < 100:
            break

    public_repositories = [
        repository
        for repository in repositories
        if repository["name"] != OWNER and not repository.get("private", False)
    ]
    public_repositories.sort(key=lambda repository: repository.get("pushed_at", ""), reverse=True)
    return public_repositories


def plain_text(value: str) -> str:
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    value = re.sub(r"!\[[^\]]*]\([^)]*\)", " ", value)
    value = re.sub(r"\[!\[[^\]]*]\([^)]*\)]\([^)]*\)", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"!\[[^\]]*]\[[^\]]*]", " ", value)
    value = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_`>#|]", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def readme_summary(repository_name: str) -> str:
    raw = github_get(
        f"/repos/{OWNER}/{repository_name}/readme",
        accept="application/vnd.github.raw+json",
    )
    if not raw:
        return ""

    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    for block in re.split(r"\n\s*\n", text):
        candidate = plain_text(block)
        if len(candidate) < 18:
            continue
        if candidate.lower().startswith(("license", "table of contents")):
            continue
        return candidate
    return ""


def short_summary(value: str, limit: int = 54) -> str:
    value = plain_text(value).strip(" -—·|")
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip("，,.;；:： ") + "…"


def project_cell(repository: dict, overrides: dict[str, str]) -> str:
    name = repository["name"]
    description = overrides.get(name, "")
    if not description:
        description = repository.get("description") or readme_summary(name)
    description = short_summary(description) or "Public project"

    language = repository.get("language")
    details = [description]
    if language:
        details.append(language)
    if repository.get("fork"):
        details.append("Fork")

    return (
        f'<a href="{html.escape(repository["html_url"], quote=True)}">'
        f"<strong>{html.escape(name)}</strong></a>"
        f"<br><sub>{html.escape(' · '.join(details))}</sub>"
    )


def render_project_table(repositories: list[dict], overrides: dict[str, str]) -> str:
    cells = [project_cell(repository, overrides) for repository in repositories]
    if len(cells) % 2:
        cells.append("")

    rows = [
        f'<tr><td width="50%" valign="top">{cells[index]}</td>'
        f'<td width="50%" valign="top">{cells[index + 1] or "&nbsp;"}</td></tr>'
        for index in range(0, len(cells), 2)
    ]

    return "\n".join(
        [
            '<table align="center" width="100%">',
            *rows,
            "</table>",
        ]
    )


def render_index(repositories: list[dict], overrides: dict[str, str]) -> str:
    visible = repositories[:VISIBLE_PROJECTS]
    remaining = repositories[VISIBLE_PROJECTS:]

    sections = [
        START_MARKER,
        render_project_table(visible, overrides),
    ]
    if remaining:
        sections.extend(
            [
                "",
                "<details>",
                "<summary><strong>Browse all projects</strong></summary>",
                "",
                render_project_table(remaining, overrides),
                "",
                "</details>",
            ]
        )
    sections.append(END_MARKER)
    return "\n".join(sections)


def main() -> int:
    overrides = json.loads(META_PATH.read_text(encoding="utf-8"))
    repositories = load_repositories()
    try:
        repositories = rank_repositories(repositories)
    except (urllib.error.URLError, ValueError) as error:
        print(
            f"Warning: falling back to recent-push ordering: {error}",
            file=sys.stderr,
        )
    generated = render_index(repositories, overrides)

    readme = README_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )
    if not pattern.search(readme):
        print("Project markers are missing from README.md.", file=sys.stderr)
        return 1

    updated = pattern.sub(generated, readme)
    if updated != readme:
        README_PATH.write_text(updated, encoding="utf-8", newline="\n")
        print(f"Updated the project index with {len(repositories)} public repositories.")
    else:
        print(f"Project index is current ({len(repositories)} public repositories).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
