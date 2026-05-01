# speedkills/legacy

Pre-package shell drivers, kept verbatim for reference / git archaeology.
**Do not use these for new work** — every flag and profile they hard-code
has been ported into the modular speedkills package:

| Legacy script               | Modern equivalent                                  |
|-----------------------------|----------------------------------------------------|
| `run_protection_compare.sh` | `python3 -m tools.speedkills compare`              |
| `run_parallel.sh`           | `python3 -m tools.speedkills sweep`                |
| `run_system.sh`             | `python3 -m tools.speedkills run`                  |

If you find yourself wanting a flag that exists here but not in the
package, add it to `tools/speedkills/profiles.py` (or the appropriate
subcommand in `tools/speedkills/__main__.py`) instead of editing these
files. Then update `.github/prompts/context-kernel-validation.md` to
match.
