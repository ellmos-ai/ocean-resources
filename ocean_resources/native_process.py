# SPDX-License-Identifier: MIT
# ruff: noqa: B009
# Preserve the upstream portable attribute probes exactly during extraction.
"""Native process fences extracted without algorithm changes from open-ocean.

Source: ellmos-ai/open-ocean@6d1afeaa7831a100bfb6ffd599bc6ec4e4696f3a,
tools/runtime_supervisor.py. Only the owned-child fence dependency closure is
packaged here; no lifecycle HTTP server or runtime discovery is imported.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

from . import windows_job

POSIX_SIGTERM = getattr(signal, "SIGTERM", 15)
POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)
POSIX_GROUP_POLL_SECONDS = 0.05
REAP_WAIT_SECONDS = 5.0

class ProcessFenceError(RuntimeError):
    """A runtime process group could not be stopped and positively verified."""


def _capture_posix_process_fence(pid: int) -> dict[str, int | str]:
    """Capture the session/process-group identity created for one runtime child."""
    try:
        getpgid = getattr(os, "getpgid")
        getsid = getattr(os, "getsid")
        process_group_id = int(getpgid(pid))
        session_id = int(getsid(pid))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ProcessFenceError(
            f"POSIX-Prozessgruppe für Runtime-PID {pid} nicht belegbar."
        ) from exc
    if process_group_id != pid or session_id != pid:
        raise ProcessFenceError(
            f"POSIX-Fence für Runtime-PID {pid} ist nicht an Session-/"
            f"Prozessgruppenführer gebunden (sid={session_id}, pgid={process_group_id})."
        )
    return {
        "kind": "posix-session-group",
        "pgid": process_group_id,
        "sid": session_id,
    }


def _validate_posix_process_fence(
    child: subprocess.Popen[Any],
    process_fence: dict[str, Any],
) -> tuple[int, int]:
    if process_fence.get("kind") != "posix-session-group":
        raise ProcessFenceError("Unbekannter oder fehlender POSIX-Prozessgruppenbeleg.")
    process_group_id = process_fence.get("pgid")
    session_id = process_fence.get("sid")
    if (
        isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 0
        or isinstance(session_id, bool)
        or not isinstance(session_id, int)
        or session_id <= 0
        or process_group_id != child.pid
        or session_id != child.pid
    ):
        raise ProcessFenceError(
            f"Ungültiger POSIX-Prozessgruppenbeleg für Runtime-PID {child.pid}."
        )
    return process_group_id, session_id


def _posix_group_empty(process_group_id: int) -> bool:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        killpg(process_group_id, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _wait_posix_group_empty(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _posix_group_empty(process_group_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(POSIX_GROUP_POLL_SECONDS, remaining))


def _signal_posix_group(process_group_id: int, signum: int) -> bool:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return False
    try:
        killpg(process_group_id, signum)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _wait_child(child: subprocess.Popen[Any], timeout: float) -> bool:
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        raise ProcessFenceError(f"Runtime-Kind konnte nicht abgefragt werden: {exc}") from exc
    return True


def _terminate_posix_group(
    child: subprocess.Popen[Any],
    process_fence: dict[str, Any],
    timeout: float,
) -> None:
    if timeout <= 0:
        raise ProcessFenceError("POSIX-Stop benötigt ein positives Zeitbudget.")
    process_group_id, _session_id = _validate_posix_process_fence(child, process_fence)
    deadline = time.monotonic() + timeout

    # While the direct child is still alive, refuse to signal if its identity
    # drifted. Once it has exited, the captured group receipt remains the only
    # safe handle to late descendants; the bounded empty-group check prevents
    # reporting success while they remain.
    if child.poll() is None:
        try:
            current_group_id = getattr(os, "getpgid")(child.pid)
            current_session_id = getattr(os, "getsid")(child.pid)
        except (AttributeError, ProcessLookupError):
            pass
        except OSError as exc:
            raise ProcessFenceError(
                f"POSIX-Fence für Runtime-PID {child.pid} nicht erneut prüfbar."
            ) from exc
        else:
            if current_group_id != process_group_id or current_session_id != child.pid:
                raise ProcessFenceError(
                    f"POSIX-Fence für Runtime-PID {child.pid} hat sich verändert."
                )

    if _posix_group_empty(process_group_id):
        return
    if not _signal_posix_group(process_group_id, POSIX_SIGTERM):
        raise ProcessFenceError(
            f"SIGTERM für POSIX-Prozessgruppe {process_group_id} konnte nicht zugestellt werden."
        )
    remaining = max(0.0, deadline - time.monotonic())
    _wait_child(child, remaining)
    if _wait_posix_group_empty(process_group_id, max(0.0, deadline - time.monotonic())):
        return

    if not _signal_posix_group(process_group_id, POSIX_SIGKILL):
        raise ProcessFenceError(
            f"SIGKILL für POSIX-Prozessgruppe {process_group_id} konnte nicht zugestellt werden."
        )
    _wait_child(child, max(0.0, deadline - time.monotonic()))
    if not _wait_posix_group_empty(process_group_id, max(0.0, deadline - time.monotonic())):
        raise ProcessFenceError(
            f"POSIX-Prozessgruppe {process_group_id} blieb nach SIGKILL nicht leer."
        )


def _terminate_unfenced_posix_child(child: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    """Best-effort bootstrap cleanup when the promised fence could not be captured.

    A group signal is used only after the requested new-session invariant is
    independently observed. If that observation is unavailable, direct-child
    cleanup is the only signal that cannot accidentally reach the caller.
    """
    try:
        process_group_id = getattr(os, "getpgid")(child.pid)
        session_id = getattr(os, "getsid")(child.pid)
    except (AttributeError, OSError):
        process_group_id = None
        session_id = None
    if (
        process_group_id == child.pid
        and session_id == child.pid
        and _signal_posix_group(process_group_id, POSIX_SIGKILL)
    ):
        pass
    else:
        try:
            child.kill()
        except OSError:
            pass
    try:
        child.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_child(
    child: subprocess.Popen[Any],
    timeout: float = 5.0,
    *,
    process_fence: dict[str, Any] | None = None,
    job_handle: int | None = None,
) -> None:
    if os.name != "nt":
        if process_fence is None:
            raise ProcessFenceError(
                "POSIX-Stop ohne Prozessgruppenbeleg verweigert; kein Direkt-Kind-Only-Fallback."
            )
        _terminate_posix_group(child, process_fence, timeout)
        return
    if job_handle is None:
        if child.poll() is None:
            raise ProcessFenceError(
                "Windows-Stop ohne Job-Objekt verweigert; kein Direkt-Kind-Only-Fallback."
            )
        return
    # Always end the job, even if the direct child is already gone: its helpers may not be.
    try:
        windows_job.terminate_job(job_handle)
    except OSError as exc:
        raise ProcessFenceError(f"Windows-Job konnte nicht beendet werden: {exc}") from exc
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProcessFenceError("Runtime-Kind endete nach dem Beenden des Jobs nicht.") from exc


def _reap_unassigned_child(child: subprocess.Popen[Any]) -> str | None:
    """kill() then wait() a child that never made it into the job; both are always tried."""
    problems = []
    for name, step in (("kill", child.kill), ("wait", lambda: child.wait(timeout=REAP_WAIT_SECONDS))):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - the primary error must survive
            problems.append(f"{name}: {exc}")
    return "; ".join(problems) or None
