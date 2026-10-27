# Shared cache trust and retention investigation

Status: research, 2026-10-23. No cache design is accepted by this note.
Shared caches remain a [later investigation](../roadmap.md#later-investigations);
`actions/cache` support remains [paused](../roadmap.md#planned-paused).
Remote execution and serving unrelated customers require separate designs.

The proposed starting point is an optional cache within one repository and
explicit trust boundaries. A cache hit supplies reusable bytes, never evidence
that a run succeeded. Missing or evicted entries must leave a cold build possible.

## Implemented behavior

These observations refer to source at
`e51b9fc640ad0a039411093ce0305d5b22eb0f4d`.

| Surface | Current behavior and source |
| --- | --- |
| Action reuse | [`ActionStore`](../../src/execution_core/actions.py) fetches full commit pins, stores trees by content digest, and rehashes them before reuse. Node actions receive attempt copies, not the store mount. Its lock is a process-local `RLock`; it is not a shared cache service. |
| Artifacts | [`named_manifest`](../../src/execution_core/artifacts.py) records workspace file sizes and digests, including explicit upload selections. `artifact.read` in [`worker.py`](../../src/execution_core/worker.py) checks the stored identity before reading workspace bytes. `_release_finished_attempt` removes the attempt directory after terminal cleanup is confirmed. Metadata can therefore outlive readable bytes. The [owned upload step](../design/upload-artifact.md) records files rather than building an archive. |
| Storage | [`disk.py`](../../src/execution_core/disk.py) defines a default state budget of `10 * 1024 * 1024 * 1024` bytes. `_prune_finished_run_folders` in `worker.py` applies the 90-day age and configurable count policy, default 100, then removes eligible oldest folders under pressure. Active runs, unresolved cleanup, and workflow snapshots awaiting a terminal status post are protected. Run records and submission keys remain. This is run-folder retention, not a dependency-cache policy. |
| Workflow support | [`plan.py`](../../src/execution_core/plan.py) leaves `cache-mode` unsupported; [`expr.py`](../../src/execution_core/expr.py) rejects `hashFiles`. Action-tree reuse and artifact naming establish neither the cache APIs nor `actions/cache` compatibility. |

## Proposed trust boundaries

A cache identity would contain an owner/repository identity, a trust namespace,
an exact ref or pull-request scope, a format version, an environment fingerprint,
and a caller key. The fingerprint should distinguish OS, architecture, runner
image and relevant toolchain/dependency inputs. The worker must derive identity
and authorization from validated submission context, not a key or ref supplied
by job code. A matching digest proves byte identity, not a trustworthy producer.

Proposed access rules:

| Producer scope | Restore | Publish |
| --- | --- | --- |
| Trusted default-branch run | Its authorized repository scope | That scope, after verified success |
| Other branch or pull request | Its own scope; optionally a specifically permitted trusted base scope | Its isolated scope only |
| Explicitly overridden untrusted run | No shared access initially | No shared access initially |

A branch name or default-branch event alone must not grant trusted publication.
Validate the producer, workflow and executed source. Never promote a pull-request
entry into a trusted scope merely because its key matches. Cross-repository
reads and writes would be denied initially. Deduplicating physical blobs would
not grant access to their metadata, contents or existence across namespaces.

GitHub's [cache reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)
describes branch and pull-request merge-ref scopes, and warns that base caches
can be readable by pull requests. Its
[security guidance](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#best-practices-for-using-caches-securely)
recommends trusted producers and excluding secrets. These are useful comparisons,
not a compatibility contract for the proposed rules above.

Restore would copy verified bytes into an attempt-owned allowed directory.
Jobs would never receive a writable shared-store mount. Capture and extraction
would reject escaping paths, links to external files, special files, and
unbounded expansion. Credentials, worker state and secret-bearing directories
would be excluded. Path exclusions cannot detect arbitrary secrets embedded in
ordinary files, so sharing still requires an operator-approved content policy.
Dependency integrity checks remain necessary when consuming cached executables.

## Proposed publication and retries

Each writer would stage its own complete entry before attempt cleanup, with a
manifest, content digest, producer run and source identities. Staged entries
would remain invisible until the run has terminal `succeeded` and `exit_code=0`.
Publication would verify the bytes and authorize the scope again, then commit
the key-to-digest binding atomically. A rename alone does not make blob storage
and a separate index transactional.

The first committed binding would win. Retrying the same operation with the
same digest would return that binding; a different digest for the same identity
would report a conflict and leave existing bytes unchanged. Cancellation,
crashes and lost replies must not expose partial entries. Recovery would reclaim
abandoned staging and unreferenced blobs without deleting committed entries.
Readers would pin entries until their private copy finishes, so eviction cannot
remove files mid-restore. Prefix fallback, if later allowed, must stay within
the same authorized scopes and format constraints.

A miss, corruption or save conflict would be reported separately from job
success. An optional cache failure would normally fall back to a cold build;
it must never turn a failed or unknown run into success.

## Proposed retention choices and unresolved decisions

| Choice | Benefit | Cost or open question |
| --- | --- | --- |
| Least recently used eviction | Keeps frequently restored entries | Reads update accounting; popularity alone can retain stale bytes |
| Maximum age | Bounds lifetime independently of popularity | Rebuilds useful entries; select whether age starts at publication or access |
| Per-scope and repository byte budgets | Contains one producer's storage growth | Must reserve staging space and account for deduplicated blobs without double charging |

Prefer a separate cache budget and eviction accounting so disposable cache
entries cannot displace protected execution evidence. No byte quota, age,
entry-size limit or reservation policy is selected here. Cache deletion must
remove authorization bindings before reclaiming blobs and respect active reader
pins. Run evidence and submission-key retention remain separate concerns.

Decisions still required before implementation:

- Local storage ownership versus a service protocol, including authentication
  if storage crosses machines. Unrelated customers need a separate threat model.
- Producer trust derivation, base-scope reads, and whether secret-enabled jobs
  may publish any reusable content.
- Manifest/format versioning, environment fingerprint inputs, exact-key
  conflicts, and whether to permit prefix or cross-OS restores.
- Quotas, staging reservations, eviction order, expiry, reader-pin recovery,
  revocation and operator deletion.
- A distinct capability and failure contract for cache APIs and workflow
  syntax. Supporting one action runtime does not provide its backing service.

Any later implementation needs adversarial fixtures for poisoned keys,
unauthorized scope access, malicious archives, concurrent writers, interrupted
publication and eviction during restore. This investigation enables none of
those runtime features.
