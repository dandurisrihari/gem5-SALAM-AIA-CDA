"""Stats / log harvesting -> summary.tsv + deltas.tsv.

Mirrors the awk logic from the legacy ``run_protection_compare.sh`` so
the output schema is unchanged. Multi-cluster aware: per-SMMU stats
are summed across all clusters before reporting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

# system.<cluster>.smmu.<stat>  -- multiple per run, one per AccCluster.
_SMMU_RE_TMPL = r"^system\.[A-Za-z0-9_]+\.smmu\.{stat}\s+(\S+)"
_SIM_TICKS_RE = re.compile(r"^simTicks\s+(\d+)", re.MULTILINE)
_SIM_SECONDS_RE = re.compile(r"^simSeconds\s+([0-9.eE+-]+)", re.MULTILINE)
_RUNTIME_RE = re.compile(r"Runtime:\s+(\S+)")
_AIA_OVERHEAD_RE = re.compile(r"TOTAL SECURITY OVERHEAD:\s+(\S+)\s+us")


@dataclass
class HarvestRow:
    """One row of the comparison TSV.

    Primary runtime metric is ``runtime_us`` derived from ``simTicks``
    (1 tick == 1 ps -> us = ticks / 1e6). The legacy per-cluster
    ``Runtime:`` printed by SALAM accelerators is reported separately as
    ``kernel_runtime_us`` because it only reflects the last cluster's
    elapsed time, not the end-to-end simulation.
    """
    label: str
    runtime_us: str = "-"           # from simTicks (end-to-end ground truth)
    sim_ticks: str = "-"
    sim_seconds: str = "-"
    kernel_runtime_us: str = "-"    # last `Runtime: N us` line in run.log
    ste_fetches: str = "-"
    cd_fetches: str = "-"
    ptw_samples: str = "-"
    trans_samples: str = "-"
    trans_mean_ps: str = "-"
    aia_kd_overhead_us: str = "-"

    def as_tsv(self) -> str:
        return "\t".join((
            self.label, self.runtime_us, self.sim_ticks, self.sim_seconds,
            self.kernel_runtime_us,
            self.ste_fetches, self.cd_fetches,
            self.ptw_samples, self.trans_samples,
            self.trans_mean_ps, self.aia_kd_overhead_us,
        ))


HEADER = ("mode\truntime_us\tsim_ticks\tsim_seconds\tkernel_runtime_us"
          "\tsteFetches\tcdFetches"
          "\tptw_samples\ttrans_samples\ttrans_mean_ps"
          "\taia_kd_overhead_us")


def _smmu_re(stat: str) -> re.Pattern[str]:
    return re.compile(_SMMU_RE_TMPL.format(stat=re.escape(stat)),
                      re.MULTILINE)


def _sum_smmu(stats_text: str, stat: str) -> str:
    vals = _smmu_re(stat).findall(stats_text)
    if not vals:
        return "-"
    total = sum(_to_number(v) for v in vals)
    return _fmt_number(total)


def _avg_smmu(stats_text: str, stat: str) -> str:
    vals = _smmu_re(stat).findall(stats_text)
    if not vals:
        return "-"
    nums = [_to_number(v) for v in vals]
    return _fmt_number(sum(nums) / len(nums))


def _to_number(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_number(n: float) -> str:
    return f"{int(n)}" if n.is_integer() else f"{n:g}"


def harvest_run(label: str, outdir: Path) -> HarvestRow:
    row = HarvestRow(label=label)
    stats_path = outdir / "stats.txt"
    log_path = outdir / "run.log"

    if not stats_path.exists():
        row.runtime_us = "FAIL"
        return row

    stats_text = stats_path.read_text()
    log_text = log_path.read_text() if log_path.exists() else ""

    if (m := _SIM_TICKS_RE.search(stats_text)):
        row.sim_ticks = m.group(1)
        # 1 tick = 1 ps  =>  us = ticks / 1e6.
        row.runtime_us = f"{int(m.group(1)) / 1e6:.3f}"
    if (m := _SIM_SECONDS_RE.search(stats_text)):
        row.sim_seconds = m.group(1)
    # Last `Runtime: N us` in run.log -- per-cluster, cross-check only.
    runtimes = _RUNTIME_RE.findall(log_text)
    if runtimes:
        row.kernel_runtime_us = runtimes[-1]
    if (m := _AIA_OVERHEAD_RE.search(log_text)):
        row.aia_kd_overhead_us = m.group(1)

    row.ste_fetches   = _sum_smmu(stats_text, "steFetches")
    row.cd_fetches    = _sum_smmu(stats_text, "cdFetches")
    row.ptw_samples   = _sum_smmu(stats_text, "ptwTimeDist::samples")
    row.trans_samples = _sum_smmu(stats_text, "translationTimeDist::samples")
    row.trans_mean_ps = _avg_smmu(stats_text, "translationTimeDist::mean")
    return row


def write_summary(rows: Iterable[HarvestRow], path: Path) -> List[HarvestRow]:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(HEADER + "\n")
        for r in rows:
            f.write(r.as_tsv() + "\n")
    return rows


def write_deltas(rows: List[HarvestRow], path: Path) -> None:
    """Compute Δticks / Δ% vs the row labelled 'plain' (if present).

    Uses ``simTicks`` as the runtime ground truth (1 tick == 1 ps).
    """
    plain = next((r for r in rows if r.label == "plain"), None)
    if plain is None or not plain.sim_ticks.isdigit():
        return
    base = int(plain.sim_ticks)
    if base <= 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write("mode\tsim_ticks\truntime_us\tdelta_ticks\t"
                "delta_us\tdelta_pct\n")
        for r in rows:
            if not r.sim_ticks.isdigit():
                f.write(f"{r.label}\t{r.sim_ticks}\t{r.runtime_us}\t-\t-\t-\n")
                continue
            v = int(r.sim_ticks)
            d = v - base
            pct = (d / base) * 100.0
            f.write(f"{r.label}\t{v}\t{r.runtime_us}\t{d}\t"
                    f"{d/1e6:.3f}\t{pct:.3f}\n")


def harvest_outdir(outroot: Path,
                   labels: Iterable[str] | None = None) -> List[HarvestRow]:
    """Harvest one outdir tree. If ``labels`` is None, use every subdir."""
    if labels is None:
        labels = sorted(p.name for p in outroot.iterdir() if p.is_dir())
    return [harvest_run(lbl, outroot / lbl) for lbl in labels]


def pretty_print(rows: Iterable[HarvestRow]) -> str:
    """Aligned text dump for stdout."""
    cols = HEADER.split("\t")
    out = ["  ".join(c.ljust(20) for c in cols)]
    for r in rows:
        out.append("  ".join(v.ljust(20) for v in r.as_tsv().split("\t")))
    return "\n".join(out)
