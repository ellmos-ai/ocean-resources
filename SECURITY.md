# Sicherheitsrichtlinie / Security Policy

## Deutsch

### Sicherheitslücken melden

Wenn Sie eine Sicherheitslücke finden, melden Sie diese bitte verantwortungsvoll:

1. **Kein öffentliches Issue eröffnen**
2. **GitHub Private Vulnerability Reporting verwenden**
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
- IPC-Dateilocks und Socket-Pfade (`~/.bach/runtime/local-inference/`)
- API-Endpunkte für Prozess- und Modell-Claiming (`:8081` & `:11434`)
- Unprivilegierte Benutzer- und Prozessausführung (Zero-Root / Non-Privileged Execution)

### Reaktionszeit

Kritische Sicherheitsprobleme werden innerhalb von 48 Stunden gesichtet und priorisiert. Bitte geben Sie ausreichend Zeit für die Behebung, bevor Details öffentlich gemacht werden.

---

## English

### Reporting a Vulnerability

If you find a security vulnerability, please report it responsibly:

1. **Do not open a public issue**
2. **Use GitHub Private Vulnerability Reporting**
3. Include a detailed description, reproduction steps, and potential impact

### How to Report

1. Open in repository: `Security` → `Advisories` → `New`
2. Fill in the title, description, severity, and affected versions
3. Submit the report privately

If private vulnerability reporting is not yet enabled, contact the maintainers through GitHub and do not publish details in a public issue.

### Scope

- Local RAM allocation and system resource arbitrations
- IPC file locks and socket paths (`~/.bach/runtime/local-inference/`)
- Local API endpoints for process and model claiming (`:8081` & `:11434`)
- Unprivileged user and process execution (Zero-Root / Non-Privileged Execution)

### Response Time

Critical security issues will be triaged and prioritized within 48 hours. Please allow reasonable time for remediation before any public disclosure.
