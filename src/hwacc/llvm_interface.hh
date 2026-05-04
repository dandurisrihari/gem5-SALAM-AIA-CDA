#ifndef __HWACC_LLVM_INTERFACE_HH__
#define __HWACC_LLVM_INTERFACE_HH__

// C++ Includes
#include <algorithm>
#include <chrono>
#include <ctime>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <list>
#include <map>
#include <memory>
#include <queue>
#include <ratio>
#include <set>
#include <type_traits>
#include <typeinfo>

// LLVM Includes
#include <llvm-c/Core.h>
#include <llvm/Analysis/LoopInfo.h>
#include <llvm/IR/BasicBlock.h>
#include <llvm/IR/Dominators.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/Instruction.h>
#include <llvm/IR/LLVMContext.h>
#include <llvm/IR/Module.h>
#include <llvm/IRReader/IRReader.h>
#include <llvm/Support/SourceMgr.h>
#include <llvm/Transforms/Utils/Cloning.h>

// SALAM Includes
#include "hwacc/HWModeling/src/hw_interface.hh"
#include "hwacc/LLVMRead/src/basic_block.hh"
#include "hwacc/LLVMRead/src/debug_flags.hh"
#include "hwacc/LLVMRead/src/function.hh"
#include "hwacc/LLVMRead/src/operand.hh"
#include "hwacc/accelerator_iommu.hh"
#include "hwacc/aia_kd_validator.hh"
#include "hwacc/compute_unit.hh"
#include "params/LLVMInterface.hh"

class LLVMInterface : public ComputeUnit
{
  private:
    std::string filename;
    std::string topName;
    uint32_t scheduling_threshold;
    int32_t clock_period;
    int cycle;
    int stalls;

    bool running;
    bool loadOpScheduled;
    bool storeOpScheduled;
    bool compOpScheduled;
    bool lockstep;
    bool dbg;

    // Kernel validation infrastructure
    // Models AIA consulting KD for SMID validation
    // to prevent confused deputy attacks

    // Forward declaration of nested class
    class ActiveFunction;

    // Kernel validation settings
    bool enableKernelValidation;
    int32_t validationIntNum;
    Tick kernelValidationLatency;
    uint64_t processId;

    // ----- IOMMU/SMMU latency model -----
    // Mutually exclusive with the AIA-KD validation cache. Models the
    // hardware behavior of an SMMU sitting on the accelerator's port:
    // every memory access incurs an IOTLB lookup; misses additionally
    // pay a page-walk cost. Unlike AIA-KD, there is no "validated
    // forever" cache -- evictions force fresh walks.
    //
    // All IOMMU state (IOTLB cache, chip-wide translation port
    // deadline, stats) lives in the AcceleratorIommu SimObject pointed
    // to by `iommu`. ONE instance per AccCluster is shared by every
    // CommInterface + LLVMInterface in the cluster, so a hit installed
    // by any CU benefits every other CU. `iommu` is null when IOMMU is
    // disabled; check `enableIommu` before dereferencing.
    bool enableIommu;
    AcceleratorIommu *iommu;

    // Pending validation tracking. In Option C the per-CU FIFO and
    // response event are gone: the launch-time decision is captured
    // as a tick delta on MemoryRequest::aiaKdDefer and realized at
    // the response port (CommInterface::tryAiaKdDelay). All shared
    // state (cache, in-flight set, kernel-driver deadline) lives on
    // the chip-wide AiaKdValidator SimObject.

    // Device-wide AIA-KD validator SimObject (null when AIA-KD
    // disabled). One shared instance per AccCluster owns the cross-CU
    // validated-pages cache, pending-pages set, and chip-wide
    // kernel-driver deadline counter -- see
    // src/hwacc/aia_kd_validator.{hh,cc} for the Option C design.
    AiaKdValidator *validator;

    // Validation statistics
    uint64_t totalKernelValidations;
    Tick totalKernelValidationLatency;
    uint64_t kernelValidationDenied;
    uint64_t validationCacheHits;
    // True cache hits (page already validated).
    uint64_t validationCoalescedWaits;
    // Instructions that waited for in-flight validation.
    Tick totalCoalescedWaitLatency;
    // Total latency for coalesced waits.
    // Subset of totalKernelValidations attributable to DMA control-reg
    // writes (i.e. stores whose target address landed in any DMA's
    // PIO range). Useful for separating the "static page" first-touch
    // tax from the per-DMA-program inspection tax. See
    // ActiveFunction::launchWrite for the bypass logic and
    // .github/prompts/01-aia-kd-design.md for the threat-model split.
    uint64_t dmaCtrlValidations;

    // Per-CU response event removed: the chip-wide validator owns
    // the single EventFunctionWrapper and drives completion via
    // completeValidation() (declared public below).

