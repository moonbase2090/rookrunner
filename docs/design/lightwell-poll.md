# Lightwell poll

Status: this is the Mac poll configuration for Lightwell's Linux jobs.
This pull request does not load a launchd agent, does not SSH, and does
not build an image.

The jobs are `.github/workflows/rookrunner.yml` in Lightwell. Each job
uses `ubuntu-latest`. `ci.yml` stays in that repository until cutover.
macOS jobs are not in this configuration.

The poller is the Mac process that already posts checks. It passes the
App key flag. The key file stays on the Mac. A host does not receive a
copy. Neither pass uses a Docker socket flag or opens a listen port.
The interval is 900 seconds. `--image` is omitted, so each place image
must equal that worker's runner image.

Each place image is the lightwell image recorded in that host's unit.
It is not the lunatui image. The documented Rookrunner parent digest
stays out of this configuration.

Lightwell has no `CODEOWNERS` file. The poller records `signoff_absent`
and does not invent a label gate.

Two passes keep separate state directories. The job lists do not
overlap, so the two locks do not submit the same job.

## x86_64

Places, in order: Nexus, then Vertex. There is no Mac place. An x86_64
package job does not fall back to the Mac.

```
poll --repository moonbase2090/lightwell
  --job .github/workflows/rookrunner.yml checks
  --job .github/workflows/rookrunner.yml linux-cli-x86_64
  --job .github/workflows/rookrunner.yml tauri-linux-artifact
  --place {"name":"nexus","ssh":"nexus","state":"/var/lib/rookrunner/state","cap":4,"image":"<nexus lightwell image>"}
  --place {"name":"vertex","ssh":"vertex","state":"/var/lib/rookrunner/state","cap":2,"image":"<vertex lightwell image>"}
```

## aarch64

One place: the Mac worker, with no `ssh` field. The cap is 1. The image
is the Mac arm64 lightwell image.

```
poll --repository moonbase2090/lightwell
  --job .github/workflows/rookrunner.yml linux-cli-aarch64
  --place {"name":"mac","state":"<mac lightwell state>","cap":1,"image":"<mac lightwell image>"}
```
