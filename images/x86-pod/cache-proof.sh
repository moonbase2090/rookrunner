#!/bin/bash
set -euo pipefail
. /etc/profile.d/pod.sh
test "$(id -u)" != 0
task_dir=$(mktemp -d)
trap 'rm -rf "$task_dir"' EXIT
cp -R /opt/pod/cache-probes/. "$task_dir/"
export CARGO_NET_OFFLINE=true
test "$(cargo run --quiet --locked --offline --manifest-path "$task_dir/cargo/Cargo.toml")" = 42
npm ci --offline --ignore-scripts --no-audit --no-fund --prefix "$task_dir/npm" >/dev/null
test "$(node "$task_dir/npm/probe.js")" = 1.2.3
printf '%s\n' 'Offline Cargo and npm dependency probes passed.'
