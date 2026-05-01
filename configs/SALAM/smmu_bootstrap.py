# Copyright (c) 2026 SALAM contributors
# SPDX-License-Identifier: BSD-3-Clause
"""
SMMUv3 bootstrap blob builder.

Materialises the in-memory data structures that an SMMUv3 in stage-1
translation mode needs in order to operate, *without* requiring any
software (Linux driver or bare-metal init code) to run on the
simulated CPU.

Two granule modes are supported:

  granule_kib = 2048   -> 2 MiB block descriptors at L2.
                          Walk depth on a cold miss = 3 (L0,L1,L2).
                          Tiny page-table footprint (~32 KiB total).
                          Good for "we just want translation enabled"
                          experiments. Each TLB entry covers 2 MiB,
                          so accelerator working sets up to a few MiB
                          fit in 1-2 entries -> no realistic TLB
                          pressure.
  granule_kib = 4      -> 4 KiB page descriptors at L3. Full 4-level
                          AArch64 walk (L0,L1,L2,L3) on a cold miss.
                          Page-table footprint ~8 MiB (a fully
                          materialised identity map of the low 4 GiB
                          at 4 KiB granule). This is the realistic
                          "edge SoC SMMU" mode -- TLB entries cover
                          4 KiB, so any accelerator that streams more
                          than a few hundred KiB will thrash a 16- or
                          32-entry TBU exactly like real silicon.

The blob layout (offsets relative to the per-cluster scratch base):

    granule = 2 MiB                        granule = 4 KiB
    +0x0000   STE table  (4 KiB)          +0x0000   STE table   (4 KiB)
    +0x1000   CD         (64 B)           +0x1000   CD          (64 B)
    +0x2000   L0 page table (4 KiB)       +0x2000   L0 page table (4 KiB)
    +0x3000   L1 page table (4 KiB)       +0x3000   L1 page table (4 KiB)
    +0x4000   L2 #0..#3   (16 KiB)        +0x4000   L2 #0..#3   (16 KiB)
                                          +0x8000   L3 #0..#2047 (8 MiB)

Stream table is linear, 64 entries (log2 = 6).
"""

import struct


# Layout constants.
STE_TABLE_OFFSET = 0x0000
CD_OFFSET        = 0x1000
L0_OFFSET        = 0x2000
L1_OFFSET        = 0x3000
L2_BASE_OFFSET   = 0x4000
L2_TABLES        = 4
L2_REGION_SIZE   = L2_TABLES * 0x1000  # 16 KiB

# L3 starts right after the L2 region; one L3 table per L2 entry that
# we want to refine to 4 KiB pages. 4 GiB / 2 MiB = 2048 L3 tables.
L3_BASE_OFFSET   = L2_BASE_OFFSET + L2_REGION_SIZE  # 0x8000
L3_TABLES        = L2_TABLES * 512   # 2048
L3_REGION_SIZE   = L3_TABLES * 0x1000   # 8 MiB

STREAM_TABLE_LOG2_ENTRIES = 6
STREAM_TABLE_NUM_ENTRIES  = 1 << STREAM_TABLE_LOG2_ENTRIES
STE_SIZE_BYTES            = 64
CD_SIZE_BYTES             = 64

PT_ENTRIES_PER_LEVEL = 512
GIB                  = 1 << 30
MIB                  = 1 << 20
KIB                  = 1 << 10

ST_CFG_FMT_LINEAR = 0x0
CR0_SMMUEN        = 0x1


def _ste_stage1_only(cd_pa: int) -> bytes:
    if cd_pa & 0x3F:
        raise ValueError(
            f"CD address {cd_pa:#x} must be 64-byte aligned")
    dw0 = 0
    dw0 |= 1 << 0                                       # valid
    dw0 |= (0x5 & 0x7) << 1                             # config=STAGE1_ONLY
    dw0 |= ((cd_pa >> 6) & ((1 << 46) - 1)) << 6        # s1ctxptr
    return struct.pack("<QQQQQQQQ", dw0, 0, 0, 0, 0, 0, 0, 0)


def _context_descriptor(l0_pa: int) -> bytes:
    if l0_pa & 0xFFF:
        raise ValueError(
            f"TTB0 {l0_pa:#x} must be 4 KiB aligned")
    dw0 = 0
    dw0 |= (16 & 0x3F)                                  # t0sz
    dw0 |= (0 & 0x3) << 6                               # tg0 = 4K
    dw0 |= (1 & 0x3) << 8                               # ir0
    dw0 |= (1 & 0x3) << 10                              # or0
    dw0 |= (3 & 0x3) << 12                              # sh0
    dw0 |= 1 << 30                                      # epd1
    dw0 |= 1 << 31                                      # valid
    dw0 |= (1 & 0x7) << 32                              # ips=40-bit
    dw0 |= 1 << 41                                      # aa64
    dw0 |= (1 & 0xFFFF) << 48                           # asid
    dw1 = 0
    dw1 |= ((l0_pa >> 4) & ((1 << 48) - 1)) << 4
    return struct.pack("<QQQQQQQQ", dw0, dw1, 0, 0, 0, 0, 0, 0)


def _table_descriptor(next_pa: int) -> int:
    if next_pa & 0xFFF:
        raise ValueError(
            f"Next-level table {next_pa:#x} must be 4 KiB aligned")
    return 0x3 | (next_pa & ((1 << 48) - (1 << 12)))


