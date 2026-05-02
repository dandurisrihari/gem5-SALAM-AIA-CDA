/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * AiaKdValidator implementation. Owns the chip-wide FIFO + response
 * event for AIA-KD cold-miss requests; per-request dispatch (waiter
 * fan-out, RAW re-check, launchRead/Write replay) is delegated to
 * LLVMInterface::completeValidation() on the originating CU.
 */
#include "hwacc/aia_kd_validator.hh"

#include "base/trace.hh"
#include "debug/LLVMInterface.hh"
#include "hwacc/llvm_interface.hh"

namespace gem5
{

AiaKdValidator::AiaKdValidator(const Params &p)
    : SimObject(p),
      nextRequestId(0),
      enabledFlag(p.enabled),
      latencyTicks(p.latency),
      responseEvent([this]{ processResponse(); }, name())
{
}

void
AiaKdValidator::enqueue(PendingRequest req)
{
    req.requestId = nextRequestId++;
    pendingRequests.push_back(std::move(req));
    if (!responseEvent.scheduled()) {
        // Head deadline drives the event. Subsequent enqueues either
        // land before the current head's deadline (no-op; existing
        // schedule already covers them) or after, in which case
        // processResponse() will reschedule when it processes the head.
        schedule(responseEvent,
                 pendingRequests.front().requestTime + latencyTicks);
    }
}

void
AiaKdValidator::processResponse()
{
    Tick now = curTick();
    while (!pendingRequests.empty()) {
        PendingRequest &req = pendingRequests.front();
        if (now < req.requestTime + latencyTicks) {
            // Head not yet ready -- reschedule for its deadline.
            schedule(responseEvent, req.requestTime + latencyTicks);
            return;
        }

        // Mark the page validated chip-wide before dispatching, so any
        // sibling CU touching this page during the dispatch fan-out
        // sees a cache hit (matches the previous semantics where the
        // cache insert happened before launchRead/Write replay).
        uint64_t pageAddr = req.addr & ~0xFFFULL;
        validatedPagesPerProcess[req.pid].insert(pageAddr);
        pendingValidationPages.erase(pageAddr);

        // Dispatch to the originating CU. The validator never derefs
        // ActiveFunction or LLVMInterface itself; both pointers are
        // type-erased through void* in the request to keep this
        // header free of LLVMInterface includes.
        LLVMInterface *cu = static_cast<LLVMInterface*>(req.cu);
        cu->completeValidation(req, now);

        pendingRequests.pop_front();
    }
}

} // namespace gem5
