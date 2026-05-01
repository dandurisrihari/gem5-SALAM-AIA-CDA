#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_protection_compare.sh
#
# Canonical sweep driver for the protection-model comparison.
# Runs one SALAM benchmark across several configurations in parallel
# and writes a single summary TSV.
#
# Modes (toggle with --modes "a b c ..."; default = all):
#   plain                 no protection (baseline)
#   aia-kd                AIA-KD kernel-driver validation (our proposal)
#   smmu-bypass           real SMMUv3 in path, stream table NOT programmed
#                         (silicon-default "SMMU present, disabled")
#   smmu-iot              fully-programmed SMMU, low-end IoT / MMU-400
#                         profile (no walk cache, TLB 16, 1 PTW thread)
#   smmu-mmu500           fully-programmed SMMU, MMU-500 small-config
#                         (small walk cache enabled, TLB 32)
#   smmu-server           fully-programmed SMMU, gem5 server defaults
#                         (huge walk cache, TLB 2048)
#   smmu-tlb-sweep        fully-programmed SMMU at IoT profile, swept
#                         over --tlb-sweep TLB sizes (one run per size)
#
# About parallelism:
#   gem5.opt is read-only -- multiple invocations share it. We just need
#   a unique --outdir per run. Background each run, then wait.
#
# Usage:
#   tools/run_protection_compare.sh \
#       [--bench mobilenetv2] \
#       [--outdir DIR] \
#       [--modes "plain aia-kd smmu-iot smmu-mmu500 smmu-server"] \
#       [--tlb-sweep "8 16 32 64 256 2048"] \
#       [--granule-kib 4] \
#       [--jobs 4] \
#       [--extra "--some --gem5 --flag"]
#
# Examples:
#   # 4-tier SoC comparison on mobilenetv2 (recommended default)
#   tools/run_protection_compare.sh --bench mobilenetv2 \
#       --outdir $PWD/BM_ARM_OUT/protection_compare_mbnet_tiers \
#       --modes "plain aia-kd smmu-iot smmu-mmu500 smmu-server" \
#       --jobs 5
#
#   # TLB sensitivity sweep at IoT profile
#   tools/run_protection_compare.sh --bench gemm \
#       --outdir $PWD/BM_ARM_OUT/gemm_tlb_sweep \
#       --modes "plain smmu-tlb-sweep" \
#       --tlb-sweep "8 16 32 64 256 2048" --jobs 6
# ---------------------------------------------------------------------------
set -uo pipefail

BENCH=mobilenetv2
OUTROOT="$PWD/BM_ARM_OUT/protection_compare"
MODES="plain aia-kd smmu-bypass smmu-iot smmu-mmu500 smmu-server"
TLB_SWEEP="8 16 32 64 256 2048"
GRANULE_KIB=4
JOBS=4
EXTRA=""
M5_PATH="$PWD"
BIN="$M5_PATH/build/ARM/gem5.opt"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bench)       BENCH="$2"; shift 2 ;;
        --outdir)      OUTROOT="$2"; shift 2 ;;
        --modes)       MODES="$2"; shift 2 ;;
        --tlb-sweep)   TLB_SWEEP="$2"; shift 2 ;;
        --granule-kib) GRANULE_KIB="$2"; shift 2 ;;
        --jobs)        JOBS="$2"; shift 2 ;;
        --extra)       EXTRA="$2"; shift 2 ;;
        -h|--help)     sed -n '2,55p' "$0"; exit 0 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Bench path autodetect: sys_validation/<bench> or top-level <bench>
if [[ -d "$M5_PATH/benchmarks/sys_validation/$BENCH" ]]; then
    BENCH_PATH="$M5_PATH/benchmarks/sys_validation/$BENCH"
else
    BENCH_PATH="$M5_PATH/benchmarks/$BENCH"
fi
KERNEL="$BENCH_PATH/sw/main.elf"
DISK="$M5_PATH/benchmarks/common/fake.iso"
FS_SCRIPT="$M5_PATH/configs/SALAM/fs_${BENCH}.py"

[[ -x "$BIN" ]]       || { echo "gem5.opt not found at $BIN" >&2; exit 1; }
[[ -f "$KERNEL" ]]    || { echo "Kernel ELF missing: $KERNEL" >&2; exit 1; }
[[ -f "$FS_SCRIPT" ]] || { echo "fs script missing: $FS_SCRIPT" >&2; exit 1; }

mkdir -p "$OUTROOT"
SUMMARY="$OUTROOT/summary.tsv"

SYS_OPTS=(
    --mem-size=4GB
    --mem-type=DDR4_2400_8x8
    --kernel="$KERNEL"
    --disk-image="$DISK"
    --machine-type=VExpress_GEM5_V1
    --dtb-file=none
    --bare-metal
    --cpu-type=DerivO3CPU
    --caches
    --l2cache
    --accpath="$BENCH_PATH"
    --accbench="$BENCH"
)

