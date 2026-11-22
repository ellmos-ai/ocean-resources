"""Offline Ocean resource core and injected BACH facade."""
from .adapters import BACHFacade, ComputeExecutorAdapter, ModelSocketBinding, ModelSocketInferenceAdapter
from .arbiter import HostLocalArbiter
from .authority import HostWriterAuthority
from .profiles import ProfileSource, ReviewedProfileGate
from .controller import ControllerClient, FocusPolicy, HostController, OverrideCommand
from .contracts import (
    CapacityReadback, Checkpoint, ExitReadback, Grant, Hold, HybridPlannerPointer,
    Override, RequestLease, ResourceSocket, SlotRequest,
)
from .host import HostObservation, StressSample, sample_host, sample_stress
from .pipelines import PipelineDefinition, ResourceBudget, load_catalog, route_job, shadow_plan, slot_request
from .client import HostClient
from .service import HostService
from .ram_distributor import (
    ConsumerCapability,
    CooldownDecision,
    DistributorRequest,
    FallbackDecision,
    RAMDistributor,
    RunCooldownGate,
    RunCooldownPolicy,
    UserProximity,
    WatchdogHysteresisPolicy,
)
from .capacity import (
    DARWIN_VM_STAT_SOURCE,
    DarwinVmStatSample,
    parse_darwin_vm_stat_output,
    sample_darwin_vm_stat,
    verify_darwin_vm_stat_binary,
)
from .model_loading import (
    WIRED_CREDIT_SOURCE,
    DarwinWiredWeightRunner,
    DurableRetirement,
    SafetensorsTensorInfo,
    WiredBacking,
    WiredEpoch,
    WiredResidentCreditProof,
    WiredScope,
    WiredWeightLease,
    WiredWeightRegion,
    build_wired_resident_credit,
    parse_safetensors_header,
    permit_unwire,
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
    "Override", "RequestLease", "ResourceSocket", "SlotRequest",
    "HostObservation", "StressSample", "sample_host", "sample_stress", "PipelineDefinition", "ResourceBudget",
    "load_catalog", "route_job", "shadow_plan", "slot_request",
    "ControllerClient", "FocusPolicy", "HostController", "OverrideCommand",
    "ProfileSource", "ReviewedProfileGate",
    "HostClient", "HostService",
    "ConsumerCapability", "DistributorRequest", "FallbackDecision",
    "RAMDistributor", "UserProximity", "WatchdogHysteresisPolicy",
    "CooldownDecision", "RunCooldownGate", "RunCooldownPolicy",
    "DARWIN_VM_STAT_SOURCE", "DarwinVmStatSample", "parse_darwin_vm_stat_output",
    "sample_darwin_vm_stat", "verify_darwin_vm_stat_binary",
    "WIRED_CREDIT_SOURCE", "DarwinWiredWeightRunner", "DurableRetirement",
    "SafetensorsTensorInfo", "WiredBacking", "WiredEpoch", "WiredResidentCreditProof",
    "WiredScope", "WiredWeightLease", "WiredWeightRegion", "build_wired_resident_credit",
    "parse_safetensors_header", "permit_unwire",
    "MAX_CONSECUTIVE_FOREGROUND", "QueuePolicyError", "QueuePriority",
    "QueueSelection", "QueueTicket", "SelectionReason", "select_next_ticket",
]
