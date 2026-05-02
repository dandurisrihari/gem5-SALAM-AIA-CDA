# Prompts — Routing Guide

This folder stores reusable prompts for working on
`gem5-SALAM-AIA-CDA`, the branch that compares two protection
mechanisms (AIA-KD software validator vs IOMMU per-access tax) on the
same SALAM accelerator platform.

**New here?** Read [00-architecture-overview.md](00-architecture-overview.md)
first — one page, jump map, diagrams. Then come back.

## When to use which prompt

| Your task                                                       | Read first                                                                                          |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| "Where is X? What does this branch do?"                          | [00-architecture-overview.md](00-architecture-overview.md)                                          |
| Edit AIA-KD logic (validator, latency, coalescing, RAW)          | [01-aia-kd-design.md](01-aia-kd-design.md) + [04-working-agreements.md](04-working-agreements.md)   |
| Edit IOMMU logic (IOTLB, port deadline, response hook)           | [02-iommu-design.md](02-iommu-design.md)  + [04-working-agreements.md](04-working-agreements.md)    |
| Add / refactor a SimObject; modify the SALAM-Configurator         | [03-simobject-layout.md](03-simobject-layout.md)                                                    |
| Run experiments, add a new CLI flag, add a new mode               | [05-cli-and-recipes.md](05-cli-and-recipes.md)                                                      |
| Sanity-check after a rebuild                                     | [sanity-test.prompt.md](sanity-test.prompt.md) (and the auto-runner `tests/aia_cda_tests/run_sanity.py`) |
| A test failed in a weird way                                     | [06-gotchas-and-history.md](06-gotchas-and-history.md) — odds are it's documented                   |
| Onboarding a new agent / starting a fresh chat                   | this file → 00 → 04 (rules) → the topic-specific file                                               |

## File layout

| File                                                             | Purpose                                                          |
|------------------------------------------------------------------|------------------------------------------------------------------|
| [00-architecture-overview.md](00-architecture-overview.md)       | One-page mental model + jump map.                                |
| [01-aia-kd-design.md](01-aia-kd-design.md)                       | AIA-KD mechanism, `AiaKdValidator` SimObject, runtime path.      |
| [02-iommu-design.md](02-iommu-design.md)                         | IOMMU mechanism, `AcceleratorIommu` SimObject, behavior classes. |
| [03-simobject-layout.md](03-simobject-layout.md)                 | gem5 SimObject contract; `.py` / `.hh` / `.cc` / SConscript / generator. |
| [04-working-agreements.md](04-working-agreements.md)             | Non-negotiable rules. Read every session.                        |
| [05-cli-and-recipes.md](05-cli-and-recipes.md)                   | CLI flags, mode definitions, `tools.speedkills` recipes.         |
| [06-gotchas-and-history.md](06-gotchas-and-history.md)           | Subtle invariants + things that have broken in the past.         |
| [sanity-test.prompt.md](sanity-test.prompt.md)                   | Manual zero-latency sanity recipe (the `--regen` script automates this). |
| `citations.txt`                                                  | Reference papers / docs used to size parameters.                 |

## Conventions for adding new prompts

- Keep each prompt focused on a single concern. The numbered files
  above replaced an earlier monolithic file because mixing topics
  made it hard for agents to find what they needed quickly.
- Reference exact file paths and function names so the assistant can
  jump straight into the code.
- Note any benchmark / latency / config that should be used to
  validate a change.
- If you add a new prompt, add it to the routing table above and to
  the `When to use which prompt` selector.
- If you change behaviour documented in any prompt, **update the
  prompt in the same commit** (rule 1 of
  [04-working-agreements.md](04-working-agreements.md)).
