# Prompts

This folder stores reusable prompts for working on `gem5-SALAM-AIA-CDA`,
specifically around the **kernel-based memory validation** feature
(AIA ↔ KD SMID verification, designed to mitigate confused-deputy attacks
against hardware accelerators).

## Files

- `context-kernel-validation.md` — Background primer on the existing
  implementation. Paste/include this when starting a new chat so the
  assistant has the right entry points.
- Add new task-specific prompts here as `*.prompt.md`.

## Conventions

- Keep each prompt focused on a single task (one feature, one bug, one
  refactor).
- Reference exact file paths and function names so the assistant can jump
  straight into the code.
- Note any benchmark / latency / config that should be used to validate
  the change.
