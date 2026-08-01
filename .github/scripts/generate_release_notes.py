#!/usr/bin/env python3
"""
Generate CHANGELOG.md and RELEASE_NOTES.md from git history.

Robust against messy commit histories:
- skips merge commits and dependency-bump noise
- dedupes commits with identical subjects (e.g. same feature on feature branches)
- categorizes via conventional-commit prefix with keyword fallback
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def previous_tag(current_tag: str) -> str | None:
    tags = run_git("tag", "--sort=-version:refname").splitlines()
    tags = [t for t in tags if re.fullmatch(r"v?\d+\.\d+\.\d+", t)]
    try:
        idx = tags.index(current_tag)
    except ValueError:
        return None
    return tags[idx + 1] if idx + 1 < len(tags) else None


def fetch_commits(prev_tag: str | None, current_tag: str) -> list[dict]:
    if prev_tag:
        lines = run_git(
            "log",
            f"{prev_tag}..{current_tag}",
            "--pretty=format:%h|%an|%ad|%s",
            "--date=short",
            "--no-merges",
        ).splitlines()
    else:
        lines = run_git(
            "log",
            current_tag,
            "--pretty=format:%h|%an|%ad|%s",
            "--date=short",
            "--no-merges",
        ).splitlines()

    commits = []
    for line in lines:
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, author, date, subject = parts
        commits.append(
            {"sha": sha, "author": author, "date": date, "subject": subject}
        )
    return commits


# Subjects to drop entirely (tooling noise).
SKIP_PATTERNS = [
    re.compile(r"uv\.lock", re.I),
    re.compile(r"^merge .*origin/main", re.I),
    re.compile(r"^merge (pull request|branch)", re.I),
    re.compile(r"^bump version", re.I),
    re.compile(r"^version bump", re.I),
    re.compile(r"^docs: update changelog", re.I),
    re.compile(r"^chore: update changelog", re.I),
    re.compile(r"^chore: bump version", re.I),
    re.compile(r"^restore .*formatting", re.I),
    re.compile(r"^gitignore .*cache", re.I),
    re.compile(r"^remove unnecessary .*cleanup", re.I),
]

CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|chore|ci|build|perf|test|refactor|style|revert|deps)"
    r"(?:\(([^)]+)\))?!?:\s*(.+)$",
    re.I,
)

KEYWORD_MAP = [
    (("fix", "bug", "error", "crash", "resolve"), "fix"),
    (("feat", "add", "new", "implement", "support", "export"), "feat"),
    (("doc", "readme", "changelog", "comment", "example"), "docs"),
    (("test", "spec", "pytest"), "test"),
    (("refactor", "clean", "reorganize"), "refactor"),
    (("perf", "optimize", "speed", "faster"), "perf"),
    (("ci", "workflow", "action", "pipeline", "cd"), "ci"),
    (("deps", "dependabot", "bump ", "update ", "replace"), "chore"),
]


def categorize(subject: str) -> tuple[str, str]:
    m = CONVENTIONAL.match(subject)
    if m:
        return m.group(1).lower(), (m.group(3) or subject).strip()
    lowered = subject.lower()
    for keywords, type_ in KEYWORD_MAP:
        if any(k in lowered for k in keywords):
            return type_, subject.strip()
    return "chore", subject.strip()


def clean_subject(subject: str) -> str:
    return subject.strip().strip(".").strip()


def dedupe(commits: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for commit in commits:
        key = clean_subject(commit["subject"]).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(commit)
    return result


def tag_date(current_tag: str) -> str:
    try:
        return run_git(
            "log", "-1", "--pretty=format:%ad", "--date=short", current_tag
        )
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def generate_section(commits: list[dict]) -> str:
    added: list[str] = []
    fixed: list[str] = []
    changed: list[str] = []
    for commit in commits:
        type_, message = categorize(commit["subject"])
        if type_ == "feat":
            added.append(message)
        elif type_ == "fix":
            fixed.append(message)
        else:
            changed.append(message)

    section = []
    if added:
        section += ["### Added", ""]
        section += [f"- **{m}**" for m in added]
        section.append("")
    if fixed:
        section += ["### Fixed", ""]
        section += [f"- **{m}**" for m in fixed]
        section.append("")
    if changed:
        section += ["### Changed", ""]
        section += [f"- **{m}**" for m in changed]
        section.append("")

    return "\n".join(section).rstrip()


def build_changelog_entry(
    current_tag: str, version: str, commits: list[dict]
) -> str:
    date = tag_date(current_tag)
    section = generate_section(commits)
    return f"## [{version}] - {date}\n\n{section}"


def update_changelog(current_tag: str, version: str, commits: list[dict]) -> str:
    entry = build_changelog_entry(current_tag, version, commits)

    try:
        with open("CHANGELOG.md", "r", encoding="utf-8") as f:
            changelog = f.read()
    except FileNotFoundError:
        changelog = (
            "# Changelog\n\n"
            "All notable changes to this project will be documented in this file.\n\n"
            "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),\n"
            "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n"
        )

    marker = "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)."
    if marker in changelog:
        head, tail = changelog.split(marker, 1)
        updated = head + marker + "\n\n" + entry + "\n\n" + tail.lstrip("\n")
    else:
        updated = entry + "\n\n" + changelog.lstrip("\n")

    with open("CHANGELOG.md", "w", encoding="utf-8") as f:
        f.write(updated)
    return updated


def build_release_notes(current_tag: str, version: str, commits: list[dict]) -> str:
    date = tag_date(current_tag)
    section = generate_section(commits)

    added = []
    fixed = []
    changed = []
    for commit in commits:
        type_, message = categorize(commit["subject"])
        if type_ == "feat":
            added.append(message)
        elif type_ == "fix":
            fixed.append(message)
        else:
            changed.append(message)

    lines = [f"# Release {version}", "", f"**goapauto {version}** released on {date}."]
    if added:
        lines += ["", "## ✨ Features", ""]
        lines += [f"- {m}" for m in added]
    if fixed:
        lines += ["", "## 🐛 Bug Fixes", ""]
        lines += [f"- {m}" for m in fixed]
    if changed:
        lines += ["", "## 🔧 Other", ""]
        lines += [f"- {m}" for m in changed]
    lines += ["", "______________________________________________________________________", ""]
    return "\n".join(lines).rstrip()


def main() -> int:
    current_tag = sys.argv[1] if len(sys.argv) > 1 else ""
    if not current_tag:
        print("usage: generate_release_notes.py <tag> [changelog] [release_notes]")
        return 1

    version = current_tag.lstrip("v")
    prev = previous_tag(current_tag)
    commits = fetch_commits(prev, current_tag)
    commits = dedupe(commits)
    commits = [
        c
        for c in commits
        if not any(p.search(c["subject"]) for p in SKIP_PATTERNS)
    ]

    update_changelog(current_tag, version, commits)

    release_notes = build_release_notes(current_tag, version, commits)
    with open("RELEASE_NOTES.md", "w", encoding="utf-8") as f:
        f.write(release_notes)

    print(f"Generated RELEASE_NOTES.md and updated CHANGELOG.md for {current_tag}")
    print(f"  {len(commits)} commits from previous tag {prev or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
