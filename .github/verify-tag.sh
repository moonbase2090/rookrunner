#!/bin/sh
# Verify an annotated tag with the public SSH key in this repository.
# A missing or invalid signature is a failed command.

set -eu

tag=${1:?tag required}
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

git config gpg.format ssh
git config gpg.ssh.allowedSignersFile "$root/.github/allowed_signers"
git verify-tag "$tag"
