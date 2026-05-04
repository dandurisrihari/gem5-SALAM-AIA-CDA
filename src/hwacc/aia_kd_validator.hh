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
 * DMA-ctrl exception
 *   Writes whose target lies in any DMA's PIO range NEVER cache and
 *   NEVER coalesce -- every store charges a fresh cold-miss tax and
 *   advances nextReadyTick. Reads are unaffected (status polling is
 *   benign).
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
     * True if `addr` falls inside the PIO range of any DMA engine in
     * the cluster. Used by the DMA-ctrl bypass in checkAndCharge().
     */
    bool isDmaCtrl(Addr addr) const {
        for (const auto &r : dmaCtrlRanges) {
            if (r.contains(addr)) return true;
        }
        return false;
    }

    /** Diagnostic outcome class returned by checkAndCharge(). */
    enum class Outcome : uint8_t
    {
        Disabled,     // validator off / lat==0; defer == 0
        CacheHit,     // page already validated this PID; defer == 0
        ColdMiss,     // first chip-wide touch; full latency charged
        Coalesced,    // followed an in-flight cold miss; partial wait
        DmaCtrl       // DMA-ctrl write; full latency, no caching
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
    /** PIO ranges of DMA engines; writes here bypass the cache. */
    const AddrRangeList dmaCtrlRanges;

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
    /** DMA-ctrl writes that paid full latency. */
    uint64_t totalDmaCtrl;
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
