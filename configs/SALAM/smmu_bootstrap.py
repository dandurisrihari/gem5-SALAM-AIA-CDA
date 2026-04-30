# Copyright (c) 2026 SALAM contributors
#
# SPDX-License-Identifier: BSD-3-Clause
"""
SMMUv3 bootstrap blob builder.

Materialises the in-memory data structures that an SMMUv3 in stage-1
translation mode needs in order to operate, *without* requiring any
software (Linux driver or bare-metal init code) to run on the
simulated CPU.

Strategy
--------
We build an identity-mapping AArch64 stage-1 page table covering the
low 4 GiB of physical address space using 2 MiB block descriptors at
L2. Identity mapping keeps the workload's pointers unchanged, while
forcing the SMMU to perform real translations means every distinct
2 MiB region the accelerator touches contributes a TLB miss + walk on
first touch, exactly like a real SoC.

A 2 MiB granule was chosen because:

  * it keeps the page-table footprint tiny (≈ 24 KiB total tree),
  * walk depth is L0 → L1 → L2 (3 reads on a cold miss), matching
    a Linux-style large-page mapping, and
  * the SMMU's main TLB caches at the block granule, so sweeping
    `--smmu-tlb-entries` produces a measurable curve for any
    workload whose footprint exceeds `tlb_entries * 2 MiB`.

The blob layout, relative to the per-cluster scratch base:

    +0x0000   STE table   (4 KiB,  64 entries × 64 B)
    +0x1000   ContextDesc (64 B; one CD shared by every valid STE)
    +0x2000   L0 page table (4 KiB; entry 0 → L1)
    +0x3000   L1 page table (4 KiB; entries 0..3 → L2 #0..#3)
    +0x4000   L2 page table #0 (4 KiB, identity-maps  0 .. 1 GiB)
    +0x5000   L2 page table #1 (4 KiB, identity-maps 1 .. 2 GiB)
    +0x6000   L2 page table #2 (4 KiB, identity-maps 2 .. 3 GiB)
    +0x7000   L2 page table #3 (4 KiB, identity-maps 3 .. 4 GiB)

Total: 32 KiB. The Python helper returns the bytes plus the values to
seed into the SMMU registers (STRTAB_BASE, STRTAB_BASE_CFG, CR0).
"""

import struct


# Layout offsets within the blob.
STE_TABLE_OFFSET = 0x0000
CD_OFFSET        = 0x1000
L0_OFFSET        = 0x2000
L1_OFFSET        = 0x3000
L2_BASE_OFFSET   = 0x4000

# 4 L2 tables × 4 KiB each, immediately after L2_BASE_OFFSET.
BLOB_TOTAL_SIZE = 0x8000   # 32 KiB

# Stream table: 64 entries × 64 B = 4 KiB. log2(64) = 6.
STREAM_TABLE_LOG2_ENTRIES = 6
STREAM_TABLE_NUM_ENTRIES  = 1 << STREAM_TABLE_LOG2_ENTRIES
STE_SIZE_BYTES            = 64
CD_SIZE_BYTES             = 64

# Page-table geometry: AArch64, 4 KiB granule.
PT_ENTRIES_PER_LEVEL = 512
GIB                  = 1 << 30
MIB                  = 1 << 20

# SMMUv3 stream-table-cfg encoding (ST_CFG_FMT_LINEAR == 0).
ST_CFG_FMT_LINEAR = 0x0

# CR0 bit.
CR0_SMMUEN = 0x1


def _ste_stage1_only(cd_pa: int) -> bytes:
    """
    Build a single 64 B STE that selects stage-1-only translation and
    points at the given Context Descriptor.

    Bit layout (DWORD0):
      [0]      valid = 1
      [3:1]    config = 0b101 (STE_CONFIG_STAGE1_ONLY)
      [5:4]    s1fmt = 0 (single CD; not a CD table)
      [51:6]   s1ctxptr = cd_pa >> 6 (placed at bit 6 -> shift left 0
               after the right shift, since the field starts at bit 6)
      [63:59]  s1cdmax = 0 (single CD)
    Other DWORDs are zero (sane defaults: stage 2 disabled, NS, etc).
    """
    if cd_pa & 0x3F:
        raise ValueError(
            f"CD address {cd_pa:#x} must be 64-byte aligned")

    dw0 = 0
    dw0 |= 1 << 0                           # valid
    dw0 |= (0x5 & 0x7) << 1                 # config = STAGE1_ONLY
    dw0 |= (0 & 0x3) << 4                   # s1fmt = 0
    dw0 |= ((cd_pa >> 6) & ((1 << 46) - 1)) << 6  # s1ctxptr
    # s1cdmax stays 0.

    return struct.pack("<QQQQQQQQ", dw0, 0, 0, 0, 0, 0, 0, 0)


