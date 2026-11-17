# Cooperative host resource service (BACH #4218)

One host-local writer owns all configured resource grants. Clients request a
reviewed socket through authenticated loopback HTTP; they cannot submit commands,
PIDs, profile filenames, new allocations or credentials for another client.
`ConfiguredHost` composes the existing writer, arbiter, controller, native
executors and service. It does not import a BACH runtime or discover jobs.

The supported product configuration currently requires POSIX private files.
Windows process fences are packaged and retain their original semantics, but
private host/client configuration fails closed until a native ACL verifier is
provided. The Mac path is covered by the native fixture tests. No production
measurements, client tokens or host configuration are shipped in this repository.

## Private configuration and service lifetime

The host file has exactly these fields:

```json
{
  "schema": "ocean-host-config.v1",
  "host_id": "<platform.node()>",
  "state_directory": "/absolute/private/host-state",
  "port": 0,
  "capacity": {
    "reserve_ram_bytes": "<positive measured/operator reserve>",
    "reserve_cpu_units": "<positive reserve>",
    "reserve_disk_bytes": "<positive reserve>",
    "directory": "/absolute/local/capacity-filesystem",
    "sample_seconds": 0.25,
    "max_age_seconds": 2
  },
  "profiles": [{
    "profile_path": "/absolute/private/model-profile.json",
    "profile_sha256": "<SHA256 of exact approved profile bytes>",
    "evidence_path": "/absolute/private/model-evidence.json"
  }],
  "profile_max_age_seconds": "<explicit positive maximum age>",
  "sockets": [{
    "socket_id": "local-model",
    "host_id": "<platform.node()>",
    "ram_bytes": "<reviewed allocation>",
    "cpu_units": "<reviewed allocation>",
    "profile_id": "reviewed-model",
    "workload_sha256": "<ModelSpec.fingerprint()>"
  }],
  "clients": [{
    "client_id": "host-operator",
    "sockets": ["local-model"],
    "token_file": "/absolute/private/operator.token",
    "operator": true,
    "allow_interactive": false,
    "priority_floor": 1,
    "require_leases": true,
    "endpoint_file": "/absolute/private/operator-client.json"
  }, {
    "client_id": "bach",
    "sockets": ["local-model"],
    "token_file": "/absolute/private/bach.token",
    "operator": false,
    "allow_interactive": true,
    "priority_floor": 1,
    "require_leases": true,
    "endpoint_file": "/absolute/private/bach-client.json"
  }],
  "executors": [{
    "socket_id": "local-model",
    "kind": "model-api",
    "model": {
      "model": "<exact installed model name>",
      "digest": "<64 lowercase hexadecimal digest>",
      "context_tokens": "<reviewed BACH num_ctx>",
      "port": 11434
    }
  }],
  "focus": {
    "idle_seconds": 300,
    "interactive_slice_seconds": 120,
    "background_min_seconds": 10
  }
}
```

This is a schema template, not an approved runnable profile. Replace bracketed
values with actual typed values and independently reviewed measurements. Paths
must be absolute, local, unlinked and outside OneDrive. Files must be owned by
the service user, single-linked and mode 0600; the existing state directory must
be private (0700). Client secrets are independent, private ASCII tokens of
32–128 characters. Exactly one configured client is the polling operator.

BACH starts the service explicitly with its
`system/tools/maintenance/resource_host.py --config PATH --sha256 SHA256` entry
point. A LaunchAgent should use the reviewed BACH Python interpreter and that
entry point, one instance only, with `BACH_RUNTIME_DIR` identical to the running
chat/worker processes. No shell command is taken from an HTTP client. Host
configuration bytes are pinned at startup; profile/evidence bytes are rechecked
on every admission. Native observation requires `ocean-resources[native]`.

After binding, the service publishes each 0600 endpoint file atomically. It
contains `schema=ocean-host-client-config.v1`, `port`, `host_id`, `authority_id`,
`client_id`, `token_file`, and an allowed `models` list. It contains no token
value. Clients must reopen the file when beginning a new turn, so an old writer
receipt can never silently target a replacement authority.

