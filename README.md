# gem5-SALAM #

gem5-SALAM (System Architecture for LLVM-based Accelerator Modeling), is a novel system architecture designed to enable LLVM-based modeling and simulation of custom hardware accelerators.

# Requirements

- gem5 dependencies
- LLVM-9 or newer
- Frontend LLVM compiler for preferred development language (eg. clang for C)

# gem5-SALAM Setup

## All Required Dependencies for gem5-SALAM (Ubuntu 20.04)

```bash
sudo apt install build-essential git m4 scons zlib1g zlib1g-dev \
    libprotobuf-dev protobuf-compiler libprotoc-dev libgoogle-perftools-dev \
    python3-dev python-is-python3 libboost-all-dev pkg-config
```

## LLVM/Clang Setup

For a quick start, one can simply run the following to install LLVM and Clang on Ubuntu 20.04.
```bash
sudo apt install llvm-12 llvm-12-tools clang-12
```
After installing these specific libraries, simply run the [update alternatives](https://github.com/TeCSAR-UNCC/gem5-SALAM/blob/main/docs/update-alternatives.sh) script in docs/.

Alternatively, you can install the latest version of LLVM via your system package manager or build from source found at https://github.com/llvm/llvm-project.

# Building gem5-SALAM

Once you have successfully installed all of the necessary dependencies, you can go ahead and clone the gem5-SALAM repository to begin working with it.

```bash
git clone https://github.com/TeCSAR-UNCC/gem5-SALAM
```

When building gem5-SALAM, there are multiple different binary types that can be created. Just like in gem5 the options are debug, opt, fast, prof, and perf. We recommend that users either use the opt or debug builds, as these are the build types we develop and test on.

Below are the bash commands you would use to build the opt or debug binary.

```bash
scons build/ARM/gem5.opt -j`nproc`
```

```bash
scons build/ARM/gem5.debug -j`nproc`
```

For more information regarding the binary types, and other build information refer to the gem5 build documentation [here](http://learning.gem5.org/book/part1/building.html).

# Using VS Code Dev Container

For the easiest setup, open the repository in VS Code with the [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension installed:

1. Open the repository folder in VS Code.
2. Press `F1` and run **Dev Containers: Reopen in Container**.
3. VS Code will build the image using `.devcontainer/devcontainer.json` (which reuses `docker/Dockerfile`) and drop you into the container at `/gem5-SALAM` with all dependencies installed.
4. Build inside the container:

    ```bash
    scons build/ARM/gem5.opt -j$(nproc)
    ```

The workspace is bind-mounted, so edits sync between host and container. Recommended C/C++, Python, clangd, Makefile, and YAML extensions are auto-installed.

# Building with docker

You can use the Dockerfile in the `docker/` directory to build and run gem5-SALAM in a containerized environment with volume mounting (changes sync between host and container).

## Setup

```bash
cd docker

# Configure your user ID (run once)
echo "HOST_UID=$(id -u)" > .env
echo "HOST_GID=$(id -g)" >> .env

# Build the Docker image
docker-compose build
```

## Running

```bash
cd docker
docker-compose run --rm gem5-salam
```

This will start a container with the repository mounted at `/gem5-SALAM`. Any changes made inside the container will be reflected on your host machine, and vice versa.

## Building gem5-SALAM inside the container

Once inside the container, build gem5-SALAM:

```bash
scons build/ARM/gem5.opt -j$(nproc)
```

## Running benchmarks inside the container

```bash
cd $M5_PATH/benchmarks/sys_validation/bfs
make
$M5_PATH/tools/run_system.sh --bench bfs --bench-path benchmarks/sys_validation/bfs
```

# Using gem5-SALAM

To use gem5-SALAM you need to define the computation model of you accelerator in your language of choice,and compile it to LLVM IR. Any control and dataflow graph optimization (eg. loop unrolling) should be handled by the compiler. You can construct accelerators by associating their LLVM IR with an LLVMInterface and connecting it to the desired CommInterface in the gem5 memory map.

Below are some resources in the gem5-SALAM directory that can be used when getting started:

- Examples for system-level configuration can be found in **configs/common/HWAcc.py**.
- Accelerator benchmarks and examples can be found in the **benchmarks** directory.
- The **benchmarks/common** directory contains basic drivers and syscalls for baremetal simulation.
- **benchmarks/sys_validation** contains examples for configuring and using gem5-SALAM with different algorithms.

## System Validation Examples

The system validation examples under **benchmarks/sys_validation** are good examples for how you interface with the gem5-SALAM simulation objects.

## Running MobileNetV2 Benchmark

MobileNetV2 is a neural network benchmark that demonstrates gem5-SALAM's accelerator capabilities.

```bash
cd $M5_PATH/benchmarks/mobilenetv2
make
```

**Run with tracing:**
```bash
./tools/run_system.sh --bench mobilenetv2 --bench-path benchmarks/mobilenetv2 --config-name 1_config.yml -f "LLVMInterface,NoncoherentDma,CommInterface" -p
```

**View results:**
```bash
cat BM_ARM_OUT/benchmarks/mobilenetv2/debug-trace.txt
```

In order to use the system validation benchmarks, it is required to have the ARM GCC cross-compiler installed. If you didn't already install it when you setup the dependencies, you can install it in Ubuntu by running the below command:

```bash
sudo apt-get install gcc-multilib gcc-arm-none-eabi
```

**run_system.sh** requires an environment variable named **M5_PATH** to be set. You will want to point it to your gem5-SALAM path as shown below.

```bash
export M5_PATH=/path/to/gem5-SALAM
```

Next, compile your desired example.

```bash
cd $M5_PATH/benchmarks/sys_validation/[benchmark]
make
```

Finally, you can run any of the benchmarks you have compiled by running the run system script.

```bash
$M5_PATH/tools/run_system.sh --bench bfs --bench-path benchmarks/sys_validation/bfs
```

If you would like to see the gem5-SALAM command created by the shell file you would just need to inspect the **RUN_SCRIPT** variable in the shell file.

# Security Validation for Accelerators

Added support to gem5-SALAM for modeling **kernel-based memory validation performance calculation** to prevent confused deputy attacks on hardware accelerators. This feature models the latency overhead of having the kernel validate accelerator memory accesses.

## Background

When hardware accelerators perform DMA operations, they access memory on behalf of user processes. Without proper validation, a malicious process could trick the accelerator into accessing memory it shouldn't have access to (confused deputy attack). The kernel validation feature models the overhead of having the OS validate each unique memory page accessed by the accelerator.

## Enabling Kernel Validation

Add the `--enable-kernel-validation` flag when running simulations:

```bash
./tools/run_system.sh --bench mobilenetv2 --bench-path benchmarks/mobilenetv2 \
    -- --enable-kernel-validation --kernel-validation-latency=10000
```

## Validation Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--enable-kernel-validation` | False | Enable validation checks |
| `--kernel-validation-latency` | 10000 | Latency in ticks for each validation |
| `--validation-int-num` | 172 | GIC interrupt number for validation |
| `--process-id` | 17 | Process ID for validation context |

## How It Works

1. **First access to a memory page**: Incurs validation latency (models kernel checking page tables)
2. **Subsequent accesses to same page**: No latency (page cached as validated)
3. **Per-process caching**: Each process has its own validation cache

## Trace Output

Enable tracing to see validation events:

```bash
./tools/run_system.sh --bench mobilenetv2 --bench-path benchmarks/mobilenetv2 \
    -f "LLVMInterface" -- --enable-kernel-validation --kernel-validation-latency=10000
```

Example trace output:
```
[AIA->KD] Validation req #0: READ addr=0x10021e00 (page 0x10021000), pid=17
[AIA->KD] Raising interrupt 172 to kernel
[AIA] Scheduled response in 10000 ticks
[KD->AIA] Response #0: READ addr=0x10021e00, pid=17 => OK (lat=10000)
[AIA] Cache HIT: READ addr=0x10021e40 (page 0x10021000) pid=17
```

# Parallel Experiment Runner

The `run_parallel.sh` script allows running multiple experiments with different validation latencies in parallel. It can automatically build benchmarks and run experiments across all available benchmarks.

## Usage

```bash
./tools/run_parallel.sh [OPTIONS]
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--bench` | mobilenetv2 | Benchmark name |
| `--bench-path` | benchmarks/mobilenetv2 | Path to benchmark |
| `--config-name` | 1_config.yml | Accelerator config file |
| `--latencies` | 0,10000,50000,100000 | Comma-separated validation latencies |
| `--parallel`, `-j` | 4 | Number of parallel jobs |
| `--all`, `-a` | disabled | Run all available benchmarks |
| `--list` | - | List all available benchmarks |
| `--trace`, `-t` | disabled | Enable debug tracing |
| `--trace-flags` | LLVMInterface | Debug trace flags |
| `--dry-run` | disabled | Print commands without executing |

## Available Benchmarks

Run `./tools/run_parallel.sh --list` to see all available benchmarks:

| Benchmark | Path | Description |
|-----------|------|-------------|
| mobilenetv2 | benchmarks/mobilenetv2 | MobileNetV2 neural network |
| bfs | benchmarks/sys_validation/bfs | Breadth-first search |
| fft | benchmarks/sys_validation/fft | Fast Fourier transform |
| gemm | benchmarks/sys_validation/gemm | General matrix multiply |
| md_grid | benchmarks/sys_validation/md_grid | Molecular dynamics (grid) |
| md_knn | benchmarks/sys_validation/md_knn | Molecular dynamics (KNN) |
| nw | benchmarks/sys_validation/nw | Needleman-Wunsch alignment |
| spmv | benchmarks/sys_validation/spmv | Sparse matrix-vector multiply |
| stencil2d | benchmarks/sys_validation/stencil2d | 2D stencil computation |
| stencil3d | benchmarks/sys_validation/stencil3d | 3D stencil computation |
| mergesort | benchmarks/sys_validation/mergesort | Merge sort |
| lenet_a | benchmarks/lenet/design_a | LeNet-5 CNN (design A) |
| lenet_b | benchmarks/lenet/design_b | LeNet-5 CNN (design B) |
| lenet_c | benchmarks/lenet/design_c | LeNet-5 CNN (design C) |
| mobilenetv2_35 | benchmarks/mobilenetv2 | MobileNetV2 (width=0.35) |
| mobilenetv2_75 | benchmarks/mobilenetv2 | MobileNetV2 (width=0.75) |

## Examples

**Run MobileNetV2 with multiple latencies:**
```bash
./tools/run_parallel.sh --bench mobilenetv2 --latencies 0,5000,10000,25000,50000 --parallel 3
```

**Run all benchmarks with validation experiments:**
```bash
./tools/run_parallel.sh --all --latencies 0,10000,50000 --parallel 4
```

**Dry run to see commands (MobileNetV2 only):**
```bash
./tools/run_parallel.sh --latencies 0,10000 --dry-run
```

**Dry run for all benchmarks:**
```bash
./tools/run_parallel.sh --all --latencies 0,10000 --dry-run
```

**Run with tracing enabled:**
```bash
./tools/run_parallel.sh --latencies 10000 --trace
```

**Run specific benchmark:**
```bash
./tools/run_parallel.sh --bench gemm --latencies 0,10000
```

## Automatic Building

The script automatically builds benchmarks if the kernel (`main.elf`) is not found:

```
[BUILD] Kernel not found for fft, building...
[BUILD] Successfully built fft
```

## Output

Results are saved to `BM_ARM_OUT/<benchmark>_experiments_<timestamp>/` for single benchmark runs, or `BM_ARM_OUT/all_benchmarks_<timestamp>/` for `--all` runs:

```
BM_ARM_OUT/
├── mobilenetv2_experiments_20251229_123456/
│   ├── baseline_no_validation/
│   │   ├── stats.txt
│   │   └── run.log
│   ├── latency_10000/
│   │   ├── stats.txt
│   │   └── run.log
│   └── summary.csv
└── all_benchmarks_20251229_234567/
    ├── bfs/
    │   ├── baseline_no_validation/
    │   └── latency_10000/
    ├── gemm/
    ├── mobilenetv2/
    └── summary.csv
```

# Experiment Monitor

The `experiment_monitor.py` script generates an HTML dashboard to monitor experiment progress in real-time.

## Usage

```bash
./tools/experiment_monitor.py [OUTPUT_DIR] [OPTIONS]
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--latest` | - | Auto-detect latest output directory |
| `--watch`, `-w` | disabled | Continuously update HTML file |
| `--interval`, `-i` | 10 | Refresh interval in seconds |
| `--output`, `-o` | experiment_status.html | Output HTML filename |
| `--serve`, `-s` | disabled | Start HTTP server and open browser |
| `--port`, `-p` | 8080 | HTTP server port |

## Examples

**Start server and open dashboard (recommended):**
```bash
./tools/experiment_monitor.py --latest --serve
```

**Watch for updates with auto-refresh and server:**
```bash
./tools/experiment_monitor.py --latest --watch --serve
```

**Generate dashboard for specific output directory:**
```bash
./tools/experiment_monitor.py BM_ARM_OUT/all_benchmarks_20251229_230316
```

**Auto-detect latest output and watch for updates:**
```bash
./tools/experiment_monitor.py --latest --watch
```

**Custom port and refresh interval:**
```bash
./tools/experiment_monitor.py --latest --watch --serve --port 9000 --interval 5
```

## Dashboard Features

- **Summary cards**: Total, Completed, Running, Failed, Pending experiment counts
- **Progress bar**: Overall completion percentage
- **Performance charts**: Bar/line charts showing simulation time vs validation latency with overhead %
- **Running processes**: Live list of gem5 PIDs with CPU/memory usage
- **Experiments by benchmark**: Grouped view showing latency, status, sim time, and overhead
- **Results table**: Completed experiments with simulation times, validations, and cache hit rates

## Viewing the Dashboard

**Option 1: Auto-start server (recommended):**
```bash
./tools/experiment_monitor.py --latest --serve
# Opens http://localhost:8080/experiment_status.html automatically
```

**Option 2: Manual HTTP server:**
```bash
OUTPUT_DIR=$(ls -d BM_ARM_OUT/all_benchmarks_* | tail -1)
python3 -m http.server 8080 --directory "$OUTPUT_DIR"
# Then open http://localhost:8080/experiment_status.html
```

The dashboard auto-refreshes every 10 seconds (configurable).

# Resources

## gem5 Documentation

https://www.gem5.org/documentation/

## gem5 Tutorial

The gem5 documentation has a [tutorial for working with gem5](http://learning.gem5.org/book/index.html#) that will help get you started with the basics of creating your own sim objects.

## Building and Integrating Accelerators in gem5-SALAM

We have written a guide on how to create the GEMM system validation example. This will help you get started with creating your own benchmarks and systems. It can be viewed [here](https://github.com/TeCSAR-UNCC/gem5-SALAM/blob/master/docs/Building_and_Integrating_Accelerators.md).

## SALAM Object Overview

The [SALAM Object Overview](https://github.com/TeCSAR-UNCC/gem5-SALAM/blob/master/docs/SALAM_Object_Overview.md) covers what various Sim Objects in gem5-SALAM are and their purpose.

## Full-system OS Simulation ##

Please download the latest version of the Linux Kernel for ARM from the [gem5 ARM kernel page](http://gem5.org/ARM_Kernel).
You will also need the [ARM disk images](http://www.gem5.org/dist/current/arm/) for full system simulation.
Devices operate in the physical memory address space.
