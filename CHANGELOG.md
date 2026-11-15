# Changelog

All notable changes to Delta Table Editor are documented here.

## [1.0.0] — 2026-11

### Added
- Editable data grid with inline column filters and click-to-edit cells
- Review diff panel — shows old → new values before committing a save
- Paste rows — paste CSV text directly into the grid
- Bulk upload — CSV / Excel / TSV with three modes: update by PK, append, overwrite
- Streaming export — server-side export to Unity Catalog Volume → browser download
- Approval workflow — per-table policy: stage → review → approve / reject → apply
- Business rule engine — 12 rule types (regex, date_order, lookup, min/max, etc.)
- Optimistic locking — `version` column prevents lost updates on concurrent edits
- Per-user auth — `X-Forwarded-Access-Token` forwarded to every SQL call; Unity Catalog enforces grants automatically
- Column-level audit log — old value / new value / user / timestamp per changed column
- AI config generator (`agent-builder/`) — LLM agent auto-generates table YAML from `DESCRIBE TABLE`
- Idle auto-stop — app stops after configurable idle period to reduce compute cost
- Config-driven onboarding — add a new table in ~10 minutes via YAML, no code changes
- Multi-table navigation — Catalog → Schema → Table → Column selector in top nav
- Zero CDN — Vite-built static assets only; no runtime CDN dependency
