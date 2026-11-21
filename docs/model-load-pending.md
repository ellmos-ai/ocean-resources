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

The begin reply also carries a native writer-readback reference. A copied
`MAY_LOAD` reply is not dispatch authority. Under its actual inference owner,
BACH must re-read the original private writer inode and prove its exclusive
native lock, live PID/creation/boot identity, current writer epoch, exact
grant/request/turn/execution, current client lease, full profile and policy.
Profile/evidence files are revalidated and the journal is read again after that
potentially slow work. Torn, replaced, unlocked, stale or terminal records close
the path. Client cancellation and pending host heartbeats are journaled before
acknowledgement. Closing a writer durably retires its epoch before releasing its
native lock, preserving unresolved reservations. Retirement I/O failure retains
the native lock and refuses further writer work until an explicit successful
close retry or process exit. A close/reopen in the same PID cannot revive an old
permit, including the interval before the replacement publishes its new epoch.
The native reader currently supports POSIX inode/flock semantics; a Windows
staged caller remains closed until an equivalent reviewed native reader exists.
This does not change ordinary Windows compute grants.

The `writer_active` journal field is also written for ordinary, nonstaged work.
The new authority can read both earlier journal forms (`unresolved` alone or
with `authority_id`) and preserves their unresolved reservation on upgrade.
Those older forms never authorize staged dispatch. Older package versions may
reject the new schema: downgrading a running authority requires an independently
verified journal/lifecycle transition that preserves every unresolved grant and
execution. Do not silently strip epoch fields, clear the journal or release its
native lock to make a downgrade start.

Controller selection pins the expected request in both ordinary and staged
admission. A concurrent higher-priority arrival must be selected anew rather than
silently entering the wrong protocol. Ordinary `end_turn` cannot terminalize a
staged attempt; it must use the explicit native load completion operation.

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

## Physical ResidentCredit v2 (`darwin-wired-metal-nocopy-vmstat.v2`)

For cooperative Apple Silicon runners, `ocean_resources.model_loading` and
`ocean_resources.capacity` implement the opt-in v2 physical attribution contract:

1. **Owned Wired Backings (`WiredBacking`, `WiredEpoch`, `DarwinWiredWeightRunner`):**
   Model weights from `.safetensors` files are loaded into private anonymous
   `mmap(MAP_PRIVATE | MAP_ANON)` regions with `minherit(..., VM_INHERIT_NONE)`
   and `mlock(...)`, bound zero-copy to Metal shared buffers via
   `newBufferWithBytesNoCopy:length:options:deallocator:`, and verified by a
   Metal compute pass reading every 32-bit word and page boundary. Only whole
   initialized weight pages (`raw_bytes // page_size`) count; aliased tensor
   views sharing an `allocation_id` count once and conflicting claims hold.
2. **Signed Platform Capacity Sample (`DarwinVmStatSample`, `sample_darwin_vm_stat`):**
   Because XNU caches `host_statistics64` for non-platform processes, v2 credits
   require a single verified `/usr/bin/vm_stat` platform-binary sample checked
   with `codesign --verify --strict -R '=anchor apple and identifier "com.apple.vm_stat"'`
   and binary identity readback before and after invocation. The projection
   `A = page_size * (Pages_free + Pages_speculative + Pages_inactive)` matches
   `psutil`'s available metric on the same kernel state and must bracket cleanly
   after wire completion (`wire_completed_ns <= sample_started_ns <= sample_completed_ns`).
3. **Three-Point Epoch Fencing & Durable Retirement (`WiredResidentCreditProof`, `permit_unwire`):**
   The host port proves `before_sample_epoch == after_sample_epoch == pre_commit_epoch`
   bound to exact `WiredScope` (boot, listener, runner, workload, digest,
   context, authority ID, grant generation fence and turn ID). Controlled release
   of Metal buffers, `munlock` and `munmap` is refused unless `permit_unwire`
   receives an exact `DurableRetirement("RETIRED", scope, generation)`.

An in-process inference runtime can use `borrow_weight` to hold a backing
across asynchronous GPU work. It returns the same lease with `release_weight`
after the last native reference has gone away. Authorized teardown closes
borrowing and waits for outstanding leases before releasing any Metal or VM
resource. The caller retries teardown after the leases drain.

On Apple Silicon, the optional `ocean_resources.mlx_weights.MLXWeightBindings`
adapter obtains and retains each Ocean lease while MLX holds its shared
buffer. Compile the extension with the same Python environment used by the
model runner. The pinned MLX and nanobind versions share the native array ABI.

