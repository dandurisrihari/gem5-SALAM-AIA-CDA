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
      dmaDescriptorRanges(p.dma_descriptor_ranges.begin(),
                          p.dma_descriptor_ranges.end()),
      dmaPioPassthroughRanges(p.dma_pio_passthrough_ranges.begin(),
                              p.dma_pio_passthrough_ranges.end()),
      totalColdMisses(0),
      totalCoalesced(0),
      totalDmaCtrl(0),
      totalDmaPioPassthrough(0),
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

    // -----------------------------------------------------------------
    // DMA PIO short-circuit. Any access whose target lies in a DMA's
    // PIO window is handled here and NEVER reaches the page-cache
    // path -- this prevents PIO addresses from polluting the per-PID
    // validated-page set.
    //
    // Within the PIO window we distinguish two cases:
    //   (a) WRITE to a descriptor reg (SRC/DST): the kernel driver
    //       must inspect the new (source, destination) capability,
    //       so we charge the full per-cold-miss latency and advance
    //       the chip-wide kernel-driver deadline.
    //   (b) Anything else (write to FLAGS/LEN/Stream-DMA regs, or any
    //       READ inside the PIO window): no new authority granted ->
    //       free passthrough. Status polls and DMA "go" pulses fall
    //       in this bucket.
    // -----------------------------------------------------------------
    if (isAnyDmaPio(addr)) {
        if (isWrite && isDmaDescriptorReg(addr)) {
            Tick start  = std::max(now, nextReadyTick);
            Tick ready  = start + latencyTicks;
            nextReadyTick = ready;
            Tick defer  = ready - now;
            totalDmaCtrl++;
            totalLatencyTicks += defer;
            outcome = Outcome::DmaCtrl;
            return defer;
        }
        // Passthrough: non-descriptor PIO reg, or read of any PIO reg.
        totalDmaPioPassthrough++;
        outcome = Outcome::PioPassthrough;
        return 0;
    }

    uint64_t pageAddr = addr & ~0xFFFULL;

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
