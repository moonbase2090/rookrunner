#!/bin/sh
# Fail when release text matches a private-infrastructure pattern.
# Extra patterns come from an optional untracked file or RELEASE_DENYLIST.
# A missing extra list is skipped. Matches are reported as file:line only.

set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
deny_file=${HOME:?}/.config/moonbase/release-denylist.txt
title_path=-
tag_path=-
artifacts=-
saw_title=0
saw_tag=0

while [ $# -gt 0 ]; do
  case $1 in
    --root)
      root=$2
      shift 2
      ;;
    --title)
      saw_title=1
      title_path=$2
      shift 2
      ;;
    --tag-message)
      saw_tag=1
      tag_path=$2
      shift 2
      ;;
    --artifacts)
      artifacts=$2
      shift 2
      ;;
    *)
      echo "release-text: unknown argument" >&2
      exit 2
      ;;
  esac
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077

cat > "$work/patterns" <<'EOF'
DESKTOP-[A-Z0-9]+
[A-Za-z0-9-]+\.local\b
/Users/
/home/[a-z]
C:\\Users
-----BEGIN [A-Z ]*PRIVATE KEY-----
(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}
\b(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)\b
EOF

append_patterns() {
  source_file=$1
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(printf '%s' "$line" | tr -d '\r')
    case $line in
      *[![:space:]]*) printf '%s\n' "$line" >> "$work/patterns" ;;
    esac
  done < "$source_file"
}

if [ -f "$deny_file" ]; then
  append_patterns "$deny_file"
fi
if [ -n "${RELEASE_DENYLIST:-}" ]; then
  printf '%s\n' "$RELEASE_DENYLIST" > "$work/env-patterns"
  append_patterns "$work/env-patterns"
fi

if [ "$saw_title" -eq 1 ]; then
  printf '%s\n' "$title_path" > "$work/title"
  title_path=$work/title
fi
if [ "$saw_tag" -eq 1 ]; then
  printf '%s\n' "$tag_path" > "$work/tag"
  tag_path=$work/tag
fi

python3 - "$root" "$work/patterns" "$title_path" "$tag_path" "$artifacts" <<'PY'
import pathlib
import re
import sys
import tarfile
import zipfile

root_s, pattern_path, title_path, tag_path, artifacts = sys.argv[1:6]
root = pathlib.Path(root_s)
allow = "322824348+mb2090@users.noreply.github.com"
email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
example_domains = ("example.com", "example.net", "example.org", "example")
patterns = []
for raw in pathlib.Path(pattern_path).read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    try:
        patterns.append(re.compile(raw, re.IGNORECASE))
    except re.error:
        sys.stderr.write("release-text: a pattern was rejected\n")
        sys.exit(2)

hits = []


def email_allowed(address):
    if address == allow:
        return True
    local, _, domain = address.rpartition("@")
    domain = domain.lower().rstrip(".")
    if domain.endswith(".invalid"):
        return True
    if any(domain == suffix or domain.endswith("." + suffix) for suffix in example_domains):
        return True
    return local.lower() == "noreply" and domain == "github.com"


def consider(report, text):
    for number, line in enumerate(text.splitlines(), 1):
        bad = any(pattern.search(line) for pattern in patterns)
        if not bad:
            bad = any(not email_allowed(match.group(0)) for match in email_re.finditer(line))
        if bad:
            hits.append(f"{report}:{number}")


def read_text(path):
    return path.read_bytes().decode("utf-8", "replace")


def add_file(report, path):
    consider(report, read_text(path))


for name in ("CHANGELOG.md", "README.md", "RELEASING.md"):
    path = root / name
    if path.is_file():
        add_file(name, path)

docs = root / "docs"
if docs.is_dir():
    for path in sorted(item for item in docs.rglob("*") if item.is_file()):
        add_file(str(path.relative_to(root)), path)

notes = root / ".github"
if notes.is_dir():
    for path in sorted(notes.glob("*-release-notes.md")):
        add_file(str(path.relative_to(root)), path)

if title_path != "-":
    consider("release-title", pathlib.Path(title_path).read_text(encoding="utf-8"))
if tag_path != "-":
    consider("tag-message", pathlib.Path(tag_path).read_text(encoding="utf-8"))


def safe_name(name):
    if name.startswith("/") or name.startswith("\\"):
        return False
    return ".." not in pathlib.PurePosixPath(name).parts


def scan_archive(report, path):
    lowered = path.name.lower()
    try:
        if lowered.endswith((".tar.gz", ".tgz", ".tar")):
            mode = "r:gz" if lowered.endswith((".gz", ".tgz")) else "r:"
            with tarfile.open(path, mode) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    if not safe_name(member.name):
                        sys.stderr.write("release-text: artifact path rejected\n")
                        sys.exit(2)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    text = extracted.read().decode("utf-8", "replace")
                    consider(f"{report}!{member.name}", text)
            return
        if lowered.endswith((".whl", ".zip")):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if not safe_name(info.filename):
                        sys.stderr.write("release-text: artifact path rejected\n")
                        sys.exit(2)
                    text = archive.read(info).decode("utf-8", "replace")
                    consider(f"{report}!{info.filename}", text)
            return
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        sys.stderr.write("release-text: artifact could not be read\n")
        sys.exit(2)
    add_file(report, path)


if artifacts != "-":
    art = pathlib.Path(artifacts)
    if not art.is_absolute():
        art = root / art
    if not art.exists():
        sys.stderr.write("release-text: artifacts path is missing\n")
        sys.exit(2)
    files = [art] if art.is_file() else sorted(item for item in art.rglob("*") if item.is_file())
    for path in files:
        try:
            report = str(path.relative_to(root))
        except ValueError:
            report = path.name
        scan_archive(report, path)

for hit in sorted(set(hits)):
    sys.stdout.write(hit + "\n")
sys.exit(1 if hits else 0)
PY
