#!/usr/bin/env bash
yes "" | scons build/ARM/gem5.opt -j"$(nproc)" 2>&1 
rm -rf BM_ARM_OUT/
python3 -m tools.speedkills compare-all \
  --outdir BM_ARM_OUT/run_all --regen --jobs "$(nproc)" --iommu-sweep 2>&1