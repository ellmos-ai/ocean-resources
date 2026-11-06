# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