Stopping the service stops new transport operations and drains callbacks. It
does not fabricate native process exit, issue an unreviewed stop, clear an
active journal, or treat lease expiry as memory release. An active execution
keeps its writer until process exit; its journal fences any replacement writer
and requires explicit native reconciliation. Automatic service restart does not
remove this evidence. Ordinary shutdown should first cancel/finish owned work
and observe an idle controller. KeepAlive launch policy must not turn an
unresolved journal into a rapid restart loop.

## Reviewed model evidence

The profile has exactly the normal `ocean-reviewed-workload-profile.v1` fields:
`schema`, `kind=model-api`, `profile_id`, `socket_id`, `host_id`, `platform`,
`architecture`, `workload_sha256`, `ram_bytes`, `cpu_units`, `ram_margin_bytes`,
`evidence_sha256`, `approved_by`, `approved_at`, `expires_at`.

Its evidence has exactly:

| Field | Contract |
| --- | --- |
| schema / scope / method | `ocean-model-measurement.v1` / `ollama-model-api` / `ollama-ps-and-native-cpu-v1` |
| host_id / platform / architecture | Exact native host, OS and architecture |
| workload_sha256 | SHA256 of canonical ModelSpec JSON, including Ollama 0.33.3 |
| model_digest / model_spec | Exact installed digest and `{model,digest,context_tokens,port}` |
| peak_resident_bytes | Peak observed `/api/ps` size for that exact name and digest |
| peak_cpu_units | Peak native cumulative CPU delta divided by the real sample interval |
| cpu_concurrency_units | Explicit reviewed concurrency, at least ceil(peak_cpu_units) |
| sample_count / sample_interval_seconds | At least two samples; positive interval |
| started_at / finished_at | Actual UTC epoch seconds, increasing |
| unload_ack_verified / model_absence_verified | Both true only after observed terminal unload ACK and actual absence |
| server_version | Exactly `0.33.3` |
| unmeasured_external_residency | False only if the reviewed scope has no unaccounted external model daemon |

RAM allocation must cover the observed peak plus a positive explicit margin.
CPU allocation must cover the reviewed concurrency. CPU measurement must include
the native server and relevant runner processes, or use conservative host-total
native CPU deltas if a complete process tree cannot be established. Never label
an unknown helper/accelerator residency as measured. Model loading and at least
the intended context/representative inference must be inside the evidence
window; a read-only idle `/api/ps` sample alone is insufficient approval evidence.
The approved envelope remains an observation-based operating policy, not a
kernel memory reservation, GPU instrumentation, or proof of worst-case usage.
Reviewer and evidence hashes are the approval boundary; boolean fields alone do
not approve a profile.

`ModelSpec.fingerprint()` hashes JSON with sorted keys and compact separators:
`schema=ocean-model-api-workload.v1`, `server_version=0.33.3`, plus all four spec
fields. The window must contain its declared samples, finish before approval,
remain within `profile_max_age_seconds`, and expire in the future. Any change to
name, digest, context, platform, profile bytes or evidence bytes blocks admission.

Model start reserves the profiled consumer; it does not load weights. BACH's
normal reserve checks, supervised cold start, native task lease, model binding,
foreground/background gate and budget all still run. Admission conservatively
requires a full allocation in fresh available capacity; it does not claim an
unverified credit for a model already loaded before this authority started.

## External cooperative compute

Add a reviewed `cpu-owned-process` socket, a scoped client and an executor:

```json
{
  "socket_id": "registered-compute",
  "kind": "cpu-owned-process",
  "command": {
    "argv": ["/absolute/pinned/python", "/absolute/pinned/worker.py"],
    "cwd": "/absolute/local/work-directory",
    "source_pins": [["/absolute/pinned/python", "<SHA256>"], ["/absolute/pinned/worker.py", "<SHA256>"]],
    "environment": [],
    "source_mode": "frozen-python"
  },
  "checkpoint_validator": {
    "path": "/absolute/private/reviewed-validator.py",
    "sha256": "<SHA256>",
    "function": "validate_checkpoint"
  }
}
```