    // ----- IOMMU latency injection -----
    // The IOMMU sits on the accelerator's memory port (downstream of
    // CommInterface). Latency is injected in CommInterface::
    // tryIommuDelay() (called from MemSidePort::recvTimingResp on the
    // Global egress only) and in DmaPort::tryIommuDelay() (off-cluster
    // DMA-engine egress). Both call iommu->translate() on the shared
    // AcceleratorIommu SimObject; this class only retains the
    // `iommu` pointer for stats reporting (printIommuStats).
    // See 02-iommu-design.md and 06-gotchas-and-history.md for why
    // upstream injection (the previous design) produced negative
    // sim_ticks deltas.

    std::chrono::duration<float> setupTime;
    std::chrono::duration<float> simTotal;
    std::chrono::duration<float> simTime;
    std::chrono::duration<float> schedulingTime;
    std::chrono::duration<float> queueProcessTime;
    std::chrono::duration<float> computeTime;
    std::chrono::duration<float> hwTime;
    std::chrono::high_resolution_clock::time_point simStop;
    std::chrono::high_resolution_clock::time_point setupStop;
    std::chrono::high_resolution_clock::time_point timeStart;


  class ActiveFunction
  {
      friend class LLVMInterface;
    private:
        LLVMInterface * owner;
        HWInterface* hw;
        std::shared_ptr<SALAM::Function> func;
        std::shared_ptr<SALAM::Instruction> caller;
        std::list<std::shared_ptr<SALAM::Instruction>> reservation;
        std::map<uint64_t, std::shared_ptr<SALAM::Instruction>> readQueue;
        std::map<MemoryRequest *, uint64_t> readQueueMap;
        std::map<uint64_t, std::shared_ptr<SALAM::Instruction>> writeQueue;
        std::map<MemoryRequest *, uint64_t> writeQueueMap;
        std::map<uint64_t, std::shared_ptr<SALAM::Instruction>> computeQueue;
        std::shared_ptr<SALAM::BasicBlock> previousBB;
        HW_Cycle_Stats hw_cycle_stats;
        uint32_t scheduling_threshold;
        bool returned = false;
        bool lockstep;
        bool dbg;

        inline bool uidActive(uint64_t id) {
          return computeUIDActive(id) || readUIDActive(id) ||
                 writeUIDActive(id);
        }

        std::map<Addr, std::shared_ptr<SALAM::Instruction>> activeWrites;
        inline void
        trackWrite(Addr writeAddr,
                   std::shared_ptr<SALAM::Instruction> writeInst)
        {
          activeWrites.insert({writeAddr, writeInst});
        }
        inline void untrackWrite(uint64_t writeAddr) {
          auto it = activeWrites.find(writeAddr);
          if (it != activeWrites.end()) activeWrites.erase(it);
        }
        inline bool writeActive(uint64_t writeAddr) {
          return (activeWrites.find(writeAddr) != activeWrites.end());
        }

        inline std::shared_ptr<SALAM::Instruction>
        getActiveWrite(uint64_t writeAddr)
        {
          return activeWrites.find(writeAddr)->second;
        }
        // std::map<Addr, std::shared_ptr<SALAM::Instruction>>
        // activeReads;
        // inline void trackRead(
        //     Addr readAddr,
        //     std::shared_ptr<SALAM::Instruction> readInst)
        // {
        //   activeReads.insert({readAddr, readInst});
        // }
        // inline void untrackRead(uint64_t readAddr) {
        //   auto it = activeReads.find(readAddr);
        //   if (it != activeReads.end()) activeReads.erase(it);
        // }
        // inline bool readActive(uint64_t readAddr) {
        //   return (activeReads.find(readAddr) != activeReads.end());
        // }
        // inline std::shared_ptr<SALAM::Instruction>
        // getActiveRead(uint64_t readAddr) {
        //   return activeReads.find(readAddr)->second;
        // }
        inline bool writeUIDActive(uint64_t uid) {
          return (writeQueue.find(uid) != writeQueue.end());
        }
        inline bool readUIDActive(uint64_t uid) {
          return (readQueue.find(uid) != readQueue.end());
        }
        inline bool computeUIDActive(uint64_t uid) {
          return (computeQueue.find(uid) != computeQueue.end());
        }
        // Remove instruction from reservation queue by UID
        inline bool removeFromReservation(uint64_t uid) {
          for (auto it = reservation.begin(); it != reservation.end(); ++it) {
            if ((*it)->getUID() == uid) {
              reservation.erase(it);
              return true;
            }
          }
          return false;
        }
        // Add instruction back to reservation queue for deferred
        // operations.
        inline void
        addToReservation(std::shared_ptr<SALAM::Instruction> inst)
        {
          reservation.push_back(inst);
        }
    public:
        ActiveFunction(LLVMInterface * _owner,
                       std::shared_ptr<SALAM::Function> _func,
                       std::shared_ptr<SALAM::Instruction> _caller)
            : owner(_owner), func(_func), caller(_caller),
              previousBB(nullptr)
        {
                          scheduling_threshold =
                              owner->getSchedulingThreshold();
                          lockstep = (owner->getLockstepStatus());
                          dbg = owner->debug();
                       }
        void readCommit(MemoryRequest *req);
        void writeCommit(MemoryRequest *req);
        void findDynamicDeps(std::shared_ptr<SALAM::Instruction> inst);
        void scheduleBB(std::shared_ptr<SALAM::BasicBlock> bb);
        void processQueues();
        void launch();
        inline bool queuesClear() {
          return readQueue.empty() && writeQueue.empty() &&
                 computeQueue.empty();
        }
        inline bool lockstepReady() {
          return !lockstep || queuesClear();
        }
        inline bool canReturn() {
            return queuesClear() && reservation.front()->isReturn();
        }
        bool launchRead(std::shared_ptr<SALAM::Instruction> readInst);
        bool launchWrite(std::shared_ptr<SALAM::Instruction> writeInst);
        bool hasReturned() { return returned; }
    };

