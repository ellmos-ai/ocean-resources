# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (2026-11-14, Path A Hygiene)
- Cross-platform CI workflow (`.github/workflows/ci.yml`) testing Python 3.11–3.13 across `ubuntu-latest`, `windows-latest`, and `macos-latest` with whitespace, `ruff`, `compileall`, CLI resource smoke checks, and `pytest` gates.
- GitHub community & dependency governance: `.github/dependabot.yml` (`github-actions` + security `pip`) and bilingual `.github/ISSUE_TEMPLATE/` (`bug_report.md`, `feature_request.md`).
- Packaging & PEP 621/639 completeness: `MANIFEST.in`, `license-files = ["LICENSE"]`, non-license PyPI classifiers, expanded `[project.urls]`, `[project.optional-dependencies]` (`dev`), `[tool.setuptools.package-data]` (`manifest.json`, `pipeline-catalog.json`), `[tool.ruff]`, and hardened `[tool.pytest.ini_options]` (`--strict-markers`, `--strict-config`, `xfail_strict = true`).
- Hardened `.gitignore` with fleet lock defense (`LOCK*.txt`, `LOCK-CACHE.*`, `LOCK.permissions.*`), internal log exclusions, and tool/venv caches.
- Expanded metadata contract tests in `tests/test_metadata.py` (CI matrix, Dependabot, Issue templates, `.gitignore` lock defense, LLM install guidance, and packaged JSON resource smoke checks).
- Bilingual LLM installation prompt section (`Install with your LLM` / `Mit deinem LLM installieren`) in `README.md` and `README_de.md`, machine-readable install/verification commands in `llms.txt`, and updated test/verification badges (version `0.2.0` preserved per freeze).

### Added (2026-11-08, PB-26)
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
- `pyproject.toml` expanded with discoverability metadata while preserving version freeze at `0.2.0` (T-20260920-167562623).

## [0.2.0] - 2026-11-03

### Added
- Decoupled Ocean resource core: `RAMDistributor`, `ConsumerCapability`, `UserProximity`, `WatchdogHysteresisPolicy`.
- Deterministic cross-process inference queue policy (`inference_queue_policy.py`) with 3:1 foreground fairness.
