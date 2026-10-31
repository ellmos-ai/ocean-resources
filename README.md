# ocean-resources

Herausgelöstes Ocean-Ressourcenmodul (`#2441`, `#1949`, `#2027`, `#2028`, `#2152`, `#2334`, `#2419`):
- **`RAMDistributor`**: Einziger Scheduler für RAM-Fackel, Modell-Residenz und Compute-Jobs.
- **`ConsumerCapability`**: Anschlusskriterium mit Fähigkeitsmeldung (`weights_ram_bytes`, `kv_reserve_bytes`, `fallback_chain`).
- **`UserProximity`**: Prioritätsklassen nach Nutzernähe mit Vererbung (`DIRECT_INTERACTIVE`, `DELEGATED_FOREGROUND`, `SCHEDULED_ROUTINE`, `AUTONOMOUS_BACKGROUND`).
- **`WatchdogHysteresisPolicy`**: Hysterese-Schutz beim 27B-Kaltstart (ersetzt `free_emergency_pages=0`).
