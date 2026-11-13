#!/usr/bin/python3
# SPDX-License-Identifier: MIT

"""Read public boot configuration and pass only the credential path to the bridge."""

import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def main():
    config = json.loads(Path("/run/pmux-pod-boot/config.json").read_text())
    url = urlsplit(config["hub_url"])
    if (
        url.scheme != "wss"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("hub_url must be a WSS URL without credentials")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", config["pod_id"]):
        raise ValueError("invalid pod_id")
    token = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "pod-token"
    os.execve(
        "/opt/pod/bin/pmux-mcp",
        [
            "pmux-mcp",
            "--pod",
            "--hub-url",
            config["hub_url"],
            "--pod-id",
            config["pod_id"],
            "--token-file",
            str(token),
            "--agent-socket",
            "/run/pmux-pod/agent.sock",
        ],
        {"PATH": "/usr/bin:/bin", "HOME": "/var/lib/pmuxbridge"},
    )


if __name__ == "__main__":
    main()
