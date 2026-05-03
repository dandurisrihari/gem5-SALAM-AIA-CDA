#ifndef __HWACC_COMM_INTERFACE_HH__
#define __HWACC_COMM_INTERFACE_HH__

#include <list>
#include <queue>
#include <vector>

#include "dev/arm/base_gic.hh"
#include "dev/io_device.hh"
#include "hwacc/LLVMRead/src/debug_flags.hh"
#include "hwacc/LLVMRead/src/mem_request.hh"
#include "hwacc/accelerator_iommu.hh"
#include "hwacc/compute_unit.hh"
#include "hwacc/scratchpad_memory.hh"
#include "hwacc/stream_port.hh"
#include "params/CommInterface.hh"
#include "sim/eventq.hh"

class CommInterface : public BasicPioDevice
{
  protected:
    Addr io_addr;
    Addr io_size;
    Addr flag_size;
    Addr config_size;
    Addr FLAG_OFFSET;
    Addr CONFIG_OFFSET;
    Addr VAR_OFFSET;
    std::string devname;
    BaseGic *gic;
    int32_t int_num;
    bool use_premap_data;
    std::vector<Addr> data_base_ptrs;
    ByteOrder endian;
    bool debugEnabled;

  public:
    bool debug() { return debugEnabled; }
    BaseGic* getGic() { return gic; }
    int32_t getIntNum() { return int_num; }

  protected:
    class MemSidePort : public StreamRequestPort
    {
      friend class CommInterface;

      public:
        // Which fabric this MemSidePort attaches to. Only `Global`
        // (the cluster's coherency-bus / `acp` port) actually crosses
        // the cluster master interface and reaches DRAM, so a
        // realistic SMMU only translates transactions on Global
        // ports. Local (cluster xbar) and Stream (in-cluster FIFOs)
        // are intra-cluster and bypass the SMMU.
        enum class Role { Local, Global, Stream };

      private:
        CommInterface *owner;
        std::queue<PacketPtr> outstandingPkts;
        MemoryRequest *readReq;
        MemoryRequest *writeReq;
        bool readActive;
        bool writeActive;
        Role role_;

      public:
        MemSidePort(const std::string& name, CommInterface *owner,
                    Role role, PortID id=InvalidPortID) :
          StreamRequestPort(name, owner, id), owner(owner), role_(role) {
          readActive = false;
          writeActive = false;
          readReq = NULL;
          writeReq = NULL;
        }
        Role role() const { return role_; }
        MemoryRequest * getReadReq() { return readReq; }
        MemoryRequest * getWriteReq() { return writeReq; }
        void setReadReq(MemoryRequest * req = nullptr) { readReq = req; }
        void setWriteReq(MemoryRequest * req = nullptr) { writeReq = req; }

      protected:
        virtual bool recvTimingResp(PacketPtr pkt);
        virtual void recvReqRetry();
        virtual void recvRangeChange() { };
        virtual Tick recvAtomic(PacketPtr pkt) {return 0;}
        virtual void recvFunctional(PacketPtr pkt) { };
        void setStalled(PacketPtr pkt)
        {
          outstandingPkts.push(pkt);
        }
        bool isStalled() { return !outstandingPkts.empty(); }
        void sendPacket(PacketPtr pkt);
        bool active() { return readActive || writeActive; }
        bool reading() { return readActive; }
        bool writing() { return writeActive; }
        bool debug() { return owner->debug(); }
    };

    class SPMPort : public ScratchpadRequestPort
    {
      friend class CommInterface;

      private:
        CommInterface *owner;
        std::queue<PacketPtr> outstandingPkts;
        MemoryRequest *readReq;
        MemoryRequest *writeReq;
        bool readActive;
        bool writeActive;

