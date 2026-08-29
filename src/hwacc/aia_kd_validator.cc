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

#include "base/cprintf.hh"
#include "base/logging.hh"
#include "sim/sim_exit.hh"

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
      violationCheck(p.violation_check),
      stopOnViolation(p.stop_on_violation),
      forbiddenRanges(p.forbidden_ranges.begin(),
                      p.forbidden_ranges.end()),
      totalColdMisses(0),
      totalCoalesced(0),
      totalDmaCtrl(0),
      totalDmaPioPassthrough(0),
      totalLatencyTicks(0),
      totalDenied(0),
      totalMissed(0),
      leakedBytes(0),
      firstViolationTick(MaxTick),
      firstDeniedIssueTick(MaxTick),
      firstDetectionTick(MaxTick)
{
    if (violationCheck && forbiddenRanges.empty()) {
        warn("%s: violation_check=True but forbidden_ranges is empty; "
             "no illegal access can ever be constructed.", name());
    }
}

void
AiaKdValidator::recordDenial(uint64_t pid, Addr addr, bool isWrite,
                             Tick detectTick)
{
    totalDenied++;
    // Latch issue and detection ticks as a PAIR from the same access,
    // so the reported latency is that one access's round-trip.
    if (firstDetectionTick == MaxTick) {
        firstDeniedIssueTick = curTick();
        firstDetectionTick = detectTick;
    }

    warn("%s: AIA-KD DENIED %s pid=%llu addr=%#018lx issued@%llu "
         "detected@%llu", name(), isWrite ? "WRITE" : "READ",
         (unsigned long long)pid, (unsigned long)addr,
         (unsigned long long)curTick(),
         (unsigned long long)detectTick);

    // Only the first denial schedules the exit; later ones inside the
    // same detection window would just queue redundant exit events.
    if (stopOnViolation && totalDenied == 1) {
        exitSimLoop(csprintf("AIA-KD violation: %s pid=%llu addr=%#018lx",
                             isWrite ? "write" : "read",
                             (unsigned long long)pid,
                             (unsigned long)addr),
                    0, detectTick);
    }
}

void
AiaKdValidator::recordMiss(unsigned size, Outcome &outcome)
{
    totalMissed++;
    leakedBytes += size;
    outcome = Outcome::Missed;
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
AiaKdValidator::checkAndCharge(uint64_t pid, Addr addr, unsigned size,
                               bool isWrite, Outcome &outcome)
{
    // Lat=0 / disabled fast path: zero overhead, identical to plain.
    if (!enabledFlag || latencyTicks == 0) {
        outcome = Outcome::Disabled;
        return 0;
    }

    Tick now = curTick();
    promoteReadyPages(now);

    // Effectiveness axis. This predicate is the KERNEL DRIVER'S
    // VERDICT, not an omniscient monitor: charging full latency IS
    // the act of consulting the driver, so the driver can only refuse
    // where full latency is charged (cold page, DMA SRC/DST
    // reprogram). That is the whole reason the "this address is
    // forbidden" condition has to be injected -- in the unmodified
    // model every consult returns "granted", so nothing could ever be
    // stopped. On the paths that skip the driver (cache hit,
    // coalesced follower, PIO passthrough) the mechanism has no
    // opinion; we still count the access, but purely as experiment
    // ground truth. Cheap early-out to false when the mode is off.
    //
    // INVARIANT: Outcome::Denied only on a path that charged full
    // latency; Outcome::Missed only on a path that did not.
    const bool illegal = isForbidden(addr, size);
    if (illegal && firstViolationTick == MaxTick) firstViolationTick = now;

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
            totalLatencyTicks += defer;
            if (illegal) {
                // The driver inspects every descriptor reprogram, so
                // this is the one PIO path where a violation is caught.
                recordDenial(pid, addr, isWrite, ready);
                outcome = Outcome::Denied;
                return defer;
            }
            totalDmaCtrl++;
            outcome = Outcome::DmaCtrl;
            return defer;
        }
        // Passthrough: non-descriptor PIO reg, or read of any PIO reg.
        // The driver is never consulted here, so an illegal access
        // through this path is by construction undetectable.
        totalDmaPioPassthrough++;
        if (illegal) {
            recordMiss(size, outcome);
            return 0;
        }
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
        if (illegal) {
            // AIA-KD's structural blind spot: the grant was issued at
            // 4 KiB granularity for a legitimate address in this page,
            // so a forbidden byte sharing the page is never re-checked.
            recordMiss(size, outcome);
            return 0;
        }
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
        if (illegal) {
            // A forbidden page is never admitted to the in-flight set,
            // so reaching here means the originator's address was legal
            // and the driver is about to grant the whole page -- same
            // blind spot as the cache-hit case, just one step earlier.
            recordMiss(size, outcome);
            return defer;
        }
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
    Tick defer = ready - now;
    totalLatencyTicks += defer;

    if (illegal) {
        // The driver is occupied for the full round-trip and then says
        // no. The page is deliberately left out of both the in-flight
        // set and the validated cache so a retry pays the tax again.
        recordDenial(pid, addr, isWrite, ready);
        outcome = Outcome::Denied;
        return defer;
    }

    pendingValidationPages[pageAddr] = {ready, pid};
    totalColdMisses++;
    outcome = Outcome::ColdMiss;
    return defer;
}

} // namespace gem5
