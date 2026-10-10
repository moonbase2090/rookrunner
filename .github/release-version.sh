#!/bin/sh
# Resolve release names from the tag. Fail when that version is not pyproject.toml.

set -eu

tag=${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}
case $tag in
  v[0-9]*) version=${tag#v} ;;
  *)
    echo "tag ${tag} does not start with v and a digit" >&2
    exit 1
    ;;
esac

pyproject=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
if [ "$pyproject" != "$version" ]; then
  echo "tag version ${version} does not match pyproject version ${pyproject}" >&2
  exit 1
fi

notes=".github/${tag}-release-notes.md"
if [ ! -f "$notes" ]; then
  echo "missing ${notes}" >&2
  exit 1
fi

wheel="execution_core-${version}-py3-none-any.whl"
sdist="execution_core-${version}.tar.gz"
title="${tag} preview"

printf 'RELEASE_VERSION=%s\n' "$version"
printf 'RELEASE_WHEEL=%s\n' "$wheel"
printf 'RELEASE_SDIST=%s\n' "$sdist"
printf 'RELEASE_TITLE=%s\n' "$title"
printf 'RELEASE_NOTES=%s\n' "$notes"

if [ -n "${GITHUB_ENV:-}" ]; then
  {
    printf 'RELEASE_VERSION=%s\n' "$version"
    printf 'RELEASE_WHEEL=%s\n' "$wheel"
    printf 'RELEASE_SDIST=%s\n' "$sdist"
    printf 'RELEASE_TITLE=%s\n' "$title"
    printf 'RELEASE_NOTES=%s\n' "$notes"
  } >> "$GITHUB_ENV"
fi