def _context_descriptor(l0_pa: int) -> bytes:
    """
    Build a 64 B Context Descriptor that programs an AArch64 stage-1
    translation regime with TTBR0 = l0_pa, 4 KiB granule, 48-bit VA.

    DWORD0:
      [5:0]    t0sz  = 16 (64 - 48 = 16)
      [7:6]    tg0   = 0 (4 KiB granule)
      [9:8]    ir0   = 1 (inner WB cacheable)
      [11:10]  or0   = 1 (outer WB cacheable)
      [13:12]  sh0   = 3 (inner shareable)
      [14]     epd0  = 0 (TTBR0 walks enabled)
      [15]     endi  = 0 (little-endian)
      [21:16]  t1sz  = 0
      [30]     epd1  = 1 (TTBR1 walks disabled)
      [31]     valid = 1
      [34:32]  ips   = 1 (40-bit PA output; plenty for our 4 GiB map)
      [41]     aa64  = 1 (AArch64 mode)
      [63:48]  asid  = 1
    DWORD1:
      [51:4]   ttb0  = l0_pa >> 4 (placed at bit 4)
    """
    if l0_pa & 0xFFF:
        raise ValueError(
            f"TTB0 address {l0_pa:#x} must be 4 KiB aligned")

    dw0 = 0
    dw0 |= (16 & 0x3F)                      # t0sz
    dw0 |= (0 & 0x3) << 6                   # tg0 = 4K
    dw0 |= (1 & 0x3) << 8                   # ir0
    dw0 |= (1 & 0x3) << 10                  # or0
    dw0 |= (3 & 0x3) << 12                  # sh0
    # epd0 = 0; endi = 0; t1sz = 0.
    dw0 |= 1 << 30                          # epd1 = 1 (no TTBR1)
    dw0 |= 1 << 31                          # valid
    dw0 |= (1 & 0x7) << 32                  # ips = 40-bit
    dw0 |= 1 << 41                          # aa64
    dw0 |= (1 & 0xFFFF) << 48               # asid

    dw1 = 0
    dw1 |= ((l0_pa >> 4) & ((1 << 48) - 1)) << 4   # ttb0 at [51:4]

    # mair, amair, _pad[3]: leave zero. Translation works without
    # MAIR being meaningful because we never check memory attributes.
    return struct.pack("<QQQQQQQQ", dw0, dw1, 0, 0, 0, 0, 0, 0)


def _table_descriptor(next_pa: int) -> int:
    """
    AArch64 stage-1 table descriptor.

      [0]     valid = 1
      [1]     type  = 1 (table)
      [47:12] next-level-table base (4 KiB aligned)
    """
    if next_pa & 0xFFF:
        raise ValueError(
            f"Next-level table {next_pa:#x} must be 4 KiB aligned")
    return 0x3 | (next_pa & ((1 << 48) - (1 << 12)))


def _block_descriptor_2mb(pa: int) -> int:
    """
    AArch64 stage-1 block descriptor for an L2 entry (2 MiB block).

      [0]      valid = 1
      [1]      type  = 0 (block)
      [4:2]    AttrIndx = 0 (MAIR index 0; we don't care for func)
      [5]      NS    = 0
      [7:6]    AP    = 0 (RW @ EL1)
      [9:8]    SH    = 3 (inner shareable)
      [10]     AF    = 1 (Access Flag set; otherwise SMMU faults on
                          access-flag check)
      [47:21]  OA[47:21] (2 MiB-aligned output address)
    """
    if pa & (2 * MIB - 1):
        raise ValueError(
            f"Block PA {pa:#x} must be 2 MiB aligned")
    desc = 0
    desc |= 1                               # valid (type = 0)
    desc |= 0 << 2                          # AttrIndx = 0
    desc |= 0 << 5                          # NS = 0
    desc |= 0 << 6                          # AP = 0 (RW EL1)
    desc |= 3 << 8                          # SH = inner shareable
    desc |= 1 << 10                         # AF
    desc |= pa & ((1 << 48) - (1 << 21))    # OA[47:21]
    return desc


