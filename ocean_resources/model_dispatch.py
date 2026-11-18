# SPDX-License-Identifier: MIT
"""Read-only native fencing for a possible model POST under the inference owner.

This reader never acquires a resource grant. It proves that the same live writer
still holds its original inode and that its durable reservation authorizes this
exact attempt. The native port must keep its inference owner across this check
and dispatch; terminal abort commits under that same owner. No controller RPC or
blocking acquisition is allowed here.
"""
import errno
import hashlib
import json
import os
import stat
import time
from dataclasses import asdict
from pathlib import Path

import psutil

from .authority import MAX_JOURNAL_BYTES
from .contracts import Grant, Hold, RequestLease, ResourceSocket, identity, instant
from .profiles import ProfileSource, ReviewedProfileGate


def native_writer_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise Hold("native model writer identity is unavailable")
    process = psutil.Process(pid)
    if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
        raise Hold("native model writer is not alive")
    return {"pid": pid, "created_at": process.create_time(), "boot_time": psutil.boot_time()}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate native journal field")
        value[key] = item
    return value


def _document(raw):
    def invalid(_):
        raise ValueError("nonfinite native journal value")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid)


def _read_current(reference):
    if (os.name == "nt" or not isinstance(reference, dict)
            or set(reference) != {"schema", "path", "host_id", "authority_id", "device", "inode", "writer"}
            or reference["schema"] != "ocean-model-dispatch-reader.v1"
            or type(reference["device"]) is not int or reference["device"] < 0
            or type(reference["inode"]) is not int or reference["inode"] <= 0):
        raise Hold("native model dispatch reader is unavailable")
    for key in ("host_id", "authority_id"):
        identity(reference[key])
    path = Path(reference["path"])
    writer = reference["writer"]
    if (not path.is_absolute() or path.resolve() != path or path.name != reference["host_id"] + ".writer"
            or path.is_symlink() or not isinstance(writer, dict)
            or set(writer) != {"pid", "created_at", "boot_time"}
            or not all(instant(writer[k]) and writer[k] > 0 for k in ("created_at", "boot_time"))
            or native_writer_identity(writer["pid"]) != writer):
        raise Hold("native model writer binding changed")
    import fcntl
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        def same_inode():
            opened, current = os.fstat(descriptor), path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o077
                    or (opened.st_dev, opened.st_ino) != (reference["device"], reference["inode"])
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or not 1 < opened.st_size <= MAX_JOURNAL_BYTES + 1):
                raise Hold("native model writer inode changed")
        def writer_still_holds():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                raise Hold("native model writer no longer holds its inode")
            if native_writer_identity(writer["pid"]) != writer:
                raise Hold("native model writer process changed")
        same_inode()
        writer_still_holds()
        raw = os.read(descriptor, MAX_JOURNAL_BYTES + 2)
        if not raw.startswith(b"0") or len(raw) > MAX_JOURNAL_BYTES + 1:
            raise Hold("native model writer journal is invalid")
        envelope = _document(raw[1:])
        if (not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}
                or not isinstance(envelope["payload"], str)
                or hashlib.sha256(envelope["payload"].encode()).hexdigest() != envelope["sha256"]):
            raise Hold("native model writer journal is torn or invalid")
        payload = _document(envelope["payload"])
        if (not isinstance(payload, dict) or set(payload) != {"authority_id", "unresolved"}
                or payload["authority_id"] != reference["authority_id"]
                or not isinstance(payload["unresolved"], dict)):
            raise Hold("native model reservation is no longer present")
        same_inode()
        writer_still_holds()
        return payload["unresolved"]
    finally:
        os.close(descriptor)


def require_dispatch_permission(permit, *, authority_id, host_id, client_id,
                                clock=time.monotonic, wall_clock=time.time):
    """Return only a current exact MAY_LOAD proof; stale copies never authorize."""
    try:
        if not isinstance(permit, dict) or permit.get("phase") != "MAY_LOAD":
            raise Hold("native model dispatch permit is missing")
        reference = permit["writer_readback"]
        if reference["authority_id"] != authority_id or reference["host_id"] != host_id:
            raise Hold("native model dispatch authority changed")
        grant = Grant(**permit["grant"])
        if grant.authority_id != authority_id:
            raise Hold("native model dispatch grant changed")
        identity(client_id)
        identity(permit["turn_id"])
        identity(permit["execution_id"])

        def validate(current):
            now, state = clock(), current.get("load_state")
            if (not instant(now) or current.get("grant") != asdict(grant)
                    or current.get("execution_identity") != permit["execution_id"]
                    or current.get("return_requested") is not False
                    or not instant(current.get("heartbeat_until")) or now >= current["heartbeat_until"]
                    or not isinstance(state, dict) or state.get("phase") != "MAY_LOAD"
                    or state.get("client_id") != client_id
                    or any(state.get(key) != permit[key] for key in (
                        "turn_id", "execution_id", "profile_sha256", "profile_readback", "socket",
                        "profile_ram_bytes", "profile_cpu_units", "workload_sha256", "policy_sha256",
                        "spec", "baseline", "deadline"))
                    or not instant(state["deadline"]) or now >= state["deadline"]):
                raise Hold("native model dispatch attempt is stale, expired or terminal")
            lease, original = RequestLease(**state["client_lease"]), RequestLease(**permit["client_lease"])
            if (lease.authority_id != authority_id or lease.request_id != grant.request_id
                    or original.authority_id != authority_id or original.request_id != grant.request_id
                    or lease.generation < original.generation or now >= lease.expires_at):
                raise Hold("native model dispatch client lease ended or changed")
            return state

        state = validate(_read_current(reference))
        source = state["profile_readback"]
        if not isinstance(source, dict) or set(source) != {"profile_path", "evidence_path", "max_age_seconds"}:
            raise Hold("native model dispatch profile source is unavailable")
        profile = ReviewedProfileGate((ProfileSource(Path(source["profile_path"]),
            state["profile_sha256"], Path(source["evidence_path"])),),
            max_age_seconds=source["max_age_seconds"], wall_clock=wall_clock)
        profile.require_model_load(ResourceSocket(**state["socket"]))
        # File/provenance checks can take time. The last read revalidates the live
        # writer, lease, heartbeat and durable phase before the native port POST.
        validate(_read_current(reference))
    except Hold:
        raise
    except Exception as exc:
        raise Hold("native model dispatch proof is unavailable") from exc
