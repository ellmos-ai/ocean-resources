# Beitragsrichtlinie / Contributing Guide

## Deutsch

Vielen Dank für Ihr Interesse, zu `ocean-resources` beizutragen!

### Wie Sie beitragen können

1. **Bug melden:** Erstellen Sie ein Issue mit dem Label `bug`
2. **Feature vorschlagen:** Erstellen Sie ein Issue mit dem Label `enhancement`
3. **Code beitragen:** Erstellen Sie einen Pull Request

### Pull Requests & Entwicklungs-Workflow

1. Forken und klonen Sie das Repository lokal (`git clone https://github.com/ellmos-ai/ocean-resources.git`).
2. Erstellen Sie einen dedizierten Feature- oder Fix-Branch: `git checkout -b feature/mein-feature` bzw. `fix/mein-fix`.
3. Halten Sie die Versionsnummer (`version` in `pyproject.toml`) unverändert; Versionserhöhungen erfolgen ausschließlich im Rahmen autorisierter Release-Zyklen.
4. Führen Sie vor jedem Commit alle Linter und Tests in einer isolierten virtuellen Umgebung aus:
   ```bash
   ruff check .
   python -m compileall -q ocean_resources tests
   python -m pytest
   git diff --check
   ```
5. Pushen Sie den Branch und eröffnen Sie einen Pull Request zur unabhängigen Zweitabnahme und CI-Prüfung.

### Lizenz- und Rechtehinweise

Pull Requests und Beiträge werden unter der MIT-Lizenz dieses Projekts bereitgestellt.

### Code-Richtlinien

- Python: PEP 8 Stil, Type Annotations (`from __future__ import annotations`), PEP 639 konforme Metadaten
- Encoding: UTF-8 für alle Dateien
- Keine hartcodierten lokalen Pfade, personenbezogenen Daten oder API-Tokens

---

## English

Thank you for your interest in contributing to `ocean-resources`!

### How to Contribute

1. **Report bugs:** Create an issue with the `bug` label
2. **Suggest features:** Create an issue with the `enhancement` label
3. **Contribute code:** Create a Pull Request

### Pull Requests & Development Workflow

1. Fork and clone the repository to a local working directory (`git clone https://github.com/ellmos-ai/ocean-resources.git`).
2. Create a dedicated feature or fix branch: `git checkout -b feature/my-feature` or `fix/my-fix`.
3. Keep the project version (`version` in `pyproject.toml`) strictly untouched; version bumps are reserved for formal release cycles.
4. Run all linters and test suites in an isolated virtual environment before committing:
   ```bash
   ruff check .
   python -m compileall -q ocean_resources tests
   python -m pytest
   git diff --check
   ```
5. Push the branch and open a Pull Request for independent review and CI verification.

### Licensing and Contribution Terms

All contributions and pull requests are submitted under the MIT License of this repository.

### Code Guidelines

- Python: PEP 8 style, strict typing, PEP 639 metadata compliance
- Encoding: UTF-8 for all files
- Never commit hardcoded credentials, personal paths, or API keys
