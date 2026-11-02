"""Offline Ocean resource core and injected BACH facade."""
from .adapters import BACHFacade, ComputeExecutorAdapter, ModelSocketBinding, ModelSocketInferenceAdapter
from .arbiter import HostLocalArbiter
from .authority import HostWriterAuthority
from .profiles import ProfileSource, ReviewedProfileGate
from .controller import ControllerClient, FocusPolicy, HostController, OverrideCommand
from .contracts import (
    CapacityReadback, Checkpoint, ExitReadback, Grant, Hold, HybridPlannerPointer,
    Override, ResourceSocket, SlotRequest,
)
from .host import HostObservation, sample_host
from .pipelines import PipelineDefinition, ResourceBudget, load_catalog, route_job, shadow_plan, slot_request
from .client import HostClient
from .service import HostService
from .ram_distributor import (
    ConsumerCapability,
    DistributorRequest,
    FallbackDecision,
    RAMDistributor,
    UserProximity,
    WatchdogHysteresisPolicy,
)
from .inference_queue_policy import (
    MAX_CONSECUTIVE_FOREGROUND,
    QueuePolicyError,
    QueuePriority,
    QueueSelection,
    QueueTicket,
    SelectionReason,
    select_next_ticket,
)

__all__ = [
    "BACHFacade", "CapacityReadback", "Checkpoint", "ComputeExecutorAdapter",
    "ExitReadback", "Grant", "Hold", "HostLocalArbiter", "HostWriterAuthority",
    "HybridPlannerPointer", "ModelSocketBinding", "ModelSocketInferenceAdapter",
    "Override", "ResourceSocket", "SlotRequest",
    "HostObservation", "sample_host", "PipelineDefinition", "ResourceBudget",
    "load_catalog", "route_job", "shadow_plan", "slot_request",
    "ControllerClient", "FocusPolicy", "HostController", "OverrideCommand",
    "ProfileSource", "ReviewedProfileGate",
    "HostClient", "HostService",
    "ConsumerCapability", "DistributorRequest", "FallbackDecision",
    "RAMDistributor", "UserProximity", "WatchdogHysteresisPolicy",
    "MAX_CONSECUTIVE_FOREGROUND", "QueuePolicyError", "QueuePriority",
    "QueueSelection", "QueueTicket", "SelectionReason", "select_next_ticket",
]
