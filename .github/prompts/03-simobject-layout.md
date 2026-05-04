# 03 — SimObject Layout (gem5-Idiomatic Refactor)

> **Use this when** adding a new SimObject to `src/hwacc/`, refactoring
> file-scope statics into a SimObject, or modifying the
> SALAM-Configurator generator. This prompt captures the contract
> between the C++ SimObject, the Python Param surface, the SCons
> build, the `HWAccConfig` glue, and the generated `AccCluster.py`.

## Why we did this (Stages A and B)

The protection-model state used to live in **file-scope statics** in
`src/hwacc/llvm_interface.cc` and `src/hwacc/comm_interface.cc`:

- IOMMU: `sIommuNextReadyTick`, the static IOTLB LRU, a static stats
  group → moved to `AcceleratorIommu` SimObject (Stage A).
- AIA-KD: `validatedPagesPerProcess`, `pendingValidationPages`,
  `waitingForPage`, `sAiaKdRegisteredPid` PID-uniformity sentinel →
  moved to `AiaKdValidator` SimObject (Stage B).

Statics violated the gem5 model in two visible ways: they prevented
a clean Param surface, and they made multi-system simulations
silently incorrect (one process-wide cache shared across unrelated
gem5 systems). The SimObject form scopes the shared state to **one
instance per `AccCluster`**, which is the right "device" boundary.

What stayed per-CU on `LLVMInterface`: the `EventFunctionWrapper`
timer, the FIFO of in-flight requests, the GIC handle, the
reservation queue / RAW dependency machinery, and the per-CU
performance counters that feed `printKernelValidationStats`.

## File triplet contract

Every SimObject under `src/hwacc/` requires three files plus one
SConscript edit:

| File                                | Contains                                                 |
|-------------------------------------|----------------------------------------------------------|
| `src/hwacc/<Name>.py`               | `class <Name>(SimObject):` + `Param.*` declarations      |
| `src/hwacc/<name>.hh`               | `class <Name> : public SimObject` (typedef Params, ctor) |
| `src/hwacc/<name>.cc`               | ctor body + behaviour                                    |
| `src/hwacc/SConscript` (edit)       | `SimObject('<Name>.py')` + `Source('<name>.cc')`         |

The Python class name is the C++ class name; the gem5 build harness
generates `params/<Name>.hh` from the `.py` and matches it against
`cxx_class = "gem5::<Name>"` in the `.py`.

### Param wiring (`<Name>.py`)

```python
from m5.params import *
from m5.SimObject import SimObject

class AiaKdValidator(SimObject):
    type = 'AiaKdValidator'
    cxx_header = "hwacc/aia_kd_validator.hh"
    cxx_class = "gem5::AiaKdValidator"

    enabled = Param.Bool(False, "Master toggle (queried on the fast path).")
```

To make a SimObject **referenceable from another SimObject's Param**,
add a nullable pointer Param to the consumer's `.py`:

```python
# src/hwacc/LLVMInterface.py
validator = Param.AiaKdValidator(NULL,
    "Cluster-shared AIA-KD validator (cache + waiters). "
    "Wired in by tools/SALAM-Configurator/config_parser.py.")
```

`NULL` (capital, m5.params NULL) makes the Param optional; the C++
side gets `p.validator == nullptr` when unset.

### C++ side (`<name>.hh` / `<name>.cc`)

```cpp
namespace SALAM { class Instruction; }   // forward decl, GLOBAL ns

namespace gem5 {

class AiaKdValidator : public SimObject {
  public:
    typedef AiaKdValidatorParams Params;
    AiaKdValidator(const Params &p);

    bool enabled() const { return enabledFlag; }
    // ... shared state, public so peers can mutate ...
  private:
    const bool enabledFlag;
};

} // namespace gem5
```

**Important — namespace gotcha:** put any forward declarations of
SALAM-namespace types **outside** `namespace gem5 { ... }`. The real
SALAM headers declare `namespace SALAM { ... }` at file scope, *not*
nested under `gem5::`. A forward decl placed inside `gem5` creates
`gem5::SALAM`, which is a different namespace and triggers an
"ambiguous reference to SALAM" diagnostic in every TU that pulls in
both this header and the real SALAM headers (this caught us the
first time we built Stage B).

