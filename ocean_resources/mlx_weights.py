# SPDX-License-Identifier: MIT
"""Optional MLX bindings for an in-process DarwinWiredWeightRunner."""
from importlib import import_module
import os
from threading import Lock

from .contracts import Hold

MLX_VERSION = "0.32.0"


class MLXWeightBindings:
    """Keep exact Ocean leases until their native MLX references have drained."""

    def __init__(self, runner):
        try:
            self._mlx = import_module("mlx.core")
            if self._mlx.__version__ != MLX_VERSION:
                raise Hold("wired MLX bindings require MLX " + MLX_VERSION)
            self._native = import_module("_ocean_mlx_wired")
        except (ImportError, OSError) as exc:
            raise Hold("optional wired MLX extension is unavailable") from exc
        self._runner = runner
        self._pid = os.getpid()
        self._leases = {}
        self._draining = False
        self._lock = Lock()

    def _require_process(self):
        if self._pid != os.getpid():
            raise Hold("foreign wired MLX process")

    def array(self, tensor_name, shape, dtype):
        """Wrap one owned mapping as an MLX array while retaining its lease."""
        self._require_process()
        with self._lock:
            if self._draining:
                raise Hold("MLX weight bindings are draining")
            lease = self._runner.borrow_weight(tensor_name)
            # A failed native bind may have retained the pointer. Keep the
            # lease until positive release evidence, never on garbage collection.
            self._leases[lease] = None
            value, reference = self._native.bind(lease.address, lease.raw_bytes, shape, dtype)
            self._leases[lease] = reference
            return value

    def drain(self):
        """Stop new bindings and return only leases with native release proof."""
        self._require_process()
        with self._lock:
            self._draining = True
            self._mlx.synchronize()
            for lease, reference in tuple(self._leases.items()):
                if reference is not None and reference.released is True:
                    self._runner.release_weight(lease)
                    del self._leases[lease]
            if self._leases:
                raise Hold("MLX weight references remain active or unconfirmed")