def build_blob(scratch_pa: int, valid_stream_ids):
    """
    Build the full SMMU bootstrap blob and return:
        (blob_bytes,
         strtab_base_value,
         strtab_base_cfg_value,
         cr0_value)

    Parameters
    ----------
    scratch_pa : int
        Physical address at which the blob will be placed in DRAM.
        Must be 4 KiB aligned. The SMMU will read its STE / CD / PT
        structures from PAs derived from this base.
    valid_stream_ids : iterable[int]
        StreamIDs that should be programmed for stage-1 translation.
        Every other STE in the table is left invalid (so a stray
        request from an unmapped StreamID will raise a translation
        fault and be visible in stats, rather than silently bypass).

    Notes
    -----
    The same Context Descriptor and the same identity page table are
    shared by every valid STE. This is fine because we are only using
    translation as a latency model; the *identity* of the translation
    is the same for every accelerator.
    """
    if scratch_pa & 0xFFF:
        raise ValueError(
            f"scratch_pa {scratch_pa:#x} must be 4 KiB aligned")

    blob = bytearray(BLOB_TOTAL_SIZE)

    # Resolve the absolute PAs of each region.
    ste_table_pa = scratch_pa + STE_TABLE_OFFSET
    cd_pa        = scratch_pa + CD_OFFSET
    l0_pa        = scratch_pa + L0_OFFSET
    l1_pa        = scratch_pa + L1_OFFSET
    l2_pa = [scratch_pa + L2_BASE_OFFSET + i * 0x1000 for i in range(4)]

    # --- L2 tables: 2 MiB block descriptors, identity mapping ---
    for table_idx in range(4):
        for entry_idx in range(PT_ENTRIES_PER_LEVEL):
            pa = (table_idx * GIB) + (entry_idx * 2 * MIB)
            desc = _block_descriptor_2mb(pa)
            off = L2_BASE_OFFSET + table_idx * 0x1000 + entry_idx * 8
            struct.pack_into("<Q", blob, off, desc)

    # --- L1 table: 4 valid table descriptors -> L2 #0..#3 ---
    for i in range(4):
        off = L1_OFFSET + i * 8
        struct.pack_into("<Q", blob, off, _table_descriptor(l2_pa[i]))
    # Entries 4..511 stay zero (invalid).

    # --- L0 table: only entry 0 valid, points at L1 ---
    struct.pack_into("<Q", blob, L0_OFFSET, _table_descriptor(l1_pa))

    # --- Context Descriptor (one, shared) ---
    cd_bytes = _context_descriptor(l0_pa)
    blob[CD_OFFSET:CD_OFFSET + CD_SIZE_BYTES] = cd_bytes

    # --- Stream table: programmed entries point at the shared CD ---
    ste_bytes = _ste_stage1_only(cd_pa)
    for sid in valid_stream_ids:
        if not (0 <= sid < STREAM_TABLE_NUM_ENTRIES):
            raise ValueError(
                f"StreamID {sid} out of range "
                f"[0, {STREAM_TABLE_NUM_ENTRIES})")
        off = STE_TABLE_OFFSET + sid * STE_SIZE_BYTES
        blob[off:off + STE_SIZE_BYTES] = ste_bytes
    # Unprogrammed entries remain zero -> dw0.valid = 0 -> the
    # SMMU model panics if a request arrives with such a StreamID.

    # SMMU register seed values.
    # STRTAB_BASE: top bits hold the stream-table base PA (already
    # 4 KiB aligned). The model masks with VMT_BASE_ADDR_MASK.
    strtab_base = ste_table_pa
    # STRTAB_BASE_CFG: linear format (FMT field = 0), size = log2(N).
    strtab_base_cfg = ST_CFG_FMT_LINEAR | STREAM_TABLE_LOG2_ENTRIES
    cr0 = CR0_SMMUEN

    return bytes(blob), strtab_base, strtab_base_cfg, cr0
