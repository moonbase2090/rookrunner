# Capacity wait observability proposal

Status: accepted direction, implementation in progress (2026-11-11). The
owner accepted a per-repository read-only report with external scheduled age
checks. Status-page metrics, slot/resource accounting, and cross-repository
fairness remain out of scope.

## Current behavior

The poller already saves capacity-blocked submissions in its repository-bound
`poll.json`. Each wait keeps its submission key, enqueue time, reason, and
request. Later poll passes admit saved work oldest first. The [development
contract](../design/development-contract.md#persistence-pagination-and-limits)
records this behavior.

The status page reads one worker's local state through `status.view`. It does
not read `poll.json` or report poll placement caps. The [status-page design](../design/status-page.md#non-goals)
records that boundary. The [PRD](../prd.md#non-goals) also excludes global
fairness. Each repository has a separate poll state file, so independent
pollers cannot coordinate a cross-repository queue or fairness policy.

## Accepted direction

Add a read-only capacity report to the existing `submissions` operator
command. Read one atomic `poll.json` image through `submissions.load_poll_state`
and report the capacity waits that remain in that image. Keep poll as the only
writer of queue state. Do not copy poll state into worker status.

Example usage:

```sh
python3 -m execution_core --state <poll-state> submissions capacity \
  --alert-after-seconds 900
```

When worker and poll state use different directories, pass the poll directory
with `--poll-state`. The command reads no worker database and contacts no
worker.

The JSON report identifies the configured repository and the read time. It
lists each wait's stable submission key, `since`, age, event, ref, workflow,
and job ID. It includes the queue depth and oldest age. If an age threshold is
provided, the report says whether any wait meets or exceeds it. Exit status 0
means the threshold is clear, 1 means it is exceeded, and 2 means the state or
threshold is invalid. Without a threshold, a valid read reports the queue and
exits 0. A failed read never looks like an empty queue.

The report describes persisted waits for one repository at the time of the
read. It does not prove that polling is active or that a worker has capacity.
An operator's existing monitor can run the command on a schedule and send
notifications. Its cadence sets the detection delay. Missing or failed checks
need a separate monitor alert.

Use the same capacity-wait membership rule as placement: `waiting.reason` is
`capacity`, and the entry has neither `run_id` nor `host`. Share that rule
between the poller and the report. Validate the saved timestamp and queue key
at the state boundary. Return an unknown-state error for malformed
capacity-wait data rather than silently dropping it. Require an aware UTC
observation time. Report whole elapsed seconds and treat a future enqueue
time as unknown, so a clock or state error cannot appear healthy.

The poll pass summary may expose the existing stable key for correlation. It
must describe one final queue snapshot if it reports aggregate depth or age.
An append-only list of waits observed during a pass cannot be used as that
snapshot because a wait may be admitted before the pass ends.

## Alternatives considered

| Shape | Result |
| --- | --- |
| Add age and depth only to each poll result | Smaller change, but reports age only when polling completes. It cannot detect an aging queue while polling is stopped or failing. |
| Read poll state through the worker status page | Rejected under the current design. The poller owns this state, and the page is scoped to one worker. A copied snapshot would be stale and ambiguous when pollers share a worker. |
| Run a resident poll status service | Could evaluate age continuously, but adds a process, access boundary, freshness rules, and deployment work. No such service is accepted. |
| Add a shared scheduler for multiple repositories | Could own cross-repository fairness, but needs shared reservations, project identity, and a chosen policy. The current PRD excludes this milestone. |

## Follow-up boundaries

1. Name the operator monitor and its cadence before claiming that alerts are
   delivered. The command reports a breach; it does not send notifications.
2. Decide whether slot use belongs in a later design. The current place `cap`
   counts queued and running jobs in one poller's view. It is not a CPU, memory,
   or shared-host reservation.

This accepted scope does not authorize changes to the worker status page or
global scheduling.
