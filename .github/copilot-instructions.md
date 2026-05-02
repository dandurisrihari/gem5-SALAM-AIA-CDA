# Copilot Instructions

When helping with this repository, especially on the kernel-based
memory validation feature, first read
[.github/prompts/context-kernel-validation.md](prompts/context-kernel-validation.md)
for an up-to-date map of the relevant files, runtime path, and gotchas.
**The "Working agreements" section near the top of that file is
mandatory reading every session** — it covers (a) keeping the prompt
in sync with the code, (b) using `python3 -m tools.speedkills` (the
`tools/speedkills/` package) as
the single run driver, (c) reasoning about cost / blast radius before
editing, (d) the AccCluster.py rebuild trap, and (e) running
`python3 tests/aia_cda_tests/run_sanity.py --regen` after any change
that triggers a `gem5.opt` rebuild — and **not committing** if it
fails.

Reusable task prompts live under [.github/prompts/](prompts/).
