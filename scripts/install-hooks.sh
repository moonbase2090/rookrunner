#!/bin/sh
# Point this clone at the committed hooks. Run once per clone.

set -eu
cd -- "$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
git config core.hooksPath .githooks
