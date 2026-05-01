"""speedkills: gem5-SALAM experiment driver.

One-stop modular runner for protection-model comparison, latency sweeps,
single-bench debug runs, and stats harvesting.

CLI entry point::

    python -m speedkills <subcommand> [options]

Subcommands: compare | sweep | run | harvest | monitor
"""
__all__ = ["config", "benchmarks", "profiles", "runner", "harvest"]
