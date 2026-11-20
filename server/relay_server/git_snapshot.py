"""Git snapshots: notice work left on another machine.

A snapshot records what's uncommitted or unpushed in a workstream's repo on
one machine. Other machines compare it with their own repo — using local
commands only. relay never runs ``git fetch`` or anything else that touches
the network; when a fetch would help, the notice says so.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MAX_PATHS = 20
MAX_BRANCHES = 15
_TIMEOUT = 10


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=_TIMEOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
    )


def normalize_remote(url: str) -> str:
    """git@github.com:me/repo.git / https://github.com/me/repo -> github.com/me/repo"""
    url = url.strip()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    url = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", url)  # scp-style host:path
    url = re.sub(r":\d+/", "/", url)
    url = re.sub(r"\.git/?$", "", url).rstrip("/")
    return url.lower()


def repo_identity(repo: Path) -> str | None:
    r = _git(repo, "remote", "get-url", "origin")
    if r.returncode == 0 and r.stdout.strip():
        return normalize_remote(r.stdout)
    return None


def take_snapshot(repo: Path) -> dict | None:
    """Snapshot a repo's uncommitted/unpushed state. None if it isn't a repo."""
    try:
        status = _git(repo, "status", "--porcelain=v2", "--branch", "--untracked-files=normal")
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None

    snap: dict = {
        "repo": repo_identity(repo),
        "branch": None, "head": None, "upstream": None, "ahead": 0, "behind": 0,
        "staged": 0, "modified": 0, "untracked": 0, "paths": [],
        "stash": 0, "branches": [],
    }
    for line in status.stdout.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            snap["head"] = None if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            head = line.split(" ", 2)[2]
            snap["branch"] = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            snap["upstream"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                snap["ahead"], snap["behind"] = int(m.group(1)), int(m.group(2))
        elif line.startswith(("1 ", "2 ", "u ")):
            parts = line.split(" ")
            xy = parts[1]
            if line.startswith("u ") or xy[1] != ".":
                snap["modified"] += 1
            if not line.startswith("u ") and xy[0] != ".":
                snap["staged"] += 1
            path = line.split("\t")[0].split(" ")[-1] if line.startswith("2 ") else parts[-1]
            if len(snap["paths"]) < MAX_PATHS:
                snap["paths"].append(path)
        elif line.startswith("? "):
            snap["untracked"] += 1
            if len(snap["paths"]) < MAX_PATHS:
                snap["paths"].append(line[2:])

    stash = _git(repo, "stash", "list")
    snap["stash"] = len(stash.stdout.splitlines()) if stash.returncode == 0 else 0

    refs = _git(
        repo, "for-each-ref", "--sort=-committerdate", f"--count={MAX_BRANCHES}",
        "--format=%(refname:short)%00%(objectname)%00%(upstream:short)", "refs/heads",
    )
    has_remotes = bool(_git(repo, "remote").stdout.strip())
    for line in refs.stdout.splitlines() if refs.returncode == 0 else []:
        name, sha, upstream = (line.split("\0") + ["", "", ""])[:3]
        if not name or not sha:
            continue
        if has_remotes:
            count = _git(repo, "rev-list", "--count", sha, "--not", "--remotes")
            unpushed = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
        else:
            unpushed = 0
        if unpushed or name == snap["branch"]:
            snap["branches"].append(
                {"name": name, "head": sha, "upstream": upstream or None, "unpushed": unpushed}
            )
    return snap


def snapshot_is_clean(snap: dict) -> bool:
    return (
        not snap.get("staged") and not snap.get("modified") and not snap.get("untracked")
        and not snap.get("stash")
        and not any(b.get("unpushed") for b in snap.get("branches", []))
    )


def _commit_exists(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _on_remote(repo: Path, sha: str) -> bool:
    r = _git(repo, "rev-list", "--count", sha, "--not", "--remotes")
    return r.returncode == 0 and r.stdout.strip() == "0"


def _contained_in_head(repo: Path, sha: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0


def _age(taken_at: str, now: datetime | None = None) -> str:
    try:
        then = datetime.strptime(taken_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "unknown time"
    secs = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def notices_for(snap: dict, host_name: str, local_repo: Path | None, now: datetime | None = None) -> tuple[list[str], bool]:
    """Lines describing another machine's snapshot, checked against the local
    repo. Returns (lines, fetch_needed). Empty lines = nothing to report."""
    if local_repo is not None:
        local_identity = repo_identity(local_repo)
        if snap.get("repo") and local_identity and snap["repo"] != local_identity:
            return [], False  # different repo under the same workstream
    details: list[str] = []
    fetch_needed = False

    dirty = []
    if snap.get("modified"):
        dirty.append(_plural(snap["modified"], "modified file"))
    if snap.get("staged"):
        dirty.append(f"{snap['staged']} staged")
    if snap.get("untracked"):
        dirty.append(f"{snap['untracked']} untracked")
    if dirty:
        where = f"on {snap['branch']}" if snap.get("branch") else "in the working tree"
        details.append(f"uncommitted {where}: {', '.join(dirty)}")
    if snap.get("stash"):
        details.append(_plural(snap["stash"], "stash") + " (git stash list)")

    for b in snap.get("branches", []):
        name, sha, unpushed = b.get("name"), b.get("head") or "", b.get("unpushed") or 0
        have = local_repo is not None and sha and _commit_exists(local_repo, sha)
        if unpushed:
            if have and _on_remote(local_repo, sha):
                if not _contained_in_head(local_repo, sha):
                    details.append(f"branch {name} ({sha[:7]}) has been pushed since; `git pull` to update")
                continue
            if have:
                continue  # this repo already has those commits
            details.append(
                f"branch {name}: {_plural(unpushed, 'unpushed commit')} at snapshot time. "
                "Run `git fetch` to check whether they've been pushed since."
            )
            fetch_needed = True
        elif sha and local_repo is not None:
            if not have:
                details.append(f"pushed {name} ({sha[:7]}), which this repo doesn't have yet. Run `git fetch`.")
                fetch_needed = True
            elif name == _current_branch(local_repo) and not _contained_in_head(local_repo, sha):
                details.append(f"pushed {name} ({sha[:7]}) ahead of this checkout; `git pull` to update")

    if not details:
        return [], False
    lines = [f"relay: work on {host_name} not visible here (snapshot {_age(snap.get('taken_at', ''), now)}):"]
    lines += [f"  {d}" for d in details]
    return lines, fetch_needed


def _current_branch(repo: Path) -> str | None:
    r = _git(repo, "symbolic-ref", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


FETCH_HINT = (
    "relay: a `git fetch` is needed to see the other machine's pushed work. "
    "Offer to run it (and re-check with `/relay:sync status`); don't run it without asking."
)
