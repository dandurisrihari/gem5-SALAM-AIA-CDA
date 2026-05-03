#ifndef __HWACC_COMPUTE_UNIT_HH__
#define __HWACC_COMPUTE_UNIT_HH__
//------------------------------------------//
#include "params/ComputeUnit.hh"
#include "sim/sim_object.hh"
#include "hwacc/comm_interface.hh"
#include "hwacc/LLVMRead/src/mem_request.hh"
#include "hwacc/LLVMRead/src/debug_flags.hh"
#include "hwacc/HWModeling/src/hw_interface.hh" 
//------------------------------------------//

class ComputeUnit : public SimObject {
  private:

  protected:
    CommInterface *comm;
    HWInterface* hw;

    class TickEvent : public Event
    {
      private:
        ComputeUnit *comp_unit;

      public:
        TickEvent(ComputeUnit *_comp_unit) : Event(CPU_Tick_Pri), comp_unit(_comp_unit) {}
        void process() { comp_unit->tick(); }
        virtual const char *description() const { return "ComputeUnit tick"; }
    };


    TickEvent tickEvent;
    int clock_period;

  public:
    virtual void tick() {}
    ComputeUnit(const ComputeUnitParams &p);
    virtual void initialize() {}
    virtual void readCommit(MemoryRequest * req) {}
    virtual void writeCommit(MemoryRequest * req) {}
    // Vestigial hook from an earlier IOMMU draft. The current model
    // injects translation latency on the response path inside
    // CommInterface::tryIommuDelay() / DmaPort::tryIommuDelay(),
    // both of which call AcceleratorIommu::translate() directly --
    // this virtual is no longer called from anywhere. Kept (and
    // returning 0) only to preserve the ABI of any out-of-tree
    // ComputeUnit subclass that may still override it.
    virtual Tick iommuLatencyForAccess(Addr addr, bool isRead) { return 0; }
    CommInterface * getCommInterface() { return comm; }
    HWInterface * getHWInterface() { return hw; }
};

#endif //__HWACC_COMPUTE_UNIT_HH__
