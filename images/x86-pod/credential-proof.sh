#!/bin/bash
# SPDX-License-Identifier: MIT
# Prove the credential and socket permissions of the pod accounts in a Linux
# container. Run as root in a disposable container. It builds the same accounts,
# groups and directory modes as install.sh and the boot injector, then checks the
# shell account and the agent identity (user podshell, group podshell, no extra
# groups) against them. It does not run systemd, so unit sandboxing is out of scope.
set -euo pipefail

test "$(id -u)" = 0
groupadd --system pmuxbridge
useradd --system --gid pmuxbridge --no-create-home --shell /usr/sbin/nologin pmuxbridge
useradd --no-create-home --shell /bin/bash podshell
useradd --no-create-home --shell /usr/sbin/nologin stranger

# Boot injection: root:pmuxbridge 0750 directory, root-only 0400 token.
install -d -o root -g pmuxbridge -m 0750 /run/pmux-pod-boot
install -o root -g root -m 0400 /dev/stdin /run/pmux-pod-boot/pod-token <<< 'fixture-token'
# The copy systemd LoadCredential makes for the bridge unit.
install -d -o pmuxbridge -g pmuxbridge -m 0700 /run/credentials/pod-bridge.service
install -o pmuxbridge -g pmuxbridge -m 0400 /dev/stdin \
    /run/credentials/pod-bridge.service/pod-token <<< 'fixture-token'
# The agent socket directory from tmpfiles/pod-agent.conf.
install -d -o podshell -g pmuxbridge -m 2770 /run/pmux-pod

as_shell()  { setpriv --reuid=podshell --regid=podshell --init-groups "$@"; }
as_agent()  { setpriv --reuid=podshell --regid=podshell --clear-groups "$@"; }
as_bridge() { setpriv --reuid=pmuxbridge --regid=pmuxbridge --clear-groups "$@"; }
as_other()  { setpriv --reuid=stranger --regid=stranger --clear-groups "$@"; }

denied() {
    local who=$1 path=$2
    if "$who" cat "$path" > /dev/null 2>&1; then
        echo "FAIL: $who could read $path" >&2
        exit 1
    fi
    echo "denied: $who cannot read $path"
}

# Neither shell path is in the bridge group.
for who in as_shell as_agent; do
    case " $($who id -nG) " in
        *" pmuxbridge "*) echo "FAIL: $who is in pmuxbridge" >&2; exit 1 ;;
    esac
done
echo "groups: shell=$(as_shell id -nG) agent=$(as_agent id -nG)"

for who in as_shell as_agent; do
    denied "$who" /run/pmux-pod-boot/pod-token
    denied "$who" /run/credentials/pod-bridge.service/pod-token
done

# Control: the check is not vacuous. The bridge reads its own credential copy.
test "$(as_bridge cat /run/credentials/pod-bridge.service/pod-token)" = fixture-token
echo "allowed: bridge reads its credential copy"

# A socket bound by the agent identity in the setgid directory takes the bridge
# group, the bridge can connect, and an unrelated account cannot.
# perl-base ships in the base image; Python does not.
cat > /tmp/bind.pl <<'PL'
use strict; use warnings; use Socket;
umask 0007;
my $path = "/run/pmux-pod/agent.sock";
socket(my $server, PF_UNIX, SOCK_STREAM, 0) or die "socket: $!";
bind($server, pack_sockaddr_un($path)) or die "bind: $!";
listen($server, 1) or die "listen: $!";
open(my $ready, ">", "/tmp/ready") or die "ready: $!";
close($ready);
accept(my $conn, $server);
PL
cat > /tmp/connect.pl <<'PL'
use strict; use warnings; use Socket;
socket(my $client, PF_UNIX, SOCK_STREAM, 0) or die "socket: $!";
if (connect($client, pack_sockaddr_un("/run/pmux-pod/agent.sock"))) { print "connect ok\n"; exit 0 }
print "connect denied: $!\n";
exit 3;
PL
chmod 0755 /tmp/bind.pl /tmp/connect.pl
as_agent perl /tmp/bind.pl &
server=$!
for _ in $(seq 1 100); do [ -e /tmp/ready ] && break; sleep 0.1; done
test -e /tmp/ready
test "$(stat -c %G /run/pmux-pod/agent.sock)" = pmuxbridge
test "$(stat -c %U /run/pmux-pod/agent.sock)" = podshell
echo "socket: $(stat -c '%U:%G %a' /run/pmux-pod/agent.sock)"
if as_other perl /tmp/connect.pl; then echo "FAIL: stranger connected" >&2; exit 1; fi
as_bridge perl /tmp/connect.pl
wait "$server"
echo "PASS"
