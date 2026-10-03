# Rookrunner manifesto

Status: working principles. They restate accepted direction and the behavior the
code actually implements. Rookrunner is a working title. The final public name,
executable name, license, and domain are undecided.

## Why it exists

Developers and coding agents need build and test results they can trust while
the checkout is still changing. A command tied to a client session can lose its
visible history, or its owner, when that session exits. Local and remote
execution often expose different controls and different result shapes. An exit
code by itself does not identify the source, workflow, and environment that
produced it.

Rookrunner exists so a person or a coding agent can submit work, leave, and
come back to a result bound to identifiable inputs. The result keeps enough
evidence to investigate a failure and to attempt reproduction. Reproduction
still depends on external services, dependencies, and the environment. It is
not a bit-for-bit guarantee.

The first product is local: one worker, one explicitly selected repository, and
a documented protocol. A useful local tool comes before a paid or hosted
service. This repository is independent of Local Actions. It does not import
that project's configuration, credentials, identity, sockets, or operating
procedures.

GitHub Actions workflow syntax is the input to aim at. Rookrunner will own
interpretation, planning, scheduling, action execution, supervision, and result
semantics, and will match documented GitHub behavior in explicit, tested
increments. It will not replace that syntax with a new mandatory workflow
language, and it will not hand execution to another engine.

## Principles

**The result tells the truth.** Success means terminal state `succeeded` and
`exit_code` 0 for the identified input. Queued, running, failed, cancelled, and
lost are different outcomes. Lost means ownership is unresolved. It is not
success, and it is not proof that every process has stopped. A protocol call
that exits 0 only means that call succeeded. Accepting a submission is not
execution success. The development worker never executes fixture text.

**Inputs are identified before they are trusted.** An accepted run must refer
to captured source, not to whatever the checkout says later. The capture
command already stores working-file bytes, modes, links, deletions, and
explicitly included untracked files, and it rejects an input set that changes
while it is being read. That snapshot is not yet a queued run. Workflow
acceptance still has to bind a snapshot before execution. Git metadata for
checkout actions is not captured. The allow-list is written and the copy
is not implemented, so those workflows are not supported.

**Unsupported behavior fails in the open.** Requested execution that this
version cannot perform is rejected before work starts. The worker does not
ignore unsupported fields, fall back to a host shell, or delegate the job to
act. The act pin is a historical record, not the engine and not a silent
fallback.

**The engine is owned. The syntax is not invented.** Compatibility is a product
goal, recorded per behavior as unsupported, implemented, locally validated,
reference-validated, or intentionally different. Local tests do not establish
equivalence to GitHub-hosted execution. Documented GitHub behavior and small
reference fixtures are the specification. Another emulator is not the authority.
No real workflow is executable yet.

**The client may leave.** Accepted work is durable before the reply. The same
submission key and the same normalized input return the same run. A different
input under that key conflicts and does not create a second run. The first
committed terminal result stays authoritative. Interrupted work is not retried
automatically and must not silently run twice. A new attempt is a new run.

**The local machine is the trust boundary.** The state directory and socket
belong to one user. The worker does not listen on the network. Known credential
paths are excluded from capture even when they are tracked, and an explicit
include cannot override that exclusion. Those filename rules are not a
universal secret detector. Secrets stay disabled until a reviewed provisioning
design exists. Workflow output can still contain sensitive data. Universal log
redaction is not promised. Running a trusted repository is not a sandbox for
hostile code.

**Limits are part of the contract.** Requests, queue depth, log pages, and
capture size are bounded. Clients page logs instead of loading the whole
stream. One worker binds one repository. The development scheduler runs one
fixture at a time. This is not a distributed scheduler, and it is not an
unattended release: development state is retained indefinitely. A disk budget
refuses new submissions when that state would exceed it and does not prune
active evidence.

**Provenance stays visible.** The runtime uses the Python standard library.
Development-only tools are locked and inventoried. Third-party code, runner
images, and licenses are reviewed before they are bundled or executed. Proposed
design, accepted direction, and implemented behavior stay in separate
documents, and a validation record says what was actually run.
