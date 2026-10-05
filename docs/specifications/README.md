# Approved Specification Index

These Version 1.1 Markdown documents are read-only and authoritative for implementation.

| File | Authority |
| --- | --- |
| `01_PRD.md` | Product intent and business rules |
| `02_TRD.md` | Architecture, security, and technical quality |
| `03_UI_UX_SPECIFICATION.md` | Interaction behavior, localization, RTL, and accessibility |
| `04_APPLICATION_FLOW.md` | Detailed flows, transitions, alternate paths, and permissions |
| `05_BACKEND_SCHEMA.md` | Persistence entities, constraints, indexes, and concurrency |
| `06_IMPLEMENTATION_PLAN.md` | Delivery order, gates, tests, rehearsals, and release evidence |

Do not edit these files during implementation. Requirement changes must be reviewed separately and synchronized across affected specifications.

Change note (2026-10-02, at the owner's request): the tool-specific wording of
`06_IMPLEMENTATION_PLAN.md` §3.1 and §24 was made tool-neutral, and the former
session-continuity document `07` was removed from the repository (its approved
decisions are recorded in these specifications and in `docs/execution/`). No
requirement changed.
