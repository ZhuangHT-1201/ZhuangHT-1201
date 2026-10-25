#!/usr/bin/env python3
"""Refresh the public-project index in the GitHub profile README."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "ZhuangHT-1201"
API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
START_MARKER = "<!-- PROJECTS:START -->"
END_MARKER = "<!-- PROJECTS:END -->"

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
    public_repositories.sort(key=lambda repository: repository.get("created_at", ""), reverse=True)
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

    language = repository.get("language") or "Code"
    details = [description, language]
    if repository.get("fork"):
        details.append("Fork")

    return (
        f'<a href="{html.escape(repository["html_url"], quote=True)}">'
        f"<strong>{html.escape(name)}</strong></a>"
        f"<br><sub>{html.escape(' · '.join(details))}</sub>"
    )


def render_index(repositories: list[dict], overrides: dict[str, str]) -> str:
    cells = [project_cell(repository, overrides) for repository in repositories]
    if len(cells) % 2:
        cells.append("")

    rows = [
        f'<tr><td width="50%" valign="top">{cells[index]}</td>'
        f'<td width="50%" valign="top">{cells[index + 1] or "&nbsp;"}</td></tr>'
        for index in range(0, len(cells), 2)
    ]
    count = len(repositories)

    return "\n".join(
        [
            START_MARKER,
            f"<sub>{count} public projects · Ordered by creation date · Refreshes every 6 hours</sub>",
            "",
            '<table align="center" width="100%">',
            *rows,
            "</table>",
            END_MARKER,
        ]
    )


def main() -> int:
    overrides = json.loads(META_PATH.read_text(encoding="utf-8"))
    repositories = load_repositories()
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
