"""Full HTTP CPU -> model -> CPU handoff, only owned temporary fixture children."""
import asyncio
import json
import time
from threading import Thread

from test_pipeline_process import make_command, verify

from ocean_resources import (
    BACHFacade,
    CapacityReadback,
    ControllerClient,
    HostController,
    HostLocalArbiter,
    HostWriterAuthority,
    ResourceSocket,
    SlotRequest,
)
from ocean_resources.client import HostClient
from ocean_resources.controller import FocusPolicy
from ocean_resources.model_client import model_turn
from ocean_resources.model_executor import ModelAPIExecutor, ModelSpec
from ocean_resources.process import OwnedProcessExecutor
from ocean_resources.service import HostService


def test_owned_compute_checkpoint_model_turn_and_exact_resume_over_http(tmp_path):
    root = tmp_path.resolve()
    state = root / "state"
    state.mkdir()
    sequence = 0
    def sensor():
        nonlocal sequence
        sequence += 1
        return CapacityReadback("fixture-host", sequence, time.monotonic(), 128, 4, True, True)
    spec = ModelSpec("fixture:1", "a" * 64, 4096)
    sockets = (ResourceSocket("cpu", "fixture-host", 64, 1, "fixture"),
               ResourceSocket("model", "fixture-host", 32, 1, "fixture", spec.fingerprint()))
    class Port:
        resident = 0
        unloads = 0
        def verify_available(self, spec):
            pass
        def unload(self, spec, owned):
            owned()
            self.unloads += 1
            self.resident = 0
        def resident_bytes(self, spec):
            return self.resident
    with HostWriterAuthority(state, "fixture-host") as writer:
        arbiter = HostLocalArbiter(writer, sockets, sensor, time.monotonic,
                                  fixture_profiles=frozenset({"fixture"}), heartbeat_ttl=30)
        with OwnedProcessExecutor(arbiter, make_command(root), state, verify) as cpu:
            port = Port()
            model = ModelAPIExecutor(arbiter, spec, port, state)
            operator = ControllerClient("operator", frozenset({"cpu", "model"}), operator=True)
            compute = ControllerClient("compute", frozenset({"cpu"}), require_leases=True)
            bach = ControllerClient("bach", frozenset({"model"}), allow_interactive=True, require_leases=True)
            controller = HostController(BACHFacade(arbiter, {"cpu": cpu.adapter(), "model": model.adapter()}),
                (operator, compute, bach), FocusPolicy(idle_seconds=.5, interactive_slice_seconds=10, background_min_seconds=.1))
            service = HostService(controller, operator)
            thread = Thread(target=service.serve_forever)
            thread.start()
            c = HostClient(service.port, writer.host_id, writer.authority_id, compute.client_id, compute.token)
            b = HostClient(service.port, writer.host_id, writer.authority_id, bach.client_id, bach.token)
            async def until(predicate):
                deadline = time.monotonic() + 10
                while True:
                    status = await asyncio.to_thread(c.status)
                    if predicate(status):
                        return status
                    assert time.monotonic() < deadline, status
                    await asyncio.sleep(.02)
            async def scenario():
                lease = await asyncio.to_thread(c.submit_leased,
                    SlotRequest("compute", "cpu", "fixture-compute", writer.host_id, "compute"), 30)
                await until(lambda s: s["state"] == "RUNNING" and s["torch"]["socket_id"] == "cpu")
                async with model_turn(b, "model", "bach", "foreground", wait_seconds=10, ttl_seconds=30):
                    assert json.loads(arbiter.checkpoint_for("compute").payload_json)["step"] == 7
                    assert all(run.exited for run in cpu._executions.values())
                    port.resident = 32
                    assert (await asyncio.to_thread(b.status))["in_flight_turns"] == 1
                await until(lambda s: s["state"] == "RUNNING" and s["torch"]["socket_id"] == "cpu")
                assert port.unloads == 1
                await asyncio.to_thread(c.cancel, lease)
                await until(lambda s: s["state"] == "IDLE")
                assert json.loads(arbiter.checkpoint_for("compute").payload_json)["step"] == 14
                assert writer.unresolved() is None
            try:
                asyncio.run(scenario())
            finally:
                service.close()
                thread.join(5)
