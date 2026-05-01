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
editing, and (d) the AccCluster.py rebuild trap.

Reusable task prompts live under [.github/prompts/](prompts/).
