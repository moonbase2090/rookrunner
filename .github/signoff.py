#!/usr/bin/env python3
"""Fail a pull request that changes a CODEOWNERS path without mb2090-signoff.

The protected-path list is read from CODEOWNERS. It is not copied here.
A missing rule on the pull-request head still protects paths listed at the
base commit, so deleting a rule does not drop that path in the same change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LABEL = "mb2090-signoff"
API = "https://api.github.com"


def _strip_comment(line: str) -> str:
    out: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            out.append("\\")
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "#":
            break
        out.append(char)
    if escaped:
        out.append("\\")
    return "".join(out)


def _first_token(line: str) -> str:
    token: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            nxt = line[index + 1]
            if nxt.isspace():
                token.append(nxt)
            else:
                token.append("\\")
                token.append(nxt)
            index += 2
            continue
        if char.isspace():
            break
        token.append(char)
        index += 1
    return "".join(token)


def parse_patterns(text: str) -> list[str]:
    patterns: list[str] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        pattern = _first_token(line)
        if not pattern or pattern.startswith("@") or pattern.startswith("!"):
            continue
        patterns.append(pattern)
    return patterns


def _glob_to_regex(glob: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(glob):
        if glob[index] == "\\" and index + 1 < len(glob):
            parts.append(re.escape(glob[index + 1]))
            index += 2
            continue
        if glob.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
            continue
        if glob.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        char = glob[index]
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def pattern_matches(pattern: str, path: str) -> bool:
    path = path.replace("\\", "/").lstrip("/")
    if not path or not pattern:
        return False
    directory = pattern.endswith("/")
    body = pattern[:-1] if directory else pattern
    if body.startswith("/"):
        body = body[1:]
        anywhere = False
    else:
        anywhere = "/" not in body
    regex = _glob_to_regex(body)
    if anywhere:
        regex = "(?:.*/)?" + regex
    if directory:
        regex += "(?:/.*)?"
    return re.fullmatch(regex, path) is not None


def path_is_protected(path: str, patterns: list[str]) -> bool:
    return any(pattern_matches(pattern, path) for pattern in patterns)


def decision(changed: list[str], patterns: list[str], labels: list[str]) -> tuple[int, list[str]]:
    hits = sorted({path for path in changed if path_is_protected(path, patterns)})
    if hits and LABEL not in labels:
        return 1, hits
    return 0, hits


def paths_from_diff(text: str) -> list[str]:
    paths: list[str] = []
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        for spec in line[len("diff --git ") :].split(" "):
            if spec.startswith(("a/", "b/")):
                path = spec[2:]
                if path and path not in paths:
                    paths.append(path)
    return paths


def report(changed: list[str], patterns: list[str], labels: list[str]) -> int:
    code, hits = decision(changed, patterns, labels)
    if code != 0:
        print("protected paths changed without mb2090-signoff:")  # noqa: T201
        for path in hits:
            print(path)  # noqa: T201
        return 1
    if hits:
        print("sign-off label present")  # noqa: T201
    else:
        print("no protected path changed")  # noqa: T201
    return 0


def git_show_codeowners(sha: str) -> str:
    proc = subprocess.run(
        ["git", "--no-pager", "show", f"{sha}:.github/CODEOWNERS"],
        check=False,
        capture_output=True,
    )
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8")
    err = proc.stderr.decode("utf-8", "replace")
    if "does not exist" in err or "not in" in err:
        return ""
    raise SystemExit("sign-off: could not read CODEOWNERS at the base commit")


def _request(url: str, token: str) -> tuple[object, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "rookrunner-signoff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            link = response.headers.get("Link", "")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"sign-off: GitHub API returned {error.code}: {detail}") from None
    return json.loads(body.decode("utf-8")), link


def _next_url(link: str) -> str | None:
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        url = part.split(";", 1)[0].strip()
        if url.startswith("<") and url.endswith(">"):
            return url[1:-1]
    return None


def pull_request_files(repo: str, number: int, token: str) -> list[str]:
    url: str | None = f"{API}/repos/{repo}/pulls/{number}/files?per_page=100"
    paths: list[str] = []
    while url:
        payload, link = _request(url, token)
        if not isinstance(payload, list):
            raise SystemExit("sign-off: pull request file list was not a list")
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = item.get("filename")
            if isinstance(name, str):
                paths.append(name)
            previous = item.get("previous_filename")
            if isinstance(previous, str):
                paths.append(previous)
        if len(paths) > 3000:
            raise SystemExit("sign-off: pull request file list exceeded 3000")
        url = _next_url(link)
    return paths


def pull_request_labels(repo: str, number: int, token: str) -> list[str]:
    payload, _link = _request(f"{API}/repos/{repo}/pulls/{number}", token)
    if not isinstance(payload, dict):
        raise SystemExit("sign-off: pull request payload was not an object")
    labels = payload.get("labels")
    if not isinstance(labels, list):
        raise SystemExit("sign-off: pull request labels were missing")
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _event_pull_request() -> dict[str, object]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise SystemExit("sign-off: GITHUB_EVENT_PATH is unset")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pull = payload.get("pull_request")
    if not isinstance(pull, dict) or not isinstance(pull.get("number"), int):
        raise SystemExit("sign-off: event is not a pull request")
    base = pull.get("base")
    if not isinstance(base, dict) or not isinstance(base.get("sha"), str):
        raise SystemExit("sign-off: pull request base sha is missing")
    return pull


def run_ci() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise SystemExit("sign-off: GITHUB_TOKEN or GITHUB_REPOSITORY is unset")
    pull = _event_pull_request()
    number = pull["number"]
    if not isinstance(number, int):
        raise SystemExit("sign-off: pull request number is missing")
    base = pull["base"]
    if not isinstance(base, dict):
        raise SystemExit("sign-off: pull request base sha is missing")
    sha = base.get("sha")
    if not isinstance(sha, str):
        raise SystemExit("sign-off: pull request base sha is missing")
    head = Path(".github/CODEOWNERS")
    head_text = head.read_text(encoding="utf-8") if head.is_file() else ""
    patterns = parse_patterns(head_text)
    patterns.extend(parse_patterns(git_show_codeowners(sha)))
    changed = pull_request_files(repo, number, token)
    labels = pull_request_labels(repo, number, token)
    return report(changed, patterns, labels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codeowners", type=Path)
    parser.add_argument("--codeowners-base", type=Path)
    parser.add_argument("--diff-file", type=Path)
    parser.add_argument("--changed", action="append", default=[])
    parser.add_argument("--label", action="append", default=[])
    args = parser.parse_args(argv)
    if args.codeowners is None:
        return run_ci()
    text = args.codeowners.read_text(encoding="utf-8")
    patterns = parse_patterns(text)
    if args.codeowners_base is not None:
        patterns.extend(parse_patterns(args.codeowners_base.read_text(encoding="utf-8")))
    changed = list(args.changed)
    if args.diff_file is not None:
        changed.extend(paths_from_diff(args.diff_file.read_text(encoding="utf-8")))
    if not changed:
        raise SystemExit("sign-off: a fixture needs --changed or --diff-file")
    return report(changed, patterns, list(args.label))


if __name__ == "__main__":
    sys.exit(main())