      public:
        SPMPort(const std::string& name, CommInterface *owner, PortID id=InvalidPortID) :
          ScratchpadRequestPort(name, owner, id), owner(owner) {
          readActive = false;
          writeActive = false;
          readReq = NULL;
          writeReq = NULL;
        }
        MemoryRequest * getReadReq() { return readReq; }
        MemoryRequest * getWriteReq() { return writeReq; }
        void setReadReq(MemoryRequest * req = nullptr) { readReq = req; }
        void setWriteReq(MemoryRequest * req = nullptr) { writeReq = req; }

      protected:
        virtual bool recvTimingResp(PacketPtr pkt);
        virtual void recvReqRetry();
        virtual void recvRangeChange() { };
        virtual Tick recvAtomic(PacketPtr pkt) {return 0;}
        virtual void recvFunctional(PacketPtr pkt) { };
        void setStalled(PacketPtr pkt)
        {
          outstandingPkts.push(pkt);
        }
        bool isStalled() { return !outstandingPkts.empty(); }
        void sendPacket(PacketPtr pkt);
        bool active() { return readActive || writeActive; }
        bool reading() { return readActive; }
        bool writing() { return writeActive; }
        bool debug() { return owner->debug(); }
    };

    class RegPort : public RequestPort
    {
      friend class CommInterface;

      private:
        CommInterface *owner;
        MemoryRequest *readReq;
        MemoryRequest *writeReq;
      public:
        RegPort(const std::string& name, CommInterface *_owner, PortID id=InvalidPortID) :
          RequestPort(name, _owner), owner(_owner) {}
        void setReadReq(MemoryRequest * req = nullptr) { readReq = req; }
        void setWriteReq(MemoryRequest * req = nullptr) { writeReq = req; }
      protected:
        virtual bool recvTimingResp(PacketPtr pkt);
        virtual void recvReqRetry();
        virtual void recvRangeChange() { };
        virtual Tick recvAtomic(PacketPtr pkt) {return 0;}
        virtual void recvFunctional(PacketPtr pkt) { };
        void sendPacket(PacketPtr pkt);
        bool debug() { return owner-debug(); }
    };

    class TickEvent : public Event
    {
      private:
        CommInterface *comm;

      public:
        TickEvent(CommInterface *_comm) : Event(CPU_Tick_Pri), comm(_comm) {}
        void process() { comm->tick(); }
        virtual const char *description() const { return "CommInterface tick"; }
        bool debug() { return comm->debug(); }
    };

    std::list<MemoryRequest*> readQueue;
    std::list<MemoryRequest*> writeQueue;
    std::list<MemoryRequest*> accRdQ;
    std::list<MemoryRequest*> accWrQ;

    int requestsInQueues;

    std::vector<MemSidePort*> localPorts;
    std::vector<MemSidePort*> globalPorts;
    std::vector<MemSidePort*> streamPorts;
    std::vector<SPMPort*>     spmPorts;
    std::vector<RegPort*>     regPorts;

    bool localPortsStalled() {
        for (auto it=localPorts.begin(); it!=localPorts.end(); ++it) {
            if (!((*it)->isStalled())) return false;
        }
        return true;
    }
    bool globalPortsStalled() {
        for (auto it=globalPorts.begin(); it!=globalPorts.end(); ++it) {
            if (!((*it)->isStalled())) return false;
        }
        return true;
    }
    bool streamPortsStalled() {
        for (auto it=streamPorts.begin(); it!=streamPorts.end(); ++it) {
            if (!((*it)->isStalled())) return false;
        }
        return true;
    }
    bool spmPortsStalled() {
        for (auto port : spmPorts) {
            if (!(port->isStalled())) return false;
        }
        return true;
    }
    bool allPortsStalled() {
        return localPortsStalled() && globalPortsStalled() && streamPortsStalled() && spmPortsStalled();
    }
    bool inStreamRange(Addr add);
    bool inSPMRange(Addr add);
    bool inRegRange(Addr add);
    bool inLocalRange(Addr add);
    bool inGlobalRange(Addr add);
    MemSidePort * getValidLocalPort(Addr add, bool read);
    MemSidePort * getValidGlobalPort(Addr add, bool read);
    MemSidePort * getValidStreamPort(Addr add, size_t len, bool read);
    SPMPort *     getValidSPMPort(Addr add, size_t len, bool read);
    RegPort *     getValidRegPort(Addr add);

