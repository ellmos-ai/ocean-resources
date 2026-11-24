# Drittanbieter-Lizenzen & Level-1 SBOM / Third-Party Licenses & Level-1 SBOM

> **Paket / Package:** `ocean-resources` (`0.2.0`)
> **Primärlizenz / Primary License:** `MIT` ([LICENSE](LICENSE))
> **Laufzeitabhängigkeiten (Standardinstallation) / Default Runtime Dependencies:** `0` (nur Python-Standardbibliothek / Python Standard Library only)
> **Stand / Last Verified:** `2026-11-24`

---

## 1. Laufzeitabhängigkeiten / Runtime Dependencies (`dependencies = []`)

| Komponente / Component | Version / Constraint | Lizenz / License (SPDX) | Rolle / Scope | Quelle / Upstream |
|---|---|---|---|---|
| Python Standard Library (`dataclasses`, `json`, `math`, `os`, `pathlib`, `subprocess`, `threading`, `http.server`, `urllib`, `ctypes`) | `>= 3.11` | `PSF-2.0` | Kernlaufzeit (Zero-Dependency-Standardinstallation) / Core runtime | https://www.python.org/ |

`ocean-resources` installiert standardmäßig **keine** externen Laufzeitpakete (`dependencies = []` in `pyproject.toml`). Alle reinen Arbitrierungs-, Vertrags-, Queue-Policy-, Cooldown- und JSON-Katalogpfade laufen ausschließlich mit der Python-Standardbibliothek.

---

## 2. Optionale Native & Hardware-Erweiterungen / Optional Native Extras

| Komponente / Component | Extra / Pfad | Version / Pin | Lizenz / License (SPDX) | Urheber / Maintainer | Zweck / Purpose |
|---|---|---|---|---|---|
| `psutil` | `ocean-resources[native]` | `>= 5.9.6, < 8` | `BSD-3-Clause` | Giampaolo Rodola & Contributors | Optionale Prozessbaum-, RSS-, CPU-Affinitäts- und Bootzeit-Messung (`capacity._psutil()`) |
| `mlx` | Opt-in C++-Quellbau (`native/mlx`) & Laufzeit | `== 0.32.0` | `MIT` | Apple Inc. (MLX Contributors) | Optionale Apple-Silicon-Metal-Array-Bindung (`_ocean_mlx_wired`, nicht automatisch installiert) |
| `mlx-lm` | Opt-in Apple-Silicon-Modellpfad | `== 0.31.3` | `MIT` | Apple Inc. (MLX Contributors) | Optionale Layout-/Quantisierungsprüfung für `QwenTextRuntime` |
| `numpy` | Opt-in Apple-Silicon-Modellpfad (`mlx_qwen_runtime.py`) | MLX-kompatibel | `BSD-3-Clause` | NumPy Developers | Subpage-Tensor-Pufferkonvertierung im optionalen In-Process-MLX-Laufzeitpfad |
| `nanobind` | Opt-in C++-Quellbau (`native/mlx/CMakeLists.txt`) | `== 2.13.0` | `BSD-3-Clause` | Wenzel Jakob & Contributors | C++20-/Python-Bindungsschicht für `native/mlx/bindings.cpp` |

> **Lizenzkompatibilität:** `BSD-3-Clause` und `MIT` sind uneingeschränkt kompatibel mit der `MIT`-Lizenz dieses Projekts. Keine der optionalen Komponenten wird binär in diesem Repository weiterverteilt; die Einbindung erfolgt ausschließlich zur Laufzeit bzw. beim expliziten lokalen Quellbau durch den Anwender.

---

## 3. Build-, Test- & CI-Werkzeuge / Build, Development & CI Tooling

| Komponente / Component | Bereich / Group | Version / Pin | Lizenz / License (SPDX) | Zweck / Purpose |
|---|---|---|---|---|
| `setuptools` | `[build-system]` | `>= 77.0.3` | `MIT` | PEP-517-/PEP-639-konformes Paket-Build-Backend |
| `build` | CI-Paketbau-Verifikation | PyPA `build` | `MIT` | Verifikation von `sdist`- und `wheel`-Artefakten in CI |
| `pytest` | `[project.optional-dependencies].dev` | `>= 8.0` | `MIT` | Hermetische Unit- und Vertrags-Testsuite |
| `ruff` | `[project.optional-dependencies].dev` | `>= 0.4` | `MIT` | Statische Code- und Stilprüfung |
| `actions/checkout` | `.github/workflows/ci.yml` | `v7` | `MIT` | GitHub-Actions-Checkout in CI |
| `actions/setup-python` | `.github/workflows/ci.yml` | `v7` | `MIT` | Python-Matrix (`3.11`, `3.12`, `3.13`) in CI |

---

## 4. Supply-Chain- & Sicherheits-Governance (`INV-SLA-09` / `INV-SLA-10`)

- **Minimale Angriffsfläche (`INV-LOCAL-01` / `INV-LOCAL-04`):** Keine Telemetrie, kein Cloud-Egress, keine impliziten Netzwerkaufrufe beim Import und keine privilegierten Systemrechte (`Zero-Root`).
- **Versionsschranken (Laufzeit & Build-Zeit):** Zur Python-Laufzeit werden optionale Abhängigkeiten (`psutil >= 5.9.6, < 8`, `mlx == 0.32.0`, `mlx-lm == 0.31.3`) vor der Nutzung geprüft und bei Abweichung fail-closed mit `Hold` abgewiesen; beim optionalen C++20-Quellbau (`native/mlx/CMakeLists.txt`) werden `nanobind == 2.13.0` (`EXACT`) und `mlx == 0.32.0` zur CMake-Konfigurationszeit erzwungen.
- **Automatisierte Überwachung:** Wöchentliche Dependabot-Prüfung für `github-actions` und `pip` (`.github/dependabot.yml`).
- **Verbindliche Reaktionsziele ([SECURITY.md](SECURITY.md)):** Eingangsbestätigung binnen **48 Stunden**, Triage binnen **5 Werktagen**, Behebungsziel für kritische/hohe Schwachstellen binnen **30 Kalendertagen**.