# ---- SoC profiles (CLI flag sets that get appended to a programmed SMMU run)
PROFILE_IOT=(
    # Defaults already match MMU-400-class; pass them explicitly so the
    # log captures the provenance and the run is reproducible regardless
    # of future binary default changes.
    --smmu-tlb-entries 16  --smmu-ifctlb-entries 16
    --smmu-utlb-entries 4  --smmu-cfg-entries 4
    --smmu-xlate-slots 2   --smmu-tbu-xlate-slots 1
    --smmu-ptw-slots 1     --smmu-tlb-slots 1
    --smmu-ifc-lat 12
    # walk cache: disabled (no --smmu-walk-enable)
)

PROFILE_MMU500=(
    --smmu-tlb-entries 32  --smmu-ifctlb-entries 32
    --smmu-utlb-entries 4  --smmu-cfg-entries 8
    --smmu-xlate-slots 4   --smmu-tbu-xlate-slots 2
    --smmu-ptw-slots 2     --smmu-tlb-slots 2
    --smmu-ifc-lat 8
    --smmu-walk-enable
    --smmu-walk-s1l0 2 --smmu-walk-s1l1 4
    --smmu-walk-s1l2 8 --smmu-walk-s1l3 4
    --smmu-walk-assoc 2 --smmu-walk-slots 2 --smmu-walk-lat 4
)

PROFILE_SERVER=(
    --smmu-tlb-entries 2048 --smmu-ifctlb-entries 2048
    --smmu-utlb-entries 32  --smmu-cfg-entries 64
    --smmu-xlate-slots 64   --smmu-tbu-xlate-slots 16
    --smmu-ptw-slots 16     --smmu-tlb-slots 4   --smmu-tlb-assoc 4
    --smmu-ifc-lat 8
    --smmu-walk-enable
    --smmu-walk-s1l0 4 --smmu-walk-s1l1 28
    --smmu-walk-s1l2 348 --smmu-walk-s1l3 4
    --smmu-walk-assoc 4 --smmu-walk-slots 16 --smmu-walk-lat 4
)

launch () {
    local label="$1"; shift
    local outdir="$OUTROOT/$label"
    rm -rf "$outdir"; mkdir -p "$outdir"
    echo "[launch] $label  ->  $outdir"
    # shellcheck disable=SC2086
    M5_PATH="$M5_PATH" "$BIN" --outdir="$outdir" "$FS_SCRIPT" \
        "${SYS_OPTS[@]}" "$@" $EXTRA >"$outdir/run.log" 2>&1 &
    # Record the exact command for reproducibility.
    {
        printf "label: %s\n" "$label"
        printf "cmd:   %s --outdir=%s %s" "$BIN" "$outdir" "$FS_SCRIPT"
        printf " %s" "${SYS_OPTS[@]}" "$@"
        [[ -n "$EXTRA" ]] && printf " %s" $EXTRA
        printf "\n"
    } > "$outdir/run.cmd"
}

throttle () {
    while [[ "$(jobs -r | wc -l)" -ge "$JOBS" ]]; do
        sleep 0.5
    done
}

# Build the list of runs we will actually launch (so harvest matches).
RUN_LABELS=()

want_mode () {
    local m="$1"
    [[ " $MODES " == *" $m "* ]]
}

if want_mode plain; then
    throttle; launch "plain"
    RUN_LABELS+=("plain")
fi

if want_mode aia-kd; then
    throttle; launch "aia-kd" \
        --enable-kernel-validation --kernel-validation-latency 1000
    RUN_LABELS+=("aia-kd")
fi

if want_mode smmu-bypass; then
    throttle; launch "smmu-bypass" --enable-real-smmu
    RUN_LABELS+=("smmu-bypass")
fi

if want_mode smmu-iot; then
    throttle; launch "smmu-iot" \
        --enable-real-smmu --smmu-program-stream-table \
        --smmu-granule-kib "$GRANULE_KIB" \
        "${PROFILE_IOT[@]}"
    RUN_LABELS+=("smmu-iot")
fi

if want_mode smmu-mmu500; then
    throttle; launch "smmu-mmu500" \
        --enable-real-smmu --smmu-program-stream-table \
        --smmu-granule-kib "$GRANULE_KIB" \
        "${PROFILE_MMU500[@]}"
    RUN_LABELS+=("smmu-mmu500")
fi

if want_mode smmu-server; then
    throttle; launch "smmu-server" \
        --enable-real-smmu --smmu-program-stream-table \
        --smmu-granule-kib "$GRANULE_KIB" \
        "${PROFILE_SERVER[@]}"
    RUN_LABELS+=("smmu-server")
fi

