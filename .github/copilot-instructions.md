# Copilot Instructions

> Routing guide for Copilot / any LLM agent working on this branch.
> Keep this file short — full context lives in
> [.github/prompts/](prompts/), one focused file per concern.

## What this branch is

`gem5-SALAM-AIA-CDA` (`aia_cda_rel` branch) extends gem5-SALAM with
two protection mechanisms gating accelerator memory access, modeled
side-by-side on the same platform:

- **AIA-KD** — software validator, per-page first-touch tax via host
  IRQ. Cluster-shared state lives on the `AiaKdValidator` SimObject.
- **IOMMU**  — hardware-style per-access tax through one shared SMMU
  port. Cluster-shared state lives on the `AcceleratorIommu` SimObject.

Both mechanisms are mutually exclusive. `plain` mode is the
unprotected baseline.

## Start here, every session

1. **Read [.github/prompts/00-architecture-overview.md](prompts/00-architecture-overview.md).**
   One-page mental model: goal, the two mechanisms, the SimObject
   layout, key files, jump map.
2. **Read [.github/prompts/04-working-agreements.md](prompts/04-working-agreements.md).**
   Non-negotiable rules: keep prompts in sync with code, use
   `python3 -m tools.speedkills` as the single run driver, the
   `AccCluster.py` rebuild trap, run
   `python3 tests/aia_cda_tests/run_sanity.py --regen` after every
   rebuild, **don't commit if sanity fails**.
3. **Pick the topic-specific prompt** for your task from
   [.github/prompts/README.md](prompts/README.md)'s routing table:
   - editing AIA-KD logic → `01-aia-kd-design.md`
   - editing IOMMU logic → `02-iommu-design.md`
   - SimObject / generator work → `03-simobject-layout.md`
   - running experiments / CLI → `05-cli-and-recipes.md`
   - debugging weird failures → `06-gotchas-and-history.md`

## Rules of thumb

- **Prefer editing existing files over creating new ones.** This
  codebase has a tight conventional layout; new files almost always
  need a SConscript / generator edit too. See `03-simobject-layout.md`
  for the SimObject file-triplet contract.
- **State your plan before editing.** What file, what symbol, what
  could break, how you will verify (rule 3 of `04-working-agreements`).
- **Always pipe `yes ""` into `scons`** — the build hangs on the
  one-time gem5 git-hook installer prompt otherwise:

  ```bash
  yes "" | scons build/ARM/gem5.opt -j$(nproc) 2>&1 | tail -20
  ```

- **Run the sanity suite after every rebuild.** It's under a minute
  and catches the regression classes documented in
  `06-gotchas-and-history.md`:

  ```bash
  python3 tests/aia_cda_tests/run_sanity.py --regen
  ```

- **If you change a prompt-documented behaviour, update the prompt
  in the same commit.** Stale prompts mislead the next agent.

## Tick base

1 tick = 1 ps (gem5 default). All latencies in code/configs are in
ticks; convert to time with ticks × 1 ps.
