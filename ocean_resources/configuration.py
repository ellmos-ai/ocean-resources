"""Explicit private host configuration; no discovery and no credential output."""
import hashlib
import json
import os
import platform
import stat
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from .adapters import BACHFacade
from .arbiter import HostLocalArbiter
from .authority import HostWriterAuthority
from .capacity import CapacityPolicy, NativeCapacitySensor
from .client import HostClient
from .contracts import Hold, ResourceSocket, identity
from .controller import ControllerClient, FocusPolicy, HostController
from .model_executor import MLXModelSpec, ModelAPIExecutor, ModelSpec
from .process import OwnedProcessExecutor, ProcessCommand
from .profiles import ProfileSource, ReviewedProfileGate
from .service import HostService, decode_json, exact


def private_bytes(path, limit=1024 * 1024):
    if os.name == "nt":
        raise Hold("native private-file ACL verification is not yet available on Windows")
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or path.is_symlink()
            or any(p.lower().startswith("onedrive") for p in path.parts)):
        raise Hold("explicit unlinked host-local private file required")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
            or os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)):
        raise Hold("private configuration permissions or size are invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Hold("private configuration changed while opening")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise Hold("private configuration exceeds bound")
    return data


def read_client(path):
    data = decode_json(private_bytes(path))
    exact(data, ("schema", "port", "host_id", "authority_id", "client_id", "token_file", "models"))
    if data["schema"] != "ocean-host-client-config.v1" or not isinstance(data["models"], list):
        raise Hold("unsupported host client configuration")
    token = private_bytes(data["token_file"], 256).decode("ascii").strip()
    client = HostClient(data["port"], data["host_id"], data["authority_id"], data["client_id"], token)
    return client, data["models"]


def _publish_private(path, value):
    path = Path(path)
    if (not path.is_absolute() or path.parent.resolve() != path.parent or not path.parent.is_dir()
            or path.is_symlink() or any(p.lower().startswith("onedrive") for p in path.parts)):
        raise Hold("explicit unlinked client endpoint path required")
    if path.exists():
        private_bytes(path)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(value, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _validator(config):
    exact(config, ("path", "sha256", "function"))
    data = private_bytes(config["path"])
    if hashlib.sha256(data).hexdigest() != config["sha256"]:
        raise Hold("checkpoint validator source changed")
    identity(config["function"])
    namespace = {"__name__": "ocean_configured_checkpoint_validator", "__file__": config["path"]}
    # Privileged operator-owned, hash-pinned source; never supplied by a client.
    exec(compile(data, config["path"], "exec"), namespace)  # noqa: S102
    callback = namespace.get(config["function"])
    if not callable(callback):
        raise Hold("configured checkpoint validator is unavailable")
    def validate(payload):
        if hashlib.sha256(private_bytes(config["path"])).hexdigest() != config["sha256"]:
            raise Hold("checkpoint validator source changed")
        return callback(payload) is True
    return validate


class ConfiguredHost:
    """One explicit host writer, controller and transport; native adapters only."""
    def __init__(self, path, source_sha256, *, model_port_factory):
        raw = private_bytes(path)
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise Hold("host configuration pin changed")
        config = decode_json(raw)
        exact(config, ("schema", "host_id", "state_directory", "port", "capacity", "profiles",
                       "profile_max_age_seconds", "sockets", "clients", "executors", "focus"))
        if config["schema"] != "ocean-host-config.v1" or config["host_id"] != platform.node():
            raise Hold("unsupported or foreign host configuration")
        self._stack = ExitStack()
        self._executors = []
        self._closed = False
        self.service = None
        try:
            state = Path(config["state_directory"])
            if not state.is_dir() or state.resolve() != state or os.name != "nt" and stat.S_IMODE(state.stat().st_mode) & 0o077:
                raise Hold("existing private host state directory required")
            profiles = tuple(ProfileSource(Path(p["profile_path"]), p["profile_sha256"], Path(p["evidence_path"])) for p in config["profiles"])
            gate = ReviewedProfileGate(profiles, max_age_seconds=config["profile_max_age_seconds"])
            sockets = tuple(ResourceSocket(**s) for s in config["sockets"])
            for socket in sockets:
                gate.require(socket)
            cap = dict(config["capacity"])
            cap["directory"] = Path(cap["directory"])
            mlx_capacity = any(item["kind"] == "model-mlx" for item in config["executors"])
            options = {"use_platform_vm_stat": True} if mlx_capacity else {}
            sensor = NativeCapacitySensor(CapacityPolicy(**cap), config["host_id"], **options)
            self.writer = self._stack.enter_context(HostWriterAuthority(state, config["host_id"]))
            self.arbiter = HostLocalArbiter(self.writer, sockets, sensor, time.monotonic,
                heartbeat_ttl=30, profile_gate=gate)
            adapters, models = {}, []
            for item in config["executors"]:
                socket_id = identity(item["socket_id"])
                if socket_id in adapters:
                    raise Hold("duplicate executor socket")
                socket = next((s for s in sockets if s.socket_id == socket_id), None)
                if socket is None:
                    raise Hold("executor has no reviewed socket")
                if item["kind"] in {"model-api", "model-mlx"}:
                    mlx = item["kind"] == "model-mlx"
                    if mlx:
                        exact(item, ("socket_id", "kind", "model", "load_policy_sha256", "model_store", "headroom_bytes"))
                        from .mlx_model_port import InProcessQwenPort
                        from .mlx_qwen import build_qwen_text_plan

                        spec = MLXModelSpec(**item["model"])
                        if platform.system() != "Darwin" or platform.machine() != "arm64":
                            raise Hold("wired MLX requires Apple Silicon macOS")
                        store = Path(item["model_store"])
                        if not store.is_absolute() or store.resolve() != store or not store.is_dir():
                            raise Hold("explicit canonical Qwen model store required")
                        load_policy = item["load_policy_sha256"]
                        if socket.workload_sha256 != spec.fingerprint():
                            raise Hold("MLX executor differs from its reviewed socket workload")
                        gate.require_model_load(socket)
                        plan = build_qwen_text_plan(store, model=spec.model, model_digest=spec.digest)
                        if plan.wired_bytes + plan.subpage_bytes > socket.ram_bytes:
                            raise Hold("Qwen weights exceed the reviewed socket allocation")
                        port = InProcessQwenPort(spec, plan, policy_sha256=load_policy,
                                                 headroom_bytes=item["headroom_bytes"])
                    else:
                        exact(item, {"socket_id", "kind", "model"} |
                              ({"load_policy_sha256"} if "load_policy_sha256" in item else set()))
                        spec = ModelSpec(**item["model"])
                        load_policy = item.get("load_policy_sha256")
                        port = model_port_factory()
                    if load_policy is not None and getattr(port, "supports_load_pending", False) is not True:
                        raise Hold("native model residency accounting contract is unavailable; LOAD_PENDING is disabled")
                    executor = ModelAPIExecutor(self.arbiter, spec, port, state,
                                                load_policy_sha256=load_policy)
                    model = {"socket_id": socket_id, **asdict(spec)}
                    if mlx:
                        model["backend"] = "mlx-qwen"
                    if load_policy is not None:
                        model["load_policy_sha256"] = load_policy
                    models.append(model)
                elif item["kind"] == "cpu-owned-process":
                    exact(item, ("socket_id", "kind", "command", "checkpoint_validator"))
                    command = dict(item["command"])
                    command["argv"] = tuple(command["argv"])
                    command["cwd"] = Path(command["cwd"])
                    command["source_pins"] = tuple((Path(p), digest) for p, digest in command["source_pins"])
                    command["environment"] = tuple(tuple(pair) for pair in command["environment"])
                    executor = OwnedProcessExecutor(self.arbiter, ProcessCommand(**command), state,
                                                    _validator(item["checkpoint_validator"]))
                else:
                    raise Hold("unsupported configured executor")
                adapters[socket_id] = executor.adapter()
                if socket.workload_sha256 != adapters[socket_id].workload_sha256:
                    raise Hold("executor differs from the reviewed socket workload")
                self._executors.append(executor)
            if set(adapters) != {s.socket_id for s in sockets}:
                raise Hold("every configured socket needs exactly one native executor")
            clients = []
            for item in config["clients"]:
                exact(item, {"client_id", "sockets", "token_file", "operator", "allow_interactive",
                             "priority_floor", "require_leases", "endpoint_file"} |
                      ({"allow_model_load"} if "allow_model_load" in item else set()))
                if item["require_leases"] is not True:
                    raise Hold("product clients require bounded request leases")
                fields = {k: item[k] for k in ("client_id", "operator", "allow_interactive", "priority_floor", "require_leases")}
                fields["allow_model_load"] = item.get("allow_model_load", False)
                clients.append(ControllerClient(**fields, sockets=frozenset(item["sockets"]),
                    token=private_bytes(item["token_file"], 256).decode("ascii").strip()))
            operators = [c for c in clients if c.operator]
            if len(operators) != 1:
                raise Hold("exactly one host polling operator required")
            self.controller = HostController(BACHFacade(self.arbiter, adapters), tuple(clients), FocusPolicy(**config["focus"]))
            self.service = HostService(self.controller, operators[0], port=config["port"])
            for item, client in zip(config["clients"], clients):
                _publish_private(item["endpoint_file"], {"schema": "ocean-host-client-config.v1",
                    "port": self.service.port, "host_id": config["host_id"], "authority_id": self.writer.authority_id,
                    "client_id": client.client_id, "token_file": item["token_file"],
                    "models": [{k: v for k, v in m.items() if client.allow_model_load or k != "load_policy_sha256"}
                               for m in models if m["socket_id"] in client.sockets]})
        except BaseException:
            if self.service is not None:
                self.service.close()
            self._stack.close()
            raise

    def serve_forever(self):
        self.service.serve_forever()

    def close(self):
        if self._closed:
            return
        self.service.close()
        if self.arbiter.status()["torch"] is not None:
            raise Hold("active native execution remains; retain host writer and reconcile")
        for executor in self._executors:
            if isinstance(executor, OwnedProcessExecutor):
                executor.close()
        self._stack.close()
        self._closed = True
