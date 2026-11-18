# Staged model loading: contract and activation boundary

This extension is an opt-in protocol within the existing single host writer. It
is not enabled in a production host by installing the code. The current BACH
Ollama 0.33.3 port advertises `supports_load_pending = False`: the required native
memory attribution is unavailable. `ConfiguredHost` rejects an attempted staged
load configuration for this port before creating a service endpoint or allowing
a load POST. The ordinary full-profile admission path and registered cooperative
CPU jobs retain their existing rules.

## Admission, lifetime and ownership

A model executor may additionally name `load_policy_sha256`. A client may request
`allow_model_load: true`, but must also have a bounded native request lease,
interactive permission and the exact model socket in its scope. Both additions
are absent/disabled by default. The native port's capability is code-owned; JSON
configuration cannot invent a memory sensor or its proof. Clients cannot submit
RAM amounts, profiles, process IDs, measurements or a resident-memory credit.

The new operations use the same authenticated, authority-bound loopback API:

| Operation | Exact payload | Effect |
| --- | --- | --- |
| `submit_model_load` | `request`, `ttl_seconds` | Queue one authorized model load; a second live load request for the same socket is rejected. |
| `begin_model_load` | `grant`, `turn_id` | Persist the exact possible-load intent before acknowledging permission to POST. |
| `complete_model_load` | `grant`, `turn_id`, `successful` | Request native proof or strict abort; the boolean is not a RAM-release or success proof. |

The existing lease renewal and cancellation API is unchanged. Normal
`submit_leased` cannot opt into the cold-start threshold. An idempotency retry
cannot change a request between ordinary and staged admission. CPU executors
cannot use this protocol, even through an otherwise privileged client.

| Durable phase | Externally visible state | Permissions |
| --- | --- | --- |
| `PREPARED` | `LOAD_PENDING` | Exclusive writer reservation at the full reviewed peak; no inference or competing grant. |
| `MAY_LOAD` | `LOAD_PENDING` | Exactly one attempt may use the existing supervised warmup and inference owner. |
| `VERIFIED` | `RUNNING` | The native port has committed a fresh independent proof under the inference owner. Normal turns may reuse this owned socket. |
| `ABORTED` | `RETURN_REQUESTED` | Native guarded cleanup is confirmed; the existing checkpoint/exit/new-capacity protocol still has to release the grant. |
| `RECOVERY_HOLD` | `RECOVERY_HOLD` | No new turn, inferred release, second load or replacement writer adoption. |

The journal binds the writer/grant generation, request, socket, execution,
load-attempt ID, exact model/digest/context/port, policy SHA, profile SHA, full
RAM/CPU envelope, process creation/boot identity, baseline, deadline and final
proof. It is fsynced before `begin_model_load` returns. The pending state and
turn ID remain visible to authenticated native status/readback for diagnosis.
A terminal attempt fences a delayed or duplicated begin. A completed proof can
be acknowledged idempotently after a lost HTTP reply.

## Original cold-start policy and the full peak

The reviewed v1 policy remains: minimum available RAM 6 GiB, hard floor 2 GiB,
maximum pressure level 2, maximum swap growth 8 GiB and load duration 300 seconds.
The BACH adapter pins the existing policy bytes with SHA256
`e638d2f4a4423fb543b3dece6e46f6b8689c21ef6e15d6e1c25ccb164a1eae9b` and rechecks the
actual runtime warmup capability, model binding and process before POST. Its
normal monitor remains responsible for stopping a load that crosses those
limits. The pending deadline includes admission-to-load delay and cannot extend
the original monitor window. CPU, disk and the normal system reserve also need
fresh native observations; pressure level 2 is an exception for the pending
model phase only.

