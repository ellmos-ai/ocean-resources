"""Explicit host-local writer authority, separate from the Polarstern lease.

All cooperating clients must use the SAME configured local directory and host
identity. The inode is never removed/replaced. The first byte is the OS lock;
the remaining checksummed journal holds unresolved reservations across crashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .contracts import Hold, identity

MAX_JOURNAL_BYTES = 64 * 1024
# A failed retirement must not let GC close the native lock while this PID is
# still alive. Only a successful explicit close retry (or process exit) releases
# these authorities; unresolved reservations are never discarded here.
_FAILED_CLOSE_HOLDS = {}


class HostWriterAuthority:
    def __init__(self, directory: Path, host_id: str):
        self.host_id = identity(host_id)
        self.directory = Path(directory)
        self.authority_id = ""
        self._stream = None
        self._pid = None
        self._path = None
        self._arbiter_bound = False
        self._epoch_active = False

    @property
    def held(self) -> bool:
        return self._stream is not None and not self._stream.closed

    def __enter__(self):
        if self.held:
            raise Hold("writer authority is already held")
        if not self.directory.is_absolute() or not self.directory.is_dir():
            raise Hold("explicit existing absolute host-local state directory required")
        if self.directory.resolve() != self.directory.absolute():
            raise Hold("linked state directory is not allowed")
        if self.directory.drive.startswith("\\\\") or any(p.lower().startswith("onedrive") for p in self.directory.parts):
            raise Hold("writer state must be host-local and outside OneDrive")
        path = self.directory / (self.host_id + ".writer")
        if path.is_symlink():
            raise Hold("linked writer file is not allowed")
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                descriptor = os.open(path, flags)
                created = False
            stream = os.fdopen(descriptor, "r+b", buffering=0)
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if "stream" in locals():
                stream.close()
            raise Hold("host-local writer cannot be exclusively acquired") from exc
        self._stream, self._path, self._pid = stream, path, os.getpid()
        self.authority_id = uuid.uuid4().hex
        self._arbiter_bound = False
        self._epoch_active = True
        try:
            if created:
                self.record(None)
            previous = self.unresolved()
            # Preserve unresolved work while publishing this new writer epoch.
            # A live process can close/reopen the same inode: PID/ctime alone
            # must never make an old MAY_LOAD journal look currently authorized.
            self.record(previous)
        except Exception:
            self.__exit__()
            raise
        return self

    def require(self, *, allow_retired=False):
        if not self.held or self._pid != os.getpid():
            raise Hold("host-local writer authority is not held by this process")
        if not allow_retired and not self._epoch_active:
            raise Hold("host-local writer epoch is retired")
        try:
            current = self._path.stat(follow_symlinks=False)
            opened = os.fstat(self._stream.fileno())
            if self._path.is_symlink() or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise Hold("writer inode identity changed")
        except OSError as exc:
            raise Hold("writer inode identity is unavailable") from exc

    def claim_arbiter(self):
        self.require()
        if self._arbiter_bound:
            raise Hold("this host writer already has an arbiter")
        self._arbiter_bound = True

    def record(self, unresolved: dict | None):
        self.require(allow_retired=True)
        payload = json.dumps({"authority_id": self.authority_id,
                             "writer_active": self._epoch_active, "unresolved": unresolved},
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        journal = json.dumps({
            "payload": payload,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }, sort_keys=True).encode("utf-8")
        if len(journal) > MAX_JOURNAL_BYTES:
            raise Hold("writer journal exceeds its durable size bound")
        # Never replace the locked inode. A torn journal fails closed on reopen.
        self._stream.seek(0)
        content = b"0" + journal
        if self._stream.write(content) != len(content):
            raise Hold("writer journal short write; independent recovery required")
        self._stream.truncate()
        os.fsync(self._stream.fileno())

    def unresolved(self) -> dict | None:
        self.require(allow_retired=True)
        self._stream.seek(1)
        raw = self._stream.read(MAX_JOURNAL_BYTES + 1)
        try:
            if len(raw) > MAX_JOURNAL_BYTES:
                raise ValueError("journal too large")
            outer = json.loads(raw)
            if not isinstance(outer, dict) or set(outer) != {"payload", "sha256"}:
                raise ValueError("journal envelope")
            payload = outer["payload"]
            if not isinstance(payload, str):
                raise TypeError("journal payload")
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != outer["sha256"]:
                raise ValueError("journal hash")
            result = json.loads(payload)
            if (not isinstance(result, dict) or set(result) not in (
                    {"unresolved"}, {"unresolved", "authority_id"},
                    {"unresolved", "authority_id", "writer_active"}) or (
                result["unresolved"] is not None and not isinstance(result["unresolved"], dict)
            )):
                raise ValueError("journal schema")
            if "authority_id" in result:
                identity(result["authority_id"])
            if "writer_active" in result and type(result["writer_active"]) is not bool:
                raise ValueError("journal writer epoch")
            return result["unresolved"]
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise Hold("writer journal is invalid; independent recovery required") from exc

    def model_dispatch_reference(self):
        """Readback reference for this held writer, never a replacement authority."""
        self.require()
        from .model_dispatch import native_writer_identity
        opened = os.fstat(self._stream.fileno())
        return {"schema": "ocean-model-dispatch-reader.v1", "path": str(self._path),
                "host_id": self.host_id, "authority_id": self.authority_id,
                "device": opened.st_dev, "inode": opened.st_ino,
                "writer": native_writer_identity(self._pid)}

    def __exit__(self, *exc):
        if self.held:
            stream = self._stream
            # fork inherits the open-file description. LOCK_UN in the child
            # would release the parent's POSIX lock as well; close only.
            if self._pid != os.getpid():
                stream.close()
                self._stream = None
                _FAILED_CLOSE_HOLDS.pop(id(self), None)
                return
            # Reopening the same inode can acquire EX before its new epoch is
            # written. Retire this epoch durably while we still own EX, so the
            # old MAY_LOAD can never become live during that restore window.
            self._epoch_active = False
            try:
                self.record(self.unresolved())
            except BaseException as error:
                _FAILED_CLOSE_HOLDS[id(self)] = self
                if isinstance(error, Exception):
                    raise Hold("writer epoch retirement unconfirmed; native lock retained") from error
                raise
            _FAILED_CLOSE_HOLDS.pop(id(self), None)
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        self._stream = None
