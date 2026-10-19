"""Extract Qt resources (rcc) embedded in a binary. usage: rcc_extract.py BIN OUTDIR"""
import re, struct, sys, zlib
from pathlib import Path

b = Path(sys.argv[1]).read_bytes()
out = Path(sys.argv[2])
log = lambda *a: print(*a, file=sys.stderr)


def qt_hash(raw):
    h = 0
    for i in range(0, len(raw), 2):
        h = ((h << 4) + ((raw[i] << 8) | raw[i + 1])) & 0xffffffff
        h ^= (h & 0xf0000000) >> 23
        h &= 0x0fffffff
    return h


_nc = {}
def name_at(off):
    if off in _nc:
        return _nc[off]
    r = None
    if 0 <= off and off + 6 <= len(b):
        n, h = struct.unpack_from(">HI", b, off)
        if 0 < n <= 255:
            raw = b[off + 6: off + 6 + 2 * n]
            if len(raw) == 2 * n and qt_hash(raw) == h:
                try:
                    r = (raw.decode("utf-16-be"), h)
                except UnicodeDecodeError:
                    pass
    _nc[off] = r
    return r


# name sections: runs of consecutive valid entries
valid = sorted(m.start() for m in re.finditer(rb"\x00[\x01-\xff]", b) if name_at(m.start()))
runs, cur = [], None
for off in valid:
    if cur and off == cur[-1] + 6 + 2 * len(name_at(cur[-1])[0]):
        cur.append(off)
    else:
        cur = [off]
        runs.append(cur)
runs = [r[0] for r in runs if len(r) >= 2]
log("name sections:", len(runs))

trees = [m.start() for m in re.finditer(rb"\x00\x00\x00\x00\x00\x02(?=....\x00\x00\x00\x01)", b, re.S)]
log("tree candidates:", len(trees))


def walk(tbase, esz, nbase):
    files = []

    def ent(i):
        return b[tbase + i * esz: tbase + (i + 1) * esz]

    def rec(i, path, depth):
        if depth > 20:
            raise ValueError
        noff, flags = struct.unpack_from(">IH", ent(i))
        if flags & 2:
            c, first = struct.unpack_from(">II", ent(i), 6)
            if c > 2000:
                raise ValueError
            prev = -1
            for k in range(c):
                cn = name_at(nbase + struct.unpack_from(">I", ent(first + k))[0])
                if not cn or cn[1] < prev:
                    raise ValueError
                prev = cn[1]
                rec(first + k, f"{path}/{cn[0]}", depth + 1)
        else:
            files.append((path, flags, struct.unpack_from(">I", ent(i), 10)[0]))
    rec(0, "", 0)
    return files


def find_data(files):
    """data base: every file's 4-byte size must fit, zlib entries must look like zlib."""
    files = sorted(files, key=lambda f: f[2])
    first = files[0]
    # file with offset 0 always exists; scan for its header pattern
    z = [f for f in files if f[1] & 1]
    for m in re.finditer(rb"(?=\x00\x00..\x00\x00..\x78[\x01\x5e\x9c\xda])" if first[1] & 1 else rb"(?=\x00[\x00-\x10]..)", b, re.S):
        d = m.start() - first[2]
        if d < 0:
            continue
        ok = True
        for p, fl, off in files:
            sz = int.from_bytes(b[d + off: d + off + 4], "big")
            if not 0 < sz < 30_000_000 or (fl & 1 and b[d + off + 8] != 0x78) or (fl & 4 and b[d + off + 4: d + off + 8] != b'\x28\xb5\x2f\xfd'):
                ok = False
                break
        if ok:
            return d
    return None


total, seen = 0, set()
for t in trees:
    for esz in (22, 14):
        for nbase in runs:
            try:
                files = walk(t, esz, nbase)
            except Exception:
                continue
            if not files:
                continue
            d = find_data(files)
            if d is None:
                log("no data for", hex(t), len(files), files[:2])
                continue
            for p, fl, off in files:
                sz = int.from_bytes(b[d + off: d + off + 4], "big")
                data = b[d + off + 4: d + off + 4 + sz]
                if fl & 1:
                    data = zlib.decompress(data[4:])
                elif fl & 4:
                    from compression import zstd
                    data = zstd.decompress(data)
                dst = out / p.lstrip("/")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                except OSError:
                    continue
                total += 1
            log(f"tree {hex(t)}: {len(files)} files, e.g. {files[0][0]}")
            break
        else:
            continue
        break
print("extracted", total)
