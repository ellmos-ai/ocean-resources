# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Hardware- & RAM-Coupled Model Tuning Catalog & Release Hygiene (2026-11-27)
- **Hardware- & RAM-Coupled Model Tuning Catalog (`ocean.model-tuning-catalog.v1`):** Added `ocean_resources/model-tuning-catalog.v1.json` and `ocean_resources/model_tuning_catalog.py` (`ResolvedModelTuning`, `detect_host_ram_gib`, `load_model_tuning_catalog`, `resolve_model_tuning`, `export_clutch_local_gears`) providing zero-config safe context/KV-cache defaults across host RAM tiers (`slot_override > model_config > catalog_default > env_fallback > safe_baseline`).
- **Release-Readiness, Packaging & Multi-Host Lock Defense:** Verified `model-tuning-catalog.v1.json` inclusion across `sdist`, `wheel`, `importlib.resources`, and CI stdlib-only import gates; registered `control.resources.model-tuning-catalog` in `ellmos-module.v2.json`; synchronized `README.md`, `README_de.md`, `SKILL.md`, and `llms.txt`; and hardened `.gitignore` with complete multi-agent `LOCK*.txt` and localized sync-conflict exclusions (version `0.2.0` preserved).

### Security & Supply-Chain Hardening (2026-11-24)
- **Fail-Closed Numeric, Temporal & Type Validation (`CWE-20` / `CWE-704`):** Hardened `StressSample`, `RunCooldownPolicy`, `CooldownDecision`, `FallbackDecision`, `swapout_mib_per_minute`, `RunCooldownGate.check`, `ConsumerCapability`, `DistributorRequest`, `WatchdogHysteresisPolicy`, and `RAMDistributor.evaluate` to reject `NaN`, `±Inf`, negative values, backward sample timestamps, sub-second baseline sliding under active swap, `bool`-as-`int` confusion, non-boolean truthy flags, single-pass iterator exhaustion, and unknown pressure states before mutating state or admitting workloads.
- **Zero-Dependency & Version-Pin Contract:** Replaced unconditional module-level `psutil` import in `ocean_resources/model_dispatch.py` and unpinned import in `ocean_resources/mlx_model_port.py` with `capacity._psutil()` (`5.9.6 <= psutil < 8`), preserving stdlib-only imports when `ocean-resources[native]` is not installed and failing closed with `Hold` on dead/zombie processes or unapproved `psutil` versions.
- **SBOM, Security SLAs, CI Build Gate & Packaging Completeness:** Added `THIRD_PARTY_LICENSES.md` (Level-1 SBOM including optional MLX/`numpy`/`nanobind` and CI `build` pins), `TODO.md` (10-gate release readiness table), PEP 561 `ocean_resources/py.typed` marker, binding `INV-SLA-09` / `INV-SLA-10` response targets in `SECURITY.md` (`48h` / `5d` / `30d`), stdlib-only import + `sdist`/`wheel` verification in `.github/workflows/ci.yml`, enabled weekly Dependabot `pip` PRs (`open-pull-requests-limit: 5`), aligned `SKILL.md` visibility (`public`), and cleaned internal workstation path/tag references from `CONTRIBUTING.md` and `README*.md`.
- **Pre-Push Data-Gate & `.gitignore` Defense:** Expanded `.gitignore` with database/SQLite/WAL, CSV export/dump, `inbox_config*`, `TO-DECIDE-USER*`, `data/temp/**`, TLS/SSH key/token, and multi-host sync-conflict exclusions (version `0.2.0` preserved).

### Added (2026-11-14, Path A Hygiene)
- Cross-platform CI workflow (`.github/workflows/ci.yml`) testing Python 3.11–3.13 across `ubuntu-latest`, `windows-latest`, and `macos-latest` with whitespace, `ruff`, `compileall`, CLI resource smoke checks, and `pytest` gates.
- GitHub community & dependency governance: `.github/dependabot.yml` (`github-actions` + security `pip`) and bilingual `.github/ISSUE_TEMPLATE/` (`bug_report.md`, `feature_request.md`).
- Packaging & PEP 621/639 completeness: `MANIFEST.in`, `license-files = ["LICENSE"]`, non-license PyPI classifiers, expanded `[project.urls]`, `[project.optional-dependencies]` (`dev`), `[tool.setuptools.package-data]` (`manifest.json`, `pipeline-catalog.json`), `[tool.ruff]`, and hardened `[tool.pytest.ini_options]` (`--strict-markers`, `--strict-config`, `xfail_strict = true`).
- Hardened `.gitignore` with fleet lock defense (`LOCK*.txt`, `LOCK-CACHE.*`, `LOCK.permissions.*`), internal log exclusions, and tool/venv caches.
- Expanded metadata contract tests in `tests/test_metadata.py` (CI matrix, Dependabot, Issue templates, `.gitignore` lock defense, LLM install guidance, and packaged JSON resource smoke checks).
- Bilingual LLM installation prompt section (`Install with your LLM` / `Mit deinem LLM installieren`) in `README.md` and `README_de.md`, machine-readable install/verification commands in `llms.txt`, and updated test/verification badges (version `0.2.0` preserved per freeze).

### Added (2026-11-08, Run Cooldown Admission)
- `RunCooldownGate`, `RunCooldownPolicy` and `CooldownDecision` in `ram_distributor.py`: admission before each run of a local large consumer with the reasons `cooldown:swap` (swap-out rate above 32 MiB/min or memory pressure above normal), `cooldown:thermal` (`pmset -g therm` warning) and `cooldown:min` (60 s minimum pause). A failed probe drops only its own criterion, so unknown swap never blocks permanently.
- `StressSample` and read-only `sample_stress()` in `host.py` (`vm_stat` Swapouts, `sysctl kern.memorystatus_vm_pressure_level`, `pmset -g therm`; no sudo, no writes).

### Corrected (2026-11-04)
- Bound the runnable selector example to a validated model target and host-owned foreground streak.
- Limit fairness claims to validated live snapshots, carried host state and no legacy foreground hold; label diagrams as target integration rather than verified dispatch.
- Execute the documented example and synthetic snapshot fairness/legacy-hold boundary tests: 17 local tests passed. No runtime change, release or deployment acceptance.

### Added
- Bilingual documentation architecture (`README.md` in English and `README_de.md` in German) with bilateral section navigation (`sec-01` through `sec-08`).
- Dual Mermaid diagrams: System Architecture Topology (`flowchart TD`) and Two-Stage Claiming Lifecycle (`sequenceDiagram`).
- Standard governance and community files: `LICENSE` (MIT), `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`.
- AI Agent guidance files: `llms.txt`, `llm.txt`, and reusable product skill `SKILL.md`.
- Hermetic metadata and contract test suite in `tests/test_metadata.py` (PEP 639 license, version freeze, bilateral anchor parity, public API exports, Mermaid syntax protection).
- Unit test coverage for `WatchdogHysteresisPolicy` in `tests/test_ram_distributor.py`.
- Discoverability metadata: PEP 621 `keywords`, `project.urls`, and GitHub repository topics.
- Pytest runtime configuration in `pyproject.toml` (`pythonpath = ["."]`).

### Changed
- `pyproject.toml` expanded with discoverability metadata while preserving version freeze at `0.2.0`.

## [0.2.0] - 2026-11-03

### Added
- Decoupled Ocean resource core: `RAMDistributor`, `ConsumerCapability`, `UserProximity`, `WatchdogHysteresisPolicy`.
- Deterministic cross-process inference queue policy (`inference_queue_policy.py`) with 3:1 foreground fairness.
