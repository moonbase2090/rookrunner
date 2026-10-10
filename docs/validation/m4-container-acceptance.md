# Clean-container acceptance

Date: 2026-10-10. Source commit `44b5fe4dead0c5a2e64989db01f4085a3b83fe5d`.

This is install evidence for the `0.1.0` wheel. It is not a P01–P12 claim and it is not an external-user run. The tag is not signed here, and the wheel and tarball are not published.

## Platform

A fresh `ubuntu:24.04` container, `linux/amd64`. `uname -m` printed `x86_64`. `/etc/os-release` reported `VERSION_ID=24.04` (`24.04.5 LTS`). The container mounts list was empty. The checkout was not mounted. The only file copied in was the wheel.

The interpreter was Python 3.12.3. The install was a new virtualenv at `/opt/accept` and that wheel. Pip installed `execution-core==0.1.0` and `PyYAML==6.0.3`.

## Checksums

`uv build` from the source commit produced these two files. `sha256sum` printed:

```text
51b6ec7a19fb4fe2b5461ffab0336d7c78444e73ca14b23861cd80e9ab0fb2f8  execution_core-0.1.0-py3-none-any.whl
8fe1d30f1a370111dc0c95f64d46fbfb9c4aecca151c3e15d33c9fcd7cd20f59  execution_core-0.1.0.tar.gz
```

The acceptance installed the wheel. The tarball was not installed.

## Worker

The worker command was:

```text
python -m execution_core --state /tmp/state worker --repository /tmp/repo --network none
```

The state directory mode was `0700`. `describe` exited 0 and printed:

```text
{"id":1,"jsonrpc":"2.0","result":{"capabilities":["development.fixture","run.cancel","run.logs","workflow.job"],"limits":{"cursor_characters":1024,"fixture_delay_ms":5000,"fixture_output_bytes":65536,"json_depth":64,"list_page":100,"log_page_bytes":65536,"message_bytes":1048576,"queued_runs":100,"request_id_characters":128,"submission_key_characters":128},"methods":["worker.describe","run.submit","run.get","run.list","run.logs","run.artifacts","artifact.read","run.cancel","run.status","status.view","poll.record"],"protocol_versions":[0,1],"readiness_error":null,"ready":true,"repository":"/tmp/repo","retention":"runs and submission keys retained indefinitely; terminal attempt directories removed","version":"0.1.0","worker_id":"7cdbb586-58e8-40c1-94ac-6bb8bb193372"}}
```

The result version is `0.1.0` and `ready` is true. The worker process was then stopped.
