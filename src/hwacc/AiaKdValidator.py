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

    # ------------------------------------------------------------------
    # Effectiveness (security-analysis) mode.
    #
    # Orthogonal to the timing model above: instead of asking "what does
    # AIA-KD cost?", this asks "what does AIA-KD actually catch?". We
    # declare a set of address ranges for which the accelerator holds NO
    # capability (kernel memory, a sibling process's buffer, a region
    # outside the granted descriptor) and observe how the mechanism
    # responds when the accelerator touches them.
    #
    # The interesting result is NOT "it gets denied" -- of course it
    # does. It is the split between:
    #   * Denied -- the illegal access reached the kernel driver (cold
    #     page, or a DMA SRC/DST reprogram) and was refused after the
    #     full validation round-trip.
    #   * Missed -- the illegal address shares a 4 KiB page with data
    #     the accelerator legitimately validated earlier, so AIA-KD's
    #     per-page first-touch cache never re-consults the driver and
    #     the access completes. This is AIA-KD's structural blind spot
    #     and it is what a per-access checker would catch instead.
    #
    # Off by default: every existing profile and the sanity suite must
    # be unaffected.
    # ------------------------------------------------------------------

    violation_check = Param.Bool(False,
        "Enable AIA-KD effectiveness analysis. When False the "
        "forbidden_ranges vector is ignored entirely and "
        "checkAndCharge() behaves exactly as in a pure timing run.")

    # Regions the accelerator was never granted. An access is illegal
    # when [addr, addr+size) OVERLAPS any of these (not merely when
    # `addr` is contained), so a straddling access is still caught.
    forbidden_ranges = VectorParam.AddrRange([],
        "Address ranges the accelerator holds no capability for. Only "
        "consulted when violation_check is True.")

    # When True the run ends via exitSimLoop() scheduled at the tick the
    # driver actually answers (issue tick + charged validation latency),
    # so the exit timestamp is a meaningful detection latency rather
    # than the moment of issue. When False the violation is counted and
    # logged but the run continues -- useful for measuring how many
    # illegal accesses a whole workload would attempt.
    stop_on_violation = Param.Bool(True,
        "End the simulation when AIA-KD denies an access. The exit is "
        "scheduled at detection time, not issue time.")
