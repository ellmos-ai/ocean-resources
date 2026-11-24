# Sicherheitsrichtlinie / Security Policy

## Deutsch

### Sicherheitslücken melden

Wenn Sie eine Sicherheitslücke finden, melden Sie diese bitte verantwortungsvoll:

1. **Kein öffentliches Issue eröffnen**
2. **GitHub Private Vulnerability Reporting verwenden** (sofern im Repository aktiviert)
3. Beschreibung, Reproduktionsschritte und potenzielle Auswirkungen angeben

### So melden Sie ein Problem

1. Öffnen Sie im Repository: `Security` → `Advisories` → `New`
2. Tragen Sie Titel, Beschreibung, Schweregrad und betroffene Versionen ein
3. Reichen Sie die Meldung privat ein

Falls Private Vulnerability Reporting im Repository noch nicht aktiviert ist,
kontaktieren Sie die Maintainer direkt über GitHub und veröffentlichen Sie
keine Details in einem öffentlichen Issue.

### Geltungsbereich

- Lokale RAM-Allokation und Kernel-/Systemressourcenzuteilung
- IPC-Dateilocks, Writer-Journale und Socket-Pfade (`~/.bach/runtime/local-inference/`)
- Lokale Loopback-API-Endpunkte für Prozess- und Modell-Claiming (`127.0.0.1`)
- Unprivilegierte Benutzer- und Prozessausführung (Zero-Root / Non-Privileged Execution)
- Supply-Chain-Integrität, Abhängigkeits-Pins und Paketmetadaten

### Reaktionszeiten & Sicherheits-SLAs (`INV-SLA-09` / `INV-SLA-10`)

- **Eingangsbestätigung:** innerhalb von **48 Stunden**
- **Ersteinschätzung & Triage:** innerhalb von **5 Werktagen**
- **Behebung kritischer / hoher Schwachstellen:** Zielkorridor **30 Kalendertage**
- **Abhängigkeits-Hygiene:** wöchentliche automatisierte Prüfung von GitHub Actions und Python-Abhängigkeiten über Dependabot

Bitte räumen Sie angemessene Zeit für die koordinierte Behebung ein, bevor Details öffentlich gemacht werden.

---

## English

### Reporting a Vulnerability

If you find a security vulnerability, please report it responsibly:

1. **Do not open a public issue**
2. **Use GitHub Private Vulnerability Reporting** (when enabled on the repository)
3. Include a detailed description, reproduction steps, and potential impact

### How to Report

1. Open in repository: `Security` → `Advisories` → `New`
2. Fill in the title, description, severity, and affected versions
3. Submit the report privately

If private vulnerability reporting is not yet enabled on the repository, contact the maintainers directly through GitHub and do not publish details in a public issue.

### Scope

- Local RAM allocation and system resource arbitration
- IPC file locks, writer journals, and socket paths (`~/.bach/runtime/local-inference/`)
- Local loopback API endpoints for process and model claiming (`127.0.0.1`)
- Unprivileged user and process execution (Zero-Root / Non-Privileged Execution)
- Supply-chain integrity, dependency version bounds, and package metadata

### Response Times & Security SLAs (`INV-SLA-09` / `INV-SLA-10`)

- **Acknowledgement:** within **48 hours**
- **Initial triage & severity assessment:** within **5 business days**
- **Remediation target (critical / high severity):** within **30 calendar days**
- **Dependency hygiene:** weekly automated monitoring of GitHub Actions and Python dependencies via Dependabot

Please allow reasonable time for coordinated remediation before any public disclosure.
