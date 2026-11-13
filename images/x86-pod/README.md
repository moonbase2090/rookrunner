# x86 pod image

Status: image definition. No cloud image has been baked or launched from this
recipe. The pod bridge and hub are separate integrations. Their runtime
acceptance is pending. This image does not change the existing activity endpoint.

The recipe targets Ubuntu 24.04 x86_64 on an On-Demand `m7i.large` in
`us-west-2`. It installs headless terminal services, caller-supplied client CLIs,
Node and Rust toolchains, native build tools, and warm Cargo and npm caches.
The services are enabled at boot. The bridge starts when boot configuration and
credentials arrive. There is no feature flag to enable the pod services.

## Prepare the payload

Produce one `payload.tar.gz` from clean build outputs. Do not archive a home
directory, workstation cache, authenticated package-manager configuration, or
an existing execution state. The payload is an operator-local input. Do not
commit or publish it from this repository.

The archive has the following paths, relative to its root:

| Path | Contents |
| --- | --- |
| `bin/pmux`, `bin/pmuxd`, `bin/pmux-mcp` | Reviewed Linux x86_64 executables, including pod bridge and local command modes |
| `toolchains/node/` | A pinned Node distribution with `bin/node` and `bin/npm` |
| `toolchains/rust/` | A pinned standalone Rust installation with `bin/cargo`, `bin/rustc`, and its target libraries |
| `clients/bin/` | The approved client CLI roster and launchers, with supporting files under `clients/` |
| `cache/cargo/registry/` | Public registry index and crate archives fetched from reviewed, locked project inputs |
| `cache/npm/_cacache/` | Package content fetched from reviewed lockfiles without authentication |
| `licenses/` | Upstream licenses and notices for every bundled component and dependency |
| `manifest.json` | Component versions, provenance, and notice paths |

Install Node and Rust into these prefixes in a clean Linux x86_64 builder.
Package the approved clients with their dependencies already installed. Use
relative launchers and symlinks within the payload. No downloader runs at pod
boot. Generate caches in an empty `CARGO_HOME` and npm cache, using `cargo fetch
--locked` and `npm ci --ignore-scripts` against the selected project lockfiles.
Also fetch the dependencies in this recipe's `cache-probes` lockfiles into those
caches. The installer builds and runs both probes offline as the shell account.
Review registry URLs and notices before copying only the listed cache trees.
Do not put private registry or Git credentials into those trees.

The manifest format is:

```json
{
  "version": 1,
  "architecture": "x86_64",
  "components": [
    {
      "group": "mux",
      "name": "terminal-tools",
      "version": "REVIEWED_VERSION_OR_COMMIT",
      "source": "https://example.com/source/REVIEWED_COMMIT",
      "license": "MIT",
      "notice": "licenses/terminal-tools/LICENSE"
    }
  ]
}
```

Include at least one component in each group: `mux`, `node`, `rust`, `clients`,
and `cache`. List each client separately. Record exact versions or source
commits, public source URLs, license identifiers, and included notice paths.
The example is a format illustration, not a usable manifest or a license grant.
Check each upstream redistribution obligation before producing a bundle.
The terminal tools use MIT, but that does not cover bundled dependencies or clients.

Compute and review the complete archive digest. Verify before any cloud operation:

```bash
python3 images/x86-pod/payload.py verify /path/to/payload.tar.gz SHA256
```

The verifier checks that digest, architecture headers, required executables,
cache presence, component provenance, notice files, and archive paths. It rejects
known credential paths, privileged modes, special files, and escaping links.
These checks do not establish the contents or redistribution rights of arbitrary
input files. Review the clean build inputs as well as the digest.

## Validate the definition locally

Packer initialization downloads its pinned provider plugin. Validation does not
launch instances or create images:

```bash
packer init images/x86-pod
packer fmt -check images/x86-pod
packer validate -syntax-only images/x86-pod
PYTHONPATH=src:tests python3 -m unittest test_pod_payload
bash -n images/x86-pod/install.sh images/x86-pod/verify-installed.sh
```