Staged admission requires `ocean-model-measurement.v2`, with method
`ollama-ps-native-rss-and-cpu-v2`. It retains every v1 identity, time, CPU,
unload-ACK and absence field and additionally records `peak_process_rss_bytes`,
`process_identity_verified` and `measured_processes` (PID, creation time and boot
time for each measured process). The full profile must cover the greater of the
API peak and the measured process-tree RSS peak, plus a positive margin. Reviewed
file hashes and actual evidence are still required; the boolean does not approve
an arbitrary file. V1 profiles remain valid for the existing full-capacity path,
but cannot enable staged loading.

## Completion accounting

A successful transport response is necessary and insufficient. Under a fresh
HostInferenceGate owner, the native port independently checks the exact server
version, unchanged listener/process identity, one exact model/digest, exact
context, native process observation, original policy and fresh normal capacity.
A second/aliased model, a changed process, unknown values, expired evidence or
pressure above normal refuses promotion. The arbiter commits the verified state
while that same inference owner is still held.

Let `P` be the unchanged full profile, `R` the system reserve, `A` the native
available RAM, `C` a proved model-specific resident lower bound and `H` the
normal KV-plus-overhead requirement. Promotion needs:

```
A - R >= max(P - C, H)
```

It also requires the independent normal BACH reserve observation. `C` is zero
unless a trusted native source proves private, non-reclaimable, model-attributable
physical pages excluded from this same host-capacity observation. The proof is
bound to model workload, process identity, increasing native sample sequence and
fresh timestamp. It cannot exceed either the model API size or the observed
process RSS. API size, Metal/GPU allocation, total RSS, USS, compressed memory or
physical-footprint accounting alone do not establish this contract. Shared or
reclaimable pages cannot be counted twice as both available and already resident.
The current BACH port deliberately supplies no such credit and cannot activate
staged loading.

## Failure and locking boundaries

The controller transition lock serializes native lifecycle changes. Native ports
never execute while holding the controller state mutex. The load-proof order is
controller transition, native inference owner, then short local arbiter/journal
operations. No arbiter callback waits for an inference owner, and no callback
under the inference owner performs controller HTTP. BACH obtains the resource
permit before entering its inference turn and completes it after leaving that
turn. The independent host proof takes its own inference turn and commits under
that owner. This prevents circular controller/HostInferenceGate waits.

Submission and begin transports are retained and shielded across repeated async
cancellation. Cleanup joins them, uses the exact known native request/attempt,
and handles lost responses through owned readback/idempotent completion. A client
TTL, host heartbeat expiry or load deadline never proves RAM release. An expired
possible-load intent enters recovery hold. A cancelled, still-prepared request
can return only after native absence and the existing release protocol.

The BACH staged path never uses the legacy warmup unload helper. Strict abort
checks the expected process under `inference.lock`, writes `unload-pending`
before its single POST, requires terminal `done=true, done_reason=unload`, exact
absence and unchanged process, and then clears the intent durably. An unresolved
inference dispatch blocks entry; an uncertain unload retains its intent. A crash
journal blocks a replacement writer. Do not clear files, lower the profile, treat
a deadline as proof, or start a parallel CPU authority beside an unbrokered model
as a recovery shortcut.

## Acceptance still required for production

1. Independently review the staged protocol, cancellation/ACK-loss tests and BACH
   control-fingerprint changes.
2. Establish a reviewed kernel/provider attribution contract for this exact
   model's private physical pages, including compressed/shared/reclaimable/Metal
   behavior and the native available-memory definition. No size heuristic.
3. Capture a fresh full-context workload profile and honest peak with CPU and
   process provenance. An old idle or cold-load observation is insufficient.
4. In a separately authorized activation window, verify existing model absence,
   no unresolved intents, one host authority, all model consumers using the same
   gate/broker, and only registered cooperative compute clients.
5. Validate supervised load, ordinary reuse, cancellation/crash hold, strict
   return and checkpointed compute resume on approved fixtures before live use.

There is no automatic activation, profile generation/approval, model load,
compute pause, restart reconciliation or production configuration in this patch.
