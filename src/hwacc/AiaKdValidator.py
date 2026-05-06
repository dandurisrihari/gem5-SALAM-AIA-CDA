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

    # ------------------------------------------------------------------
    # DMA control-register classification.
    #
    # The kernel-driver / capability monitor only needs to inspect the
    # SUBSET of DMA register writes that actually grant the engine new
    # memory authority -- i.e. SOURCE and DESTINATION descriptor regs.
    # Writes to other DMA registers (FLAGS go-bit, LEN, status resets,
    # the entire Stream-DMA register file) reveal no new address
    # capability and so cost nothing in the AIA-KD model.
    #
    # We split the ranges into two disjoint vectors so checkAndCharge()
    # can cheaply distinguish "security-critical write" (full latency,
    # no cache) from "PIO passthrough" (free, no state change). Both
    # vectors are populated by the SALAM-Configurator from each DMA's
    # register layout.
    #
    # IMPORTANT: every byte that lies inside SOME DMA's PIO window must
    # appear in EXACTLY ONE of these two vectors. checkAndCharge()
    # treats any address in either vector as "PIO" and short-circuits
    # the page-cache path so PIO addresses never pollute the per-PID
    # validated-page set.
    # ------------------------------------------------------------------

    # Descriptor registers: SRC and DST of NonCoherent DMAs.  A WRITE
    # here re-arms the engine at a new (source, destination) pair and
    # therefore demands a fresh capability check by the kernel
    # driver -- we charge full per-cold-miss latency, never cache, and
    # serialize on the chip-wide kernel-driver deadline. READS to
    # these addresses are still passthrough (benign).
    dma_descriptor_ranges = VectorParam.AddrRange([],
        "DMA descriptor reg ranges (SRC/DST). Writes here pay full "
        "validation latency (no cache, no coalescing). Reads are "
        "passthrough.")

    # Passthrough PIO bytes: every other DMA control reg
    # (NonCoherent FLAGS + LEN, plus the entire Stream DMA register
    # window). Reads AND writes here cost nothing -- they carry no new
    # security-relevant authority.
    dma_pio_passthrough_ranges = VectorParam.AddrRange([],
        "DMA non-descriptor PIO ranges. Reads and writes here are "
        "AIA-KD-free and bypass the page cache entirely.")