The archive tests exercise validation and extraction. Their architecture-header
fixtures are not working executables and are not runtime proof of the pod tools.

## Bake after approval

Live baking remains on hold until the coordinator confirms that the hub PR exists
and routes the bake. `cloud_build_authorized` defaults to false and fails normal
Packer validation. Set it to true only for that authorized operation.

Supply the reviewed Ubuntu AMI ID, subnet, existing security group, archive path,
and archive SHA-256 as local Packer variables. The security group admits private
SSH only from the bake controller. The builder needs outbound HTTPS for Ubuntu
packages. Do not put tokens in Packer
variables, user data, tags, provisioner environments, or image files. The builder
does not need a runtime instance profile. Use short-lived builder credentials
outside the guest through the normal AWS credential chain.

The definition pins the base AMI and payload digest. Ubuntu packages resolve at
bake time. `/opt/pod/packages.tsv` records the resolved versions, and
`/opt/pod/manifest.json` plus `payload.sha256` identify the tool inputs. This is
not a claim of bit-for-bit rebuilds. The image uses an encrypted 40 GiB gp3 root
volume, deletes it on termination, and requires IMDSv2. Images are not public.

The provisioner runs `pod-verify` and `pod-cache-proof` before image capture.
The cache proof installs the locked npm probe with `--offline` and compiles the
locked Cargo probe with `--offline`; their outputs must be `1.2.3` and `42`.
It then removes builder
authorized keys, host keys, cloud-init boot data, shell histories, and the staged
payload. It clears the machine ID. Pod services are enabled but are not started
during the bake. No cloud resources or published packages are created by the
local verification commands above.

## Supply credentials at first boot

Deliver the runtime token after launch to `/run/pmux-pod-boot/pod-token`. That
directory is tmpfs. Create it as root with group `pmuxbridge` and mode `0750`.
The token is a root-owned regular file with mode `0400`. Never store it in user
data, the payload, or the captured image. Deliver client authentication by a
separate scoped boot mechanism. This recipe installs no authenticated client
configuration.

Write `/run/pmux-pod-boot/config.json`, root-owned with group `pmuxbridge` and
mode `0640`, with these public fields:

```json
{"hub_url": "wss://hub.example.com/pods/connect", "pod_id": "pod-01"}
```

Start `pod-bridge.service` after both files exist. If the boot injector runs
before the boot target, the enabled service starts normally. If it runs later,
it must explicitly start the service. The URL must not contain credentials.
The installer does not create DNS, a tunnel, an edge service, or a hub.

The accounts and services are separated as follows:

| Service | Account | Access |
| --- | --- | --- |
| `pod-mux` | `podshell` | Private same-user pmux socket, PTYs, workspace, and caches |
| `pod-agent` | `podshell`, group `pmuxbridge` | pmux commands, shell commands, and shared local agent socket |
| `pod-bridge` | `pmuxbridge` | Outbound hub session, local agent socket, and its systemd credential |

The bridge receives the token through systemd `LoadCredential`. Its launcher
passes the credential path, with a scrubbed environment. Neither pmuxd nor the
local agent has that credential. The shell account has no sudo or Docker group
membership. Systemd hides the bridge credential directory from those services.
The private pmux socket retains same-user authentication. Only the local agent
shares a socket with the bridge. The agent must authenticate the bridge's UID.

Run `pod-verify` and `pod-cache-proof` as `podshell` before token injection.
After injection, runtime acceptance must
exercise registration, activity, send, mail, shell execution as `podshell`, token
read denial from both shell paths, reconnect, and the hub kill switch. It must
also build the selected locked projects with network disabled to prove the warm
caches serve their intended workload. Cache presence alone does not prove that.
The local recipe tests do not replace this integrated acceptance.

The service contract is coordinated separately with the pod-mode implementation.
Its final artifact and command-line compatibility must pass acceptance before
the image is used. Keep this definition separate from the workflow job-container
image under `images/ubuntu-runner`.

Reference: [Packer Amazon EBS builder](https://developer.hashicorp.com/packer/integrations/hashicorp/amazon/latest/components/builder/ebs)
and [systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#LoadCredential=).
