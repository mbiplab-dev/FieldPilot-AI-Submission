# FieldPilot AI documentation

Everything except the project description lives here. The root `README.md` says what FieldPilot
is; these files say how it works, how to run it, and what is and is not built.

## Start here

| Document | What it covers |
|---|---|
| [setup.md](setup.md) | Install, run the three services, connect a phone, troubleshooting |
| [commands.md](commands.md) | Every `make` target, what it does, and which are destructive |
| [architecture.md](architecture.md) | The event chain and why it is the one inviolable rule |

## How the system is built

| Document | What it covers |
|---|---|
| [agents.md](agents.md) | The ten-agent reference architecture mapped onto real modules |
| [prd.md](prd.md) | The original product requirements |
| [plan.md](plan.md) | The living implementation plan, including corrections to the PRD |
| [tracking.md](tracking.md) | Build status per milestone |
| [privacy-and-safety.md](privacy-and-safety.md) | Advisory posture, data handling, worker consent |

## Honest accounting

| Document | What it covers |
|---|---|
| [pitch-vs-code.md](pitch-vs-code.md) | Every pitch-deck claim marked built / built-differently / not built |
| [possibilities.md](possibilities.md) | The catalogue of construction CV applications |
| [possibilities-coverage.md](possibilities-coverage.md) | Which of those this system actually covers |
| [branch-integration.md](branch-integration.md) | How the `hrm` branch was merged in |
| [audit-2026-08-11.md](audit-2026-08-11.md) | Security and correctness audit — **6 Critical findings, all still open** |
| [build-log.txt](build-log.txt) | Dated log of what was built, why, and the traps hit along the way |

If you are picking this project up cold, read **audit-2026-08-11.md** first — it lists six open
Critical defects, including that most of the REST surface and the entire vision edge currently
accept unauthenticated requests. Then **pitch-vs-code.md**, which says which impressive-sounding
claims are real.

## Conventions

- Comments and docs explain **why**, not what.
- Nothing is stubbed to look finished. Unbuilt things are listed as not-started rather than
  faked, so a placeholder is never mistaken for a working path.
- Where a measurement is quoted, it was measured. Projections are labelled as projections.