    CommInterface *comm;
    RequestorID masterId;
    TickEvent tickEvent;
    unsigned cacheLineSize;

    virtual void checkMMR();
    virtual void processMemoryRequests();
    virtual void tick();

    bool running;
    bool computationNeeded;
    bool int_flag;

    void tryRead(MemSidePort * port);
    void tryWrite(MemSidePort * port);

    void tryRead(SPMPort * port);
    void tryWrite(SPMPort * port);

    void tryRead(RegPort * port);
    void tryWrite(RegPort * port);

    Addr dataAddr;

    uint8_t *mmreg;

    bool processingDone;
    int processDelay;
    int clock_period;

    bool reset_spm;

    ComputeUnit *cu;

    // ----- IOMMU response-path latency injection -----
    // The chip-wide SMMU is modeled by a SimObject (AcceleratorIommu)
    // shared by every CommInterface + LLVMInterface in this
    // AccCluster. `iommu` is null when IOMMU is disabled. The
    // per-instance pending queue + event dispatch deferred packets
    // at the slots returned by `iommu->translate()`. This is the
    // centralized-SMMU edge-IoT design point (Arm SMMUv2 IHI 0062):
    // one TBU/TCU shared across every accelerator master.
    AcceleratorIommu *iommu;
    struct PendingIommuResp
    {
        PacketPtr pkt;
        Tick readyTick;
    };
    std::list<PendingIommuResp> pendingIommuResps;
    EventFunctionWrapper iommuRespEvent;
    void processIommuRespQueue();
    // Returns true if the response was deferred (caller must NOT
    // touch pkt afterwards); false if no IOMMU latency applies and
    // the caller should dispatch the packet inline. Called only from
    // MemSidePort::recvTimingResp on the Global (coherency_bus / acp)
    // role -- that is the only CommInterface egress that actually
    // crosses the cluster master interface to reach DRAM. SPM, Reg,
    // local-xbar and stream-FIFO traffic all stay on-cluster and
    // bypass the SMMU, matching real-HW behaviour.
    bool tryIommuDelay(PacketPtr pkt);

  public:
    PARAMS(CommInterface);

    CommInterface(const CommInterfaceParams &p);

    void startup();

    virtual Tick read(PacketPtr pkt);

    virtual Tick write(PacketPtr pkt);

    Port& getPort(const std::string& if_name,
                                  PortID idk = InvalidPortID) override;

    void recvPacket(PacketPtr pkt);

    // Addr value stored in gep register, length based on data type
    void enqueueRead(MemoryRequest * req); 

    void enqueueWrite(MemoryRequest * req);

    //uint8_t* getReadBuffer() { return readBuffer; }

    bool isRunning() { return running; }
    bool isCompNeeded() { return computationNeeded; }

    uint64_t getGlobalVar(unsigned offset, unsigned size);
    int getProcessDelay() { return processDelay; }
    virtual int getReadPorts()  { return 0; }
    virtual int getWritePorts()  { return 0; }
    virtual int getReadBusWidth()  { return 0; }
    virtual int getWriteBusWidth()  { return 0; }  
    virtual int getPmemRange() { return 0; }
    void registerCompUnit(ComputeUnit *compunit) { cu = compunit; }
    virtual void finish();

    MemoryRequest * findMemRequest(PacketPtr pkt, bool isRead);
    void clearMemRequest(MemoryRequest * req, bool isRead);
    virtual void refreshMemPorts() {}
    std::string getName() const { return name(); }

    virtual bool isBaseCommInterface() { return true; }
  protected:
};

#endif //__HWACC_COMM_INTERFACE_HH__

/*
* MM Register Layout
* | Location of Data 32bits | Compute Finished 1bit | Unused 30bits | Start Operation 1bit |
*/
