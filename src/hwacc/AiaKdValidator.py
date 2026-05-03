# Copyright (c) 2026 SALAM contributors
# SPDX-License-Identifier: BSD-3-Clause
#
# AiaKdValidator -- a single, device-wide AIA-KD page-validator
# SimObject. One instance is shared by every LLVMInterface (CU)
# inside an AccCluster: AIA-KD models ONE chip-wide validator service
# that all CUs consult to (a) coalesce in-flight validations across
# CUs and (b) cache validated pages per-process.
#
# Replaces the file-scope statics that used to live in
# llvm_interface.cc (validatedPagesPerProcess, pendingValidationPages,
# waitingForPage) and the sAiaKdRegisteredPid uniformity sentinel
# (no longer needed: with one shared SimObject per cluster the PID
# is structurally uniform).
from m5.params import *
from m5.proxy import *
from m5.SimObject import SimObject


class AiaKdValidator(SimObject):
    type = "AiaKdValidator"
    cxx_class = "gem5::AiaKdValidator"
    cxx_header = "hwacc/aia_kd_validator.hh"

    # Master toggle. When False the validator is a no-op container --
    # LLVMInterface short-circuits before touching its state. The
    # SimObject itself is always instantiated so the `iommu`-style
    # Param wiring on LLVMInterface always resolves regardless of
    # whether AIA-KD is active for this run.
    enabled = Param.Bool(False, "Master AIA-KD validator toggle")

    # Per-cold-miss validation latency (ticks). Single source of truth
    # for the chip-wide AIA-KD service: the validator owns the FIFO
    # and the response event, so latency lives here too. The legacy
    # per-CU `kernel_validation_latency` Param on LLVMInterface is
    # cross-checked against this value at startup.
    latency = Param.Tick(0, "Per-cold-miss kernel validation latency")

    # Address ranges of DMA-engine PIO control reg banks. Writes to a
    # page that intersects any of these ranges are NEVER cached: each
    # store re-fires a full validation, modeling the threat that any
    # write to a DMA control reg can re-target the engine at a new
    # source/destination. Reads are unaffected (status polling is
    # benign). Populated by the SALAM-Configurator for every DMA in
    # the cluster.
    dma_pio_ranges = VectorParam.AddrRange([],
        "DMA control reg PIO ranges -- writes here always pay full "
        "validation latency (no cache, no coalescing)")
