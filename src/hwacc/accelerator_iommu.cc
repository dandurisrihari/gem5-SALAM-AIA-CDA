/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "hwacc/accelerator_iommu.hh"

#include <algorithm>

namespace gem5
{

AcceleratorIommu::AcceleratorIommu(const Params &p)
    : SimObject(p),
      enabledFlag(p.enabled),
      iotlbEntries(p.iotlb_entries),
      hitLatency(p.hit_latency),
      missLatency(p.miss_latency),
      nextReadyTick(0),
      stats(this)
{
}

bool
AcceleratorIommu::iotlbAccess(uint64_t pageAddr)
{
    if (iotlbEntries == 0) {
        // Caching disabled: still record the access in stats but
        // always miss.
        return false;
    }

    auto it = set.find(pageAddr);
    if (it != set.end()) {
        // Hit. Promote to MRU.
        lru.remove(pageAddr);
        lru.push_front(pageAddr);
        return true;
    }

    // Miss. Insert; evict LRU if at capacity.
    lru.push_front(pageAddr);
    set.insert(pageAddr);
    if (lru.size() > iotlbEntries) {
        uint64_t evicted = lru.back();
        lru.pop_back();
        set.erase(evicted);
    }
    return false;
}

Tick
AcceleratorIommu::translate(uint64_t pageAddr, bool /*isRead*/)
{
    // Lat=0 / disabled fast path: zero overhead, identical to plain.
    if (!enabledFlag || (hitLatency == 0 && missLatency == 0)) {
        return curTick();
    }

    bool hit = iotlbAccess(pageAddr);
    Tick lat = hit ? hitLatency : missLatency;

    // Single chip-wide translation port: serialize. Each access
    // begins no earlier than max(curTick(), nextReadyTick), then
    // occupies the port for `lat`. Subsequent accesses queue.
    Tick startTick = std::max(curTick(), nextReadyTick);
    Tick ready     = startTick + lat;
    nextReadyTick  = ready;

    stats.totalChecks++;
    if (hit)
        stats.tlbHits++;
    else
        stats.tlbMisses++;
    stats.totalLatencyTicks += lat;

    return ready;
}

AcceleratorIommu::IommuStats::IommuStats(statistics::Group *parent)
    : statistics::Group(parent),
      ADD_STAT(totalChecks,       statistics::units::Count::get(),
               "IOTLB lookups performed"),
      ADD_STAT(tlbHits,           statistics::units::Count::get(),
               "IOTLB hits"),
      ADD_STAT(tlbMisses,         statistics::units::Count::get(),
               "IOTLB misses (page walks)"),
      ADD_STAT(totalLatencyTicks, statistics::units::Tick::get(),
               "Sum of latency added by IOMMU (ticks)")
{
}

} // namespace gem5
