# macOS process supervision

## Implemented path

`dvd_research/process_supervisor.c` is the direct parent for local research
services. It places the child in a fresh process group, retains the child as an
unreaped child while observing it with native `waitid(WNOWAIT)`, and accepts a
stop request through a mode-0600 FIFO named by a mode-0600 run identity file.
It verifies that the child leads the new process group before using group
signals and records that result; if verification fails, it only signals the
direct child while it remains waitable.
Callers identify a run record; they never submit a PID for the supervisor to
signal. The parent checks child exit and sends group signals in one serialized
loop, then reaps the child. This keeps a recycled PID from becoming the target
of a stale run's signal.

`research/owned_process.sh` compiles the helper with Clang or the system C
compiler when its source is newer than the ignored local binary. Research scenario runners, the
top-level Antoine launcher, the Antoine/Gazebo/NATS proof services and bounded
probe commands, the optional signed sensor forwarder, and the research console
use this supervisor. The console and scenario stop paths request shutdown by
run identity. Python's process identity module uses pidfds where available and
now refuses signal delivery when there is no atomic kernel handle.

The external `scripts/gz_flight_proof.sh` launcher also starts every service
and bounded probe through this supervisor. Its cleanup uses run-local identity
files instead of a `ps`-then-`kill` check; launch evidence continues to record
the supervised command's child PID. The interactive MAVLink runner exports the
compiled supervisor path before invoking the proof launcher.

The supervisor inherits the process environment directly and does not write it
to disk. Run identities contain only supervisor/child PIDs and the control FIFO
path; they are private to the current user. The external Antoine proof runner
requires the checkout-owned `DVD_PROCESS_SUPERVISOR` path.

## Limits and remaining evidence

- These guarantees apply to processes started through this helper. A child that
  deliberately creates a new session/process group can escape its run group.
- If the supervisor itself is forcibly killed (for example, `SIGKILL` or a
  machine crash), it cannot run cleanup; a child may outlive it. Orphan cleanup
  and stale FIFO recovery need explicit operational handling.
- This helper is native Darwin code. On Linux, its `waitid(WNOWAIT)` path is
  still available, while independent Python signaling uses pidfds where the
  kernel and Python expose them.
- `research/test_process_supervisor.sh` has passed on macOS. It verifies
  replaced-identity refusal, a stop-versus-exit race, descendant cleanup, and
  removal of run identity/control files. The external Antoine bounded-wait
  regression also passes with the native supervisor and confirms timed-out
  probes and their pipeline descendants do not survive.
- The full multi-service Antoine proof lifecycle has not been rerun under the
  migrated launcher. Linux pidfd and no-pidfd checks also remain outstanding.
- The legacy `process_identity` capture/match format remains for inspection and
  provenance; it is not a safe macOS signaling mechanism.

## Acceptance evidence still required

- Exercise the full Antoine proof stop path on the supported macOS host; verify
  no runner-owned process remains.
- Exercise Linux with pidfds and on a kernel without pidfds; the latter must
  fail closed instead of falling back to `ps` followed by `kill`.
