#!/bin/bash
set -euo pipefail
. /etc/profile.d/pod.sh
test "$(uname -m)" = x86_64
test "$(id -u podshell)" != 0
test "$(id -u pmuxbridge)" != 0
test "$(id -u podshell)" != "$(id -u pmuxbridge)"
case " $(id -nG podshell) " in
    *" sudo "*|*" docker "*|*" pmuxbridge "*)
        echo 'The shell account must not belong to privileged or bridge groups.' >&2
        exit 1
        ;;
esac
for tool in pmux pmuxd pmux-mcp node npm rustc cargo gcc clang cmake git python3; do
    command -v "$tool" >/dev/null
done
node --version
npm --version
rustc --version
cargo --version
pmux --version
pmux-mcp --version
find "$CARGO_HOME/registry/cache" -type f -name '*.crate' -print -quit | grep -q .
test -d "$CARGO_HOME/registry/index"
find "$npm_config_cache/_cacache" -type f -print -quit | grep -q .
systemd-analyze verify /etc/systemd/system/pod-{mux,agent,bridge}.service
for unit in pod-mux pod-agent pod-bridge; do
    systemctl is-enabled "$unit.service"
done
test ! -e /run/pmux-pod-boot/pod-token
test ! -e /etc/pmux-pod/boot-token
printf '%s\n' 'Pod tools, caches, accounts, and boot units verified; no boot token present.'
