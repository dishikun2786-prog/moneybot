#!/usr/bin/env python3
"""mmap 共享内存快照读取端（Go 引擎 → Python 零拷贝读取）
与 engine-go/internal/ipc/shm.go 的二进制布局严格对应（小端）。
用法:
    r = ShmReader("/dev/shm/moneybot_book.shm")
    snap = r.read()   # {"seq":..., "ts_ns":..., "books": {SYM: {"bids": [(p,s),...], "asks": [...]}}}
    r.close()
"""
import mmap
import os
import struct

MAGIC = b"MBBOOK01"
MAX_SYMS = 16
MAX_LEVELS_PER_SIDE = 200
MAX_LEVELS = MAX_SYMS * 2 * MAX_LEVELS_PER_SIDE
HEADER_SIZE = 64
SYM_SLOT_SIZE = 48
LEVEL_SIZE = 16
TOTAL_SIZE = HEADER_SIZE + MAX_SYMS * SYM_SLOT_SIZE + MAX_LEVELS * LEVEL_SIZE


class ShmReader:
    def __init__(self, path="/dev/shm/moneybot_book.shm"):
        self.path = path
        self._mm = None
        self._open()

    def _open(self):
        fd = os.open(self.path, os.O_RDONLY)
        self._mm = mmap.mmap(fd, TOTAL_SIZE, access=mmap.ACCESS_READ)
        os.close(fd)

    def read(self):
        mm = self._mm
        if mm is None or mm[:8] != MAGIC:
            return None
        version = struct.unpack_from("<I", mm, 8)[0]
        seq = struct.unpack_from("<Q", mm, 16)[0]
        ts_ns = struct.unpack_from("<Q", mm, 24)[0]
        n_syms = struct.unpack_from("<I", mm, 32)[0]

        books = {}
        level_base = HEADER_SIZE + MAX_SYMS * SYM_SLOT_SIZE
        for i in range(min(n_syms, MAX_SYMS)):
            slot = HEADER_SIZE + i * SYM_SLOT_SIZE
            sym = mm[slot:slot + 16].rstrip(b"\x00").decode()
            bid_start, bid_count = struct.unpack_from("<II", mm, slot + 16)
            ask_start, ask_count = struct.unpack_from("<II", mm, slot + 24)

            bids = []
            for j in range(bid_count):
                off = level_base + (bid_start + j) * LEVEL_SIZE
                p, s = struct.unpack_from("<dd", mm, off)
                bids.append((p, s))
            asks = []
            for j in range(ask_count):
                off = level_base + (ask_start + j) * LEVEL_SIZE
                p, s = struct.unpack_from("<dd", mm, off)
                asks.append((p, s))
            books[sym] = {"bids": bids, "asks": asks}
        return {"version": version, "seq": seq, "ts_ns": ts_ns, "books": books}

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/dev/shm/moneybot_book.shm"
    with ShmReader(path) as r:
        snap = r.read()
        if not snap:
            print("快照未就绪（Go 引擎尚未写入）")
            sys.exit(1)
        print(f"seq={snap['seq']} ts_ns={snap['ts_ns']} 标的数={len(snap['books'])}")
        for sym, b in list(snap["books"].items())[:8]:
            bb = b["bids"][0] if b["bids"] else None
            ba = b["asks"][0] if b["asks"] else None
            print(f"  {sym}: bid1={bb} ask1={ba} (档数 {len(b['bids'])}/{len(b['asks'])})")