/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * AcceleratorIommu
 * -----------------
 * Device-wide accelerator IOMMU/SMMU SimObject. ONE instance per
 * AccCluster, shared by every LLVMInterface (CU) and CommInterface in
 * the cluster. Models a chip-wide SMMU sitting on the accelerator's
 * memory port, between the device and DRAM.
 *
 * State owned (formerly process-static globals in LLVMInterface and
 * CommInterface):
 *   * fully-associative LRU IOTLB                     (was iotlbLru/Set)
 *   * monotonic translation-port deadline           (was sIommuNextReadyTick)
 *
 * Public API:
 *   * `translate(pageAddr, isRead)` performs one IOTLB lookup,
 *     advances the in-order port deadline, and returns the absolute
 *     tick at which the translation completes. Cost = hit_latency on
 *     IOTLB hit, miss_latency on miss/walk; serialized at one
 *     chip-wide port (no overlap across CUs).
 *
 * Why a SimObject (not statics)?
 *   * State has an owner with a `name()` (visible in DPRINTF, panics).
 *   * Stats are emitted under the SimObject's hierarchy
 *     (e.g. `system.acc_cluster.iommu.iotlb_hits`).
 *   * Multiple AccClusters in one simulation each get their own IOMMU
 *     -- previously forbidden by a runtime panic in startup().
 *   * Test isolation: gem5 destructs SimObjects per --outdir run.
 *
 * Lat=0 invariant
 *   When both `hit_latency` and `miss_latency` are 0, `translate()`
 *   short-circuits without touching the LRU and returns curTick(),
 *   guaranteeing bit-identical behavior with `enabled=False` /
 *   plain mode. Verified by tests/aia_cda_tests/run_sanity.py.
 */
#ifndef __HWACC_ACCELERATOR_IOMMU_HH__
#define __HWACC_ACCELERATOR_IOMMU_HH__

#include <list>
#include <set>

#include "base/statistics.hh"
#include "base/types.hh"
#include "params/AcceleratorIommu.hh"
#include "sim/sim_object.hh"

namespace gem5
{

class AcceleratorIommu : public SimObject
{
  public:
    typedef AcceleratorIommuParams Params;
    AcceleratorIommu(const Params &p);

    /**
     * Translate one accelerator-side memory access at page-granularity.
     *
     * @param pageAddr  4 KiB-aligned page address (caller already
     *                  performed `addr & ~0xFFFULL`).
     * @param isRead    True for loads, false for stores. Currently
     *                  used only for stat hooks; both directions go
     *                  through the same translation port.
     * @return Absolute tick at which the translation slot completes.
     *         Caller may proceed (e.g. forward the response packet) at
     *         that tick. When IOMMU is disabled, returns curTick().
     */
    Tick translate(uint64_t pageAddr, bool isRead);

    /** Master toggle accessor (read by CommInterface fast path). */
    bool enabled() const { return enabledFlag; }

  protected:
    /**
     * Internal IOTLB access helper. Returns true on hit, false on miss.
     * Always promotes the looked-up page to MRU; evicts LRU on miss
     * when capacity is reached. iotlb_entries==0 disables caching
     * (every access misses).
     */
    bool iotlbAccess(uint64_t pageAddr);

  private:
    // Parameters (immutable after construction)
    const bool     enabledFlag;
    const uint32_t iotlbEntries;
    const Tick     hitLatency;
    const Tick     missLatency;

    // -----------------------------------------------------------------
    // Owned state: chip-wide IOTLB cache.
    // Front of `lru` is MRU, back is LRU. `set` mirrors membership for
    // O(log n) hit tests. The two MUST be kept in sync.
    // -----------------------------------------------------------------
    std::list<uint64_t> lru;
    std::set<uint64_t>  set;

    /**
     * Cumulative set of every distinct 4 KiB page ever translated by
     * this IOMMU (independent of IOTLB capacity churn). Compared
     * against AIA-KD's per-page validated-pages cache to verify both
     * mechanisms observe the same page footprint -- the IOTLB miss
     * count can be larger than |uniquePages| when capacity churn
     * forces re-walks of previously seen pages.
     */
    std::set<uint64_t>  uniquePagesSeen;

    /**
     * Monotonic deadline of the translation port. Each successful
     * `translate()` call advances this by the appropriate latency,
     * serializing the entire device's translation traffic through
     * one chip-wide port. Replaces the old `sIommuNextReadyTick`
     * file-scope static.
     */
    Tick nextReadyTick;

  public:
    // -----------------------------------------------------------------
    // Stats. Aggregated across the whole device (chip-wide totals);
    // CUs no longer need per-CU IOTLB stats since the cache is shared.
    // -----------------------------------------------------------------
    struct IommuStats : public statistics::Group
    {
        IommuStats(statistics::Group *parent);
        statistics::Scalar totalChecks;     // every translate() call
        statistics::Scalar tlbHits;         // IOTLB hits
        statistics::Scalar tlbMisses;       // IOTLB misses (page walks)
        // Distinct pages translated (cumulative).
        statistics::Scalar uniquePages;
        statistics::Scalar totalLatencyTicks;  // sum of added latency
    } stats;
};

} // namespace gem5

#endif // __HWACC_ACCELERATOR_IOMMU_HH__
