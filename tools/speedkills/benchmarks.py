"""Per-benchmark metadata.

Single source of truth for which directory holds each benchmark's HW/SW
artefacts and which top-level YAML the SALAM Configurator should consume.

Variants of the *same* underlying benchmark (e.g. ``mobilenetv2`` /
``mobilenetv2_35`` / ``mobilenetv2_75``) share a ``bench_path``; this is
why their SW build cannot run in parallel (they'd clobber each other's
``sw/main.elf``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable

from . import config as cfg


@dataclass(frozen=True)
class Bench:
    """One benchmark variant runnable end-to-end."""
    name: str            # --accbench / fs_<name>.py
    path: Path           # benchmark directory (holds sw/, hw/, configs/)
    config_yml: str      # top-level YAML for the Configurator

    @property
    def kernel_elf(self) -> Path:
        return self.path / "sw" / "main.elf"

    @property
    def fs_script(self) -> Path:
        return cfg.CONFIGS_DIR / f"fs_{self.name}.py"


def _b(name: str, rel_path: str, yml: str = "config.yml") -> Bench:
    return Bench(name=name, path=cfg.M5_PATH / rel_path, config_yml=yml)


# Group name -> ordered list of bench names. Variants in the same group
# SHARE a path and therefore must build/run sequentially within the group.
GROUPS: Dict[str, list[str]] = {
    "mobilenetv2": ["mobilenetv2", "mobilenetv2_35", "mobilenetv2_75"],
    "lenet":       ["lenet_a", "lenet_b", "lenet_c"],
    "bfs":         ["bfs"],
    "fft":         ["fft"],
    "gemm":        ["gemm"],
    "md_grid":     ["md_grid"],
    "md_knn":      ["md_knn"],
    "nw":          ["nw"],
    "spmv":        ["spmv"],
    "stencil2d":   ["stencil2d"],
    "stencil3d":   ["stencil3d"],
    "mergesort":   ["mergesort"],
}

REGISTRY: Dict[str, Bench] = {b.name: b for b in (
    _b("mobilenetv2",    "benchmarks/mobilenetv2",            "1_config.yml"),
    _b("mobilenetv2_35", "benchmarks/mobilenetv2",            "35_config.yml"),
    _b("mobilenetv2_75", "benchmarks/mobilenetv2",            "75_config.yml"),
    _b("lenet_a",        "benchmarks/lenet/design_a"),
    _b("lenet_b",        "benchmarks/lenet/design_b"),
    _b("lenet_c",        "benchmarks/lenet/design_c"),
    _b("bfs",            "benchmarks/sys_validation/bfs"),
    _b("fft",            "benchmarks/sys_validation/fft"),
    _b("gemm",           "benchmarks/sys_validation/gemm"),
    _b("md_grid",        "benchmarks/sys_validation/md_grid"),
    _b("md_knn",         "benchmarks/sys_validation/md_knn"),
    _b("nw",             "benchmarks/sys_validation/nw"),
    _b("spmv",           "benchmarks/sys_validation/spmv"),
    _b("stencil2d",      "benchmarks/sys_validation/stencil2d"),
    _b("stencil3d",      "benchmarks/sys_validation/stencil3d"),
    _b("mergesort",      "benchmarks/sys_validation/mergesort"),
)}


def resolve(name: str,
            bench_path: str | None = None,
            config_yml: str | None = None) -> Bench:
    """Look up a registered benchmark or build a custom one on the fly."""
    if name in REGISTRY and bench_path is None and config_yml is None:
        return REGISTRY[name]
    base = REGISTRY.get(name)
    path = Path(bench_path) if bench_path else (base.path if base else None)
    yml = config_yml or (base.config_yml if base else "config.yml")
    if path is None:
        raise KeyError(
            f"Unknown benchmark '{name}'. Pass --bench-path / --config "
            f"or use one of: {', '.join(sorted(REGISTRY))}"
        )
    if not path.is_absolute():
        path = cfg.M5_PATH / path
    return Bench(name=name, path=path, config_yml=yml)


def group_for(bench_name: str) -> str | None:
    for g, members in GROUPS.items():
        if bench_name in members:
            return g
    return None


def all_names() -> Iterable[str]:
    return REGISTRY.keys()