The CPU measurement/profile contract is unchanged. The server owns each child,
captures its process fence before releasing its startup gate, requests a
cooperative checkpoint, validates and durably records it, then stops the exact
owned tree. Only verified full-tree exit, zero residency and fresh capacity
permit the next grant. Resume receives the exact previous checkpoint. Arbitrary
foreign PIDs cannot be adopted, SIGSTOPed or declared released. Existing external
workloads must first be migrated into this registered cooperative protocol.

The configured validator is reviewed privileged local code, never an upload from
a requesting process. Its pinned source is rechecked before each validation.
The extracted process fence dependency closure retains the implementations from
`ellmos-ai/open-ocean@6d1afeaa7831a100bfb6ffd599bc6ec4e4696f3a`; it no longer depends
on the unrelated and missing top-level `tools` Python package.

## API, client TTL and fairness

Use `python -m ocean_resources.client_cli OP --config CLIENT.json --payload
REQUEST.json`, or the equivalent `ocean-resource-client` entry point. API methods
are on `HostClient`; no BACH boot/database is required by this client. JSON-only
errors report HOLD without private credential/path content.

The loopback API is POST `/v1/focus/OP` with the existing
`ocean-host-transport.v1` envelope, exact `host_id`, `authority_id`, payload,
`X-Ocean-Client`, and `Authorization: Bearer ...`. Use HostClient rather than
copying credentials into command-line arguments. It supplies the canonical
loopback Host header, disables ambient proxies and rejects redirects.

| Operation | Meaning |
| --- | --- |
| submit_leased(request, ttl_seconds) | Submit an immutable registered socket request for 1–3600 seconds; retry does not extend TTL |
| request_status(request_id) | Read owned QUEUED/RUNNING/RETURN_REQUESTED/FINISHED/CANCELLED/EXPIRED state and fenced lease/grant |
| renew(lease, renewal_id, ttl_seconds) | Owned compare-and-swap renewal; idempotent renewal IDs, no expired revival |
| cancel(lease) | Remove a waiting request or request safe return of active work |
| begin_turn / end_turn | Account for existing BACH turns; unknown/expired grants cannot start new turns |
| finish(grant) | Owned cooperative completion through checkpoint/exit/readback |
| status | Read authority/controller/grant/queue state; no mutation |
| tick / override | Configured operator operations, never general client privileges |

The controller heartbeat is independent of client TTL. A dead client loses its
waiting request or triggers a safe return; it never gains a new lease merely
because the controller is alive. An in-flight turn cannot be declared finished
by a timeout. A crashed client with an unresolved turn therefore holds admission
until explicit reconciliation. Up to 10,000 request records and 1,024 renewal
IDs per request are retained; bounds require deliberate idle archival/restart,
not unsafe identity reuse.

BACH keeps a resident model grant across successive channels using its shared
configured client. Foreground requests use P1, background requests P4; the
existing inference gate still handles their actual per-turn fairness. The client
renews while a turn runs and uses a bounded 30-minute request TTL. Idle policy
returns an unused model after `idle_seconds`; a competing compute request asks
for return after `interactive_slice_seconds`, always after current turns finish.
Compute is then resumed from its original checkpoint. Broker idle retention
must be coordinated with BACH/Ollama `keep_alive`; the broker does not silently
extend the model server's own residency timer.

## Lock order and uncertain effects

The order is resource admission, then BACH inference.lock, then existing local
compute/dispatch checks. BACH leaves inference.lock before end_turn. Controller
callbacks never hold its state mutex. During an explicit model return the native
port acquires the same HostInferenceGate and may only perform short local grant
identity checks; it performs no controller HTTP RPC under the inference lock.

Model return requires exact owned grant/execution, fixed loopback endpoint,
Ollama 0.33.3 and unchanged name/digest. Durable unload-pending intent precedes
the POST. A terminal `done=true, done_reason=unload` ACK is necessary but does not
prove release. The native gate and intent remain until `/api/ps` proves the exact
target absent, then the arbiter separately checks current host capacity. A
timeout, malformed response, wrong digest, unresolved previous intent or active
native turn retains HOLD. An ambiguous POST is not automatically repeated.
The independent pressure watchdog keeps its own pressure policy unchanged.
