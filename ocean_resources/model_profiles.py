"""Validation of independently reviewed native model residency evidence."""
import math

from .contracts import identity, instant, positive, sha256_hex


def validate_model_profile(profile, evidence, *, host, system, architecture, now, max_age):
    fields = {"schema", "scope", "method", "host_id", "platform", "architecture", "workload_sha256",
              "model_digest", "model_spec", "peak_resident_bytes", "sample_count", "sample_interval_seconds",
              "started_at", "finished_at", "unload_ack_verified", "model_absence_verified",
              "server_version", "unmeasured_external_residency", "peak_cpu_units", "cpu_concurrency_units"}
    schema = evidence.get("schema")
    mlx = schema == "ocean-mlx-model-measurement.v1"
    native_peak = mlx or schema == "ocean-model-measurement.v2"
    if native_peak:
        fields |= {"peak_process_rss_bytes", "process_identity_verified", "measured_processes"}
    if mlx:
        fields.remove("server_version")
        fields.add("runtime_versions")
    if set(evidence) != fields:
        raise ValueError("exact reviewed model measurement fields required")
    if mlx:
        from .mlx_qwen import MLX_LM_VERSION
        from .mlx_weights import MLX_VERSION

        valid_measurement = (
            system == "Darwin" and architecture == "arm64"
            and evidence["scope"] == "in-process-mlx"
            and evidence["method"] == "mlx-wired-native-rss-and-cpu-v1"
            and evidence["runtime_versions"] == {"mlx": MLX_VERSION, "mlx-lm": MLX_LM_VERSION}
        )
    else:
        valid_measurement = (
            schema in {"ocean-model-measurement.v1", "ocean-model-measurement.v2"}
            and evidence["scope"] == "ollama-model-api" and evidence["server_version"] == "0.33.3"
            and evidence["method"] == ("ollama-ps-native-rss-and-cpu-v2" if native_peak
                                       else "ollama-ps-and-native-cpu-v1")
        )
    if (profile["schema"] != "ocean-reviewed-workload-profile.v1" or profile["kind"] != "model-api"
            or not valid_measurement
            or evidence["unload_ack_verified"] is not True or evidence["model_absence_verified"] is not True
            or evidence["unmeasured_external_residency"] is not False):
        raise ValueError("unsupported or incomplete model measurement scope")
    if native_peak and (not positive(evidence["peak_process_rss_bytes"])
            or evidence["process_identity_verified"] is not True
            or profile["ram_bytes"] < evidence["peak_process_rss_bytes"] + profile["ram_margin_bytes"]):
        raise ValueError("model profile lacks native process evidence and full peak margin")
    if native_peak:
        from .model_loading import ProcessIdentity
        processes = evidence["measured_processes"]
        if not isinstance(processes, list) or not 1 <= len(processes) <= 128:
            raise ValueError("bounded native measurement process identities required")
        identities = [ProcessIdentity(**p) for p in processes]
        if (len({p.pid for p in identities}) != len(identities)
                or len({p.boot_time for p in identities}) != 1):
            raise ValueError("ambiguous measurement process scope")
    for key in ("profile_id", "socket_id", "host_id", "approved_by"):
        identity(profile[key])
    for key in ("workload_sha256", "evidence_sha256"):
        sha256_hex(profile[key])
    sha256_hex(evidence["model_digest"])
    from .model_executor import MLXModelSpec, ModelSpec
    spec = (MLXModelSpec if mlx else ModelSpec)(**evidence["model_spec"])
    if spec.fingerprint() != profile["workload_sha256"] or spec.digest != evidence["model_digest"]:
        raise ValueError("model evidence does not match the reviewed target and context")
    if any(profile[k] != v for k, v in (("host_id", host), ("platform", system), ("architecture", architecture))):
        raise ValueError("model profile belongs to a different native host")
    if any(evidence[k] != profile[k] for k in ("host_id", "platform", "architecture", "workload_sha256")):
        raise ValueError("model measurement and profile identities differ")
    if (any(not positive(profile[k]) for k in ("ram_bytes", "cpu_units", "ram_margin_bytes"))
            or not positive(evidence["peak_resident_bytes"]) or type(evidence["sample_count"]) is not int
            or not 2 <= evidence["sample_count"] <= 10_000_000
            or not instant(evidence["sample_interval_seconds"]) or evidence["sample_interval_seconds"] <= 0
            or profile["ram_bytes"] < evidence["peak_resident_bytes"] + profile["ram_margin_bytes"]
            or not instant(evidence["peak_cpu_units"]) or not positive(evidence["cpu_concurrency_units"])
            or math.ceil(evidence["peak_cpu_units"]) > evidence["cpu_concurrency_units"]
            or evidence["cpu_concurrency_units"] > profile["cpu_units"]):
        raise ValueError("model allocation does not cover measured residency and margin")
    times = (evidence["started_at"], evidence["finished_at"], profile["approved_at"], profile["expires_at"], now)
    if (not all(instant(t) for t in times)
            or not evidence["started_at"] < evidence["finished_at"] <= profile["approved_at"] <= now < profile["expires_at"]
            or now - evidence["finished_at"] > max_age
            or evidence["finished_at"] - evidence["started_at"] <
               (evidence["sample_count"] - 1) * evidence["sample_interval_seconds"] * .9):
        raise ValueError("model measurement window is invalid or stale")
    return profile
