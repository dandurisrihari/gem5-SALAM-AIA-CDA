# Prompts

This folder stores reusable prompts for working on `gem5-SALAM-AIA-CDA`,
specifically around the **kernel-based memory validation** feature
(AIA ↔ KD SMID verification, designed to mitigate confused-deputy attacks
against hardware accelerators).

## Files

- `context-kernel-validation.md` — Background primer on the existing
  implementation. Paste/include this when starting a new chat so the
  assistant has the right entry points.
- `sanity-test.prompt.md` — Mandatory zero-latency sanity check after
  any edit to the IOMMU / AIA-KD scheduling or request paths in
  `src/hwacc/llvm_interface.{cc,hh}`. Catches event-queue perturbation
  bugs that make protection modes look faster (or crash StreamDma)
  than `plain`.
- Add new task-specific prompts here as `*.prompt.md`.

## Conventions

- Keep each prompt focused on a single task (one feature, one bug, one
  refactor).
- Reference exact file paths and function names so the assistant can jump
  straight into the code.
- Note any benchmark / latency / config that should be used to validate
  the change.
