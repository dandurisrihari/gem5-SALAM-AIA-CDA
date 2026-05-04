#ifndef MEMORY_REQUEST_HH
#define MEMORY_REQUEST_HH
//------------------------------------------//
#include "mem/packet.hh"
#include "mem/port.hh"
#include "debug_flags.hh"
//------------------------------------------//

using namespace gem5;

class MemoryRequest {
  friend class CommInterface;
  // friend class LLVMInterface;
  private:
    Addr address;
    size_t length;
    bool needToRead;
    bool needToWrite;
    Addr currentReadAddr;
    Addr currentWriteAddr;
    Addr beginAddr;
    Tick writeLeft;
    Tick writeDone;
    Tick readLeft;
    Tick readDone;
    Tick totalLength;

    uint8_t *buffer;
    bool *readsDone;

    PacketPtr pkt;
    RequestPort * port;
  public:
    // ----- AIA-KD response-side defer stamp (Option C) -----
    // Set by LLVMInterface::ActiveFunction::launchRead/launchWrite at
    // the moment the access is launched: number of ticks the
    // chip-wide AIA-KD validator wants to stall this access on the
    // RESPONSE path (cold-miss latency, or a coalesced share of an
    // in-flight cold miss, or zero on a cache hit / DMA-ctrl bypass
    // is irrelevant since DMA-ctrl always charges full latency).
    //
    // CommInterface::tryAiaKdDelay() reads this field on the first
    // response packet for this MemoryRequest, queues the packet for
    // `curTick() + aiaKdDefer`, and ZEROS the field so subsequent
    // packets of the same multi-packet request pass through
    // unmodified -- this preserves "one validation tax per LLVM-IR
    // load/store" rather than "per cache-line packet".
    Tick aiaKdDefer = 0;
  public:
    MemoryRequest(Addr add, size_t len);
    MemoryRequest(Addr add, const void *data, size_t len);
    ~MemoryRequest() {
        delete[] readsDone;
        delete[] buffer;
        // if (pkt) delete pkt;
    }
    void setCarrierPort(RequestPort * _port) { port = _port; }
    RequestPort * getCarrierPort() { return port; }
    uint8_t * getBuffer() { return buffer; }
    Addr getAddress() { return address; }
    std::string printBuffer();
};

#endif //__MEM_REQUEST_HH__