if want_mode smmu-tlb-sweep; then
    for tlb in $TLB_SWEEP; do
        throttle
        launch "smmu-tlb${tlb}" \
            --enable-real-smmu --smmu-program-stream-table \
            --smmu-granule-kib "$GRANULE_KIB" \
            "${PROFILE_IOT[@]}" \
            --smmu-tlb-entries "$tlb" --smmu-ifctlb-entries "$tlb"
        RUN_LABELS+=("smmu-tlb${tlb}")
    done
fi

if [[ ${#RUN_LABELS[@]} -eq 0 ]]; then
    echo "No runs queued -- check --modes value: '$MODES'" >&2
    exit 1
fi

echo "[wait] waiting for ${#RUN_LABELS[@]} gem5 process(es) ..."
wait
echo "[done] all runs finished"

# ----- Harvest ------------------------------------------------------------
{
    printf "mode\tcycles\tsim_ticks\tsteFetches\tcdFetches"
    printf "\tptw_samples\ttrans_samples\ttrans_mean_ps"
    printf "\taia_kd_overhead_us\n"
} > "$SUMMARY"

sum_stat () {
    local stats="$1" pat="$2"
    awk -v pat="$pat" '
        $1 ~ "^system\\.[A-Za-z0-9_]+\\.smmu\\." pat "$" {
            sum += $2
        }
        END { if (NR>0) printf "%g", sum; else printf "-" }
    ' "$stats"
}
avg_stat () {
    local stats="$1" pat="$2"
    awk -v pat="$pat" '
        $1 ~ "^system\\.[A-Za-z0-9_]+\\.smmu\\." pat "$" {
            sum += $2; n++
        }
        END { if (n>0) printf "%g", sum/n; else printf "-" }
    ' "$stats"
}

harvest () {
    local label="$1"
    local outdir="$OUTROOT/$label"
    local stats="$outdir/stats.txt"
    local logfile="$outdir/run.log"

    if [[ ! -f "$stats" ]]; then
        echo -e "$label\tFAIL\t-\t-\t-\t-\t-\t-\t-" >> "$SUMMARY"
        return
    fi

    local cycles ticks ste cd ptw trans trans_mean overhead
    cycles=$(grep -m1 "Runtime:" "$logfile" 2>/dev/null \
             | awk '{print $2}' | head -1)
    ticks=$(grep -E "^simTicks " "$stats" 2>/dev/null \
            | head -1 | awk '{print $2}')
    ste=$(sum_stat "$stats" steFetches)
    cd=$(sum_stat  "$stats" cdFetches)
    ptw=$(sum_stat "$stats" "ptwTimeDist::samples")
    trans=$(sum_stat "$stats" "translationTimeDist::samples")
    trans_mean=$(avg_stat "$stats" "translationTimeDist::mean")
    overhead=$(grep -m1 "TOTAL SECURITY OVERHEAD" "$logfile" 2>/dev/null \
               | awk '{print $4}')

    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$label" "${cycles:--}" "${ticks:--}" \
        "${ste:--}" "${cd:--}" "${ptw:--}" \
        "${trans:--}" "${trans_mean:--}" "${overhead:--}" \
        >> "$SUMMARY"
}

for label in "${RUN_LABELS[@]}"; do
    harvest "$label"
done

# ----- Compute deltas vs plain baseline (if present) ----------------------
DELTA_TSV="$OUTROOT/deltas.tsv"
if grep -q "^plain	" "$SUMMARY"; then
    awk -F'\t' '
        NR==1 {
            print "mode\tsim_ticks\tdelta_ticks\tdelta_pct";
            next
        }
        $1=="plain" { base=$3; print $1"\t"$3"\t0\t0.000"; next }
        {
            if ($3 ~ /^[0-9]+$/ && base>0) {
                d = $3 - base
                pct = (d / base) * 100.0
                printf "%s\t%s\t%d\t%.3f\n", $1, $3, d, pct
            } else {
                printf "%s\t%s\t-\t-\n", $1, $3
            }
        }
    ' "$SUMMARY" > "$DELTA_TSV"
fi

echo
echo "===== Summary ($BENCH) ====="
awk -F'\t' '{ for(i=1;i<=NF;i++) printf "%-22s", $i; print "" }' "$SUMMARY"
if [[ -f "$DELTA_TSV" ]]; then
    echo
    echo "===== Deltas vs plain ====="
    awk -F'\t' '{ for(i=1;i<=NF;i++) printf "%-22s", $i; print "" }' "$DELTA_TSV"
fi
echo
echo "Per-run output dirs:   $OUTROOT/<mode>/"
echo "Logs / stats / cmd:    $OUTROOT/<mode>/{run.log,stats.txt,run.cmd}"
echo "Summary TSV:           $SUMMARY"
[[ -f "$DELTA_TSV" ]] && echo "Deltas TSV:            $DELTA_TSV"