```sh
python -m pip install mlx==0.32.0 nanobind==2.13.0
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
cmake -S native/mlx -B build/mlx -DCMAKE_BUILD_TYPE=Release \
  -DPython_EXECUTABLE="$(command -v python)" \
  -Dnanobind_DIR="$(python -m nanobind --cmake_dir)" \
  -DCMAKE_CXX_COMPILER="$DEVELOPER_DIR/usr/bin/clang++" \
  -DCMAKE_OSX_SYSROOT="$DEVELOPER_DIR/SDKs/MacOSX.sdk"
cmake --build build/mlx
PYTHONPATH="$PWD/build/mlx${PYTHONPATH:+:$PYTHONPATH}" python -c 'import mlx.core; import _ocean_mlx_wired'
```

Call `array(name, shape, dtype)` after the runner has loaded the named
tensor. Stop admitting requests and dispose of model and cache arrays before
calling `drain()`. It synchronizes MLX and returns leases with confirmed native
release. Retry after outstanding references end. An ambiguous native bind
remains held until the runner process exits or positive cleanup is established.
The build and runtime are opt-in. Existing non-MLX port configurations use
their existing dependencies.

For the pinned `qwen3.8:27b-mlx` checkpoint, `build_qwen_text_plan`
validates the manifest, configuration, tensor headers and NVFP4 scale layout
against MLX-LM 0.31.3. Its result describes every text parameter and reports
page-rounded wired bytes and subpage bytes before starting any weight load.
The runtime must combine these sizes with the current capacity, context and
working-memory budget. The text plan omits Vision and speculative MTP
parameters.

`QwenTextRuntime` executes that plan in process. Before binding a wired
tensor it verifies its file against the content-addressed blob name and
checks that the copied tensor bytes match the digest obtained in that same
verification pass. It then verifies Metal access and transfers a weight lease
to MLX. `load` and `generate` require a caller-owned grant check. Each
`generate` call uses an independent cache within the configured context
limit. The native port owns admission, capacity, durable retirement and
final unmapping. Drain the runtime before releasing the runner's mappings.

The opt-in `InProcessQwenPort` owns that runtime. An in-process consumer
submits and begins the existing staged load, then passes the durable
`MAY_LOAD` permit to `materialize`. The native port independently verifies
the same permit throughout loading and checks signed RAM headroom before the
first allocation. Call the controller's normal load completion before
starting an inference turn. Completion registers and verifies the wired epoch.
For ordinary return and for abort, the port drains MLX, waits for durable
retirement when required, and separately reports absence. This port requires
the `native` dependency group and the optional MLX extension.

A configured loopback client can invoke `materialize_model_load(grant, turn_id)`
after begin. The host retains the running operation when the connection times
out. A retry on the same exact grant and turn reports `LOADING`, `READY` or
`FAILED` without starting a second load. `READY` means the weight load ended.
The normal completion operation must still verify the native proof before
inference. Host polling remains available during materialization.

After `begin_turn`, `generate_model(grant, turn_id, prompt, max_new_tokens)`
executes a bounded token request on the native port. Repeating the exact input
for that live turn reads `RUNNING`, `COMPLETE` or `FAILED` without launching
another decode. Ending the turn discards its result and revokes further
generation. The controller retains the resource grant while any admitted
native computation is still unwinding. This endpoint uses token IDs and
does not handle text rendering, tokenization or tool calls.



For a verified wired load, the native port implements `unload_wired(spec,
retire_epoch)`. It passes the exact admitted epoch to the callback after the
controller has requested return and recorded the execution checkpoint. The
callback persists and reads back the epoch retirement before the port unwires.
The loaded model's journal records the admitted epoch scope, generation,
backing count and SHA-256 commitment after validating all three epoch samples.
This keeps the writer record bounded for checkpoints with many weight buffers.
The exact epoch must still match that commitment before retirement.

A cooperative port's `load_wired_observation` calls `register_epoch` with the
exact pending load's wired epoch before taking the signed capacity sample.
If that sample or admission fails, `abort_wired_load` obtains retirement for
the registered epoch and performs native cleanup under the same process fence.
The controller keeps the grant in recovery until it receives zero residency.
A temporary cleanup or readback failure can retry the same durable retirement.
The controller keeps the resource grant until the native port separately
reports zero residency and the arbiter accepts a subsequent capacity sample.
The port must make unload retries idempotent, including retries after native
cleanup when the controller could not yet verify the final release.

Stock Ollama 0.33.3 does not export this cooperative wire/generation proof and
remains at `supports_load_pending = False` with `0` bytes credited.

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

The process and inference owner are rechecked after metadata and intent fsync,
before the unload POST. Terminal `ABORTED` is committed by a required local
callback while the same inference owner still protects the absence proof,
including the already-absent case. Thus a delayed former dispatcher cannot enter
between proof and terminalization and spend its stale permit after release.

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
