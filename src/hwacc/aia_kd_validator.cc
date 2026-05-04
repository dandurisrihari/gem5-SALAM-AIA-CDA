/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * AiaKdValidator implementation. Owns the chip-wide per-PID page
 * cache + in-flight set + monotonic kernel-driver deadline. All cost
 * is reported through `checkAndCharge()` as a Tick delta and realized
 * on the response path by CommInterface::tryAiaKdDelay -- the
 * validator itself owns NO events.
 */
#include "hwacc/aia_kd_validator.hh"

#include <algorithm>

namespace gem5
{

AiaKdValidator::AiaKdValidator(const Params &p)
    : SimObject(p),
      nextReadyTick(0),
      enabledFlag(p.enabled),
      latencyTicks(p.latency),
      dmaCtrlRanges(p.dma_pio_ranges.begin(), p.dma_pio_ranges.end()),
      totalColdMisses(0),
      totalCoalesced(0),
      totalDmaCtrl(0),
      totalLatencyTicks(0)
{
}

void
AiaKdValidator::promoteReadyPages(Tick now)
{
    // Sweep in-flight pages whose IRQ deadline has passed and move
    // them into the validated cache for the originator's PID. O(n)
    // in the in-flight set size which is bounded by the number of
    // outstanding cold misses (one IRQ at a time, so very small).
    for (auto it = pendingValidationPages.begin();
         it != pendingValidationPages.end(); ) {
        if (it->second.readyTick <= now) {
            validatedPagesPerProcess[it->second.originatorPid]
                .insert(it->first);
            it = pendingValidationPages.erase(it);
        } else {
            ++it;
        }
    }
}

Tick
AiaKdValidator::checkAndCharge(uint64_t pid, Addr addr, bool isWrite,
                               Outcome &outcome)
{
    // Lat=0 / disabled fast path: zero overhead, identical to plain.
    if (!enabledFlag || latencyTicks == 0) {
        outcome = Outcome::Disabled;
        return 0;
    }

    Tick now = curTick();
    promoteReadyPages(now);

    uint64_t pageAddr = addr & ~0xFFFULL;
    bool dmaCtrl = isWrite && isDmaCtrl(addr);

    // -----------------------------------------------------------------
    // DMA-ctrl write: bypass cache + coalescing entirely. Every store
    // re-fires a cold-miss tax and serializes on nextReadyTick.
    // Reads to DMA pages (status polls) are NOT bypassed -- they
    // follow the normal cache/coalesce path.
    // -----------------------------------------------------------------
    if (dmaCtrl) {
        Tick start  = std::max(now, nextReadyTick);
        Tick ready  = start + latencyTicks;
        nextReadyTick = ready;
        Tick defer  = ready - now;
        totalDmaCtrl++;
        totalLatencyTicks += defer;
        outcome = Outcome::DmaCtrl;
        return defer;
    }

    // -----------------------------------------------------------------
    // Cache hit: this PID already paid for this page chip-wide.
    // -----------------------------------------------------------------
    auto pidIt = validatedPagesPerProcess.find(pid);
    if (pidIt != validatedPagesPerProcess.end() &&
        pidIt->second.count(pageAddr)) {
        outcome = Outcome::CacheHit;
        return 0;
    }

    // -----------------------------------------------------------------
    // Coalesce on an in-flight cold miss for the same page. We do
    // NOT bump nextReadyTick (the IRQ is already scheduled) and we do
    // NOT charge a fresh totalLatencyTicks slice -- the originator
    // already accounted for it. We DO return a non-zero defer so the
    // follower's response sits behind the validator's IRQ deadline.
    // -----------------------------------------------------------------
    auto inflightIt = pendingValidationPages.find(pageAddr);
    if (inflightIt != pendingValidationPages.end()) {
        Tick defer = inflightIt->second.readyTick > now
                     ? inflightIt->second.readyTick - now : 0;
        totalCoalesced++;
        outcome = Outcome::Coalesced;
        return defer;
    }

    // -----------------------------------------------------------------
    // Cold miss: insert into the in-flight set, advance the chip-wide
    // kernel-driver deadline, charge the full latency.
    // -----------------------------------------------------------------
    Tick start  = std::max(now, nextReadyTick);
    Tick ready  = start + latencyTicks;
    nextReadyTick = ready;
    pendingValidationPages[pageAddr] = {ready, pid};
    Tick defer = ready - now;
    totalColdMisses++;
    totalLatencyTicks += defer;
    outcome = Outcome::ColdMiss;
    return defer;
}

} // namespace gem5
