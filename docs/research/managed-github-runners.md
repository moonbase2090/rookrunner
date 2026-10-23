# Managed GitHub runners: registration and webhook delivery

Status: proposed investigation, 2026-10-23. No integration is accepted or
implemented by this note. Sources were read on that date. This is a study of
GitHub.com, not a claim about every GitHub Enterprise Server version.

## Relationship to the local worker

The accepted [owned-engine decision](../decisions/0003-owned-execution-engine.md)
remains in force. Rookrunner owns local workflow interpretation and execution.
The [owner CI path](../design/owner-ci.md) polls configured repositories and
reports results through outbound HTTPS. It does not register as a GitHub
runner. The [architecture](../design/architecture.md#local-access) separates
private remote execution from serving unrelated customers.

A future official-runner integration would be a separate execution path.
GitHub would dispatch jobs to `actions/runner`; a capacity controller would
manage machines around it. Registering a runner does not make Rookrunner's
owned engine implement GitHub's runner protocol. The earlier
[engine survey](actions-runner-requirements.md) addresses compatibility scope.

## Documented GitHub lifecycle

GitHub routes jobs by matching runner labels and groups. An assigned job is
requeued if its runner does not pick it up within 60 seconds. A job with no
matching runner can remain queued for 24 hours. Runner communication uses
outbound HTTPS on port 443. An inbound webhook receiver is a separate service.
See the [self-hosted runner reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

The runner's [authentication design at an identified revision](https://github.com/actions/runner/blob/e2251bac60fe76e92fbbb861ab1215ff530e0871/docs/design/auth.md)
describes registration creating a local RSA key pair, followed by a listener
that obtains a token and receives messages through HTTP long polling. A
worker subprocess receives the job message. This is upstream design evidence,
not a stable protocol specification or a live interoperability test.

GitHub's [webhook event reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads#workflow_job)
requires Actions repository read permission for an App subscribing to
`workflow_job`. The [runner reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#webhooks-for-autoscaling)
describes `queued`, `in_progress`, and `completed` notifications for autoscaling.
A completion notification includes unsuccessful jobs. It is not proof of success.

### Registration and credential roles

The [runner REST reference](https://docs.github.com/en/rest/actions/self-hosted-runners)
documents these GitHub App permissions and endpoints:

| Scope | Registration endpoint | Required permission |
| --- | --- | --- |
| Repository | `POST /repos/{owner}/{repo}/actions/runners/registration-token` | [Repository Administration write](https://docs.github.com/en/rest/actions/self-hosted-runners#fine-grained-access-tokens-for-create-a-registration-token-for-a-repository) |
| Organization | `POST /orgs/{org}/actions/runners/registration-token` | [Organization Self-hosted runners write](https://docs.github.com/en/rest/actions/self-hosted-runners#fine-grained-access-tokens-for-create-a-registration-token-for-an-organization) |

Both linked permission sections explicitly support GitHub App installation
access tokens. These permissions authorize the management API call, not the
job's `GITHUB_TOKEN`.

Registration tokens expire after one hour. Removal tokens also expire after
one hour. Each scope has `generate-jitconfig`, runner listing, and runner
deletion endpoints. JIT configuration takes a runner name, runner group ID,
labels, and an optional work folder, returning `encoded_jit_config`.

The following separation is a proposed credential design, not current behavior:

| Credential | Proposed holder and purpose |
| --- | --- |
| App private key and installation token | Controller only, to call authorized management APIs |
| Registration token or encoded JIT configuration | One bootstrap process, with no value in logs or durable run metadata |
| Runner identity on disk | One disposable runner environment, destroyed with that environment |
| Job credentials | Official runner and the assigned job, under GitHub's job permissions |
| Webhook secret | Receiver only, to verify deliveries |

The current result-reporting App permissions do not authorize runner management.
A future integration needs an explicit permission and installation decision.
Prefer a dedicated management App as a proposal. Do not place its management
credentials inside a job environment.

### Webhook delivery and recovery

GitHub documents [HMAC-SHA256 validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
of the original request body against `X-Hub-Signature-256`, using a webhook
secret and a constant-time comparison. Authentication must precede processing.
It does not establish that the installation or repository is authorized to
consume this service's capacity.

[Webhook best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)
call for HTTPS, event and action checks, and a 2XX response within 10 seconds.
The [delivery-header section](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks#use-the-x-github-delivery-header)
states that `X-GitHub-Delivery` identifies a delivery and that a requested
redelivery retains the original identifier. GitHub [does not automatically retry failed deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries).
Delivery APIs support inspection and explicit redelivery.

A proposed receiver would validate a bounded payload, authorize its installation
and repository, and durably enqueue it before acknowledging. Provisioning would
run asynchronously. Persist delivery identity separately from job identity.
Duplicate deliveries must not create additional capacity for the same demand.

Treat notifications as hints to reconcile demand. Recheck the job before
provisioning, tolerate delayed or reordered events, and recover after receiver
or controller restarts. GitHub's [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#get-a-job-for-a-workflow-run)
exposes job state, conclusion, run identity, labels, and runner identity.
Repository and job identity must remain scoped to an authorized installation.
Bound reconciliation, redelivery, and provisioning retries, and respect API
rate limits. These are proposed reliability requirements, not tested guarantees.

## Isolation and operations

GitHub's [secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners)
warns that workflow code can compromise a self-hosted runner and reach its
secrets and network. JIT runners process at most one job, but reused hardware
still needs a clean environment. Runner deregistration is not machine erasure.
[Runner groups](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access)
restrict repository access. Labels select capacity and are not a substitute
for those access policies.

Proposed isolation requirements are separate from the local Docker trust model:

- Use a fresh VM per job and isolate its network, disks, caches, and credentials.
- Keep controller credentials and infrastructure management endpoints unreachable
  from jobs. Define trusted repository and workflow admission before registration.
- Preserve diagnostic logs outside the disposable environment with access and
  retention controls. Destroy the environment even after a job or controller crash.
- Reconcile machine identity, runner registration, job ownership, and cleanup.
  An API deletion response alone cannot prove the machine stopped executing.
- Keep GitHub conclusions separate from local run records. A queued job,
  registration response, or completed notification cannot produce local
  `succeeded`. That state still requires `exit_code=0` for identified inputs.

GitHub [recommends ephemeral runners for autoscaling](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling)
and external diagnostic logs. Runner software normally updates automatically.
With automatic updates disabled, updates are required within 30 days of a
release, and critical security updates can stop assignment sooner. Image
patching and runner version management therefore need an owner and recovery plan.

## Proposed options and tradeoffs

These are alternatives to investigate, not accepted delivery scope.

| Option | Benefit | Cost or limitation |
| --- | --- | --- |
| Continue local owner CI | Keeps the existing owned engine and outbound polling path | Does not accept GitHub-dispatched runner jobs |
| Official runner with a small JIT controller | Separates GitHub execution semantics from machine lifecycle | Requires webhook recovery, admission, provisioning, cleanup, and credential operations |
| Actions Runner Controller | Uses GitHub's recommended Kubernetes autoscaling implementation | Introduces Kubernetes and its operating responsibilities |
| Runner Scale Set Client | Offers a supported starting point for custom scaling outside Kubernetes | Still requires a controller and an infrastructure lifecycle |

GitHub documents [ARC's Kubernetes role](https://docs.github.com/en/actions/concepts/runners/actions-runner-controller)
and the [Scale Set Client alternative](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#actions-runner-controller).
Before selecting a custom webhook controller, compare those existing lifecycle
implementations. No cost, latency, availability, or compatibility measurements
were made. No option is selected by this note.

## Provenance and licensing

The [official runner license at the inspected revision](https://github.com/actions/runner/blob/e2251bac60fe76e92fbbb861ab1215ff530e0871/LICENSE)
is MIT. Redistribution must preserve its copyright and permission notice.
That finding does not clear bundled runtimes, image packages, or actions.
Before any import, inventory the exact release archive and image, retain
applicable third-party notices, and verify source provenance and checksums.
This investigation imports no code, binaries, dependencies, or configuration.

GitHub service access is separate from the runner's source license.
The [additional product terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
apply Actions restrictions even with self-hosted runners. A future customer
service needs a separate terms and licensing review for its actual offering.
This note does not establish permission to resell a service or redistribute
every component of a runner image.

## Decisions and evidence needed before implementation

The proposed next milestone is a design decision for one owner's trusted
repositories. It must choose scope, controller approach, App permissions,
job admission, isolation, and result ownership. Remote workers and unrelated
customers remain separate milestones.

If that milestone is accepted, its validation plan must exercise duplicate,
missing, delayed, and invalid webhook deliveries, expired bootstrap credentials,
revoked installation access, cancellation during startup, controller restart,
orphan cleanup, and terminal success and failure. Record the exact runner
release, image digest, GitHub job identity, and cleanup evidence. Run behavior
changes in a clean VM under the repository review policy.

Validation of this investigation is limited to primary-source review, relative
documentation links, diff review, and the release-text check. No runner was
registered, job dispatched, listener started, or infrastructure provisioned.
