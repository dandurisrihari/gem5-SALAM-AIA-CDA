/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * AiaKdValidator -- chip-wide AIA-KD page validator (Option C).
 * --------------------------------------------------------------
 * One instance per AccCluster, shared by every LLVMInterface (CU)
 * AND every CommInterface in the cluster. Models a single chip-wide
 * kernel-driver validation service whose cost is charged on the
 * RESPONSE PATH of every accelerator memory access (mirroring the
 * AcceleratorIommu model -- see accelerator_iommu.{hh,cc}).
 *
 * Why response-path defer?
 *   * Upstream-of-CommInterface stalls (the previous "return false +
 *     reservation re-queue" design) demonstrably reshape DDR
 *     contention across the cluster's many parallel CUs and produce
 *     NEGATIVE sim_ticks deltas vs. plain (-28% on `nw`, occasional
 *     dips on mobilenetv2_75). See .github/prompts/06-gotchas-and-
 *     history.md "5 failed attempts" for the full history.
 *   * Decision MUST happen at LLVM IF (per-load/store, per-page,
 *     per-process) so AIA-KD can model coalescing + per-PID page
 *     cache + DMA-ctrl bypass.  Stall MUST happen at the response
 *     port so DDR pressure is unaltered.
 *   * `checkAndCharge()` does the decision inline at launch time and
 *     returns a Tick delta that the LLVMInterface stamps onto the
 *     MemoryRequest. CommInterface::tryAiaKdDelay reads the stamp on
 *     the first response packet and queues it for that delta.
 *
 * Coalescing model (per .github/prompts/01-aia-kd-design.md)
 *   * `validatedPagesPerProcess[pid]` -- pages this PID already paid
 *     for chip-wide. Future accesses by ANY sibling CU return 0.
 *   * `pendingValidationPages[page] -> {readyTick, originatorPid}`
 *     -- pages with an in-flight validation. Followers (any CU, any
 *     access) coalesce by returning the same readyTick (they
 *     piggyback on the SAME IRQ; they do NOT bump nextReadyTick a
 *     second time).
 *   * Lazy promotion: at the top of every checkAndCharge() we sweep
 *     pendingValidationPages for entries whose readyTick has passed
 *     and move them into the validated cache. No event needed --
 *     the first checkAndCharge after readyTick observes them.
 *   * `nextReadyTick` is a chip-wide monotonic deadline that
 *     serializes cold misses (one IRQ at a time per cluster --
 *     single kernel-driver thread).
 *
 * DMA-ctrl exception (security-critical writes only)
 *   The kernel-driver / capability monitor only inspects DMA register
 *   writes that grant the engine new memory authority -- i.e. SOURCE
 *   and DESTINATION descriptor registers. Writes to other DMA regs
 *   (FLAGS go-bit, LEN, status resets, the full Stream-DMA register
 *   file) reveal no new address capability and cost nothing in this
 *   model. The configurator partitions every DMA's PIO window into
 *   two disjoint sets -- `dmaDescriptorRanges` (SRC/DST) and
 *   `dmaPioPassthroughRanges` (everything else):
 *     * write into `dmaDescriptorRanges`  -> full per-cold-miss tax
 *       (no cache, no coalescing); chip-wide nextReadyTick advances.
 *     * any other access into ANY DMA PIO byte -> 0 ticks, no state
 *       change (passthrough). Reads of descriptor regs are also
 *       passthrough (benign, no new authority granted).
 *   PIO bytes are never folded into the per-PID page cache, so PIO
 *   addresses cannot pollute or shadow ordinary code/data pages.
 *
 * Lat=0 invariant
 *   When `latency` is 0 (or `enabled` is false) `checkAndCharge`
 *   returns 0 immediately without touching any state. Behavior is
 *   then bit-identical to plain mode. Verified by
 *   tests/aia_cda_tests/run_sanity.py.
 */
#ifndef __HWACC_AIA_KD_VALIDATOR_HH__
#define __HWACC_AIA_KD_VALIDATOR_HH__

#include <cstdint>
#include <map>
#include <set>

#include "base/addr_range.hh"
#include "base/types.hh"
#include "params/AiaKdValidator.hh"
#include "sim/sim_object.hh"

namespace gem5
{

class AiaKdValidator : public SimObject
{
  public:
    typedef AiaKdValidatorParams Params;
    AiaKdValidator(const Params &p);

    /** Master toggle accessor. */
    bool enabled() const { return enabledFlag; }

    /** Per-cold-miss validation latency (chip-wide). */
    Tick latency() const { return latencyTicks; }

    /**
     * True if `addr` lies in a DMA descriptor register (SRC or DST of
     * any NonCoherent DMA in the cluster). Writes to such addresses
     * grant the engine new memory authority and are the only DMA-PIO
     * accesses that pay validation cost in the current model.
     */
    bool isDmaDescriptorReg(Addr addr) const {
        for (const auto &r : dmaDescriptorRanges) {
            if (r.contains(addr)) return true;
        }
        return false;
    }

    /**
     * True if `addr` lies in a DMA control reg that is NOT a
     * descriptor (FLAGS go-bit, LEN, Stream-DMA regs, status). These
     * accesses are AIA-KD-free.
     */
    bool isDmaPioPassthrough(Addr addr) const {
        for (const auto &r : dmaPioPassthroughRanges) {
            if (r.contains(addr)) return true;
        }
        return false;
    }

    /**
     * Convenience: true if `addr` lies in ANY DMA PIO byte (descriptor
     * or passthrough). Used by checkAndCharge() to short-circuit the
     * page-cache path for PIO addresses, which prevents PIO bytes from
     * polluting the per-PID validated-page set.
     */
    bool isAnyDmaPio(Addr addr) const {
        return isDmaDescriptorReg(addr) || isDmaPioPassthrough(addr);
    }

    /** Diagnostic outcome class returned by checkAndCharge(). */
    enum class Outcome : uint8_t
    {
        Disabled,         // validator off / lat==0; defer == 0
        CacheHit,         // page already validated this PID; defer == 0
        ColdMiss,         // first chip-wide touch; full latency charged
        Coalesced,        // followed an in-flight cold miss; partial wait
        DmaCtrl,          // DMA descriptor (SRC/DST) write; full latency
        PioPassthrough    // non-descriptor DMA PIO access; defer == 0
    };

    /**
     * Per-load/store decision + cost, called from
     * LLVMInterface::ActiveFunction::launchRead/launchWrite.
     *
     * @param pid     Process id of the issuing CU (per-PID cache key).
     * @param addr    Byte address of the access (page-rounded inside).
     * @param isWrite True for stores, false for loads. Drives the
     *                DMA-ctrl bypass (writes only).
     * @param[out] outcome Diagnostic outcome class (for stats).
     * @return Number of ticks the response should be deferred. Zero
     *         means "no AIA-KD overhead on this access". Non-zero
     *         must be stamped onto MemoryRequest::aiaKdDefer by the
     *         caller; CommInterface::tryAiaKdDelay then realizes the
     *         stall on the response port.
     */
    Tick checkAndCharge(uint64_t pid, Addr addr, bool isWrite,
                        Outcome &outcome);

  private:
    /**
     * Lazy promotion: sweep pendingValidationPages for entries whose
     * readyTick has passed and move them into the per-PID validated
     * cache. Called at the top of every checkAndCharge().
     */
    void promoteReadyPages(Tick now);

    // -----------------------------------------------------------------
    // Owned state.
    // -----------------------------------------------------------------

    /** Per-process validated-page cache (chip-wide). */
    std::map<uint64_t, std::set<uint64_t>> validatedPagesPerProcess;

    /**
     * Pages with an in-flight validation -> the absolute tick at
     * which the IRQ response is expected, plus the PID of the
     * cold-miss originator (used to route the page into the right
     * per-PID cache entry on lazy promotion).
     */
    struct InFlightEntry
    {
        Tick readyTick;
        uint64_t originatorPid;
    };
    std::map<uint64_t, InFlightEntry> pendingValidationPages;

    /**
     * Monotonic deadline of the kernel-driver thread. Each cold miss
     * begins no earlier than max(curTick(), nextReadyTick) and
     * occupies the driver for `latencyTicks`. Mirrors
     * AcceleratorIommu::nextReadyTick.
     */
    Tick nextReadyTick;

    const bool enabledFlag;
    const Tick latencyTicks;
    /**
     * Security-critical DMA register ranges (SRC and DST of every
     * NonCoherent DMA in the cluster). Writes here grant the engine
     * new memory authority and pay full validation latency.
     */
    const AddrRangeList dmaDescriptorRanges;
    /**
     * Non-descriptor DMA PIO ranges (FLAGS, LEN, Stream-DMA regs,
     * status). Reads and writes here are AIA-KD-free.
     */
    const AddrRangeList dmaPioPassthroughRanges;

  public:
    // -----------------------------------------------------------------
    // Stats. Aggregated chip-wide; LLVMInterface keeps per-CU mirror
    // counters so printKernelValidationStats() preserves the exact
    // strings the speedkills harvester regex-binds to.
    // -----------------------------------------------------------------
    /** Distinct (PID, page) cold misses ever charged. */
    uint64_t totalColdMisses;
    /** Coalesced followers (cross-CU and same-CU combined). */
    uint64_t totalCoalesced;
    /** DMA descriptor (SRC/DST) writes that paid full latency. */
    uint64_t totalDmaCtrl;
    /**
     * DMA PIO accesses that were short-circuited as passthrough
     * (non-descriptor regs, or reads to descriptor regs). Diagnostic
     * only -- these contribute zero ticks.
     */
    uint64_t totalDmaPioPassthrough;
    /** Cumulative delay ticks injected (sum of returned values). */
    Tick totalLatencyTicks;

    /**
     * Total distinct (PID, page) pairs ever validated chip-wide.
     * Computed by summing |validatedPagesPerProcess[pid]| + the
     * still-in-flight entries that have not yet been promoted.
     * Cheap O(n_pids); used by printKernelValidationStats().
     */
    size_t uniquePages() const {
        size_t n = 0;
        for (const auto &kv : validatedPagesPerProcess) n += kv.second.size();
        n += pendingValidationPages.size();
        return n;
    }
    size_t numCachedProcesses() const {
        return validatedPagesPerProcess.size();
    }
};

} // namespace gem5

#endif // __HWACC_AIA_KD_VALIDATOR_HH__
