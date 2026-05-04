/*
 * Copyright (c) 2026 SALAM contributors
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * AiaKdValidator
 * --------------
 * Device-wide AIA-KD validator SimObject. ONE instance per AccCluster,
 * shared by every LLVMInterface (CU) in the cluster. Models a single
 * chip-wide kernel-driver validation service:
 *
 *   * `validatedPagesPerProcess` -- once any CU pays the validation
 *     latency for (pid, page) the entry is cached so future accesses
 *     by ANY sibling CU are zero-cost cache hits.
 *   * `pendingValidationPages`   -- if CU0 has a validation in flight
 *     for page X, a sibling CU's first touch coalesces (waits for
 *     the same response, no duplicate IRQ to the kernel driver).
 *   * `waitingForPage`           -- queue of waiters keyed by page.
 *     Each waiter carries its OWN `func` pointer (the originating
 *     LLVMInterface::ActiveFunction), so dispatch returns to the
 *     correct CU's scheduler regardless of which CU triggered the
 *     IRQ. Actual dispatch lives in
 *     LLVMInterface::completeValidation() -- this SimObject
 *     just owns the storage.
 *
 * State only -- no behavior. The validation protocol (responseEvent,
 * GIC IRQ raise, pending-FIFO bookkeeping, scheduler re-dispatch)
 * stays in LLVMInterface because it touches per-CU machinery
 * (instruction reservation queues, the CU's own EventQueue, the
 * shared GIC). The maps below are public so LLVMInterface can touch
 * them directly without an awkward indirection layer.
 */
#ifndef __HWACC_AIA_KD_VALIDATOR_HH__
#define __HWACC_AIA_KD_VALIDATOR_HH__

#include <cstdint>
#include <deque>
#include <list>
#include <map>
#include <memory>
#include <set>

#include "base/addr_range.hh"
#include "base/types.hh"
#include "params/AiaKdValidator.hh"
#include "sim/eventq.hh"
#include "sim/sim_object.hh"

// Forward decl in the GLOBAL SALAM namespace (matches where the real
// definition lives -- src/hwacc/LLVMRead/src/instruction.hh declares
// `namespace SALAM { ... }` at file scope, NOT inside `gem5::`).
// Putting this inside `namespace gem5` would create `gem5::SALAM`,
// which is a different namespace and triggers an "ambiguous reference
// to SALAM" diagnostic in every TU that pulls in both this header and
// the real SALAM headers.
namespace SALAM { class Instruction; }

namespace gem5
{

// Avoid pulling LLVMInterface into this header. The waiter struct
// stores SALAM::Instruction by shared_ptr (forward-declared above)
// and the originating CU's ActiveFunction as an opaque pointer (cast
// back inside llvm_interface.cc where the concrete type is visible).

class AiaKdValidator : public SimObject
{
  public:
    typedef AiaKdValidatorParams Params;
    AiaKdValidator(const Params &p);

    /** Master toggle accessor (queried by LLVMInterface fast path). */
    bool enabled() const { return enabledFlag; }

    /** Per-cold-miss validation latency (chip-wide). */
    Tick latency() const { return latencyTicks; }

    /**
     * True if `addr` falls inside the PIO range of any DMA engine in
     * the cluster. Used by `LLVMInterface::ActiveFunction::launchWrite`
     * to bypass the validated-page cache for DMA control-reg writes:
     * every store reprograms the engine, so the kernel must re-inspect
     * each one. Reads are unaffected. Page-aligned input is fine; the
     * check uses the raw byte address.
     */
    bool isDmaCtrl(Addr addr) const {
        for (const auto &r : dmaCtrlRanges) {
            if (r.contains(addr)) return true;
        }
        return false;
    }

    /**
     * One waiter on an in-flight validation. `func` is the originating
     * CU's ActiveFunction* (opaque here; cast back inside
     * LLVMInterface::completeValidation() to dispatch through the
     * correct scheduler). `inst` holds the SALAM::Instruction
     * shared_ptr so the instruction stays alive while it waits.
     */
    struct WaitingInstruction
    {
        std::shared_ptr<SALAM::Instruction> inst;
        void *func;        // LLVMInterface::ActiveFunction*
        bool isRead;
        uint64_t addr;
        size_t size;
        Tick queueTime;
    };

    /**
     * One cold-miss request parked in the chip-wide FIFO. Carries the
     * originating CU pointer (`cu`, opaque LLVMInterface*) and its
     * ActiveFunction (`func`) so completeValidation() can dispatch
     * the originator back through the right CU's scheduler. The
     * validator never dereferences either pointer -- it only forwards
     * the request to LLVMInterface::completeValidation().
     */
    struct PendingRequest
    {
        uint64_t addr;
        size_t size;
        bool isRead;
        std::shared_ptr<SALAM::Instruction> inst;
        void *func;        // LLVMInterface::ActiveFunction*
        void *cu;          // LLVMInterface*
        Tick requestTime;
        uint64_t pid;
        uint64_t requestId;
    };

    // -----------------------------------------------------------------
    // Owned shared state. Public so LLVMInterface can mutate directly.
    // -----------------------------------------------------------------

    /** Per-process validated-page cache. */
    std::map<uint64_t, std::set<uint64_t>> validatedPagesPerProcess;

    /** Pages with an in-flight validation request (cross-CU coalescing). */
    std::set<uint64_t> pendingValidationPages;

    /** Waiters keyed by 4 KiB-aligned page address. */
    std::map<uint64_t, std::list<WaitingInstruction>> waitingForPage;

    /**
     * Chip-wide FIFO of cold-miss validation requests, in arrival
     * order. ONE FIFO services every CU in the cluster -- mirrors a
     * single kernel-driver thread fielding one IRQ at a time. The
     * head's `requestTime + latencyTicks` deadline drives
     * `responseEvent`; on completion the request is dispatched back
     * to its originating CU via LLVMInterface::completeValidation().
     */
    std::deque<PendingRequest> pendingRequests;

    /** Monotonic id stamped onto each PendingRequest (debug only). */
    uint64_t nextRequestId;

    /**
     * Append a cold-miss request to the chip-wide FIFO and (re)schedule
     * the response event for the head deadline if it isn't already
     * scheduled. Caller must have already inserted `pageAddr` into
     * `pendingValidationPages` so coalescing siblings see the inflight
     * state immediately.
     */
    void enqueue(PendingRequest req);

  private:
    /**
     * Drain ready entries from `pendingRequests`. For each ready entry
     * the validator: marks the page validated, removes it from
     * `pendingValidationPages`, then forwards the request to
     * LLVMInterface::completeValidation() on the originator's CU,
     * which performs the per-CU dispatch + waiter fan-out.
     * Reschedules itself for the next deadline if work remains.
     */
    void processResponse();

    const bool enabledFlag;
    const Tick latencyTicks;
    /** PIO ranges of DMA engines; writes here bypass the cache. */
    const AddrRangeList dmaCtrlRanges;
    EventFunctionWrapper responseEvent;
};

} // namespace gem5

#endif // __HWACC_AIA_KD_VALIDATOR_HH__