### Constructor pattern (`<name>.cc`)

```cpp
namespace gem5 {

AiaKdValidator::AiaKdValidator(const Params &p)
    : SimObject(p), enabledFlag(p.enabled)
{ }

} // namespace gem5
```

### Build registration (`src/hwacc/SConscript`)

```python
SimObject('AiaKdValidator.py')
Source('aia_kd_validator.cc')
```

## Consumer wiring

Two layers handle the connection from the SimObject instance to the
consumers (LLVMInterface, CommInterface):

### Layer 1 — `configs/SALAM/HWAccConfig.py` (`AccConfig()`)

Add a kwarg for the new SimObject and assign it onto
`acc.llvm_interface` (or `acc.comm_interface`) **after** the
LLVMInterface is constructed in `AccConfig`:

```python
def AccConfig(acc, ..., validator=None):
    acc.llvm_interface = LLVMInterface()
    ...
    if validator is not None:
        acc.llvm_interface.validator = validator
```

The kwarg defaults to `None` so non-SALAM configs that call
`AccConfig` directly continue to work.

### Layer 2 — `tools/SALAM-Configurator/config_parser.py` (the generator)

This generator emits `configs/SALAM/AccCluster.py`, which is then
**embedded into `gem5.opt` at build time** (`[EMBED PY]`). Two edit
sites:

1. **`AccCluster.genConfig()`** — emit the SimObject instantiation
   on the cluster (one per cluster):

   ```python
   lines.append("\tclstr.validator = AiaKdValidator(")
   lines.append("\t    enabled=getattr(options, "
                "'enable_kernel_validation', False),")
   lines.append("\t    latency=getattr(options, "
                "'kernel_validation_latency', 0))")
   ```

2. **`Accelerator.genDefinition()`** — pass the cluster-shared
   pointer through to `AccConfig` as a kwarg:

   ```python
   lines.append("          validator=clstr.validator)")
   ```

   For pointers wired onto `CommInterface` directly (e.g.
   `iommu`, which both `LLVMInterface` and `CommInterface`
   consume), emit a direct assignment **before** the `AccConfig(`
   call:

   ```python
   lines.append("clstr." + self.name + ".iommu = clstr.iommu")
   ```

   Do **not** emit a direct
   `clstr.<acc>.llvm_interface.validator = ...` line: at that point
   `llvm_interface` does not exist yet — it is created inside
   `AccConfig()`. Pass through the kwarg instead. (This bug bit us
   the first time on Stage B; the failure mode is
   `AttributeError: object 'CommInterface' has no attribute
   'llvm_interface'` at gem5 elaboration.)

## Rebuild + sanity checklist for SimObject changes

Any edit under `src/hwacc/*.py`, `*.hh`, `*.cc`, the SConscript, or
the SALAM-Configurator generator templates **requires**:

```bash
yes "" | scons build/ARM/gem5.opt -j$(nproc) 2>&1 | tail -20
python3 tests/aia_cda_tests/run_sanity.py --regen
```

The `yes ""` is required because scons stalls on the gem5 git-hook
installer prompt the first time a worktree is built. The sanity
suite asserts the protection-mode invariants are still intact (see
[sanity-test.prompt.md](sanity-test.prompt.md)). **Do not commit if
either step fails.**

## Type-erasure trick (for SALAM consumer types)

If a SimObject struct field needs to point at a type that lives in
`LLVMInterface` itself (e.g. `WaitingInstruction::func` →
`LLVMInterface::ActiveFunction*`), don't include `llvm_interface.hh`
from the SimObject header — that creates a cyclic include. Store
the pointer as `void*` in the SimObject and `static_cast<>` it
back at the dispatch site (`completeValidation`) where the
concrete type is already visible:

```cpp
// in aia_kd_validator.hh
struct WaitingInstruction {
    std::shared_ptr<SALAM::Instruction> inst;
    void *func;        // LLVMInterface::ActiveFunction*
    bool isRead;
    uint64_t addr;
    size_t size;
    Tick queueTime;
};

// in llvm_interface.cc, dispatch site (completeValidation):
ActiveFunction *func =
    static_cast<ActiveFunction*>(req.func);
```

This keeps the SimObject header dependency-free.