def _block_descriptor_2mb(pa: int) -> int:
    if pa & (2 * MIB - 1):
        raise ValueError(f"Block PA {pa:#x} must be 2 MiB aligned")
    desc = 0
    desc |= 1                                # valid (type=0 -> block at L2)
    desc |= 3 << 8                           # SH=inner-shareable
    desc |= 1 << 10                          # AF
    desc |= pa & ((1 << 48) - (1 << 21))     # OA[47:21]
    return desc


def _page_descriptor_4kb(pa: int) -> int:
    if pa & (4 * KIB - 1):
        raise ValueError(f"Page PA {pa:#x} must be 4 KiB aligned")
    desc = 0
    desc |= 0x3                              # valid + page (L3 needs type=1)
    desc |= 3 << 8                           # SH=inner-shareable
    desc |= 1 << 10                          # AF
    desc |= pa & ((1 << 48) - (1 << 12))     # OA[47:12]
    return desc


def build_blob(scratch_pa, valid_stream_ids, granule_kib=2048):
    """
    Build the SMMU bootstrap blob and return:
        (blob_bytes,
         strtab_base_value,
         strtab_base_cfg_value,
         cr0_value)

    Parameters
    ----------
    scratch_pa : int        4 KiB-aligned base PA where the blob lives.
    valid_stream_ids : iter StreamIDs to program. Others stay invalid.
    granule_kib : int       2048 (2 MiB blocks) or 4 (4 KiB pages).
    """
    if scratch_pa & 0xFFF:
        raise ValueError(
            f"scratch_pa {scratch_pa:#x} must be 4 KiB aligned")
    if granule_kib not in (4, 2048):
        raise ValueError("granule_kib must be 4 or 2048")

    if granule_kib == 2048:
        blob_size = L3_BASE_OFFSET    # 32 KiB
    else:
        blob_size = L3_BASE_OFFSET + L3_REGION_SIZE  # 32 KiB + 8 MiB

    blob = bytearray(blob_size)

    ste_table_pa = scratch_pa + STE_TABLE_OFFSET
    cd_pa        = scratch_pa + CD_OFFSET
    l0_pa        = scratch_pa + L0_OFFSET
    l1_pa        = scratch_pa + L1_OFFSET
    l2_pa = [scratch_pa + L2_BASE_OFFSET + i * 0x1000
             for i in range(L2_TABLES)]

    if granule_kib == 2048:
        # L2 holds 2 MiB block descriptors directly.
        for table_idx in range(L2_TABLES):
            for entry_idx in range(PT_ENTRIES_PER_LEVEL):
                pa = (table_idx * GIB) + (entry_idx * 2 * MIB)
                desc = _block_descriptor_2mb(pa)
                off = (L2_BASE_OFFSET + table_idx * 0x1000
                       + entry_idx * 8)
                struct.pack_into("<Q", blob, off, desc)
    else:
        # L2 holds table descriptors -> L3; each L3 covers 2 MiB at 4K.
        for table_idx in range(L2_TABLES):
            for entry_idx in range(PT_ENTRIES_PER_LEVEL):
                # L3 table sits at L3_BASE + (l2_idx * 512 + entry) * 4K.
                l3_idx = table_idx * PT_ENTRIES_PER_LEVEL + entry_idx
                l3_pa  = (scratch_pa + L3_BASE_OFFSET
                          + l3_idx * 0x1000)
                desc = _table_descriptor(l3_pa)
                off = (L2_BASE_OFFSET + table_idx * 0x1000
                       + entry_idx * 8)
                struct.pack_into("<Q", blob, off, desc)
                # Fill L3 with 512 page descriptors covering 2 MiB.
                base_pa = (table_idx * GIB) + (entry_idx * 2 * MIB)
                for page_idx in range(PT_ENTRIES_PER_LEVEL):
                    page_pa = base_pa + page_idx * 4 * KIB
                    page_desc = _page_descriptor_4kb(page_pa)
                    page_off = (L3_BASE_OFFSET + l3_idx * 0x1000
                                + page_idx * 8)
                    struct.pack_into("<Q", blob, page_off, page_desc)

    # L1 -> L2 (always 4 valid table descriptors)
    for i in range(L2_TABLES):
        off = L1_OFFSET + i * 8
        struct.pack_into("<Q", blob, off,
                         _table_descriptor(l2_pa[i]))

    # L0[0] -> L1
    struct.pack_into("<Q", blob, L0_OFFSET, _table_descriptor(l1_pa))

    # Context descriptor + stream-table entries.
    cd_bytes = _context_descriptor(l0_pa)
    blob[CD_OFFSET:CD_OFFSET + CD_SIZE_BYTES] = cd_bytes

    ste_bytes = _ste_stage1_only(cd_pa)
    for sid in valid_stream_ids:
        if not (0 <= sid < STREAM_TABLE_NUM_ENTRIES):
            raise ValueError(
                f"StreamID {sid} out of range "
                f"[0, {STREAM_TABLE_NUM_ENTRIES})")
        off = STE_TABLE_OFFSET + sid * STE_SIZE_BYTES
        blob[off:off + STE_SIZE_BYTES] = ste_bytes

    strtab_base     = ste_table_pa
    strtab_base_cfg = ST_CFG_FMT_LINEAR | STREAM_TABLE_LOG2_ENTRIES
    cr0             = CR0_SMMUEN
    return bytes(blob), strtab_base, strtab_base_cfg, cr0
