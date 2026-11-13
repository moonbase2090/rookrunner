#!/bin/bash
set -euo pipefail
umask 022

test "$(id -u)" = 0
test "$(uname -m)" = x86_64
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04
: "${POD_PAYLOAD_SHA256:?Supply the reviewed payload SHA-256}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates clang cmake curl git jq libclang-dev \
    libssl-dev lld ninja-build openssh-client pkg-config python3 python3-venv \
    sqlite3 unzip xz-utils zlib1g-dev

python3 /tmp/pod-image/payload.py install /tmp/pod-payload.tar.gz "$POD_PAYLOAD_SHA256"
groupadd --system pmuxbridge
useradd --system --gid pmuxbridge --home-dir /var/lib/pmuxbridge --create-home \
    --shell /usr/sbin/nologin pmuxbridge
useradd --create-home --shell /bin/bash podshell
install -d -o root -g root -m 0755 /usr/local/libexec /etc/pmux-pod
install -m 0755 /tmp/pod-image/verify-installed.sh /usr/local/libexec/pod-verify
install -m 0755 /tmp/pod-image/cache-proof.sh /usr/local/libexec/pod-cache-proof
install -m 0755 /tmp/pod-image/start-bridge.py /usr/local/libexec/pod-start-bridge
install -m 0644 /tmp/pod-image/pod.env /etc/profile.d/pod.sh
install -m 0644 /tmp/pod-image/systemd/*.service /etc/systemd/system/
cp -R /tmp/pod-image/cache-probes /opt/pod/cache-probes
install -d -o podshell -g podshell -m 0755 /var/cache/pod /work
cp -a /opt/pod/cache/. /var/cache/pod/
chown -R podshell:podshell /var/cache/pod
dpkg-query -W -f='${Package}\t${Version}\n' > /opt/pod/packages.tsv
printf '%s\n' "$POD_PAYLOAD_SHA256" > /opt/pod/payload.sha256

systemctl daemon-reload
systemctl enable pod-mux.service pod-agent.service pod-bridge.service
/usr/local/libexec/pod-verify
runuser -u podshell -- /usr/local/libexec/pod-cache-proof

# Leave no builder login, boot data, or persistent runtime credentials in the AMI.
cloud-init clean --logs --seed
find /root /home /var/lib -name authorized_keys -type f -delete
rm -f /etc/ssh/ssh_host_* /root/.bash_history /home/ubuntu/.bash_history
rm -rf /tmp/pod-image /tmp/pod-payload.tar.gz /var/lib/pmuxbridge/*
apt-get clean
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