    std::list<ActiveFunction> activeFunctions;
    std::map<MemoryRequest *, ActiveFunction *> globalReadQueue;
    std::map<MemoryRequest *, ActiveFunction *> globalWriteQueue;

    std::vector<std::shared_ptr<SALAM::Function>> functions;
    std::vector<std::shared_ptr<SALAM::Value>> values;
  protected:
    // const std::string name() const { return comm->getName() + ".compute"; }
    virtual bool debug() { return comm->debug(); }
    // virtual bool debug() { return true; }
  public:
    PARAMS(LLVMInterface);
    LLVMInterface(const LLVMInterfaceParams &p);
    void tick();
    void constructStaticGraph();
    void startup();
    void initialize();
    void finalize();
    void debug(uint64_t flags);
    bool getLockstepStatus() { return lockstep; }
    void readCommit(MemoryRequest *req);
    void writeCommit(MemoryRequest *req);
    void dumpModule(llvm::Module *m);
    void printResults();
    void launchFunction(std::shared_ptr<SALAM::Function> callee,
                        std::shared_ptr<SALAM::Instruction> caller);
    void launchTopFunction();
    void endFunction(ActiveFunction * afunc);
    void launchRead(MemoryRequest * memReq, ActiveFunction * func);
    void launchWrite(MemoryRequest * memReq, ActiveFunction * func);
    std::shared_ptr<SALAM::Instruction>
    createInstruction(llvm::Instruction *inst, uint64_t id);
    void dumpQueues();
    uint32_t getSchedulingThreshold() { return scheduling_threshold; }
    void addSchedulingTime(std::chrono::duration<float> timeDelta) {
        schedulingTime = schedulingTime + timeDelta;
    }
    void addQueueTime(std::chrono::duration<float> timeDelta) {
        queueProcessTime = queueProcessTime + timeDelta;
    }
    void addComputeTime(std::chrono::duration<float> timeDelta) {
        computeTime = computeTime + timeDelta;
    }
    void addHWTime(std::chrono::duration<float> timeDelta) {
        hwTime = hwTime + timeDelta;
    }

    // Kernel validation functions
    bool isKernelValidationEnabled() { return enableKernelValidation; }
    /**
     * Option C launch-time AIA-KD decision + bookkeeping.
     *
     * Called from ActiveFunction::launchRead/launchWrite for every
     * LLVM-IR memory access when AIA-KD is enabled. Delegates the
     * decision (cache hit / coalesced wait / cold miss / DMA-ctrl
     * write) to the chip-wide AiaKdValidator (which owns all shared
     * state: per-PID page cache, in-flight set, kernel-driver
     * deadline) and returns the per-access tick delta to stamp onto
     * MemoryRequest::aiaKdDefer. The CommInterface response path
     * (tryAiaKdDelay) realizes that delta as a true wall-clock
     * stall by holding the response packet for `defer` ticks.
     *
     * Per-CU mirror counters (totalKernelValidations,
     * validationCacheHits, validationCoalescedWaits, dmaCtrl-
     * Validations, totalKernelValidationLatency, totalCoalesced-
     * WaitLatency) are updated based on the Outcome so
     * printKernelValidationStats can render the harvested-string
     * report ("Validation requests (full lat): ...", "of which
     * DMA-ctrl writes: ...") that tools/speedkills/harvest.py
     * binds to.
     */
    Tick chargeValidation(uint64_t addr, bool isWrite);
    void printKernelValidationStats();

    // ----- IOMMU helpers -----
    bool isIommuEnabled() { return enableIommu; }
    void printIommuStats();
};

#endif //__HWACC_LLVM_INTERFACE_HH__
