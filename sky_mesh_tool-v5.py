#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sky_mesh_tool.py — Sky 0.32.2 mesh 全能转换器 v5
==================================================
输入: FBX / OBJ / STL / PLY / glTF / DAE / GLB / mesh
输出: FBX / OBJ / STL / PLY / glTF / DAE / GLB / mesh
版本: 0x19 .. 0x20 (25 .. 32)
界面: 完整 TUI（alt-screen / 鼠标 / 方向键 / 分页 / CJK 对齐）
依赖: 标准库（lz4 可选加速，缺失时手写完整 LZ4）
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from xml.dom import minidom


# ==================== 常量 ====================
VERSIONS       = tuple(range(25, 33))
WRITE_VERSIONS = (25, 26, 27, 28, 29, 30, 31, 32)
MODERN_VER     = (31, 32)
FLAG_ORDER = (
    "StripGeo", "StripAnim", "UncompAnim", "Add", "CompOcc", "CompEdges",
    "CompAdj", "ZipPos", "ZipUvs", "StripUv13", "StripNorm", "ForceIdx32",
    "CopyFrameDelay",
)
FLAG_DESCRIPTIONS = {
    "StripGeo":       "剥离几何（由结构决定）",
    "StripAnim":      "剥离动画片段，仍可保留骨骼蒙皮（由结构决定）",
    "UncompAnim":     "动画轨道使用float，取消LZ4与16位量化",
    "Add":            "叠加动画（只支持原始数据保留）",
    "CompOcc":        "遮挡辅助数据（仅几何和蒙皮未变时保留）",
    "CompEdges":      "边辅助数据（只支持原始数据保留）",
    "CompAdj":        "邻接辅助数据（只支持原始数据保留）",
    "ZipPos":         "位置量化为10/10/10位，另存1字节顶点属性",
    "ZipUvs":         "UV0/UV2的4个分量分别量化为8位",
    "StripUv13":      "剥离UV1/UV3",
    "StripNorm":      "剥离法线",
    "ForceIdx32":     "强制32位索引（旧版本必须使用）",
    "CopyFrameDelay": "动画帧复制延迟（只支持原始数据保留）",
}
REFERENCE_ONLY = {"Add", "CompEdges", "CompAdj", "CopyFrameDelay"}
EXT            = "SKY_mesh_source"
EXTRA_GEOMETRY = "sky_extra_geometry"
MAX_DATA       = 1024 * 1024 * 1024
DAE_NS         = "http://www.collada.org/2005/11/COLLADASchema"
SUPPORTED_IN   = (".fbx", ".obj", ".stl", ".ply", ".gltf", ".dae", ".glb", ".mesh")
MESH_INPUT_EXTS = (".fbx", ".obj", ".stl", ".ply", ".gltf", ".dae")
MESH_OUTPUT_EXTS = ("fbx", "obj", "stl", "ply", "gltf", "dae", "both")


# ==================== ANSI 颜色 ====================
class _Ansi:
    enabled = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    RESET = "\x1b[0m" if enabled else ""
    BOLD  = "\x1b[1m" if enabled else ""
    DIM   = "\x1b[2m" if enabled else ""
    RED   = "\x1b[31m" if enabled else ""
    GREEN = "\x1b[32m" if enabled else ""
    WHITE = "\x1b[37m" if enabled else ""


# ==================== 基础工具 ====================
class FormatError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise FormatError(message)


def fixed_name(s, n=64):
    b = s.encode("utf-8")
    require(len(b) < n, f"名称超过{n-1}字节: {s}")
    return b + b"\0" * (n - len(b))


def name_of(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def digest(b):
    return hashlib.sha256(b).hexdigest()


def split_name(path):
    s = Path(path).stem
    flags = []
    while True:
        match = next((f for f in FLAG_ORDER if s.endswith("_" + f)), None)
        if match:
            s = s[: -len(match) - 1]
            flags.append(match)
            continue
        if s.endswith(".") and flags:
            s = s[:-1]
        break
    return s, set(flags)


def suffix(flags):
    return "".join("_" + f for f in FLAG_ORDER if f in flags)


class Reader:
    def __init__(self, data, label="mesh"):
        self.data = data
        self.p = 0
        self.label = label

    def take(self, n):
        require(
            0 <= n <= len(self.data) - self.p,
            f"{self.label}: 0x{self.p:x}需要{n}字节，剩余{len(self.data)-self.p}",
        )
        b = self.data[self.p : self.p + n]
        self.p += n
        return b

    def unpack(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def u32(self):
        return self.unpack("I")[0]

    def byte(self):
        return self.take(1)[0]


# ==================== LZ4 ====================
def lz4_decode(src, expected):
    require(0 <= expected <= MAX_DATA, "LZ4解压长度超限")
    try:
        import lz4.block
        return lz4.block.decompress(src, uncompressed_size=expected)
    except ImportError:
        pass
    out = bytearray()
    p = 0

    def length(v):
        nonlocal p
        if v == 15:
            while True:
                require(p < len(src), "截断的LZ4长度")
                x = src[p]
                p += 1
                v += x
                if x != 255:
                    break
        return v

    while p < len(src):
        token = src[p]
        p += 1
        n = length(token >> 4)
        require(p + n <= len(src) and len(out) + n <= expected, "无效LZ4 literal")
        out.extend(src[p : p + n])
        p += n
        if p == len(src):
            break
        require(p + 2 <= len(src), "截断的LZ4偏移")
        off = src[p] | src[p + 1] << 8
        p += 2
        require(0 < off <= len(out), "无效LZ4回溯偏移")
        n = length(token & 15) + 4
        require(len(out) + n <= expected, "LZ4输出溢出")
        block = bytes(out[-off:])
        out.extend((block * ((n + off - 1) // off))[:n])
    require(len(out) == expected, "LZ4解压长度不匹配")
    return bytes(out)


def lz4_encode(data):
    try:
        import lz4.block
        return lz4.block.compress(data, store_size=False)
    except ImportError:
        pass
    n = len(data)
    if n == 0:
        return b""
    out = bytearray()
    pos = 0
    MFLIMIT = 12
    tbl = [0] * 4096
    lit_start = 0

    def _ext(rem):
        e = b""
        while rem >= 255:
            e += b"\xff"
            rem -= 255
        return e + bytes([rem])

    while pos < n - MFLIMIT:
        key = (data[pos] | data[pos + 1] << 8 | data[pos + 2] << 16
               | data[pos + 3] << 24) & 4095
        cand = tbl[key]
        tbl[key] = pos + 1
        if cand:
            cand -= 1
            if (cand + MFLIMIT <= pos and pos - cand <= 65535
                    and data[cand:cand + 4] == data[pos:pos + 4]):
                mlen = 4
                while pos + mlen < n and data[cand + mlen] == data[pos + mlen]:
                    mlen += 1
                lit = pos - lit_start
                tok = ((15 if lit >= 15 else lit) << 4
                       | (15 if mlen - 4 >= 15 else mlen - 4))
                out.append(tok)
                if lit >= 15:
                    out.extend(_ext(lit - 15))
                out.extend(data[lit_start:pos])
                out += struct.pack("<H", pos - cand)
                if mlen - 4 >= 15:
                    out.extend(_ext(mlen - 4 - 15))
                pos += mlen
                lit_start = pos
                for k in range(pos - 4, pos):
                    if k + 4 <= n:
                        tk = (data[k] | data[k + 1] << 8 | data[k + 2] << 16
                              | data[k + 3] << 24) & 4095
                        tbl[tk] = k + 1
                continue
        pos += 1
    lit = n - lit_start
    out.append((15 if lit >= 15 else lit) << 4)
    if lit >= 15:
        out.extend(_ext(lit - 15))
    out.extend(data[lit_start:n])
    return bytes(out)


# ==================== 数学 ====================
def identity():
    return [1.0 if i % 5 == 0 else 0.0 for i in range(16)]


def transpose(m):
    return [m[j * 4 + i] for i in range(4) for j in range(4)]


def mm(a, b):
    return [sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
            for i in range(4) for j in range(4)]


def inverse(m):
    a = [list(m[i * 4:i * 4 + 4]) + [float(i == j) for j in range(4)]
         for i in range(4)]
    for i in range(4):
        k = max(range(i, 4), key=lambda k: abs(a[k][i]))
        require(abs(a[k][i]) > 1e-12, "奇异矩阵不能求逆")
        a[i], a[k] = a[k], a[i]
        q = a[i][i]
        a[i] = [x / q for x in a[i]]
        for j in range(4):
            if j != i:
                q = a[j][i]
                a[j] = [x - q * y for x, y in zip(a[j], a[i])]
    return [x for row in a for x in row[4:]]


def normal(v):
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v) if n > 1e-15 else (0.0, 1.0, 0.0)


def transform(m, v, w=1.0):
    return tuple(sum(m[i * 4 + j] * v[j] for j in range(3)) + m[i * 4 + 3] * w
                 for i in range(3))


# ==================== 数据结构 ====================
@dataclass
class Lod:
    distance: float
    bounds: tuple
    quant_lo: tuple
    quant_hi: tuple
    vertices: int
    indices_count: int
    idx32: int
    counts: tuple
    has_normals: int
    has_secondary: int
    third: int
    zipped: tuple
    groups: tuple
    streams: dict = field(default_factory=dict)
    raw: bytes = b""


@dataclass
class Mesh:
    version: int
    name: str
    animated: int
    extra: int
    descriptors: bytes
    lods: list
    skeleton: bytes
    extra_data: bytes
    compressed: int
    raw: bytes
    filename_flags: set = field(default_factory=set)
    warnings: list = field(default_factory=list)


@dataclass
class Clip:
    start: int
    end: int
    flags: int
    bounds: tuple
    quant_min: tuple
    quant_extent: tuple
    mask: bytes
    tracks: dict
    name: str = ""


@dataclass
class Animation:
    fps: int
    frame_count: int
    bind: list
    clips: list
    compression: int
    raw_payload: bytes = b""


# ==================== Mesh 解析 ====================
def read_lod(r, version, animated):
    start = r.p
    distance = r.unpack("f")[0]
    require(not math.isnan(distance), "LOD距离是NaN")
    bounds = r.unpack("12f" if version >= 28 else "6f")
    if version < 28:
        bounds = bounds + bounds
    lo = r.unpack("8f") if version > 28 else (0.0,) * 8
    hi = r.unpack("8f") if version > 28 else (1.0,) * 8
    n, ni = r.unpack("2I")
    require(n <= 10000000 and ni <= 60000000 and ni % 3 == 0, "无效顶点/三角形数量")
    idx32 = r.u32() if version > 29 else 1
    require(idx32 in (0, 1), "索引格式不是0/1")
    counts = r.unpack("5I")
    require(max(counts, default=0) <= 60000000, "辅助数据数量超限")
    require(max(counts[:2]) <= n, "顶点映射组数超过顶点数")
    if version >= 29:
        hn, hs, t = r.unpack("3B")
        zp, zu, z13 = r.unpack("3I")
        groups = r.unpack("4I")
        require(hn in (0, 1) and hs in (0, 1) and t in (0, 1), "无效LOD布尔字段")
        require(all(x in (0, n * 4) for x in (zp, zu, z13)),
                "压缩属性长度必须为0或4×顶点数")
    else:
        hn, hs, t = 1, 1, 0
        groups = r.unpack("4I")
        zp = zu = z13 = 0
    lod = Lod(distance, bounds, lo, hi, n, ni, idx32, counts,
              hn, hs, t, (zp, zu, z13), groups)
    s = lod.streams
    unit = 4 if idx32 else 2

    def take(key, sz):
        s[key] = r.take(sz)

    if not zp:
        take("positions", n * 16)
    if hn:
        take("normals", n * 4)
    if not zu:
        take("uvs", n * 16)
    if animated:
        take("weights", n * 8)
    take("indices", ni * unit)
    if hs:
        take("secondary_indices", ni * unit)
    if counts[0]:
        take("position_map", n * unit)
    if counts[1]:
        take("normal_map", n * unit)
    if counts[2]:
        take("strips", counts[2] * unit)
    if counts[3]:
        take("metrics", counts[3] * 4)
    if counts[4]:
        take("pairs", counts[4] * 2 * unit)
    if version > 28:
        take("areas", ni // 3 * 4)
    if zp:
        take("zip_positions", n * 4)
        take("alpha", n)
    if zu:
        take("zip_uvs", n * 4)
    if z13:
        take("zip_uv13", n * 4)
    lod.raw = r.data[start:r.p]
    indices = struct.unpack("<" + ("I" if idx32 else "H") * ni, s["indices"])
    require(not indices or max(indices) < n, "三角形索引超出顶点范围")
    for key, count in (("position_map", counts[0]), ("normal_map", counts[1])):
        if count:
            require(all(x[0] < count for x in struct.iter_unpack(
                "<I" if idx32 else "<H", s[key])),
                f"{key}索引超出映射组范围")
    return lod


def decode_extra_geometry(data):
    r = Reader(data, "附加几何")
    n, ni = r.unpack("2I")
    require(n <= 10000000 and ni <= 60000000 and ni % 3 == 0, "附加几何数量无效")
    positions = list(struct.iter_unpack("<3f", r.take(n * 12)))
    indices = [x[0] for x in struct.iter_unpack("<I", r.take(ni * 4))]
    require(r.p == len(data), "附加几何尾部有未解释字节")
    require(all(math.isfinite(x) for p in positions for x in p),
            "附加几何位置含非有限数")
    require(all(i < n for i in indices), "附加几何索引越界")
    return {"POSITION": positions, "indices": indices}


def encode_extra_geometry(geom):
    positions, indices = geom["POSITION"], geom["indices"]
    require(len(positions) <= 10000000 and len(indices) <= 60000000,
            "附加几何数量超限")
    require(all(len(p) == 3 and
                all(math.isfinite(x) and abs(x) <= 3.4028234663852886e38 for x in p)
                for p in positions),
            "附加几何位置必须是有限float32 XYZ")
    require(len(indices) % 3 == 0, "附加几何三角索引数量不是3的倍数")
    require(all(isinstance(i, int) and 0 <= i < len(positions) for i in indices),
            "附加几何索引越界")
    return (struct.pack("<2I", len(positions), len(indices))
            + b"".join(struct.pack("<3f", *p) for p in positions)
            + pack_indices(indices, 1))


def read_mesh(data, filename="model.mesh"):
    r = Reader(data)
    v = r.u32()
    require(v in VERSIONS, f"不支持版本0x{v:x}；该so正常接受0x19..0x20")
    name = name_of(r.take(64))
    nl = r.u32()
    require(nl <= 64, "LOD数量超限")
    animated, extra = r.unpack("2B")
    require(animated in (0, 1) and extra in (0, 1), "无效mesh结构标记")
    nm = r.u32() if v > 31 else 0
    require(nm <= 100000, "头部附加描述数量超限")
    descriptors = r.take(nm * 112)
    comp = r.u32() if v >= 27 else 0
    require(comp in (0, 1), "压缩类型不是0/1")
    if comp:
        cs, us = r.unpack("2I")
        payload = lz4_decode(r.take(cs), us)
        pr = Reader(payload, "LOD payload")
    else:
        pr = r
    lods = [read_lod(pr, v, animated) for _ in range(nl)]
    ex = b""
    if extra:
        start = pr.p
        np, ni = pr.unpack("2I")
        require(np <= 10000000 and ni <= 60000000 and ni % 3 == 0,
                "附加几何数量无效")
        pr.take(np * 12 + ni * 4)
        ex = pr.data[start:pr.p]
        decode_extra_geometry(ex)
    if comp:
        require(pr.p == len(payload), f"LOD payload尾部有{len(payload)-pr.p}未解释字节")
    skel = r.take(len(data) - r.p)
    if animated:
        read_skeleton(skel)
    else:
        require(not skel, "静态mesh尾部有未解释数据")
    m = Mesh(v, name, animated, extra, descriptors, lods, skel, ex,
             comp, data, split_name(filename)[1])
    if v < 31:
        m.warnings.append("该版本会触发游戏Old mesh version提示")
    actual = detect_flags(m)
    for f in ("ZipPos", "ZipUvs", "StripNorm", "ForceIdx32", "CompOcc"):
        if (f in m.filename_flags) != (f in actual):
            m.warnings.append(f"文件名{f}与数据特征不一致；采用数据特征")
    return m


def read_skeleton(data):
    r = Reader(data, "skeleton")
    v = r.u32()
    require(7 <= v <= 11, f"骨骼版本{v}不在so正常范围7..11")
    name = name_of(r.take(64))
    n, na, nf = r.unpack("3I")
    comp = r.byte()
    pool = r.u32() if v >= 10 else n * 64
    require(n <= 255 and na <= 100000 and nf <= 10000000, "骨骼/动画数量超限")
    bones = []
    for i in range(n):
        bn = name_of(r.take(64))
        mat = r.unpack("16f")
        parent = r.u32() - 1
        require(-1 <= parent < n and parent != i, "骨骼父索引无效")
        if na:
            require(all(math.isfinite(x) for x in mat), "骨骼矩阵不是有限数")
        bones.append({"name": bn, "matrix": transpose(mat), "parent": parent})
    for i in range(n):
        seen = set()
        j = i
        while j >= 0:
            require(j not in seen, "骨骼父子关系成环")
            seen.add(j)
            j = bones[j]["parent"]
    return {"version": v, "name": name, "bones": bones,
            "frame_count": na, "fps": nf, "compression": comp,
            "pool": pool, "tail": data[r.p:]}


def detect_flags(m):
    out = set()
    ls = m.lods
    if not ls:
        out.add("StripGeo")
    if m.animated:
        s = read_skeleton(m.skeleton)
        if not s["frame_count"]:
            out.add("StripAnim")
        if s["frame_count"] and not s["compression"]:
            out.add("UncompAnim")
    if ls:
        if all(l.zipped[0] for l in ls):
            out.add("ZipPos")
        if all(l.zipped[1] for l in ls):
            out.add("ZipUvs")
        if all(not l.has_normals for l in ls):
            out.add("StripNorm")
        if all(not l.zipped[2] for l in ls) and any(l.zipped[1] for l in ls):
            out.add("StripUv13")
        if all(l.idx32 for l in ls):
            out.add("ForceIdx32")
        if any(l.counts[2] for l in ls):
            out.add("CompOcc")
    out |= m.filename_flags & {"Add", "CompEdges", "CompAdj", "CopyFrameDelay"}
    return out


def decode_lod(l):
    s = l.streams
    n = l.vertices
    out = {"POSITION": [], "TEXCOORD_0": [], "COLOR_0": []}
    if l.zipped[0]:
        lo = l.bounds[6:9]
        hi = l.bounds[9:12]
        for i, (pk,) in enumerate(struct.iter_unpack("<I", s["zip_positions"])):
            out["POSITION"].append(tuple(
                lo[j] + ((pk >> (20 - 10 * j)) & 1023) / 1023 * (hi[j] - lo[j])
                for j in range(3)))
            out["COLOR_0"].append((1.0, 1.0, 1.0, s["alpha"][i] / 255))
    else:
        for x, y, z, c in struct.iter_unpack("<3fI", s["positions"]):
            out["POSITION"].append((x, y, z))
            out["COLOR_0"].append(tuple((c >> (8 * j) & 255) / 255 for j in range(4)))
    if l.has_normals:
        out["NORMAL"] = [normal(tuple(x / 127 for x in row[:3]))
                         for row in struct.iter_unpack("4b", s["normals"])]
    if l.zipped[1]:
        for channel, dims in ((0, (0, 1)), (2, (4, 5))):
            out["TEXCOORD_" + str(channel)] = [
                tuple(l.quant_lo[d] + b[j + channel] / 255
                      * (l.quant_hi[d] - l.quant_lo[d]) for j, d in enumerate(dims))
                for b in struct.iter_unpack("4B", s["zip_uvs"])]
    else:
        uvs = [struct.unpack("<8e", s["uvs"][i * 16:i * 16 + 16]) for i in range(n)]
        for j in range(4):
            out["TEXCOORD_" + str(j)] = [tuple(x[j * 2:j * 2 + 2]) for x in uvs]
    if l.zipped[2]:
        for channel, dims in ((1, (2, 3)), (3, (6, 7))):
            out["TEXCOORD_" + str(channel)] = [
                tuple(l.quant_lo[d] + b[j + (channel - 1)] / 255
                      * (l.quant_hi[d] - l.quant_lo[d]) for j, d in enumerate(dims))
                for b in struct.iter_unpack("4B", s["zip_uv13"])]
    if "weights" in s:
        out["JOINTS_0"] = []
        out["WEIGHTS_0"] = []
        for row in struct.iter_unpack("8B", s["weights"]):
            merged = {}
            for joint, weight in zip(row[:4], row[4:]):
                if weight:
                    merged[joint - 1] = merged.get(joint - 1, 0) + weight
            total = sum(merged.values())
            require(total > 0, "蒙皮顶点无有效权重")
            pairs = list(merged.items())
            out["JOINTS_0"].append(tuple(j for j, w in pairs) + (0,) * (4 - len(pairs)))
            out["WEIGHTS_0"].append(tuple(w / total for j, w in pairs)
                                    + (0.0,) * (4 - len(pairs)))
    out["indices"] = [x[0] for x in struct.iter_unpack(
        "<I" if l.idx32 else "<H", s["indices"])]
    for k, rows in out.items():
        if k != "indices":
            require(all(math.isfinite(x) for row in rows for x in row),
                    f"{k}包含非有限数")
    return out


def smooth_normals(pos, indices):
    out = [[0.0, 0.0, 0.0] for _ in pos]
    for i in range(0, len(indices), 3):
        a, b, c = indices[i:i + 3]
        u = [pos[b][j] - pos[a][j] for j in range(3)]
        v = [pos[c][j] - pos[a][j] for j in range(3)]
        cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                 u[0] * v[1] - u[1] * v[0])
        for k in (a, b, c):
            for j in range(3):
                out[k][j] += cross[j]
    return [normal(x) for x in out]


# ==================== Mesh 编码 ====================
def bounds_of(pos):
    return (tuple(min(p[i] for p in pos) for i in range(3))
            + tuple(max(p[i] for p in pos) for i in range(3)))


def pack_indices(ids, idx32):
    return struct.pack("<" + ("I" if idx32 else "H") * len(ids), *ids)


def area_stream(pos, ids):
    out = []
    acc = 0.0
    for i in range(0, len(ids), 3):
        a, b, c = (pos[j] for j in ids[i:i + 3])
        u = [a[j] - c[j] for j in range(3)]
        v = [b[j] - c[j] for j in range(3)]
        cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                 u[0] * v[1] - u[1] * v[0])
        acc += math.sqrt(sum(x * x for x in cross)) * 0.5
        out.append(acc)
    return struct.pack("<" + "f" * len(out), *out)


def quantize(x, lo, hi, maximum):
    return (max(0, min(maximum, int((x - lo) / (hi - lo) * maximum + 0.5)))
            if hi > lo else 0)


def skin_equivalent(a, b):
    if ("JOINTS_0" in a) != ("JOINTS_0" in b):
        return False
    if "JOINTS_0" not in a:
        return True
    for aj, aw, bj, bw in zip(a["JOINTS_0"], a["WEIGHTS_0"],
                              b["JOINTS_0"], b["WEIGHTS_0"]):
        aa, bb = {}, {}
        for out, js, ws in ((aa, aj, aw), (bb, bj, bw)):
            total = sum(ws)
            if total <= 0:
                return False
            for j, w in zip(js, ws):
                if w > 0:
                    out[j] = out.get(j, 0.0) + w / total
        if aa.keys() != bb.keys() or any(abs(aa[j] - bb[j]) > 1e-6 for j in aa):
            return False
    return True


def vertex_maps(pos, norms, source_groups=None, source_positions=None):
    pmap, nmap, pm, nm = {}, {}, [], []
    for i, (p, norm) in enumerate(zip(pos, norms)):
        pk = struct.unpack("<3f", struct.pack("<3f", *p))
        normal_position = source_positions[i] if source_positions is not None else pk
        nk = ((normal_position, source_groups[i]) if source_groups is not None
              else (normal_position,
                    struct.unpack("<3f", struct.pack("<3f", *normal(norm)))))
        if pk not in pmap:
            pmap[pk] = len(pmap)
        if nk not in nmap:
            nmap[nk] = len(nmap)
        pm.append(pmap[pk])
        nm.append(nmap[nk])
    return pm, nm, len(pmap), len(nmap)


def build_lod(geom, version, flags, bones, reference=None):
    pos = geom["POSITION"]
    ids = geom["indices"]
    n = len(pos)
    ni = len(ids)
    require(ni > 0, "mesh必须有三角形")
    zp = "ZipPos" in flags
    zu = "ZipUvs" in flags
    hn = "StripNorm" not in flags
    require(version >= 29 or not (zp or zu or not hn),
            "0x19..0x1C不支持ZipPos/ZipUvs/StripNorm")
    require(not zp or all(all(abs(x - 1.0) < 1e-6 for x in row[:3])
                          for row in geom.get("COLOR_0", [])),
            "ZipPos仅保留顶点颜色alpha；RGB有色顶点请取消ZipPos")
    idx32 = int(version <= 29 or "ForceIdx32" in flags
                or n > 65536 or max(ids) > 65535)
    bo = bounds_of(pos)
    uvs = [geom.get("TEXCOORD_" + str(i), [(0.0, 0.0)] * n) for i in range(4)]
    lo = tuple(min(row[j] for row in uvs[i]) for i in range(4) for j in range(2))
    hi = tuple(max(row[j] for row in uvs[i]) for i in range(4) for j in range(2))
    z13 = zu and "StripUv13" not in flags
    s = {}
    groups = (0, 0, 0, 0)
    counts = (0, 0, 0, 0, 0)
    third = 0
    hs = int(version < 29)
    same = False
    same_positions = same_normals = same_skin = False
    norms = geom.get("NORMAL") or smooth_normals(pos, ids)
    refgeom = None
    if reference:
        refgeom = decode_lod(reference)
        same = reference.vertices == n and refgeom["indices"] == ids
        if same:
            same_positions = all(
                struct.pack("<3f", *p) == struct.pack("<3f", *q)
                for p, q in zip(pos, refgeom["POSITION"]))
            same_normals = (
                ("NORMAL" not in geom and not reference.has_normals)
                or ("NORMAL" in geom and reference.has_normals and all(
                    max(abs(x - y) for x, y in zip(normal(p), q)) < 1e-6
                    for p, q in zip(geom["NORMAL"], refgeom["NORMAL"]))))
            same_skin = skin_equivalent(geom, refgeom)
        if same:
            s = {k: v for k, v in reference.streams.items()
                 if k in ("secondary_indices", "position_map", "normal_map",
                          "strips", "metrics", "pairs")}
            groups = reference.groups
            counts = reference.counts
            third = reference.third
            hs = int(version < 29 or reference.has_secondary)
            if reference.idx32 != idx32:
                for key in ("secondary_indices", "position_map", "normal_map",
                            "strips", "pairs"):
                    if key in s:
                        vals = [x[0] for x in struct.iter_unpack(
                            "<I" if reference.idx32 else "<H", s[key])]
                        restart = 0xFFFFFFFF if reference.idx32 else 0xFFFF
                        if key == "strips":
                            vals = [(0xFFFFFFFF if idx32 else 0xFFFF)
                                    if x == restart else x for x in vals]
                        s[key] = pack_indices(vals, idx32)
        elif bones:
            groups = (reference.groups if sum(reference.groups) == n
                      else (n, 0, 0, 0))
    if "CompOcc" in flags:
        require(same and same_positions and same_skin and reference.counts[2] > 0,
                "CompOcc尚不能重新烘焙；仅能保留位置、索引和蒙皮未变的来源数据。"
                "修改几何后请取消CompOcc")
    else:
        counts = counts[:2] + (0, 0, counts[4])
        s.pop("strips", None)
        s.pop("metrics", None)
    if any(f in flags for f in ("CompEdges", "CompAdj")):
        require(same and same_positions and same_normals and same_skin,
                "边/邻接数据尚不能重新烘焙；需要位置、法线、索引和蒙皮未变的来源数据")
    keep_normal_groups = same and "NORMAL" not in geom and reference.counts[1] > 0
    if not same or not same_positions or not same_normals:
        source_groups = ([x[0] for x in struct.iter_unpack(
            "<I" if reference.idx32 else "<H", reference.streams["normal_map"])]
            if keep_normal_groups else None)
        keep_positions = same and same_positions and reference.counts[0] > 0
        source_positions = ([x[0] for x in struct.iter_unpack(
            "<I" if reference.idx32 else "<H", reference.streams["position_map"])]
            if keep_positions else None)
        pm, nm, np, nn = vertex_maps(pos, norms, source_groups, source_positions)
        counts = (counts[0] if keep_positions else np, nn,
                  counts[2], counts[3], counts[4])
        if not keep_positions:
            s["position_map"] = pack_indices(pm, idx32)
        s["normal_map"] = pack_indices(nm, idx32)
    if zp:
        s["zip_positions"] = b"".join(struct.pack(
            "<I", sum(quantize(p[j], bo[j], bo[j + 3], 1023) << (20 - 10 * j)
                      for j in range(3))) for p in pos)
        s["alpha"] = bytes(quantize(row[3], 0, 1, 255)
                           for row in geom.get("COLOR_0", [(1.0, 1.0, 1.0, 1.0)] * n))
    else:
        cols = geom.get("COLOR_0", [(1.0, 1.0, 1.0, 1.0)] * n)
        s["positions"] = b"".join(struct.pack(
            "<3fI", *p, sum(quantize(col[j], 0, 1, 255) << (j * 8)
                            for j in range(4))) for p, col in zip(pos, cols))
    if hn:
        s["normals"] = b"".join(struct.pack(
            "4b", *(max(-127, min(127, int(round(x * 127))))
                    for x in normal(norm)), 0) for norm in norms)
    if zu:
        s["zip_uvs"] = bytes(
            quantize(uvs[ch][i][j], lo[ch * 2 + j], hi[ch * 2 + j], 255)
            for i in range(n) for ch in (0, 2) for j in range(2))
        if z13:
            s["zip_uv13"] = bytes(
                quantize(uvs[ch][i][j], lo[ch * 2 + j], hi[ch * 2 + j], 255)
                for i in range(n) for ch in (1, 3) for j in range(2))
    else:
        if "StripUv13" in flags:
            uvs[1] = [(0.0, 0.0)] * n
            uvs[3] = [(0.0, 0.0)] * n
        require(all(abs(x) <= 65504 for rows in uvs for row in rows for x in row),
                "UV超过half浮点范围65504")
        s["uvs"] = b"".join(struct.pack(
            "<8e", *(x for ch in range(4) for x in uvs[ch][i]))
            for i in range(n))
    if bones:
        rows = []
        groupcounts = [0] * 4
        for joints, weights in zip(geom["JOINTS_0"], geom["WEIGHTS_0"]):
            require(all(isinstance(j, int) and -1 <= j < len(bones) for j in joints),
                    "JOINTS_0超出骨骼范围")
            require(all(w >= 0 for w in weights) and sum(weights) > 0,
                    "无效蒙皮权重")
            total = sum(weights)
            ws = [w / total * 255 for w in weights]
            wi = [int(x) for x in ws]
            for j in sorted(range(4), key=lambda j: ws[j] - wi[j],
                            reverse=True)[:255 - sum(wi)]:
                wi[j] += 1
            js = [j + 1 if w else 0 for j, w in zip(joints, wi)]
            rows.append(bytes(js + wi))
            groupcounts[max(1, sum(bool(w) for w in wi)) - 1] += 1
        s["weights"] = b"".join(rows)
        order = sorted(range(n), key=lambda i: sum(bool(x) for x in rows[i][4:]))
        if order != list(range(n)):
            require(not any(f in flags for f in ("CompOcc", "CompEdges", "CompAdj")),
                    "参考顶点的权重组发生变化；无法保留拓扑辅助数据")
            remap = {old: new for new, old in enumerate(order)}
            gg = {k: ([v[i] for i in order]
                      if isinstance(v, list) and k not in ("indices", "distance")
                      else v) for k, v in geom.items()}
            gg["indices"] = [remap[i] for i in ids]
            gg["_sorted"] = True
            return build_lod(gg, version, flags, bones, None)
        groups = tuple(groupcounts)
    s["indices"] = pack_indices(ids, idx32)
    if hs and "secondary_indices" not in s:
        s["secondary_indices"] = s["indices"]
    s["areas"] = area_stream(pos, ids)
    return Lod(geom.get("distance", math.inf), bo + bo, lo, hi, n, ni, idx32,
               counts, int(hn), hs, third,
               (n * 4 if zp else 0, n * 4 if zu else 0, n * 4 if z13 else 0),
               groups, s)


def encode_lod(l, version, animated):
    out = bytearray(struct.pack("<f", l.distance))
    out.extend(struct.pack("<" + ("12f" if version >= 28 else "6f"),
                           *(l.bounds if version >= 28 else l.bounds[:6])))
    if version > 28:
        out.extend(struct.pack("<16f", *l.quant_lo, *l.quant_hi))
    out.extend(struct.pack("<2I", l.vertices, l.indices_count))
    if version > 29:
        out.extend(struct.pack("<I", l.idx32))
    out.extend(struct.pack("<5I", *l.counts))
    if version >= 29:
        out.extend(struct.pack("<3B7I", l.has_normals, l.has_secondary, l.third,
                               *l.zipped, *l.groups))
    else:
        out.extend(struct.pack("<4I", *l.groups))
    keys = []
    if not l.zipped[0]:
        keys.append("positions")
    if l.has_normals:
        keys.append("normals")
    if not l.zipped[1]:
        keys.append("uvs")
    if animated:
        keys.append("weights")
    keys.append("indices")
    if l.has_secondary:
        keys.append("secondary_indices")
    for i, k in enumerate(("position_map", "normal_map", "strips",
                           "metrics", "pairs")):
        if l.counts[i]:
            keys.append(k)
    if version > 28:
        keys.append("areas")
    if l.zipped[0]:
        keys.extend(("zip_positions", "alpha"))
    if l.zipped[1]:
        keys.append("zip_uvs")
    if l.zipped[2]:
        keys.append("zip_uv13")
    for k in keys:
        out.extend(l.streams[k])
    return bytes(out)


def encode_mesh(m, compressed=None):
    v = m.version
    comp = m.compressed if compressed is None else int(compressed)
    require(v >= 27 or not comp, "0x19/0x1A没有外层压缩字段")
    require(bool(m.extra) == bool(m.extra_data), "附加几何结构标记与数据不一致")
    if m.extra:
        decode_extra_geometry(m.extra_data)
    out = bytearray(struct.pack("<I", v) + fixed_name(m.name)
                    + struct.pack("<I2B", len(m.lods), m.animated, m.extra))
    if v > 31:
        require(len(m.descriptors) % 112 == 0, "无效头部附加描述")
        out.extend(struct.pack("<I", len(m.descriptors) // 112) + m.descriptors)
    else:
        require(not m.descriptors, "该版本没有头部附加描述字段")
    payload = b"".join(encode_lod(l, v, m.animated) for l in m.lods) + m.extra_data
    if v >= 27:
        out.extend(struct.pack("<I", comp))
    if comp:
        block = lz4_encode(payload)
        out.extend(struct.pack("<2I", len(block), len(payload)) + block)
    else:
        out.extend(payload)
    out.extend(m.skeleton)
    result = bytes(out)
    read_mesh(result, m.name + ".mesh")
    return result


def new_skeleton(name, bones):
    out = bytearray(struct.pack("<I", 11) + fixed_name(name)
                    + struct.pack("<3IBI", len(bones), 0, 30, 2,
                                  sum(len(b["name"].encode()) + 1 for b in bones)))
    for b in bones:
        out.extend(fixed_name(b["name"])
                   + struct.pack("<16fI", *transpose(b["matrix"]), b["parent"] + 1))
    return bytes(out)


# ==================== 动画 ====================
def unit_quaternion(row):
    length = math.sqrt(sum(x * x for x in row))
    require(length > 1e-12, "零长度动画四元数")
    return tuple(x / length for x in row)


def read_trs(r):
    scale = r.unpack("3f")
    rotation = r.unpack("4f")
    translation = r.unpack("3f")
    require(all(math.isfinite(x) for x in scale + rotation + translation),
            "动画TRS含非有限数")
    return {"scale": scale, "rotation": unit_quaternion(rotation),
            "translation": translation}


def read_animation(skeleton):
    s = read_skeleton(skeleton)
    count = s["frame_count"]
    fps = s["fps"]
    n = len(s["bones"])
    comp = s["compression"]
    v = s["version"]
    if not count:
        require(not s["tail"], "StripAnim骨骼资源有意外尾部")
        return Animation(fps, 0, [], [], comp)
    require(comp in (0, 1, 2) and fps > 0, "无效动画压缩模式/帧率")
    r = Reader(s["tail"], "animation outer")
    [read_trs(r) for _ in range(n)]
    if comp:
        cs = r.u32()
        us = r.u32() if v >= 9 else 0x300000
        require(v >= 9, "骨骼动画v7/8的无长度LZ4尚不支持")
        payload = lz4_decode(r.take(cs), us)
        require(r.p == len(r.data), "动画压缩块尾部有多余数据")
    else:
        payload = r.take(len(r.data) - r.p)
    r = Reader(payload, "animation payload")
    nc, np, nq, nt = r.unpack("4I")
    ntq = r.u32() if v >= 10 else 0
    nm = r.u32()
    require(nc <= 100000 and max(np, nq, nt, ntq, nm) <= 100000000,
            "动画池数量超限")
    bind = [read_trs(r) for _ in range(n)]
    clips = []
    used = [0, 0, 0, 0, 0]
    paths = ("scale", "rotation", "translation")
    for ci in range(nc):
        start, end, flags = r.unpack("3I")
        require(start <= end < count, "动画片段帧范围无效")
        bounds = r.unpack("6f") if v > 8 else (0.0,) * 6
        qmin = r.unpack("3f") if v >= 11 else (-20.0,) * 3
        qext = r.unpack("3f") if v >= 11 else (40.0,) * 3
        mask = r.take(n)
        require(all(x < 64 for x in mask), "骨骼通道掩码含未知位")
        require(all(not (x & (1 << j) and x & (1 << (j + 3)))
                    for j in range(3) for x in mask),
                "骨骼通道同时标成常量和逐帧")
        tracks = {}

        def read_value(path):
            if path == "scale":
                used[0] += 1
                return r.unpack("3f")
            if path == "rotation":
                used[1] += 1
                return (tuple(x * (2.0 / 65535.0) - 1.0 for x in r.unpack("4H"))
                        if comp == 2 else r.unpack("4f"))
            if comp == 2 and flags & 1:
                used[3] += 1
                return tuple(qmin[j] + x / 65535.0 * qext[j]
                             for j, x in enumerate(r.unpack("3H")))
            used[2] += 1
            return r.unpack("3f")

        for j, path in enumerate(paths):
            for bi, m in enumerate(mask):
                if m & (1 << (j + 3)):
                    tracks[(bi, path)] = [read_value(path)]
                    used[4] += 1
                elif m & (1 << j):
                    tracks[(bi, path)] = []
                    used[4] += 1
        for frame in range(end - start + 1):
            for j, path in enumerate(paths):
                for bi, m in enumerate(mask):
                    if m & (1 << j):
                        tracks[(bi, path)].append(read_value(path))
        for (bi, path), rows in tracks.items():
            require(all(math.isfinite(x) for row in rows for x in row),
                    "动画通道含非有限数")
            if path == "rotation":
                tracks[(bi, path)] = [unit_quaternion(row) for row in rows]
        clips.append(Clip(start, end, flags, bounds, qmin, qext, mask, tracks,
                          f"frames_{start}_{end}"))
    require(r.p == len(payload), f"动画payload剩余{len(payload)-r.p}字节")
    require(used == [np, nq, nt, ntq, nm],
            f"动画池声明与实际不一致: {used} != {[np, nq, nt, ntq, nm]}")
    return Animation(fps, count, bind, clips, comp, payload)


def add_glb_animations(builder, animation, bone_offset):
    doc = builder.doc
    doc["animations"] = []
    for clip in animation.clips:
        duration = (clip.end - clip.start) / animation.fps
        data = {"name": clip.name, "samplers": [], "channels": [],
                "extras": {"sky_start": clip.start, "sky_end": clip.end,
                           "sky_fps": animation.fps, "sky_flags": clip.flags}}
        for (bi, path), rows in sorted(clip.tracks.items()):
            if len(rows) == 1:
                times = [0.0, duration] if duration else [0.0]
                values = [rows[0]] * len(times)
            else:
                times = [i / animation.fps for i in range(len(rows))]
                values = rows
            if path == "rotation":
                values = [unit_quaternion(row) for row in values]
                for j in range(1, len(values)):
                    if sum(x * y for x, y in zip(values[j - 1], values[j])) < 0:
                        values[j] = tuple(-x for x in values[j])
            time_acc = builder.accessor([(t,) for t in times], "SCALAR", bounds=True)
            val_acc = builder.accessor(values, "VEC4" if path == "rotation" else "VEC3")
            si = len(data["samplers"])
            data["samplers"].append({"input": time_acc, "output": val_acc,
                                     "interpolation": "LINEAR"})
            data["channels"].append({"sampler": si,
                                     "target": {"node": bone_offset + bi,
                                                "path": path}})
        if not data["channels"] and animation.bind:
            path = "translation"
            values = [animation.bind[0][path]] * (2 if duration else 1)
            times = [0.0, duration] if duration else [0.0]
            data["samplers"].append({
                "input": builder.accessor([(t,) for t in times], "SCALAR",
                                          bounds=True),
                "output": builder.accessor(values, "VEC3")})
            data["channels"].append({"sampler": 0,
                                     "target": {"node": bone_offset, "path": path}})
        doc["animations"].append(data)


def pack_trs(trs):
    return struct.pack("<10f", *trs["scale"],
                       *unit_quaternion(trs["rotation"]), *trs["translation"])


def encode_animation(skeleton, animation, compression=2):
    s = read_skeleton(skeleton)
    bones = s["bones"]
    n = len(bones)
    require(compression in (0, 1, 2), "不支持动画压缩模式")
    if not animation.clips:
        return new_skeleton(s["name"], bones)
    require(len(animation.bind) == n and 1 <= animation.fps <= 240,
            "动画bind/fps无效")
    bodies = []
    counts = [0, 0, 0, 0, 0]
    for clip in animation.clips:
        nf = clip.end - clip.start + 1
        require(nf > 0, "空动画片段")
        mask = [0] * n
        for (bi, path), rows in clip.tracks.items():
            require(0 <= bi < n and path in ("scale", "rotation", "translation"),
                    "动画通道类型/骨骼索引错误")
            require(len(rows) in (1, nf), "动画行数与片段帧数不符")
            j = ("scale", "rotation", "translation").index(path)
            mask[bi] |= 1 << (j + 3 if len(rows) == 1 else j)
        translations = [row for (bi, path), rows in clip.tracks.items()
                        if path == "translation" for row in rows]
        qmin = (tuple(min(row[j] for row in translations) for j in range(3))
                if translations else (0.0,) * 3)
        qext = (tuple(max(row[j] for row in translations) - qmin[j]
                      for j in range(3)) if translations else (0.0,) * 3)
        flags = (clip.flags | 1) if compression == 2 else (clip.flags & ~1)
        body = bytearray(struct.pack("<3I12f", clip.start, clip.end, flags,
                                     *clip.bounds, *qmin, *qext) + bytes(mask))

        def write_value(path, row):
            require(all(math.isfinite(x) for x in row), "动画数据非有限")
            if path == "scale":
                counts[0] += 1
                body.extend(struct.pack("<3f", *row))
            elif path == "rotation":
                counts[1] += 1
                row = unit_quaternion(row)
                if compression == 2:
                    body.extend(struct.pack(
                        "<4H", *(quantize(x, -1, 1, 65535) for x in row)))
                else:
                    body.extend(struct.pack("<4f", *row))
            elif compression == 2:
                counts[3] += 1
                body.extend(struct.pack(
                    "<3H", *(quantize(row[j], qmin[j], qmin[j] + qext[j], 65535)
                             for j in range(3))))
            else:
                counts[2] += 1
                body.extend(struct.pack("<3f", *row))

        for j, path in enumerate(("scale", "rotation", "translation")):
            for bi, m in enumerate(mask):
                if m & (1 << (j + 3)):
                    write_value(path, clip.tracks[(bi, path)][0])
                    counts[4] += 1
                elif m & (1 << j):
                    counts[4] += 1
        for fi in range(nf):
            for j, path in enumerate(("scale", "rotation", "translation")):
                for bi, m in enumerate(mask):
                    if m & (1 << j):
                        write_value(path, clip.tracks[(bi, path)][fi])
        bodies.append(body)
    payload = (struct.pack("<6I", len(bodies), *counts)
               + b"".join(pack_trs(t) for t in animation.bind)
               + b"".join(bodies))
    frame_count = max(clip.end for clip in animation.clips) + 1
    out = bytearray(struct.pack("<I", 11) + fixed_name(s["name"])
                    + struct.pack("<3IBI", n, frame_count, animation.fps,
                                  compression,
                                  sum(len(b["name"].encode()) + 1 for b in bones)))
    for b in bones:
        out.extend(fixed_name(b["name"])
                   + struct.pack("<16fI", *transpose(b["matrix"]), b["parent"] + 1))
    out.extend(b"".join(pack_trs(t) for t in animation.bind))
    if compression:
        block = lz4_encode(payload)
        out.extend(struct.pack("<2I", len(block), len(payload)) + block)
    else:
        out.extend(payload)
    result = bytes(out)
    read_animation(result)
    return result


def decompose_trs(matrix):
    require(len(matrix) == 16 and all(math.isfinite(x) for x in matrix),
            "无效TRS矩阵")
    require(max(abs(matrix[i] - v) for i, v in
                zip((12, 13, 14, 15), (0.0, 0.0, 0.0, 1.0))) < 1e-5,
            "非仿射骨骼矩阵")
    columns = [tuple(matrix[i * 4 + j] for i in range(3)) for j in range(3)]
    scales = [math.sqrt(sum(x * x for x in col)) for col in columns]
    require(min(scales) > 1e-10, "零缩放骨骼无法分解")
    determinant = (matrix[0] * (matrix[5] * matrix[10] - matrix[6] * matrix[9])
                   - matrix[1] * (matrix[4] * matrix[10] - matrix[6] * matrix[8])
                   + matrix[2] * (matrix[4] * matrix[9] - matrix[5] * matrix[8]))
    if determinant < 0:
        scales[0] = -scales[0]
    cols = [tuple(x / scales[j] for x in col) for j, col in enumerate(columns)]
    require(max(abs(sum(x * y for x, y in zip(cols[j], cols[k])))
                for j, k in ((0, 1), (0, 2), (1, 2))) < 1e-4,
            "骨骼变换含剪切，不能编码成游戏TRS")
    m = [[cols[j][i] for j in range(3)] for i in range(3)]
    trace = sum(m[i][i] for i in range(3))
    if trace > 0:
        t = math.sqrt(trace + 1) * 2
        q = ((m[2][1] - m[1][2]) / t, (m[0][2] - m[2][0]) / t,
             (m[1][0] - m[0][1]) / t, 0.25 * t)
    else:
        i = max(range(3), key=lambda j: m[j][j])
        j = (i + 1) % 3
        k = (i + 2) % 3
        t = math.sqrt(max(0.0, 1 + m[i][i] - m[j][j] - m[k][k])) * 2
        vals = [0.0] * 4
        vals[i] = 0.25 * t
        vals[j] = (m[j][i] + m[i][j]) / t
        vals[k] = (m[k][i] + m[i][k]) / t
        vals[3] = (m[k][j] - m[j][k]) / t
        q = tuple(vals)
    return {"scale": tuple(scales), "rotation": unit_quaternion(q),
            "translation": (matrix[3], matrix[7], matrix[11])}


def slerp(a, b, t):
    a = unit_quaternion(a)
    b = unit_quaternion(b)
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0:
        b = tuple(-x for x in b)
        dot = -dot
    if dot > 0.9995:
        return unit_quaternion(tuple(x + (y - x) * t for x, y in zip(a, b)))
    theta = math.acos(min(1.0, dot))
    den = math.sin(theta)
    return tuple((x * math.sin((1 - t) * theta) + y * math.sin(t * theta)) / den
                 for x, y in zip(a, b))


def sampler_value(times, values, mode, path, t):
    import bisect
    n = len(times)
    require(mode in ("LINEAR", "STEP", "CUBICSPLINE"), f"未知动画插值: {mode}")
    require(n > 0 and all(times[j] < times[j + 1] for j in range(n - 1)),
            "动画时间必须严格递增")
    expected = n * 3 if mode == "CUBICSPLINE" else n
    require(len(values) == expected, "动画输入输出数量不符")

    def key(i):
        return values[i * 3 + 1] if mode == "CUBICSPLINE" else values[i]

    if t <= times[0]:
        return key(0)
    if t >= times[-1]:
        return key(n - 1)
    i = bisect.bisect_right(times, t) - 1
    dt = times[i + 1] - times[i]
    u = (t - times[i]) / dt
    a = key(i)
    b = key(i + 1)
    if mode == "STEP":
        return a
    if mode == "LINEAR":
        return (slerp(a, b, u) if path == "rotation"
                else tuple(x + (y - x) * u for x, y in zip(a, b)))
    require(mode == "CUBICSPLINE", f"未知动画插值: {mode}")
    h00 = 2 * u ** 3 - 3 * u * u + 1
    h10 = u ** 3 - 2 * u * u + u
    h01 = -2 * u ** 3 + 3 * u * u
    h11 = u ** 3 - u * u
    row = tuple(h00 * a[j] + h10 * dt * values[i * 3 + 2][j]
                + h01 * b[j] + h11 * dt * values[(i + 1) * 3][j]
                for j in range(len(a)))
    return unit_quaternion(row) if path == "rotation" else row


def from_glb(g, bones, fps=None):
    if fps is None:
        rates = {clip.get("extras", {}).get("sky_fps")
                 for clip in g.doc.get("animations", [])
                 if "sky_fps" in clip.get("extras", {})}
        require(len(rates) <= 1, "动画片段sky_fps不一致，请用--fps指定统一采样帧率")
        fps = next(iter(rates), 30)
    require(isinstance(fps, int) and 1 <= fps <= 240, "动画采样帧率必须为1—240整数")
    nodes = g.doc.get("nodes", [])
    parents = {child: i for i, n in enumerate(nodes)
               for child in n.get("children", [])}
    js = [b["node"] for b in bones]
    require(js, "GLB动画没有可关联的骨架")
    relevant = set(js)
    for j in js:
        while j in parents:
            j = parents[j]
            relevant.add(j)

    def pose(channels, time):
        cache = {}

        def world(i):
            if i in cache:
                return cache[i]
            node = dict(nodes[i])
            for path, times, values, mode in channels.get(i, []):
                require("matrix" not in node, "带动画通道的节点必须使用TRS")
                node[path] = sampler_value(times, values, mode, path, time)
            local = node_matrix(node)
            cache[i] = mm(world(parents[i]), local) if i in parents else local
            return cache[i]

        result = []
        for i, j in enumerate(js):
            p = bones[i]["parent"]
            local = mm(inverse(world(js[p])), world(j)) if p >= 0 else world(j)
            result.append(decompose_trs(local))
        return result

    bind = pose({}, 0.0)
    clips = []
    cursor = 0
    for source in g.doc.get("animations", []):
        source_flags = source.get("extras", {}).get("sky_flags", 0)
        require(isinstance(source_flags, int)
                and 0 <= source_flags <= 0xFFFFFFFF, "动画sky_flags必须为u32")
        channels = {}
        first = math.inf
        last = -math.inf
        for channel in source["channels"]:
            target = channel["target"]
            path = target["path"]
            j = target["node"]
            require(path in ("scale", "rotation", "translation"),
                    "暂不支持morph权重动画")
            require(0 <= j < len(nodes), "动画节点索引越界")
            require(j in relevant, "动画通道与目标骨架/几何无关联，不能静默丢弃")
            sp = source["samplers"][channel["sampler"]]
            times = [r[0] for r in g.accessor(sp["input"])]
            values = g.accessor(sp["output"])
            mode = sp.get("interpolation", "LINEAR")
            require(times and all(math.isfinite(x) and x >= 0 for x in times),
                    "动画时间非法")
            require(all(math.isfinite(x) for row in values for x in row),
                    "动画通道非有限")
            require(not any(p == path for p, *rest in channels.get(j, [])),
                    "重复动画通道")
            width = 4 if path == "rotation" else 3
            require(all(len(row) == width for row in values), "动画输出分量数量错误")
            sampler_value(times, values, mode, path, times[0])
            channels.setdefault(j, []).append((path, times, values, mode))
            first = min(first, times[0])
            last = max(last, times[-1])
        require(channels, "空动画片段")
        duration = last - first
        frame_count = max(1, int(math.ceil(duration * fps - 1e-6)) + 1)
        require(frame_count * len(bones) <= 20000000, "动画采样数量超限")
        frames = [pose(channels, min(last, first + i / fps))
                  for i in range(frame_count)]
        tracks = {}
        for bi in range(len(bones)):
            for path in ("scale", "rotation", "translation"):
                rows = [p[bi][path] for p in frames]
                if path == "rotation":
                    for k in range(1, len(rows)):
                        if sum(x * y for x, y in zip(rows[k - 1], rows[k])) < 0:
                            rows[k] = tuple(-x for x in rows[k])
                constant = (max(abs(x - y) for row in rows
                                for x, y in zip(row, rows[0])) < 1e-7)
                if constant:
                    equal = (max(abs(x - y) for x, y in
                                 zip(rows[0], bind[bi][path])) < 1e-7)
                    if path == "rotation":
                        equal = equal or (max(abs(x + y) for x, y in
                                              zip(rows[0], bind[bi][path])) < 1e-7)
                    if not equal:
                        tracks[(bi, path)] = [rows[0]]
                else:
                    tracks[(bi, path)] = rows
        clips.append(Clip(cursor, cursor + frame_count - 1, source_flags,
                          (0.0,) * 6, (0.0,) * 3, (0.0,) * 3, b"", tracks,
                          source.get("name", f"animation_{len(clips)}")))
        cursor += frame_count
    return Animation(fps, cursor, bind, clips, 2)


def animation_fingerprint(g):
    payload = {"animations": g.doc.get("animations", []), "access": {},
               "nodes": [{k: v for k, v in node.items()
                          if k in ("matrix", "translation", "rotation",
                                   "scale", "children")}
                         for node in g.doc.get("nodes", [])]}
    for clip in payload["animations"]:
        for sampler in clip["samplers"]:
            for k in ("input", "output"):
                i = sampler[k]
                payload["access"][str(i)] = g.accessor(i)
    return digest(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode())


# ==================== GLB ====================
COMPONENTS = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
              5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
          "MAT2": 4, "MAT3": 9, "MAT4": 16}


class GlbBuilder:
    def __init__(self):
        self.bin = bytearray()
        self.doc = {"asset": {"version": "2.0",
                              "generator": "Sky mesh converter (v5)"},
                    "buffers": [], "bufferViews": [], "accessors": [],
                    "meshes": [], "nodes": [],
                    "scenes": [{"nodes": []}], "scene": 0}

    def view(self, data, target=None):
        self.bin.extend(b"\0" * (-len(self.bin) % 4))
        off = len(self.bin)
        self.bin.extend(data)
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        self.doc["bufferViews"].append(v)
        return len(self.doc["bufferViews"]) - 1

    def accessor(self, rows, typ, component=5126, target=None, bounds=False):
        fmt = {5126: "f", 5123: "H", 5125: "I", 5121: "B"}[component]
        width = WIDTHS[typ]
        raw = b"".join(struct.pack("<" + fmt * width, *row) for row in rows)
        v = self.view(raw, target)
        a = {"bufferView": v, "componentType": component,
             "count": len(rows), "type": typ}
        if bounds and rows:
            a.update(min=[min(row[i] for row in rows) for i in range(width)],
                     max=[max(row[i] for row in rows) for i in range(width)])
        self.doc["accessors"].append(a)
        return len(self.doc["accessors"]) - 1

    def data(self):
        for key in ("meshes", "nodes", "accessors", "skins", "animations"):
            if self.doc.get(key) == []:
                self.doc.pop(key, None)
        for scene in self.doc.get("scenes", []):
            if scene.get("nodes") == []:
                scene.pop("nodes")
        self.bin.extend(b"\0" * (-len(self.bin) % 4))
        self.doc["buffers"] = [{"byteLength": len(self.bin)}]
        jb = json.dumps(self.doc, ensure_ascii=False, separators=(",", ":"),
                        allow_nan=False).encode()
        jb += b" " * (-len(jb) % 4)
        return (struct.pack("<III", 0x46546C67, 2, 28 + len(jb) + len(self.bin))
                + struct.pack("<II", len(jb), 0x4E4F534A) + jb
                + struct.pack("<II", len(self.bin), 0x004E4942) + self.bin)


def builder_view(builder):
    view = object.__new__(Glb)
    view.doc = builder.doc
    view.bin = bytes(builder.bin)
    return view


class Glb:
    def __init__(self, data):
        require(len(data) >= 20, "GLB文件过短")
        magic, v, total = struct.unpack_from("<III", data)
        require(magic == 0x46546C67 and v == 2 and total == len(data),
                "无效GLB头/文件长度")
        p = 12
        doc = None
        blob = None
        while p < len(data):
            require(p + 8 <= len(data), "GLB chunk头截断")
            n, t = struct.unpack_from("<II", data, p)
            p += 8
            require(n % 4 == 0 and p + n <= len(data), "GLB chunk长度无效")
            b = data[p:p + n]
            p += n
            if t == 0x4E4F534A:
                require(doc is None, "重复JSON chunk")
                doc = json.loads(b)
            elif t == 0x004E4942:
                require(blob is None, "重复BIN chunk")
                blob = b
        require(doc is not None, "没有GLB JSON chunk")
        self.doc = doc
        self.bin = blob or b""
        require(doc.get("asset", {}).get("version") == "2.0", "只接受glTF 2.0")
        required = set(doc.get("extensionsRequired", []))
        unsupported = required - {"KHR_mesh_quantization", EXT, "MSFT_lod"}
        require(not unsupported,
                f"不支持必要扩展: {sorted(unsupported)}；请先解压Draco/meshopt")

    def view(self, i):
        v = self.doc["bufferViews"][i]
        require(v.get("buffer", 0) == 0, "GLB外部buffer不支持")
        off = v.get("byteOffset", 0)
        n = v["byteLength"]
        require(0 <= off <= len(self.bin) and 0 <= n <= len(self.bin) - off,
                "bufferView越界")
        return self.bin[off:off + n]

    def accessor(self, i):
        a = self.doc["accessors"][i]
        fmt, size = COMPONENTS[a["componentType"]]
        width = WIDTHS[a["type"]]
        n = a["count"]
        require(0 <= n <= 60000000, "accessor数量超限")
        rows = [tuple(0 for _ in range(width)) for _ in range(n)]

        def read(view, count, offset, stride):
            b = self.view(view)
            require(offset >= 0 and stride >= size * width and
                    (count == 0 or
                     offset + (count - 1) * stride + size * width <= len(b)),
                    "accessor越界/步长无效")
            return [struct.unpack_from("<" + fmt * width, b, offset + j * stride)
                    for j in range(count)]

        if "bufferView" in a:
            v = self.doc["bufferViews"][a["bufferView"]]
            rows = read(a["bufferView"], n, a.get("byteOffset", 0),
                        v.get("byteStride", size * width))
        if "sparse" in a:
            sp = a["sparse"]
            sc = sp["count"]
            ix = sp["indices"]
            vf = sp["values"]
            it, isz = COMPONENTS[ix["componentType"]]
            require(ix["componentType"] in (5121, 5123, 5125) and sc <= n,
                    "无效sparse索引")
            ib = self.view(ix["bufferView"])
            offset = ix.get("byteOffset", 0)
            require(offset >= 0 and offset + sc * isz <= len(ib),
                    "sparse索引越界")
            indices = struct.unpack_from("<" + it * sc, ib, offset)
            require(all(0 <= x < n for x in indices)
                    and all(x < y for x, y in zip(indices, indices[1:])),
                    "sparse索引无序/越界")
            vals = read(vf["bufferView"], sc, vf.get("byteOffset", 0),
                        size * width)
            for j, row in zip(indices, vals):
                rows[j] = row
        if a.get("normalized") and a["componentType"] != 5126:
            maxv = {5120: 127, 5121: 255, 5122: 32767, 5123: 65535,
                    5125: 4294967295}[a["componentType"]]
            rows = [tuple(max(-1.0, x / maxv) for x in row) for row in rows]
        return rows


def node_matrix(node):
    if "matrix" in node:
        return transpose(node["matrix"])
    tx, ty, tz = node.get("translation", (0, 0, 0))
    x, y, z, w = node.get("rotation", (0, 0, 0, 1))
    sx, sy, sz = node.get("scale", (1, 1, 1))
    return [(1 - 2 * y * y - 2 * z * z) * sx, (2 * x * y - 2 * z * w) * sy,
            (2 * x * z + 2 * y * w) * sz, tx,
            (2 * x * y + 2 * z * w) * sx, (1 - 2 * x * x - 2 * z * z) * sy,
            (2 * y * z - 2 * x * w) * sz, ty,
            (2 * x * z - 2 * y * w) * sx, (2 * y * z + 2 * x * w) * sy,
            (1 - 2 * x * x - 2 * y * y) * sz, tz, 0, 0, 0, 1]


def extra_geometry_role(doc, node):
    extras = node.get("extras", {})
    if EXTRA_GEOMETRY not in extras and "mesh" in node:
        extras = doc.get("meshes", [])[node["mesh"]].get("extras", {})
    if EXTRA_GEOMETRY not in extras:
        return None
    role = extras[EXTRA_GEOMETRY]
    require(type(role) is bool, "sky_extra_geometry标记必须是true或false")
    return role


def glb_fingerprint(doc, blob):
    obj = object.__new__(Glb)
    obj.doc = doc
    obj.bin = blob
    access = {}
    for mesh in doc.get("meshes", []):
        for p in mesh.get("primitives", []):
            for i in list(p.get("attributes", {}).values()) + \
                    ([p["indices"]] if "indices" in p else []):
                access[str(i)] = obj.accessor(i)
    for s in doc.get("skins", []):
        if "inverseBindMatrices" in s:
            access[str(s["inverseBindMatrices"])] = obj.accessor(
                s["inverseBindMatrices"])
    for a in doc.get("animations", []):
        for s in a.get("samplers", []):
            for k in ("input", "output"):
                access[str(s[k])] = obj.accessor(s[k])
    nodes = [{k: v for k, v in n.items()
              if k in ("mesh", "skin", "matrix", "translation", "rotation",
                       "scale", "children")}
             for n in doc.get("nodes", [])]
    for result, node in zip(nodes, doc.get("nodes", [])):
        if EXTRA_GEOMETRY in node.get("extras", {}):
            result[EXTRA_GEOMETRY] = node["extras"][EXTRA_GEOMETRY]
    payload = {"meshes": doc.get("meshes", []), "skins": doc.get("skins", []),
               "nodes": nodes, "animations": doc.get("animations", []),
               "access": access}
    return digest(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode())


def export_glb(m):
    g = GlbBuilder()
    d = g.doc
    nl = len(m.lods)
    animation = _sky_animation

    bones = []
    game_animation = None
    if m.animated:
        bones = read_skeleton(m.skeleton)["bones"]
        game_animation = animation.read_animation(m.skeleton)
    identity_slot = any(
        joint == 0 and weight > 0
        for lod in m.lods
        for row in struct.iter_unpack("8B", lod.streams.get("weights", b""))
        for joint, weight in zip(row[:4], row[4:]))
    worlds = []
    for b in bones:
        try:
            worlds.append(inverse(b["matrix"]))
        except FormatError:
            require(not read_skeleton(m.skeleton)["frame_count"],
                    "动画骨骼bind矩阵无效")
            b["matrix"] = identity()
            worlds.append(identity())
    for i, l in enumerate(m.lods):
        geom = decode_lod(l)
        if "JOINTS_0" in geom:
            require(bones, "蒙皮没有骨架")
            geom["JOINTS_0"] = [tuple(len(bones) if j == -1 else j for j in row)
                                for row in geom["JOINTS_0"]]
            require(all(0 <= j < len(bones) + int(identity_slot)
                        for row in geom["JOINTS_0"] for j in row),
                    "蒙皮骨骼索引越界")
        uv_channels = [int(k.rsplit("_", 1)[1]) for k in geom
                       if k.startswith("TEXCOORD_")]
        for channel in range(max(uv_channels, default=-1) + 1):
            geom.setdefault("TEXCOORD_" + str(channel), [(0.0, 0.0)] * l.vertices)
        attrs = {}
        for key, rows in geom.items():
            if key == "indices":
                continue
            typ = "VEC" + str(len(rows[0])) if rows else "VEC3"
            if not rows:
                continue
            attrs[key] = g.accessor(rows, typ,
                                    5123 if key == "JOINTS_0" else 5126,
                                    34962, key == "POSITION")
        ids = geom["indices"]
        ia = g.accessor([(i,) for i in ids], "SCALAR",
                        5125 if max(ids, default=0) > 65535 else 5123, 34963)
        d["meshes"].append({"name": m.name + f"_LOD{i}",
                            "primitives": [{"attributes": attrs,
                                            "indices": ia, "mode": 4}]})
        node = {"name": m.name + f"_LOD{i}", "mesh": i,
                "extras": {"sky_lod": i,
                           "sky_distance": (l.distance
                                            if math.isfinite(l.distance) else "inf")}}
        if bones:
            node["skin"] = 0
        d["nodes"].append(node)
    for i, b in enumerate(bones):
        p = b["parent"]
        local = mm(inverse(worlds[p]), worlds[i]) if p >= 0 else worlds[i]
        node = {"name": b["name"], "matrix": transpose(local)}
        if game_animation and game_animation.clips:
            node = {"name": b["name"], **game_animation.bind[i]}
        children = [nl + j for j, c in enumerate(bones) if c["parent"] == i]
        if children:
            node["children"] = children
        d["nodes"].append(node)
    if nl:
        d["scenes"][0]["nodes"].append(0)
    if nl > 1:
        d["nodes"][0]["extensions"] = {"MSFT_lod": {"ids": list(range(1, nl))}}
        d["extensionsUsed"] = ["MSFT_lod"]
    if bones:
        skin_joints = list(range(nl, nl + len(bones)))
        bind_matrices = [transpose(b["matrix"]) for b in bones]
        roots = [nl + i for i, b in enumerate(bones) if b["parent"] < 0]
        if identity_slot:
            virtual = len(d["nodes"])
            d["nodes"].append({"name": "SKY_mesh_transform",
                               "extras": {"sky_identity_slot": True}})
            skin_joints.append(virtual)
            bind_matrices.append(identity())
            roots.append(virtual)
        if len(roots) > 1:
            root = len(d["nodes"])
            d["nodes"].append({"name": "SKY_skeleton_root", "children": roots})
            roots = [root]
        ib = g.accessor(bind_matrices, "MAT4")
        d["skins"] = [{"joints": skin_joints, "inverseBindMatrices": ib}]
        d["scenes"][0]["nodes"] += roots
    if game_animation and game_animation.clips:
        animation.add_glb_animations(g, game_animation, nl)
    extra_node = None
    if m.extra:
        geom = decode_extra_geometry(m.extra_data)
        if geom["POSITION"] and geom["indices"]:
            pa = g.accessor(geom["POSITION"], "VEC3", 5126, 34962, True)
            ia = g.accessor([(i,) for i in geom["indices"]], "SCALAR", 5125, 34963)
            mesh_id = len(d["meshes"])
            d["meshes"].append({"name": m.name + "_附加几何",
                                "extras": {EXTRA_GEOMETRY: True},
                                "primitives": [{"attributes": {"POSITION": pa},
                                                "indices": ia, "mode": 4}]})
            extra_node = len(d["nodes"])
            d["nodes"].append({"name": m.name + "_附加几何", "mesh": mesh_id,
                               "extras": {EXTRA_GEOMETRY: True}})
            d["scenes"].append({"name": "附加几何", "nodes": [extra_node]})
    source_view = g.view(m.raw)
    d.setdefault("extensionsUsed", []).append(EXT)
    d["extensions"] = {EXT: {
        "bufferView": source_view, "sha256": digest(m.raw),
        "version": m.version, "flags": sorted(detect_flags(m)),
        "filenameFlags": sorted(m.filename_flags),
        "coordinateSystem": "game-native",
        "animationClipsExported": bool(game_animation and game_animation.clips),
        "animationHash": animation.fingerprint(builder_view(g)),
        "extraGeometryExported": extra_node is not None}}
    d["extensions"][EXT]["geometryHash"] = glb_fingerprint(d, bytes(g.bin))
    return g.data()


def import_glb(g, extra_only=False):
    d = g.doc
    nodes = d.get("nodes", [])
    parents = {}
    for i, n in enumerate(nodes):
        for j in n.get("children", []):
            require(j not in parents and 0 <= j < len(nodes),
                    "节点存在多个父节点/越界")
            parents[j] = i
    worlds = {}

    def world(i, active=None):
        if i in worlds:
            return worlds[i]
        active = set() if active is None else active
        require(i not in active, "GLB节点成环")
        active.add(i)
        mat = node_matrix(nodes[i])
        worlds[i] = mm(world(parents[i], active), mat) if i in parents else mat
        active.remove(i)
        return worlds[i]

    for i in range(len(nodes)):
        world(i)
    roots = d.get("scenes", [{"nodes": list(range(len(nodes)))}])[
        d.get("scene", 0)].get("nodes", [])
    active = set()

    def visit(i):
        if i in active:
            return
        active.add(i)
        for j in nodes[i].get("children", []):
            visit(j)

    for i in roots:
        visit(i)
    lod_ids = {j for n in nodes
               for j in n.get("extensions", {}).get("MSFT_lod", {}).get("ids", [])}
    tagged = any("sky_lod" in n.get("extras", {}) for n in nodes)
    selected = [
        (i, n) for i, n in enumerate(nodes) if "mesh" in n
        and (extra_geometry_role(d, n) is True if extra_only
             else extra_geometry_role(d, n) is None
             and (i in active or i in lod_ids
                  or (tagged and "sky_lod" in n.get("extras", {}))))]
    if extra_only:
        animated_nodes = {ch.get("target", {}).get("node")
                          for clip in d.get("animations", [])
                          for ch in clip.get("channels", [])}
        for i, n in selected:
            require("skin" not in n, "附加几何只存静态XYZ，不能使用蒙皮")
            j = i
            while j >= 0:
                require(j not in animated_nodes, "附加几何或其父节点不能添加动画")
                j = parents.get(j, -1)
    groups = {}
    rigid_nodes = ([i for i, n in selected]
                   if not extra_only and d.get("animations")
                   and all("skin" not in n for i, n in selected) else [])
    has_skin = any(n.get("skin") is not None for _, n in selected)
    mixed_rigid_nodes = []
    skin_id = None
    mesh_world = identity()
    for i, n in selected:
        level = (0 if extra_only
                 else (n.get("extras", {}).get("sky_lod", 0) if tagged
                       else (1 + sorted(lod_ids).index(i) if i in lod_ids else 0)))
        geom = groups.setdefault(level, {
            "POSITION": [], "indices": [],
            "distance": float(n.get("extras", {}).get("sky_distance", "inf"))})
        sk = n.get("skin")
        if sk is not None:
            require(skin_id is None or skin_id == sk,
                    "多个独立骨架不能装入一个mesh")
            skin_id = sk
            if mesh_world != identity():
                require(mesh_world == worlds[i], "同一蒙皮有不同mesh节点变换")
            mesh_world = worlds[i]
        mesh = d["meshes"][n["mesh"]]
        for prim in mesh["primitives"]:
            require(not prim.get("targets"),
                    "当前不生成游戏头部附加描述；未修改的来源GLB可精确保留")
            require("KHR_draco_mesh_compression" not in prim.get("extensions", {}),
                    "先在建模软件解压Draco")
            attrs = {k: g.accessor(v) for k, v in prim["attributes"].items()}
            require("POSITION" in attrs, "primitive缺少POSITION")
            if extra_only:
                require(not any(k.startswith(("JOINTS_", "WEIGHTS_"))
                                for k in attrs), "附加几何不能使用蒙皮权重")
                attrs = {"POSITION": attrs["POSITION"]}
            pos = attrs["POSITION"]
            require(all(len(p) == 3 for p in pos), "POSITION必须是XYZ")
            count = len(pos)
            if rigid_nodes:
                attrs["JOINTS_0"] = [(rigid_nodes.index(i), 0, 0, 0)] * count
                attrs["WEIGHTS_0"] = [(1.0, 0.0, 0.0, 0.0)] * count
            elif has_skin:
                skin_attrs = any(k.startswith(("JOINTS_", "WEIGHTS_"))
                                 for k in attrs)
                if sk is not None and skin_attrs:
                    require("JOINTS_0" in attrs and "WEIGHTS_0" in attrs,
                            "蒙皮缺少JOINTS_0/WEIGHTS_0")
                    joint_count = len(d["skins"][sk]["joints"])
                    require(
                        all(len(row) == 4 and all(
                            isinstance(j, int) and 0 <= j < joint_count
                            for j in row) for row in attrs["JOINTS_0"])
                        and all(len(row) == 4 for row in attrs["WEIGHTS_0"]),
                        "蒙皮分量数量错误或JOINTS_0超出GLB蒙皮范围")
                else:
                    if i not in mixed_rigid_nodes:
                        mixed_rigid_nodes.append(i)
                    attrs = {k: v for k, v in attrs.items()
                             if not k.startswith(("JOINTS_", "WEIGHTS_"))}
                    attrs["JOINTS_0"] = [(-i - 2,) * 4] * count
                    attrs["WEIGHTS_0"] = [(1.0, 0.0, 0.0, 0.0)] * count
            require(all(len(rows) == count for rows in attrs.values()),
                    "属性顶点数量不一致")
            ids = ([row[0] for row in g.accessor(prim["indices"])]
                   if "indices" in prim else list(range(count)))
            require(all(isinstance(j, int) and 0 <= j < count for j in ids),
                    "GLB索引越界")
            mode = prim.get("mode", 4)
            if mode == 5:
                ids = [v for j in range(len(ids) - 2)
                       for v in ((ids[j], ids[j + 1], ids[j + 2]) if j % 2 == 0
                                 else (ids[j + 1], ids[j], ids[j + 2]))
                       if len(set(ids[j:j + 3])) == 3]
            elif mode == 6:
                ids = [v for j in range(1, len(ids) - 1)
                       for v in (ids[0], ids[j], ids[j + 1])]
            else:
                require(mode == 4, "仅支持三角形/三角带/三角扇")
            require(len(ids) % 3 == 0, "三角索引数量不是3的倍数")
            mat = worlds[i]
            nm = transpose(inverse(mat))
            attrs["POSITION"] = [transform(mat, p) for p in pos]
            if "NORMAL" in attrs:
                attrs["NORMAL"] = [normal(transform(nm, p, 0.0))
                                   for p in attrs["NORMAL"]]
            det = (mat[0] * (mat[5] * mat[10] - mat[6] * mat[9])
                   - mat[1] * (mat[4] * mat[10] - mat[6] * mat[8])
                   + mat[2] * (mat[4] * mat[9] - mat[5] * mat[8]))
            if det < 0:
                for j in range(0, len(ids), 3):
                    ids[j + 1], ids[j + 2] = ids[j + 2], ids[j + 1]
            base = len(geom["POSITION"])
            all_keys = set(geom) - {"indices", "distance"} | set(attrs)
            defaults = {"NORMAL": (0.0, 1.0, 0.0),
                        "COLOR_0": (1.0, 1.0, 1.0, 1.0),
                        "JOINTS_0": (0, 0, 0, 0),
                        "WEIGHTS_0": (1.0, 0.0, 0.0, 0.0)}
            for k in all_keys:
                width = (3 if k in ("NORMAL", "POSITION")
                         else 4 if k.startswith(("COLOR", "JOINTS", "WEIGHTS"))
                         else 2)
                default = defaults.get(k, (0.0,) * width)
                if k not in geom:
                    geom[k] = [default] * base
                rows = attrs.get(k, [default] * count)
                if k == "COLOR_0":
                    rows = [tuple(x) + (1.0,) if len(x) == 3 else x for x in rows]
                geom[k].extend(rows)
            geom["indices"].extend(base + j for j in ids)
    if extra_only:
        for geom in groups.values():
            require(geom["POSITION"] and geom["indices"],
                    "附加几何必须包含顶点和三角形")
            encode_extra_geometry(geom)
        return list(groups.values()), [], False
    bones = []
    if not selected:
        skins = d.get("skins", [])
        require(len(skins) <= 1, "动画资源存在多个独立骨架")
        if skins:
            skin_id = 0
        elif d.get("animations"):
            rigid_nodes = sorted(active)
    if skin_id is not None:
        skin = d["skins"][skin_id]
        original_joints = skin["joints"]
        virtual = [j for j in original_joints
                   if nodes[j].get("extras", {}).get("sky_identity_slot") is True]
        require(len(virtual) <= 1, "蒙皮有多个保留的mesh变换槽")
        js = [j for j in original_joints if j not in virtual]
        require(len(js) <= 255, "游戏单mesh最多255根可索引骨骼")
        mapping = {j: i for i, j in enumerate(js)}
        ib = (g.accessor(skin["inverseBindMatrices"])
              if "inverseBindMatrices" in skin
              else [identity() for _ in original_joints])
        require(len(ib) == len(original_joints), "inverseBindMatrices数量不一致")
        for j, row in zip(original_joints, ib):
            if j in virtual:
                require(not nodes[j].get("children"), "保留的mesh变换槽不能有子节点")
                require(all(abs(x - y) < 1e-6 for x, y in
                            zip(mm(transpose(row), inverse(mesh_world)), identity()))
                        and all(abs(x - y) < 1e-6 for x, y in
                                zip(worlds[j], identity())),
                        "保留的mesh变换槽不能修改变换")
                require(not any(ch.get("target", {}).get("node") == j
                                for clip in d.get("animations", [])
                                for ch in clip.get("channels", [])),
                        "保留的mesh变换槽不能添加动画")
                continue
            p = parents.get(j, -1)
            while p >= 0 and p not in mapping:
                p = parents.get(p, -1)
            bones.append({"name": nodes[j].get("name", f"bone{mapping[j]}"),
                          "parent": mapping.get(p, -1),
                          "matrix": mm(transpose(row), inverse(mesh_world)),
                          "node": j})
        animated_nodes = {ch.get("target", {}).get("node")
                          for clip in d.get("animations", [])
                          for ch in clip.get("channels", [])}
        attached_or_animated = set(original_joints) | animated_nodes
        moving_rigid_nodes = []
        for j in mixed_rigid_nodes:
            p = j
            while p >= 0:
                if p in attached_or_animated:
                    moving_rigid_nodes.append(j)
                    break
                p = parents.get(p, -1)
        rigid_mapping = {j: len(bones) + k
                         for k, j in enumerate(moving_rigid_nodes)}
        hierarchy_mapping = {**mapping, **rigid_mapping}
        for j in moving_rigid_nodes:
            p = parents.get(j, -1)
            while p >= 0 and p not in hierarchy_mapping:
                p = parents.get(p, -1)
            bones.append({"name": f"SKY_rigid_mesh_{j}",
                          "parent": hierarchy_mapping.get(p, -1),
                          "matrix": inverse(worlds[j]), "node": j})
        require(len(bones) <= 255, "游戏单mesh最多255根可索引骨骼（含刚性部件）")
        remap_joints = {i: mapping.get(j, -1)
                        for i, j in enumerate(original_joints)}
        remap_joints.update({-j - 2: rigid_mapping.get(j, -1)
                             for j in mixed_rigid_nodes})
        for geom in groups.values():
            if "JOINTS_0" in geom:
                require(all(isinstance(j, int) and j in remap_joints
                            for row in geom["JOINTS_0"] for j in row),
                        "JOINTS_0超出GLB蒙皮范围")
                geom["JOINTS_0"] = [tuple(remap_joints[j] for j in row)
                                    for row in geom["JOINTS_0"]]
    if rigid_nodes:
        require(len(rigid_nodes) <= 255, "游戏单mesh最多255根可索引骨骼")
        mapping = {j: i for i, j in enumerate(rigid_nodes)}
        for j in rigid_nodes:
            parent = parents.get(j, -1)
            while parent >= 0 and parent not in mapping:
                parent = parents.get(parent, -1)
            bones.append({"name": nodes[j].get("name", f"bone{mapping[j]}"),
                          "parent": mapping.get(parent, -1),
                          "matrix": inverse(worlds[j]), "node": j})
    require(groups or bones
            or any(extra_geometry_role(d, n) is not None
                   for n in nodes if "mesh" in n),
            "GLB场景没有可转换的几何或骨架")
    for geom in groups.values():
        require(geom["POSITION"], "空几何不能转换")
        require("JOINTS_1" not in geom and "WEIGHTS_1" not in geom,
                "超过4个权重不能无损转换，请在建模软件限制为4个")
        if bones:
            require("JOINTS_0" in geom and "WEIGHTS_0" in geom,
                    "蒙皮缺少JOINTS_0/WEIGHTS_0")
        for k, rows in geom.items():
            if isinstance(rows, list) and k != "indices":
                require(all(math.isfinite(v) for row in rows for v in row),
                        f"{k}含非有限数")
    return ([groups[i] for i in sorted(groups)], bones,
            bool(d.get("animations")))


# ==================== FBX 解析（修复：Geometry↔Skin↔Cluster 正确关联） ====================
def _parse_fbx_ascii(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r\n':
            i += 1
        elif c in '{}:,*':
            tokens.append(c)
            i += 1
        elif c == '"':
            j = text.index('"', i + 1)
            tokens.append(('str', text[i + 1:j]))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}=:,*"':
                j += 1
            tokens.append(('id', text[i:j]))
            i = j

    def parse(idx):
        node = {'name': None, 'props': [], 'children': []}
        if tokens[idx][0] in ('id', 'str'):
            node['name'] = tokens[idx][1]
            idx += 1
        if idx < len(tokens) and tokens[idx] == ':':
            idx += 1
            while idx < len(tokens) and tokens[idx] not in ('{', '}'):
                t = tokens[idx]
                if t == '*':
                    idx += 1
                    node['props'].append(tokens[idx][1]
                                         if isinstance(tokens[idx], tuple)
                                         else tokens[idx])
                    idx += 1
                elif isinstance(t, tuple):
                    if idx + 1 < len(tokens) and tokens[idx + 1] == ':':
                        break
                    node['props'].append(t[1])
                    idx += 1
                elif t == ',':
                    idx += 1
                else:
                    break
        if idx < len(tokens) and tokens[idx] == '{':
            idx += 1
            while idx < len(tokens) and tokens[idx] != '}':
                if tokens[idx] == '{':
                    idx += 1
                child, idx = parse(idx)
                node['children'].append(child)
            idx += 1
        return node, idx

    root = []
    idx = 0
    while idx < len(tokens):
        if tokens[idx] == '}':
            idx += 1
            continue
        node, idx = parse(idx)
        root.append(node)
    return root


def _parse_fbx_binary(data):
    require(data[:20] == b'Kaydara FBX Binary', "非二进制 FBX")
    pos = 27
    n = len(data)

    def read_node():
        nonlocal pos
        if pos + 13 > n:
            return None
        ns = pos
        eo, np, pl = struct.unpack('<III', data[pos:pos + 12])
        pos += 12
        nl = data[pos]
        pos += 1
        name = data[pos:pos + nl].decode('latin1', 'replace')
        if name and name[-1] == '\x00':
            name = name[:-1]
        pos += nl
        props = []
        ps = pos
        while pos < ps + pl:
            pt = chr(data[pos])
            pos += 1
            if pt == 'Y':
                v = struct.unpack('<h', data[pos:pos + 2])[0]
                pos += 2
            elif pt == 'C':
                v = data[pos]
                pos += 1
            elif pt == 'I':
                v = struct.unpack('<i', data[pos:pos + 4])[0]
                pos += 4
            elif pt == 'F':
                v = struct.unpack('<f', data[pos:pos + 4])[0]
                pos += 4
            elif pt == 'D':
                v = struct.unpack('<d', data[pos:pos + 8])[0]
                pos += 8
            elif pt == 'L':
                v = struct.unpack('<q', data[pos:pos + 8])[0]
                pos += 8
            elif pt in 'fdlib':
                c = struct.unpack('<I', data[pos:pos + 4])[0]
                pos += 4
                if pt == 'f':
                    v = list(struct.unpack('<%df' % c, data[pos:pos + 4 * c]))
                    pos += 4 * c
                elif pt == 'd':
                    v = list(struct.unpack('<%dd' % c, data[pos:pos + 8 * c]))
                    pos += 8 * c
                elif pt == 'l':
                    v = list(struct.unpack('<%dq' % c, data[pos:pos + 8 * c]))
                    pos += 8 * c
                elif pt == 'i':
                    v = list(struct.unpack('<%di' % c, data[pos:pos + 4 * c]))
                    pos += 4 * c
                else:
                    v = list(data[pos:pos + c])
                    pos += c
            elif pt == 'S':
                sl = struct.unpack('<I', data[pos:pos + 4])[0]
                pos += 4
                v = data[pos:pos + sl].decode('utf-8', 'replace')
                pos += sl
            elif pt == 'R':
                sl = struct.unpack('<I', data[pos:pos + 4])[0]
                pos += 4
                v = data[pos:pos + sl]
                pos += sl
            else:
                raise ValueError('未知 FBX 属性 %r' % pt)
            props.append(v)
        children = []
        while pos < ns + eo:
            child = read_node()
            if child is None:
                break
            children.append(child)
        return {'name': name, 'props': props, 'children': children}

    root = []
    while pos < n - 13:
        node = read_node()
        if node is None:
            break
        root.append(node)
    return root


def _find_all(nodes, name):
    out = []

    def walk(lst):
        for nd in lst:
            if nd['name'] == name:
                out.append(nd)
            walk(nd['children'])

    walk(nodes)
    return out


def _extract_fbx(tree):
    """FBX → (geom, bones, bone_names)
    修复: 正确关联 Geometry ↔ Skin ↔ Cluster（之前 or True 的 bug）
    """
    conns = []

    def walk_c(lst):
        for nd in lst:
            if nd['name'] == 'C' and len(nd['props']) >= 3:
                try:
                    conns.append((nd['props'][0], int(nd['props'][1]),
                                  int(nd['props'][2])))
                except (ValueError, TypeError):
                    pass
            walk_c(nd['children'])

    walk_c(tree)

    def arr(node, nm):
        for c in node['children']:
            if c['name'] == nm:
                if c['children'] and c['children'][0]['name'] == 'a':
                    return c['children'][0]['props']
                if c['props'] and isinstance(c['props'][0], list):
                    return c['props'][0]
        return None

    def cprop(node, nm, idx=0):
        for c in node['children']:
            if c['name'] == nm and c['props']:
                return c['props'][idx]
        return None

    models = {}
    for m in _find_all(tree, 'Model'):
        if len(m['props']) < 3:
            continue
        try:
            mid = int(m['props'][0])
            models[mid] = {'name': m['props'][1], 'type': m['props'][2]}
        except (ValueError, TypeError):
            continue

    deformers = _find_all(tree, 'Deformer')
    skin_ids = set()
    cluster_list = []
    for d in deformers:
        props = d['props']
        if len(props) < 3:
            continue
        try:
            did = int(props[0])
        except (ValueError, TypeError):
            continue
        dtype = props[2]
        if dtype == 'Skin':
            skin_ids.add(did)
        elif dtype == 'Cluster':
            idxs = arr(d, 'Indexes')
            wts = arr(d, 'Weights')
            if not idxs or not wts or len(idxs) != len(wts):
                continue
            tl = cprop(d, 'TransformLink')
            cl = {'id': did, 'indexes': [int(x) for x in idxs],
                  'weights': [float(w) for w in wts],
                  'transform_link': tl, 'bone_model': None, 'skin': None}
            cluster_list.append(cl)

    cluster_ids = {c['id'] for c in cluster_list}

    # 建立 cluster → skin 关联
    for ct, a, b in conns:
        if ct != 'OO':
            continue
        if a in cluster_ids and b in skin_ids:
            for c in cluster_list:
                if c['id'] == a:
                    c['skin'] = b
                    break
        elif b in cluster_ids and a in skin_ids:
            for c in cluster_list:
                if c['id'] == b:
                    c['skin'] = a
                    break

    # 建立 skin → geometry 关联
    geoms_list = _find_all(tree, 'Geometry')
    require(geoms_list, "FBX 无 Geometry")
    geom_ids = set()
    for g in geoms_list:
        try:
            geom_ids.add(int(g['props'][0]))
        except (ValueError, TypeError, IndexError):
            pass
    skin_to_geom = {}
    for ct, a, b in conns:
        if ct != 'OO':
            continue
        if a in skin_ids and b in geom_ids:
            skin_to_geom[a] = b
        elif b in skin_ids and a in geom_ids:
            skin_to_geom[b] = a

    # 建立 cluster → bone model 关联
    for ct, a, b in conns:
        if ct != 'OO':
            continue
        if a in cluster_ids and b in models:
            for c in cluster_list:
                if c['id'] == a:
                    c['bone_model'] = b
                    break
        elif b in cluster_ids and a in models:
            for c in cluster_list:
                if c['id'] == b:
                    c['bone_model'] = a
                    break

    bone_ids = []
    seen = set()
    for c in cluster_list:
        bm = c['bone_model']
        if bm is not None and bm not in seen:
            bone_ids.append(bm)
            seen.add(bm)
    bone_index = {bid: i for i, bid in enumerate(bone_ids)}
    bones = []
    for i, bid in enumerate(bone_ids):
        mn = models[bid]['name']
        if '::' in mn:
            mn = mn.split('::', 1)[1]
        parent = -1
        for ct, a, b in conns:
            if ct == 'OO' and b == bid and a in bone_index and a != bid:
                parent = bone_index[a]
                break
        cl = next((c for c in cluster_list if c['bone_model'] == bid), None)
        matrix = identity()
        if cl and cl['transform_link'] and len(cl['transform_link']) >= 16:
            tl_row = [float(x) for x in cl['transform_link']]
            try:
                matrix = inverse(transpose(tl_row))
            except FormatError:
                pass
        bones.append({'name': mn, 'parent': parent, 'matrix': matrix, 'node': -1})

    # 骨骼名去重
    names = {}
    new_bones = []
    for b in bones:
        n = b['name'] or 'Bone'
        if n in names:
            names[n] += 1
            n = f'{n}_{names[n] - 1}'
        else:
            names[n] = 0
        new_bones.append({**b, 'name': n})
    bones = new_bones

    # 几何
    all_pos = []
    all_norm = []
    all_uv = []
    all_ids = []
    all_joints = []
    all_weights = []
    has_skin = False
    for g in geoms_list:
        verts = arr(g, 'Vertices')
        pvi = arr(g, 'PolygonVertexIndex')
        if not verts or not pvi:
            continue
        vc = len(verts) // 3
        positions = [tuple(float(verts[i * 3 + k]) for k in range(3))
                     for i in range(vc)]
        tris = []
        poly = []
        for v in pvi:
            v = int(v)
            idx = (-v - 1) if v < 0 else (v & 0x3FFFFFFF if v & 0x40000000 else v)
            if v < 0 or v & 0x40000000:
                poly.append(idx)
                for k in range(1, len(poly) - 1):
                    tris.append((poly[0], poly[k], poly[k + 1]))
                poly = []
            else:
                poly.append(idx)
        nors_flat = None
        map_n = None
        for c in g['children']:
            if c['name'] == 'LayerElementNormal':
                na = arr(c, 'Normals')
                map_n = cprop(c, 'MappingInformationType')
                if na:
                    nors_flat = [tuple(float(na[i * 3 + k]) for k in range(3))
                                 for i in range(len(na) // 3)]
        n_per_v = [(0.0, 0.0, 1.0)] * vc
        if nors_flat:
            if map_n == 'ByPolygonVertex' or len(nors_flat) == len(tris) * 3:
                acc = [[0.0] * 3 for _ in range(vc)]
                for ti, (a, b, c) in enumerate(tris):
                    for vidx, nidx in ((a, ti * 3), (b, ti * 3 + 1),
                                       (c, ti * 3 + 2)):
                        if nidx < len(nors_flat):
                            for k in range(3):
                                acc[vidx][k] += nors_flat[nidx][k]
                n_per_v = [normal(tuple(x)) for x in acc]
            elif len(nors_flat) == vc:
                n_per_v = [normal(n) for n in nors_flat]
        uvs_flat = None
        uv_idx = None
        for c in g['children']:
            if c['name'] == 'LayerElementUV':
                ua = arr(c, 'UV')
                uv_idx = arr(c, 'UVIndex')
                if ua:
                    uvs_flat = [tuple(float(ua[i * 2 + k]) for k in range(2))
                                for i in range(len(ua) // 2)]
        uv_per_v = [(0.0, 0.0)] * vc
        if uvs_flat:
            if len(uvs_flat) == vc:
                uv_per_v = list(uvs_flat)
            elif uv_idx is not None and len(uv_idx) == len(tris) * 3:
                tmp = [None] * vc
                for ti, (a, b, c) in enumerate(tris):
                    for vidx, uvi in ((a, ti * 3), (b, ti * 3 + 1),
                                      (c, ti * 3 + 2)):
                        uvi = int(uvi)
                        if tmp[vidx] is None and uvi < len(uvs_flat):
                            tmp[vidx] = uvs_flat[uvi]
                uv_per_v = [u if u is not None else (0.0, 0.0) for u in tmp]
        base = len(all_pos)
        all_pos += positions
        all_norm += n_per_v
        all_uv += uv_per_v
        for a, b, c in tris:
            all_ids += [a + base, b + base, c + base]
        gid = None
        try:
            gid = int(g['props'][0]) if g['props'] else None
        except (ValueError, TypeError):
            pass
        # 修复: 根据 gid 对应的 skin，找到该 skin 下的所有 cluster
        g_skin = None
        for sid, gid2 in skin_to_geom.items():
            if gid2 == gid:
                g_skin = sid
                break
        gclusters = [c for c in cluster_list
                     if c.get('bone_model') is not None
                     and c['bone_model'] in bone_index
                     and (g_skin is None or c['skin'] == g_skin)]
        if gclusters:
            has_skin = True
            per_v = [[] for _ in range(vc)]
            for cl in gclusters:
                b_i = bone_index.get(cl['bone_model'], -1)
                if b_i < 0:
                    continue
                for vi, w in zip(cl['indexes'], cl['weights']):
                    vi -= base
                    if 0 <= vi < vc and w > 0:
                        per_v[vi].append((b_i, w))
            for lst in per_v:
                lst.sort(key=lambda x: -x[1])
                lst = lst[:4]
                total = sum(w for _, w in lst)
                if total <= 0:
                    all_joints.append((0, 0, 0, 0))
                    all_weights.append((0.0,) * 4)
                else:
                    js = [b for b, _ in lst] + [0] * (4 - len(lst))
                    ws = [w / total for _, w in lst] + [0.0] * (4 - len(lst))
                    all_joints.append(tuple(js))
                    all_weights.append(tuple(ws))
        else:
            all_joints += [(0, 0, 0, 0)] * vc
            all_weights += [(1.0, 0.0, 0.0, 0.0)] * vc

    geom = {"POSITION": all_pos, "NORMAL": all_norm, "TEXCOORD_0": all_uv,
            "indices": all_ids, "distance": math.inf}
    if has_skin:
        geom["JOINTS_0"] = all_joints
        geom["WEIGHTS_0"] = all_weights
        return geom, bones, [b['name'] for b in bones]
    return geom, [], []


# ==================== OBJ ====================
def _parse_obj(text):
    positions, normals, uvs, faces = [], [], [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == '#':
            continue
        parts = line.split()
        if parts[0] == 'v':
            positions.append(tuple(float(x) for x in parts[1:4]))
        elif parts[0] == 'vn':
            normals.append(tuple(float(x) for x in parts[1:4]))
        elif parts[0] == 'vt':
            uvs.append(tuple(float(x) for x in parts[1:3]))
        elif parts[0] == 'f':
            idxs = []
            for p in parts[1:]:
                comp = p.split('/')
                idxs.append((int(comp[0]) - 1,
                             int(comp[1]) - 1 if len(comp) > 1 and comp[1] else None,
                             int(comp[2]) - 1 if len(comp) > 2 and comp[2] else None))
            for k in range(1, len(idxs) - 1):
                faces.append((idxs[0], idxs[k], idxs[k + 1]))
    n_per_v = [(0.0, 0.0, 1.0)] * len(positions)
    uv_per_v = [(0.0, 0.0)] * len(positions)
    all_ids = []
    for a, b, c in faces:
        for vidx, ti, ni in (a, b, c):
            all_ids.append(vidx)
            if ni is not None:
                n_per_v[vidx] = normals[ni]
            if ti is not None:
                uv_per_v[vidx] = uvs[ti]
    return {"POSITION": positions, "NORMAL": n_per_v, "TEXCOORD_0": uv_per_v,
            "indices": all_ids, "distance": math.inf}


# ==================== STL ====================
def parse_stl(data):
    if data[:5].lower() == b'solid' and b'facet' in data[:300].lower():
        return _parse_stl_ascii(data.decode('utf-8', 'replace'))
    return _parse_stl_binary(data)


def _parse_stl_ascii(text):
    positions = []
    normals = []
    ids = []
    cur_normal = (0.0, 0.0, 1.0)
    cur_verts = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith('facet normal'):
            parts = line.split()
            try:
                cur_normal = (float(parts[2]), float(parts[3]), float(parts[4]))
            except (ValueError, IndexError):
                cur_normal = (0.0, 0.0, 1.0)
            cur_verts = []
        elif low.startswith('vertex'):
            parts = line.split()
            try:
                cur_verts.append((float(parts[1]), float(parts[2]),
                                  float(parts[3])))
            except (ValueError, IndexError):
                raise FormatError("STL ASCII: 无效 vertex 行")
        elif low.startswith('endfacet'):
            if len(cur_verts) == 3:
                base = len(positions)
                positions.extend(cur_verts)
                normals.extend([cur_normal] * 3)
                ids.extend([base, base + 1, base + 2])
            cur_verts = []
    require(ids, "STL ASCII: 无三角形")
    return {"POSITION": positions, "NORMAL": normals,
            "TEXCOORD_0": [(0.0, 0.0)] * len(positions),
            "indices": ids, "distance": math.inf}


def _parse_stl_binary(data):
    require(len(data) >= 84, "STL 二进制: 文件过短")
    tri_count = struct.unpack_from('<I', data, 80)[0]
    require(tri_count <= 20000000, "STL: 三角形数超限")
    expected = 84 + tri_count * 50
    require(len(data) >= expected, f"STL: 需 {expected} 字节，实际 {len(data)}")
    positions = []
    normals = []
    ids = []
    p = 84
    for _ in range(tri_count):
        nx, ny, nz = struct.unpack_from('<3f', data, p)
        v0 = struct.unpack_from('<3f', data, p + 12)
        v1 = struct.unpack_from('<3f', data, p + 24)
        v2 = struct.unpack_from('<3f', data, p + 36)
        base = len(positions)
        positions.extend([v0, v1, v2])
        normals.extend([(nx, ny, nz)] * 3)
        ids.extend([base, base + 1, base + 2])
        p += 50
    return {"POSITION": positions, "NORMAL": normals,
            "TEXCOORD_0": [(0.0, 0.0)] * len(positions),
            "indices": ids, "distance": math.inf}


def face_normal(a, b, c):
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
         u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(sum(x * x for x in n))
    return (n[0] / ln, n[1] / ln, n[2] / ln) if ln > 1e-12 else (0.0, 0.0, 1.0)


def _fmt_f(x):
    if x != x or x in (float('inf'), float('-inf')):
        return '0'
    s = '%.6f' % x
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
        if not s or s == '-':
            s = '0'
    return s


def export_stl(geom, path, binary=True, name="mesh"):
    pos = geom["POSITION"]
    ids = geom["indices"]
    require(len(ids) % 3 == 0, "STL: 三角不整")
    tris = [(ids[i], ids[i + 1], ids[i + 2]) for i in range(0, len(ids), 3)]
    if binary:
        buf = bytearray(b'\0' * 80)
        buf[:len(name[:79])] = name[:79].encode('ascii', 'replace')
        buf.extend(struct.pack('<I', len(tris)))
        for a, b, c in tris:
            na = face_normal(pos[a], pos[b], pos[c])
            buf.extend(struct.pack('<3f', *na))
            buf.extend(struct.pack('<3f', *pos[a]))
            buf.extend(struct.pack('<3f', *pos[b]))
            buf.extend(struct.pack('<3f', *pos[c]))
            buf.extend(b'\0\x00')
        with open(path, 'wb') as f:
            f.write(buf)
    else:
        with open(path, 'w') as f:
            f.write(f'solid {name}\n')
            for a, b, c in tris:
                na = face_normal(pos[a], pos[b], pos[c])
                f.write(f'  facet normal {_fmt_f(na[0])} {_fmt_f(na[1])} '
                        f'{_fmt_f(na[2])}\n')
                f.write('    outer loop\n')
                for v in (pos[a], pos[b], pos[c]):
                    f.write(f'      vertex {_fmt_f(v[0])} {_fmt_f(v[1])} '
                            f'{_fmt_f(v[2])}\n')
                f.write('    endloop\n  endfacet\n')
            f.write(f'endsolid {name}\n')
    with open(path, 'rb') as f:
        check = parse_stl(f.read())
    require(len(check["indices"]) == len(ids), "STL 回读验证失败")
    return len(tris)


# ==================== PLY ====================
PLY_TYPES = {
    'char': ('b', 1), 'uchar': ('B', 1), 'int8': ('b', 1), 'uint8': ('B', 1),
    'short': ('h', 2), 'ushort': ('H', 2), 'int16': ('h', 2), 'uint16': ('H', 2),
    'int': ('i', 4), 'uint': ('I', 4), 'int32': ('i', 4), 'uint32': ('I', 4),
    'float': ('f', 4), 'float32': ('f', 4), 'double': ('d', 8), 'float64': ('d', 8),
}


def parse_ply(data):
    require(data[:3] == b'ply', "PLY: 缺少 magic")
    he = data.find(b'end_header\n')
    if he < 0:
        he = data.find(b'end_header\r\n')
        require(he >= 0, "PLY: 缺少 end_header")
        he += len(b'end_header\r\n')
    else:
        he += len(b'end_header\n')
    header = data[:he].decode('ascii', 'replace')
    lines = [l.strip() for l in header.splitlines() if l.strip()]
    fmt = None
    cur = None
    vertex_props = []
    face_prop = None
    vertex_count = 0
    face_count = 0
    for line in lines:
        if line.startswith('format '):
            fmt = line.split()[1]
        elif line.startswith('element '):
            parts = line.split()
            cur = parts[1]
            cnt = int(parts[2])
            if cur == 'vertex':
                vertex_count = cnt
            elif cur == 'face':
                face_count = cnt
        elif line.startswith('property ') and cur:
            parts = line.split()
            if parts[1] == 'list':
                if cur == 'face':
                    face_prop = (parts[2], parts[3])
            else:
                if cur == 'vertex':
                    vertex_props.append((parts[1], parts[2]))
    require(fmt in ('ascii', 'binary_little_endian'),
            f"PLY: 不支持格式 {fmt}")
    names = [p[1] for p in vertex_props]
    require(all(n in names for n in ('x', 'y', 'z')), "PLY: 缺 x/y/z")
    body = data[he:]
    positions = []
    normals = []
    uvs = []
    colors = []
    ids = []
    if fmt == 'ascii':
        tokens = body.decode('ascii', 'replace').split()
        idx = 0
        for _ in range(vertex_count):
            row = {}
            for _, pname in vertex_props:
                row[pname] = float(tokens[idx])
                idx += 1
            positions.append((row['x'], row['y'], row['z']))
            if 'nx' in row:
                normals.append((row['nx'], row['ny'], row['nz']))
            if 's' in row:
                uvs.append((row['s'], row['t']))
            if 'red' in row:
                colors.append((row['red'] / 255, row['green'] / 255,
                               row['blue'] / 255, row.get('alpha', 255) / 255))
        for _ in range(face_count):
            n = int(tokens[idx])
            idx += 1
            face = [int(tokens[idx + k]) for k in range(n)]
            idx += n
            for k in range(1, n - 1):
                ids.extend([face[0], face[k], face[k + 1]])
    else:
        p = 0
        for _ in range(vertex_count):
            row = {}
            for typ, pname in vertex_props:
                fchar, fsize = PLY_TYPES[typ]
                row[pname] = struct.unpack_from('<' + fchar, body, p)[0]
                p += fsize
            positions.append((row['x'], row['y'], row['z']))
            if 'nx' in row:
                normals.append((row['nx'], row['ny'], row['nz']))
            if 's' in row:
                uvs.append((row['s'], row['t']))
            if 'red' in row:
                colors.append((row['red'] / 255, row['green'] / 255,
                               row['blue'] / 255, row.get('alpha', 255) / 255))
        require(face_prop is not None, "PLY: face 无 list 属性")
        cnt_type, idx_type = face_prop
        cf, cs = PLY_TYPES[cnt_type]
        df, ds = PLY_TYPES[idx_type]
        for _ in range(face_count):
            n = struct.unpack_from('<' + cf, body, p)[0]
            p += cs
            face = [struct.unpack_from('<' + df, body, p + k * ds)[0]
                    for k in range(n)]
            p += n * ds
            for k in range(1, n - 1):
                ids.extend([face[0], face[k], face[k + 1]])
    result = {"POSITION": positions, "indices": ids, "distance": math.inf}
    if len(normals) == len(positions):
        result["NORMAL"] = normals
    if len(uvs) == len(positions):
        result["TEXCOORD_0"] = uvs
    if len(colors) == len(positions):
        result["COLOR_0"] = colors
    return result


def export_ply(geom, path, binary=True):
    pos = geom["POSITION"]
    ids = geom["indices"]
    nrm = geom.get("NORMAL")
    uvs = geom.get("TEXCOORD_0")
    cols = geom.get("COLOR_0")
    require(len(ids) % 3 == 0, "PLY: 三角不整")
    tris = [(ids[i], ids[i + 1], ids[i + 2]) for i in range(0, len(ids), 3)]
    has_n = nrm is not None and len(nrm) == len(pos)
    has_uv = uvs is not None and len(uvs) == len(pos)
    has_c = cols is not None and len(cols) == len(pos)
    lines = ['ply',
             'format ascii 1.0' if not binary else 'format binary_little_endian 1.0',
             'comment Exported by sky_mesh_tool']
    lines.append(f'element vertex {len(pos)}')
    lines += ['property float x', 'property float y', 'property float z']
    if has_n:
        lines += ['property float nx', 'property float ny', 'property float nz']
    if has_uv:
        lines += ['property float s', 'property float t']
    if has_c:
        lines += ['property uchar red', 'property uchar green',
                  'property uchar blue', 'property uchar alpha']
    lines.append(f'element face {len(tris)}')
    lines.append('property list uchar int vertex_indices')
    lines.append('end_header')
    header = '\n'.join(lines) + '\n'
    if not binary:
        with open(path, 'w') as f:
            f.write(header)
            for i, p in enumerate(pos):
                parts = [f'{p[0]:.6f}', f'{p[1]:.6f}', f'{p[2]:.6f}']
                if has_n:
                    parts += [f'{nrm[i][0]:.6f}', f'{nrm[i][1]:.6f}',
                              f'{nrm[i][2]:.6f}']
                if has_uv:
                    parts += [f'{uvs[i][0]:.6f}', f'{uvs[i][1]:.6f}']
                if has_c:
                    parts += [str(max(0, min(255, int(cols[i][k] * 255))))
                              for k in range(4)]
                f.write(' '.join(parts) + '\n')
            for a, b, c in tris:
                f.write(f'3 {a} {b} {c}\n')
    else:
        with open(path, 'wb') as f:
            f.write(header.encode('ascii'))
            for i, p in enumerate(pos):
                f.write(struct.pack('<3f', *p))
                if has_n:
                    f.write(struct.pack('<3f', *nrm[i]))
                if has_uv:
                    f.write(struct.pack('<2f', *uvs[i]))
                if has_c:
                    f.write(bytes(max(0, min(255, int(cols[i][k] * 255)))
                                  for k in range(4)))
            for a, b, c in tris:
                f.write(struct.pack('<B3i', 3, a, b, c))
    with open(path, 'rb') as f:
        check = parse_ply(f.read())
    require(len(check["indices"]) == len(ids), "PLY 回读验证失败")
    return len(tris)


# ==================== glTF（分离式） ====================
def parse_gltf(gltf_path):
    """解析 .gltf + 外部 .bin。返回 (geom, bones, has_anim)。"""
    gltf_path = Path(gltf_path)
    with open(gltf_path, 'r', encoding='utf-8') as f:
        doc = json.load(f)
    buffers = []
    for buf in doc.get("buffers", []):
        uri = buf.get("uri", "")
        if uri.startswith("data:"):
            b64 = uri.split(",", 1)[1]
            buffers.append(base64.b64decode(b64))
        elif uri:
            bin_path = gltf_path.parent / uri
            require(bin_path.exists(), f"glTF: 外部 buffer 缺失 {bin_path}")
            buffers.append(bin_path.read_bytes())
        else:
            buffers.append(b"")
    if len(buffers) > 1:
        # 修复: 合并多 buffer，正确调整 byteOffset
        merged = bytearray()
        offsets = []
        for b in buffers:
            while len(merged) % 4:
                merged.append(0)
            offsets.append(len(merged))
            merged.extend(b)
        for bv in doc.get("bufferViews", []):
            buf_idx = bv.get("buffer", 0)
            if buf_idx > 0:
                bv["byteOffset"] = bv.get("byteOffset", 0) + offsets[buf_idx]
                bv["buffer"] = 0
        g = object.__new__(Glb)
        g.doc = doc
        g.bin = bytes(merged)
    else:
        g = object.__new__(Glb)
        g.doc = doc
        g.bin = buffers[0] if buffers else b""
    required = set(doc.get("extensionsRequired", []))
    unsupported = required - {"KHR_mesh_quantization", EXT, "MSFT_lod"}
    require(not unsupported, f"glTF: 不支持扩展 {sorted(unsupported)}")
    geoms, bones, has_anim = import_glb(g)
    if not geoms:
        raise FormatError("glTF: 无可转换几何")
    return geoms[0], bones, has_anim


def export_gltf(m, path):
    """导出为 .gltf + .bin。"""
    glb_bytes = export_glb(m)
    g = Glb(glb_bytes)
    doc = g.doc
    path = Path(path)
    bin_path = path.with_suffix('.bin')
    with open(bin_path, 'wb') as f:
        f.write(g.bin)
    doc["buffers"] = [{"uri": bin_path.name, "byteLength": len(g.bin)}]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, allow_nan=False)
    check_geom, _, _ = parse_gltf(path)
    expected = len(decode_lod(m.lods[0])["indices"])
    require(len(check_geom["indices"]) == expected, "glTF 回读验证失败")
    return len(g.bin)


# ==================== DAE ====================
def parse_dae(text):
    """解析 Collada DAE（修复: stride 强制 3 for POSITION）。"""
    root = ET.fromstring(text)

    def strip_ns(tag):
        return tag.split('}', 1)[1] if '}' in tag else tag

    def find(elem, nm):
        for c in elem:
            if strip_ns(c.tag) == nm:
                return c
        return None

    def findall(elem, nm):
        return [c for c in elem if strip_ns(c.tag) == nm]

    lib_geoms = find(root, 'library_geometries')
    require(lib_geoms is not None, "DAE: 缺少 library_geometries")
    all_pos = []
    all_ids = []
    for geom in findall(lib_geoms, 'geometry'):
        mesh_elem = find(geom, 'mesh')
        if mesh_elem is None:
            continue
        sources = {}
        for src in findall(mesh_elem, 'source'):
            src_id = src.get('id', '')
            fa = find(src, 'float_array')
            if fa is None:
                continue
            values = [float(x) for x in fa.text.strip().split()]
            tc = find(src, 'technique_common')
            # 修复: DAE 标准要求 POSITION accessor stride=3
            stride = 3
            if tc is not None:
                acc = find(tc, 'accessor')
                if acc is not None:
                    stride = int(acc.get('stride', '3'))
            sources[src_id] = (values, stride)
        verts_elem = find(mesh_elem, 'vertices')
        if verts_elem is None:
            continue
        v_input = find(verts_elem, 'input')
        require(v_input is not None, "DAE: vertices 缺 input")
        pos_src_id = v_input.get('source', '').lstrip('#')
        require(pos_src_id in sources,
                f"DAE: 位置 source {pos_src_id} 未定义")
        pos_values, pos_stride = sources[pos_src_id]
        # 修复: stride 必须 >= 3
        pos_stride = max(pos_stride, 3)
        positions = [tuple(pos_values[i:i + 3])
                     for i in range(0, len(pos_values), pos_stride)]
        base = len(all_pos)
        all_pos.extend(positions)
        for tri_elem in (findall(mesh_elem, 'triangles')
                         + findall(mesh_elem, 'polylist')):
            inputs = findall(tri_elem, 'input')
            p_elem = find(tri_elem, 'p')
            if p_elem is None or not inputs:
                continue
            idx_list = [int(x) for x in p_elem.text.strip().split()]
            stride_in = len(inputs)
            pos_off = -1
            for inp in inputs:
                sem = inp.get('semantic')
                off = int(inp.get('offset', '0'))
                if sem == 'VERTEX':
                    pos_off = off
                    break
            if pos_off < 0:
                pos_off = int(inputs[0].get('offset', '0'))
            vcount_elem = find(tri_elem, 'vcount')
            if vcount_elem is not None:
                vcounts = [int(x) for x in vcount_elem.text.strip().split()]
                idx_pos = 0
                for vc in vcounts:
                    for k in range(1, vc - 1):
                        for role in (0, k, k + 1):
                            all_ids.append(base + idx_list[
                                (idx_pos + role) * stride_in + pos_off])
                    idx_pos += vc
            else:
                for t in range(0, len(idx_list), stride_in * 3):
                    for k in range(3):
                        all_ids.append(base + idx_list[
                            t + k * stride_in + pos_off])
    require(all_ids, "DAE: 无三角形")
    require(len(all_ids) % 3 == 0, "DAE: 三角不整")
    geom = {"POSITION": all_pos, "indices": all_ids, "distance": math.inf}
    geom["NORMAL"] = smooth_normals(all_pos, all_ids)
    geom["TEXCOORD_0"] = [(0.0, 0.0)] * len(all_pos)
    return geom, [], False


def export_dae(m, path):
    if not m.lods:
        raise FormatError("DAE: 无 LOD")
    geom = decode_lod(m.lods[0])
    pos = geom["POSITION"]
    nrm = geom.get("NORMAL", [(0.0, 0.0, 1.0)] * len(pos))
    uvs = geom.get("TEXCOORD_0", [(0.0, 0.0)] * len(pos))
    ids = geom["indices"]
    tris = [(ids[i], ids[i + 1], ids[i + 2]) for i in range(0, len(ids), 3)]
    bones = read_skeleton(m.skeleton)["bones"] if m.animated else []
    pos_txt = ' '.join(f'{x:.6f}' for p in pos for x in p)
    nrm_txt = ' '.join(f'{x:.6f}' for n in nrm for x in n)
    uv_txt = ' '.join(f'{x:.6f}' for uv in uvs for x in uv)
    p_parts = []
    for a, b, c in tris:
        for v in (a, b, c):
            p_parts += [str(v), str(v), str(v)]
    p_txt = ' '.join(p_parts)
    node_lines = []
    for i, b in enumerate(bones):
        name = (b['name'] or f'bone_{i}').replace('"', "'")
        node_lines.append(f'      <node id="bone_{i}" name="{name}" type="JOINT">')
        node_lines.append('        <matrix sid="transform">'
                          '1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>')
        node_lines.append('      </node>')
    dae = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>sky_mesh_tool v5</authoring_tool></contributor>
    <created>2024-01-01T00:00:00</created>
    <modified>2024-01-01T00:00:00</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Y_UP</up_axis>
  </asset>
  <library_geometries>
    <geometry id="mesh_geom" name="{m.name}">
      <mesh>
        <source id="mesh_pos">
          <float_array id="mesh_pos_arr" count="{len(pos)*3}">{pos_txt}</float_array>
          <technique_common>
            <accessor source="#mesh_pos_arr" count="{len(pos)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="mesh_nrm">
          <float_array id="mesh_nrm_arr" count="{len(nrm)*3}">{nrm_txt}</float_array>
          <technique_common>
            <accessor source="#mesh_nrm_arr" count="{len(nrm)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="mesh_uv">
          <float_array id="mesh_uv_arr" count="{len(uvs)*2}">{uv_txt}</float_array>
          <technique_common>
            <accessor source="#mesh_uv_arr" count="{len(uvs)}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="mesh_verts">
          <input semantic="POSITION" source="#mesh_pos"/>
          <input semantic="NORMAL" source="#mesh_nrm"/>
          <input semantic="TEXCOORD" source="#mesh_uv" set="0"/>
        </vertices>
        <triangles material="mat0" count="{len(tris)}">
          <input semantic="VERTEX" source="#mesh_verts" offset="0"/>
          <input semantic="NORMAL" source="#mesh_verts" offset="1"/>
          <input semantic="TEXCOORD" source="#mesh_verts" offset="2" set="0"/>
          <p>{p_txt}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="scene" name="Scene">
      <node id="mesh_node" name="{m.name}">
        <instance_geometry url="#mesh_geom">
          <bind_material>
            <technique_common>
              <instance_material symbol="mat0" target="#mat0"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
{chr(10).join(node_lines)}
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#scene"/>
  </scene>
</COLLADA>
'''
    try:
        pretty = minidom.parseString(dae).toprettyxml(indent="  ", encoding="utf-8")
        with open(path, 'wb') as f:
            f.write(pretty)
    except Exception as e:
        raise FormatError(f"DAE 生成失败: {e}")
    with open(path, 'r', encoding='utf-8') as f:
        check_geom, _, _ = parse_dae(f.read())
    require(len(check_geom["indices"]) == len(ids), "DAE 回读验证失败")
    return len(tris)


# ==================== Mesh 导出（FBX/OBJ） ====================
def export_mesh_to_fbxobj(m, out_stem, fmt='fbx', bone_names=None):
    if not m.lods:
        raise SystemExit('无 LOD')
    l = m.lods[0]
    geom = decode_lod(l)
    verts = geom["POSITION"]
    normals = geom.get("NORMAL")
    uvs = geom.get("TEXCOORD_0")
    ids = geom["indices"]
    tris = []
    for t in range(0, len(ids), 3):
        tris.append((ids[t], ids[t + 2], ids[t + 1]) if m.version in MODERN_VER
                    else (ids[t], ids[t + 1], ids[t + 2]))
    weights = None
    n_bones = 0
    if "JOINTS_0" in geom:
        weights = [[] for _ in verts]
        for vi, (js, ws) in enumerate(zip(geom["JOINTS_0"], geom["WEIGHTS_0"])):
            for b, w in zip(js, ws):
                if w > 0:
                    weights[vi].append((b, w))
        n_bones = max((b for lst in weights for b, _ in lst), default=-1) + 1
    if bone_names is None:
        emb = read_skeleton(m.skeleton)["bones"] if m.animated else []
        bone_names = ([b["name"] for b in emb] if emb
                      else ['Bone%d' % i for i in range(n_bones)])
    if weights and len(bone_names) < n_bones:
        bone_names += ['Bone%d' % i for i in range(len(bone_names), n_bones)]
    n_bones = len(bone_names)
    if fmt in ('obj', 'both'):
        with open(out_stem + '.obj', 'w') as f:
            f.write(f'# Exported from {m.name}\n')
            for x, y, z in verts:
                f.write(f'v {x:.6g} {y:.6g} {z:.6g}\n')
            if uvs:
                for u, v in uvs:
                    f.write(f'vt {u:.6g} {v:.6g}\n')
            if normals:
                for x, y, z in normals:
                    f.write(f'vn {x:.6g} {y:.6g} {z:.6g}\n')
            for a, b, c in tris:
                if uvs and normals:
                    f.write(f'f {a+1}/{a+1}/{a+1} {b+1}/{b+1}/{b+1} '
                            f'{c+1}/{c+1}/{c+1}\n')
                elif uvs:
                    f.write(f'f {a+1}/{a+1} {b+1}/{b+1} {c+1}/{c+1}\n')
                elif normals:
                    f.write(f'f {a+1}//{a+1} {b+1}//{b+1} {c+1}//{c+1}\n')
                else:
                    f.write(f'f {a+1} {b+1} {c+1}\n')
        if weights:
            with open(out_stem + '.weights.json', 'w') as f:
                json.dump({'bones': bone_names,
                           'weights': [[[b, round(w, 6)] for b, w in lst]
                                       for lst in weights]},
                          f, ensure_ascii=False, indent=1)
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} OBJ: {out_stem}.obj '
              f'({len(verts)}v {len(tris)}t '
              f'{("蒙皮"+str(n_bones)+"骨") if weights else "无蒙皮"})')
    if fmt in ('fbx', 'both'):
        _export_fbx_ascii(out_stem + '.fbx', verts, normals, uvs, tris,
                          weights, bone_names, m)
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} FBX: {out_stem}.fbx '
              f'({len(verts)}v {len(tris)}t '
              f'{("蒙皮"+str(n_bones)+"骨") if weights else "无蒙皮"})')


def _export_fbx_ascii(path, verts, normals, uvs, tris, weights, bone_names, m):
    vc = len(verts)
    n_bones = len(bone_names) if weights else 0
    ROOT_ID, GEOM_ID, MODEL_ID = 0, 100, 200
    BONE_BASE, SKIN_ID, CLUSTER_BASE = 201, 300, 301
    skel_bones = read_skeleton(m.skeleton)["bones"] if m.animated else []
    L = []
    L.append('; FBX 7.4.0 project file')
    L.append('; Generated by sky_mesh_tool v5')
    L.append(f'; source: {m.name} (v=0x{m.version:02X})')
    L.append('')
    L.append('FBXHeaderExtension:  {')
    L.append('\tFBXHeaderVersion: 1003')
    L.append('\tFBXVersion: 7400')
    L.append('\tCreationTimeStamp:  {')
    L.append('\t\tVersion: 1000')
    L.append('\t\tYear: 2024')
    L.append('\t\tMonth: 1')
    L.append('\t\tDay: 1')
    L.append('\t\tHour: 0')
    L.append('\t\tMinute: 0')
    L.append('\t\tSecond: 0')
    L.append('\t\tMillisecond: 0')
    L.append('\t}')
    L.append('\tCreator: "sky_mesh_tool v5"')
    L.append('\tFileId: "00000000000000000000000000000000"')
    L.append('\tCreationTime: "2024-01-01 00:00:00:000"')
    L.append('}')
    L.append('GlobalSettings:  {')
    L.append('\tVersion: 1000')
    L.append('\tProperties70:  {')
    L.append('\t\tP: "UpAxis", "int", "Integer", "",1')
    L.append('\t\tP: "UpAxisSign", "int", "Integer", "",1')
    L.append('\t\tP: "FrontAxis", "int", "Integer", "",2')
    L.append('\t\tP: "FrontAxisSign", "int", "Integer", "",1')
    L.append('\t\tP: "CoordAxis", "int", "Integer", "",0')
    L.append('\t\tP: "CoordAxisSign", "int", "Integer", "",1')
    L.append('\t\tP: "UnitScaleFactor", "double", "Number", "",1')
    L.append('\t}')
    L.append('}')
    L.append('Documents:  {')
    L.append('\tCount: 1')
    L.append('\tDocument: 1000000000, "", "Scene" {')
    L.append('\t\tProperties70:  {')
    L.append('\t\t\tP: "SourceObject", "object", "", ""')
    L.append('\t\t\tP: "ActiveAnimStackName", "KString", "", "", ""')
    L.append('\t\t}')
    L.append('\t\tRootNode: 0')
    L.append('\t}')
    L.append('}')
    L.append('References:  {')
    L.append('}')
    n_obj = 2 + (1 + n_bones) + (1 + n_bones if weights else 0)
    L.append('Definitions:  {')
    L.append('\tVersion: 100')
    L.append(f'\tCount: {n_obj}')
    L.append('\tObjectType: "GlobalSettings" {')
    L.append('\t\tCount: 1')
    L.append('\t}')
    L.append('\tObjectType: "Geometry" {')
    L.append('\t\tCount: 1')
    L.append('\t}')
    L.append('\tObjectType: "Model" {')
    L.append(f'\t\tCount: {1 + n_bones}')
    L.append('\t}')
    if weights:
        L.append('\tObjectType: "Deformer" {')
        L.append(f'\t\tCount: {1 + n_bones}')
        L.append('\t}')
    L.append('}')
    L.append('Objects:  {')
    L.append(f'\tGeometry: {GEOM_ID}, "Geometry::mesh", "Mesh" {{')
    L.append('\t\tGeometryVersion: 124')
    L.append(f'\t\tVertices: *{vc*3} {{')
    L.append('\t\t\ta: ' + ','.join(_fmt_f(v) for p in verts for v in p))
    L.append('\t\t}')
    pvi = [x for a, b, c in tris for x in (a, b, -(c + 1))]
    L.append(f'\t\tPolygonVertexIndex: *{len(pvi)} {{')
    L.append('\t\t\ta: ' + ','.join(str(x) for x in pvi))
    L.append('\t\t}')
    if normals:
        L.append('\t\tLayerElementNormal: 0 {')
        L.append('\t\t\tVersion: 102')
        L.append('\t\t\tName: ""')
        L.append('\t\t\tMappingInformationType: "ByVertice"')
        L.append('\t\t\tReferenceInformationType: "Direct"')
        L.append(f'\t\t\tNormals: *{vc*3} {{')
        L.append('\t\t\t\ta: ' + ','.join(_fmt_f(v) for p in normals for v in p))
        L.append('\t\t\t}')
        L.append('\t\t}')
    if uvs:
        L.append('\t\tLayerElementUV: 0 {')
        L.append('\t\t\tVersion: 101')
        L.append('\t\t\tName: "UVMap"')
        L.append('\t\t\tMappingInformationType: "ByVertice"')
        L.append('\t\t\tReferenceInformationType: "Direct"')
        L.append(f'\t\t\tUV: *{vc*2} {{')
        L.append('\t\t\t\ta: ' + ','.join(_fmt_f(v) for p in uvs for v in p))
        L.append('\t\t\t}')
        L.append('\t\t}')
    L.append('\t\tLayer: 0 {')
    L.append('\t\t\tVersion: 100')
    if normals:
        L.append('\t\t\tLayerElement:  {')
        L.append('\t\t\t\tType: "LayerElementNormal"')
        L.append('\t\t\t\tTypedIndex: 0')
        L.append('\t\t\t}')
    if uvs:
        L.append('\t\t\tLayerElement:  {')
        L.append('\t\t\t\tType: "LayerElementUV"')
        L.append('\t\t\t\tTypedIndex: 0')
        L.append('\t\t\t}')
    L.append('\t\t}')
    L.append('\t}')
    L.append(f'\tModel: {MODEL_ID}, "Model::mesh", "Mesh" {{')
    L.append('\t\tVersion: 232')
    L.append('\t\tProperties70:  {')
    L.append('\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",0,0,0')
    L.append('\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0')
    L.append('\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1')
    L.append('\t\t\tP: "Visibility", "Visibility", "", "A",1')
    L.append('\t\t}')
    L.append('\t\tShading: T')
    L.append('\t\tCulling: "CullingOff"')
    L.append('\t}')
    for i, bn in enumerate(bone_names):
        safe = bn.replace('"', "'") or f'Bone{i}'
        L.append(f'\tModel: {BONE_BASE+i}, "Model::{safe}", "LimbNode" {{')
        L.append('\t\tVersion: 232')
        L.append('\t\tProperties70:  {')
        L.append('\t\t\tP: "Lcl Translation", "Lcl Translation", "", "A",0,0,0')
        L.append('\t\t\tP: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0')
        L.append('\t\t\tP: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1')
        L.append('\t\t}')
        L.append('\t\tShading: T')
        L.append('\t\tCulling: "CullingOff"')
        L.append('\t}')
    if weights:
        L.append(f'\tDeformer: {SKIN_ID}, "Deformer::skin", "Skin" {{')
        L.append('\t\tVersion: 101')
        L.append('\t\tLink_DeformAcuracy: 50')
        L.append('\t\tSkinningType: "Linear"')
        L.append('\t}')
        for b in range(n_bones):
            idxs = [i for i, lst in enumerate(weights)
                    if any(bi == b for bi, _ in lst)]
            if not idxs:
                idxs = [0]
                wts = [0.0]
            else:
                wts = [next((w for bi, w in weights[i] if bi == b), 0.0)
                       for i in idxs]
            tl_str = '1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1'
            if b < len(skel_bones):
                try:
                    bw = inverse(skel_bones[b]["matrix"])
                    tl_str = ','.join(_fmt_f(v) for v in bw)
                except FormatError:
                    pass
            L.append(f'\tDeformer: {CLUSTER_BASE+b}, '
                     f'"SubDeformer::cluster_{b}", "Cluster" {{')
            L.append('\t\tVersion: 100')
            L.append('\t\tUserData: "", ""')
            L.append(f'\t\tIndexes: *{len(idxs)} {{')
            L.append('\t\t\ta: ' + ','.join(str(i) for i in idxs))
            L.append('\t\t}')
            L.append(f'\t\tWeights: *{len(wts)} {{')
            L.append('\t\t\ta: ' + ','.join(_fmt_f(w) for w in wts))
            L.append('\t\t}')
            L.append('\t\tTransform: 1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1')
            L.append(f'\t\tTransformLink: {tl_str}')
            L.append('\t}')
    L.append('}')
    L.append('Connections:  {')
    L.append(f'\tC: "OO",{MODEL_ID},{ROOT_ID}')
    L.append(f'\tC: "OO",{GEOM_ID},{MODEL_ID}')
    if weights:
        L.append(f'\tC: "OO",{SKIN_ID},{GEOM_ID}')
        for b in range(n_bones):
            L.append(f'\tC: "OO",{CLUSTER_BASE+b},{SKIN_ID}')
            L.append(f'\tC: "OO",{CLUSTER_BASE+b},{BONE_BASE+b}')
        for i in range(n_bones):
            L.append(f'\tC: "OO",{BONE_BASE+i},{MODEL_ID}')
    L.append('}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


def export_mesh(m, out_stem, fmt='fbx', bone_names=None):
    """mesh → 任意支持格式。"""
    if fmt in ('stl', 'ply', 'gltf', 'dae'):
        if not m.lods:
            raise FormatError("无 LOD")
        geom = decode_lod(m.lods[0])
        if fmt == 'stl':
            if "JOINTS_0" in geom:
                print(f'  {_Ansi.RED}⚠{_Ansi.RESET} STL 不支持骨骼/UV，'
                      f'蒙皮数据将被丢弃')
            n = export_stl(geom, out_stem + '.stl', binary=True, name=m.name)
            print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} STL: {out_stem}.stl ({n} 三角形)')
        elif fmt == 'ply':
            if "JOINTS_0" in geom:
                print(f'  {_Ansi.RED}⚠{_Ansi.RESET} PLY 不支持骨骼，'
                      f'蒙皮数据将被丢弃')
            n = export_ply(geom, out_stem + '.ply', binary=True)
            print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} PLY: {out_stem}.ply ({n} 三角形)')
        elif fmt == 'gltf':
            sz = export_gltf(m, out_stem + '.gltf')
            print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} glTF: {out_stem}.gltf '
                  f'+ .bin ({sz//1024}KB)')
        elif fmt == 'dae':
            n = export_dae(m, out_stem + '.dae')
            print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} DAE: {out_stem}.dae ({n} 三角形)')
        return
    export_mesh_to_fbxobj(m, out_stem, fmt=fmt, bone_names=bone_names)


# ==================== 转换编排 ====================
def source_mesh(g):
    e = g.doc.get("extensions", {}).get(EXT)
    if not e:
        return None, None
    raw = g.view(e["bufferView"])
    require(digest(raw) == e["sha256"], "GLB内嵌mesh校验和不匹配")
    m = read_mesh(raw, "source" + suffix(set(e.get("filenameFlags", []))) + ".mesh")
    return m, e


def source_lods_unchanged(g, reference):
    nodes = g.doc.get("nodes", [])
    if len(nodes) < len(reference.lods):
        return False
    for i, node in enumerate(nodes):
        extras = node.get("extras", {})
        actual = {k: extras[k] for k in ("sky_lod", "sky_distance") if k in extras}
        expected = {}
        if i < len(reference.lods):
            distance = reference.lods[i].distance
            expected = {"sky_lod": i,
                        "sky_distance": distance if math.isfinite(distance) else "inf"}
        if actual != expected:
            return False
    return True


def convert_glb(g, filename, version, flags=None, compress=True, fps=None):
    ref, meta = source_mesh(g)
    flags = set(detect_flags(ref) if flags is None and ref
                else split_name(filename)[1] if flags is None else flags)
    require(flags <= set(FLAG_ORDER), f"未知参数: {flags-set(FLAG_ORDER)}")
    same = bool(ref and meta
                and glb_fingerprint(g.doc, g.bin) == meta["geometryHash"]
                and source_lods_unchanged(g, ref))
    if (same and version == ref.version
            and flags == set(meta["flags"])
            and bool(compress) == bool(ref.compressed)
            and fps is None):
        return ref.raw, flags, "完整原始数据恢复（字节一致）"
    geoms, bones, has_animations = import_glb(g)
    extra_geoms, _, _ = import_glb(g, extra_only=True)
    extra_roles = [extra_geometry_role(g.doc, n)
                   for n in g.doc.get("nodes", []) if "mesh" in n]
    extra_data = (encode_extra_geometry(extra_geoms[0]) if extra_geoms
                  else (b"" if any(role is not None for role in extra_roles)
                        else ref.extra_data if ref else b""))
    animation = _sky_animation
    if ref:
        changed_refs = (flags ^ detect_flags(ref)) & REFERENCE_ONLY
        require(not changed_refs, f"这些参数没有可用原始数据: {sorted(changed_refs)}")
    else:
        require(not (flags & (REFERENCE_ONLY | {"CompOcc"})),
                f"这些烘焙参数需要原始游戏参考: "
                f"{sorted(flags & (REFERENCE_ONLY | {'CompOcc'}))}")
    if bones:
        skeleton = new_skeleton(ref.name if ref else split_name(filename)[0], bones)
        reuse = bool(
            ref and ref.animated
            and len(bones) == len(read_skeleton(ref.skeleton)["bones"])
            and fps is None
            and ((not has_animations and not meta.get("animationClipsExported"))
                 or (meta and animation.fingerprint(g) == meta.get("animationHash"))))
        if reuse:
            sb = read_skeleton(ref.skeleton)["bones"]
            require([b["name"] for b in sb] == [b["name"] for b in bones],
                    "原始骨架名字/顺序已改变；不能复用原始动画")
            skeleton = ref.skeleton
            sv = read_skeleton(skeleton)["version"]
            offset = 4 + 64 + 12 + 1 + (4 if sv >= 10 else 0)
            out = bytearray(skeleton)
            for i, b in enumerate(bones):
                out[offset + i * 132:offset + (i + 1) * 132] = (
                    fixed_name(b["name"])
                    + struct.pack("<16fI", *transpose(b["matrix"]),
                                  b["parent"] + 1))
            skeleton = bytes(out)
            ga = animation.read_animation(skeleton)
            desired = 0 if "UncompAnim" in flags else 2
            if ga.clips and ga.compression != desired:
                skeleton = animation.encode_animation(skeleton, ga, desired)
        elif has_animations:
            ga = animation.from_glb(g, bones, fps)
            skeleton = animation.encode_animation(
                skeleton, ga, 0 if "UncompAnim" in flags else 2)
        if read_skeleton(skeleton)["frame_count"]:
            flags.discard("StripAnim")
        else:
            flags.add("StripAnim")
            flags.discard("UncompAnim")
    else:
        require(not has_animations, "动画未关联到可转换的几何")
        skeleton = b""
        flags.discard("StripAnim")
        flags.discard("UncompAnim")
    if geoms:
        flags.discard("StripGeo")
    else:
        flags.add("StripGeo")
        flags.difference_update({"CompOcc", "ZipPos", "ZipUvs",
                                 "StripUv13", "StripNorm", "ForceIdx32"})
    if geoms and (version <= 29
                  or any(len(gm["POSITION"]) > 65536
                         or max(gm["indices"], default=0) > 65535
                         for gm in geoms)):
        flags.add("ForceIdx32")
    else:
        if "ForceIdx32" not in flags:
            flags.discard("ForceIdx32")
    refs = ref.lods if ref else []
    lods = [build_lod(geom, version, flags, bones,
                      refs[i] if i < len(refs) else None)
            for i, geom in enumerate(geoms)]
    name = ref.name if ref else split_name(filename)[0]
    m = Mesh(version, name, int(bool(bones)), int(bool(extra_data)),
             ref.descriptors if ref and same else b"",
             lods, skeleton, extra_data,
             int(compress and version >= 27), b"", flags)
    require(not ref or not ref.descriptors or same,
            "该mesh含头部附加描述；修改后暂不能重新生成")
    return encode_mesh(m), flags, "已重新编码"


def convert_any_to_mesh(src, dst, name="", version=32, flags=None, compress=True):
    """任意支持格式 → mesh。"""
    flags = set(flags or [])
    with open(src, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(src)[1].lower()
    bones = []
    bone_names = []
    if ext == '.fbx':
        tree = (_parse_fbx_binary(raw) if raw[:20] == b'Kaydara FBX Binary'
                else _parse_fbx_ascii(raw.decode('utf-8', 'replace')))
        geom, bones, bone_names = _extract_fbx(tree)
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} FBX 解析完成 '
              f'({len(geom["POSITION"])}v, {len(bones)}骨)')
    elif ext == '.obj':
        geom = _parse_obj(raw.decode('utf-8', 'replace'))
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} OBJ 解析完成 '
              f'({len(geom["POSITION"])}v)')
    elif ext == '.stl':
        geom = parse_stl(raw)
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} STL 解析完成 '
              f'({len(geom["POSITION"])}v, 无UV/骨骼)')
    elif ext == '.ply':
        geom = parse_ply(raw)
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} PLY 解析完成 '
              f'({len(geom["POSITION"])}v)')
    elif ext == '.gltf':
        geom, bones, _ = parse_gltf(src)
        bone_names = [b['name'] for b in bones]
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} glTF 解析完成 '
              f'({len(geom["POSITION"])}v, {len(bones)}骨)')
    elif ext == '.dae':
        geom, bones, _ = parse_dae(raw.decode('utf-8', 'replace'))
        bone_names = [b['name'] for b in bones]
        print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} DAE 解析完成 '
              f'({len(geom["POSITION"])}v)')
    else:
        raise SystemExit(f'不支持输入: {ext}')
    if not bones:
        geom.pop("JOINTS_0", None)
        geom.pop("WEIGHTS_0", None)
    if version < 31:
        for bad in ("ZipPos", "ZipUvs"):
            flags.discard(bad)
    skeleton = b""
    if bones:
        skeleton = new_skeleton(name, [
            {"name": b["name"], "parent": b["parent"], "matrix": b["matrix"]}
            for b in bones])
    lod = build_lod(geom, version, flags, bones, None)
    m = Mesh(version, name, int(bool(bones)), 0, b"", [lod], skeleton, b"",
             int(compress and version >= 27), b"", flags)
    result = encode_mesh(m)
    atomic_write(dst, result)
    print(f'  {_Ansi.GREEN}✓{_Ansi.RESET} Mesh: {dst} '
          f'({len(result)//1024}KB ver=0x{version:02X} '
          f'LZ4={"开" if compress and version >= 27 else "关"})')
    return result


def convert_mesh_to_glb(src, dst):
    m = read_mesh(Path(src).read_bytes(), os.path.basename(src))
    result = export_glb(m)
    g = Glb(result)
    src_m, _ = source_mesh(g)
    require(src_m.raw == m.raw, "来源mesh保留失败")
    atomic_write(dst, result)
    return m


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.exists(), f"输出已存在，未覆盖: {path}")
    fd, tmp = tempfile.mkstemp(prefix=".sky-convert-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def convert_file(path, output=None, version=32, flags=None, compress=True,
                 fps=None, fmt='glb'):
    """统一转换入口。
    输入 mesh → 任意输出格式（fmt 指定）
    输入 glb → mesh
    输入其他模型格式 → mesh
    """
    path = Path(path)
    require(path.is_file(), f"文件不存在: {path}")
    ext = path.suffix.lower()
    if ext == ".mesh":
        m = read_mesh(path.read_bytes(), path.name)
        fs = detect_flags(m)
        base, _ = split_name(path)
        if fmt == 'glb':
            out = (Path(output) if output
                   else path.with_name(base + ("." + suffix(fs) if fs else "") + ".glb"))
            result = export_glb(m)
            g = Glb(result)
            src, meta = source_mesh(g)
            require(src.raw == m.raw, "来源mesh保留失败")
            atomic_write(out, result)
            print(f'版本: 0x{m.version:02X} | LOD: {len(m.lods)} | '
                  f'骨骼: {len(read_skeleton(m.skeleton)["bones"]) if m.animated else 0}')
            for w in m.warnings:
                print("核对:", w)
            if m.animated and read_skeleton(m.skeleton)["frame_count"]:
                print("游戏动画已导出为标准glTF动画轨道，原始数据同时保留。")
            if m.extra:
                eg = decode_extra_geometry(m.extra_data)
                print(f'附加几何: {len(eg["POSITION"])}顶点 / '
                      f'{len(eg["indices"]) // 3}三角形；'
                      f'GLB的"附加几何"场景可单独编辑。')
            print("完成:", out)
            return out
        # 其他格式
        stem = (Path(output).with_suffix('') if output
                else path.with_name(base))
        export_mesh(m, str(stem), fmt=fmt)
        return stem
    if ext == ".glb":
        g = Glb(path.read_bytes())
        result, fs, status = convert_glb(g, path.name, version, flags, compress, fps)
        base, _ = split_name(path)
        out = (Path(output) if output
               else path.with_name(base + suffix(fs) + ".mesh"))
        atomic_write(out, result)
        print(status)
        print("完成:", out)
        return out
    if ext in MESH_INPUT_EXTS:
        base = path.stem
        out = (Path(output) if output
               else path.with_name(base + suffix(flags or set()) + ".mesh"))
        convert_any_to_mesh(path, str(out), name=base, version=version,
                            flags=flags or set(),
                            compress=not (not compress))
        return out
    raise FormatError(f"不支持输入: {ext}")


# ==================== 界面工具 ====================
def _menu_color(text, color="white", bold=False, enabled=None):
    if enabled is None:
        enabled = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    if not enabled:
        return text
    code = {"white": "37", "green": "32", "red": "31"}[color]
    return f'\x1b[{"1;" if bold else ""}{code}m{text}\x1b[0m'


def _menu_width(text):
    return sum(
        0 if unicodedata.combining(ch)
        else 2 if unicodedata.east_asian_width(ch) in ("W", "F")
        else 1
        for ch in text)


def _menu_clip(text, width):
    if _menu_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _menu_width(out + ch) > max(0, width - 1):
            break
        out += ch
    return out + "…" if width else ""


def _menu_pad(text, width):
    text = _menu_clip(text, width)
    return text + " " * max(0, width - _menu_width(text))


def _menu_wrap(text, width, count=2):
    lines = []
    current = ""
    for ch in text:
        if _menu_width(current + ch) > width and current:
            lines.append(current)
            current = ""
            if len(lines) == count - 1:
                return lines + [_menu_clip(text[sum(len(x) for x in lines):], width)]
        current += ch
    return lines + [current]


class _MenuTerminal:
    """ANSI 字体样式和可选的 cbreak 输入，带回退。"""

    def __init__(self):
        self.raw = False
        self.color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.row_map = {}
        self.size = shutil.get_terminal_size((88, 28))

    def __enter__(self):
        if (os.name == "posix" and sys.stdin.isatty() and sys.stdout.isatty()
                and os.environ.get("TERM") != "dumb"):
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            try:
                tty.setcbreak(self.fd)
                self.raw = True
                sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H\x1b[?1003h\x1b[?1006h")
                sys.stdout.flush()
            except BaseException:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
                self.raw = False
                raise
        return self

    def __exit__(self, *args):
        if self.raw:
            import termios
            try:
                sys.stdout.write("\x1b[?1003l\x1b[?1006l\x1b[0m\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
            finally:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def draw(self, title, rows, focus, controls, message="", error=False, buffer=""):
        self.size = shutil.get_terminal_size((88, 28))
        self.width = max(12, min(94, self.size.columns - 8))
        page_size = max(1, self.size.lines - 12)
        start = focus // page_size * page_size
        visible = rows[start:start + page_size]
        paint = lambda s, color="white", bold=False: _menu_color(
            s, color, bold, self.color)
        lines = [
            "  " + paint(_menu_clip("SKY  ·  mesh ↔ 多格式", self.width), bold=True),
            "  " + paint(_menu_clip(title, self.width), bold=True),
        ]
        lines.append("  ┌" + "─" * (self.width + 2) + "┐")
        self.row_map = {}
        for i, row in enumerate(visible, start):
            color = ("red" if row.get("locked")
                     else "green" if row.get("selected") else "white")
            mark = "●" if row.get("selected") else "○"
            label_width = min(16, max(5, self.width - 22))
            text = f'{"›" if i == focus else " "} {i + 1:2}. {mark} ' \
                   f'{_menu_pad(row["label"], label_width)} {row.get("status", "")}'
            self.row_map[len(lines) + 1] = i
            lines.append("  │ " + paint(_menu_pad(text, self.width),
                                         color, i == focus) + " │")
        lines.append("  └" + "─" * (self.width + 2) + "┘")
        detail = rows[focus].get("description", "") if rows else ""
        lines.extend("  " + paint(s) for s in _menu_wrap(detail, self.width))
        lines.extend("  " + paint(_menu_clip(s, self.width)) for s in controls)
        if len(rows) > page_size:
            message = (message or
                       f"显示 {start + 1}–{start + len(visible)} / {len(rows)} 项；"
                       f"上下键或滚轮翻页")
        lines.append("  " + paint(_menu_clip(message or " ", self.width),
                                   "red" if error else "white"))
        if self.raw:
            lines.append("  " + paint(
                "输入 › " + _menu_clip(buffer, max(1, self.width - 7)), bold=True))
            sys.stdout.write("\x1b[H" + "\n".join(lines) + "\x1b[J")
            sys.stdout.flush()
        else:
            print("\n" + "\n".join(lines))

    def read_event(self):
        if not self.raw:
            return "line", input("  输入 › ")
        import select
        while not select.select([self.fd], [], [], 0.2)[0]:
            if shutil.get_terminal_size((88, 28)) != self.size:
                return "resize", None
        ch = os.read(self.fd, 1)
        if not ch or ch == b"\x04":
            raise EOFError
        if ch == b"\x03":
            raise KeyboardInterrupt
        if ch in (b"\r", b"\n"):
            return "enter", None
        if ch == b" ":
            return "space", None
        if ch in (b"\x7f", b"\x08"):
            return "backspace", None
        if ch == b"\t":
            return "down", None
        if ch != b"\x1b":
            return "char", ch.decode("ascii", "replace")
        sequence = b""
        if select.select([self.fd], [], [], 0.04)[0]:
            sequence += os.read(self.fd, 1)
        if sequence in (b"[", b"O"):
            while len(sequence) < 64 and select.select([self.fd], [], [], 0.04)[0]:
                part = os.read(self.fd, 1)
                sequence += part
                if 0x40 <= part[0] <= 0x7E:
                    break
        # 注: 左右键映射为上/下（列表只有一列，方便滚轮式浏览）
        key = {
            b"[A": "up", b"OA": "up",
            b"[B": "down", b"OB": "down",
            b"[C": "down", b"[D": "up",
            b"[H": "home", b"[1~": "home", b"OH": "home",
            b"[F": "end", b"[4~": "end", b"OF": "end",
            b"[5~": "page_up", b"[6~": "page_down",
        }.get(sequence)
        if key:
            return key, None
        mouse = re.fullmatch(rb"\[<(\d+);(\d+);(\d+)([Mm])", sequence)
        if mouse:
            button, x, y = (int(v) for v in mouse.groups()[:3])
            return "mouse", (button, x, y, mouse.group(4) == b"M")
        return "escape", None

    def navigate(self, event, focus, total):
        kind, value = event
        if kind in ("up", "down", "page_up", "page_down"):
            step = (max(1, self.size.lines - 12) if kind.startswith("page")
                    else 1)
            target = focus + (-step if kind in ("up", "page_up") else step)
            return (min(total - 1, max(0, target)) if kind.startswith("page")
                    else target % total), False
        if kind == "home":
            return 0, False
        if kind == "end":
            return total - 1, False
        if kind == "mouse":
            button, x, y, pressed = value
            if button & 64:
                return (min(total - 1, max(0, focus + (-1 if button & 1 == 0 else 1))),
                        False)
            if 3 <= x <= self.width + 6 and y in self.row_map:
                return self.row_map[y], pressed and button & 35 == 0
        return focus, False


def _menu_choice(title, labels, descriptions, default=0, back="q",
                 values=None, message=""):
    focus, buffer = default, ""
    with _MenuTerminal() as ui:
        while True:
            rows = [{"label": label, "selected": i == focus,
                     "status": "当前选择" if i == focus else "",
                     "description": descriptions[i]}
                    for i, label in enumerate(labels)]
            ui.draw(title, rows, focus,
                    ["↑ / ↓ 定位  ·  输入编号或值",
                     f"回车确认  ·  {back} {'返回' if back == 'b' else '退出'}"],
                    message, error=bool(message), buffer=buffer)
            event = ui.read_event()
            kind, value = event
            old_focus = focus
            focus, clicked = ui.navigate(event, focus, len(rows))
            if (kind in ("up", "down", "home", "end", "page_up", "page_down")
                    or clicked or focus != old_focus):
                buffer = ""
            if kind == "line":
                return value.strip()
            if kind == "escape":
                return back
            if kind == "enter":
                return buffer.strip() or str(focus + 1)
            if kind == "backspace":
                buffer = buffer[:-1]
            elif kind == "char":
                if value.lower() == back and not buffer:
                    return back
                if value in "0123456789xXabcdefABCDEF":
                    buffer += value
                else:
                    message = "请输入菜单编号" if values is None else "请输入菜单编号或版本号"
            if buffer:
                try:
                    number = (int(buffer, 0) if buffer.lower().startswith("0x")
                              else int(buffer))
                    if buffer.isdigit() and 1 <= number <= len(rows):
                        focus = number - 1
                    elif values and number in values:
                        focus = values.index(number)
                except ValueError:
                    pass


# ==================== flags_menu（多选 + TUI） ====================
def flags_menu(g, filename, initial=None):
    ref, meta = source_mesh(g)
    geoms, bones, animations = import_glb(g)
    selected = detect_flags(ref) if ref else split_name(filename)[1]
    selected = set(selected)
    if initial is not None:
        selected = set(initial)
    locked = {}
    if bones:
        keep_legacy = (ref and ref.animated
                       and not meta.get("animationClipsExported")
                       and read_skeleton(ref.skeleton)["frame_count"])
        locked["StripAnim"] = not (animations or keep_legacy)
    else:
        locked["StripAnim"] = False
    locked["StripGeo"] = not bool(geoms)
    if not geoms:
        locked.update({f: False for f in
                       ("CompOcc", "ZipPos", "ZipUvs", "StripUv13",
                        "StripNorm", "ForceIdx32")})
    if not bones or locked["StripAnim"]:
        locked["UncompAnim"] = False
    if any(len(x["POSITION"]) > 65536
           or max(x["indices"], default=0) > 65535 for x in geoms):
        locked["ForceIdx32"] = True
    for f in REFERENCE_ONLY:
        if ref and f in detect_flags(ref):
            locked[f] = True
    for f, v in locked.items():
        if v:
            selected.add(f)
        else:
            selected.discard(f)
    unavailable = (REFERENCE_ONLY | {"CompOcc"}) - (detect_flags(ref) if ref else set())
    for f in unavailable:
        selected.discard(f)
    focus = next((i for i, f in enumerate(FLAG_ORDER)
                  if f not in locked and f not in unavailable), 0)
    buffer, message, error = "", "", False

    def toggle(answer):
        nonlocal focus, message, error
        try:
            items = [int(x) for x in re.split(r"[ ,，]+", answer.strip())]
        except ValueError:
            message, error = "请输入菜单编号；只有 y 确认后进入版本选择", True
            return
        notices = []
        error = False
        for i in items:
            if not 1 <= i <= len(FLAG_ORDER):
                notices.append(f"无效编号：{i}")
                error = True
                continue
            focus = i - 1
            f = FLAG_ORDER[focus]
            if f in locked or f in unavailable:
                notices.append(f"{f} 已锁定，不能修改")
                error = True
            else:
                if f in selected:
                    selected.remove(f)
                else:
                    selected.add(f)
                notices.append(f'{f} {"已选择" if f in selected else "已取消"}')
        message = "；".join(notices)

    with _MenuTerminal() as ui:
        while True:
            rows = []
            for f in FLAG_ORDER:
                status = ("来源保留 · 锁定" if f in locked and f in REFERENCE_ONLY
                          else "结构固定 · 锁定" if f in locked
                          else "无来源 · 锁定" if f in unavailable
                          else "已选择" if f in selected else "未选择")
                rows.append({
                    "label": f,
                    "selected": f in selected,
                    "locked": f in locked or f in unavailable,
                    "status": status,
                    "description": f"{f}：{FLAG_DESCRIPTIONS[f]}",
                })
            ui.draw(
                "GLB → mesh  /  参数多选",
                rows, focus,
                ["编号 + 空格/回车切换  ·  ↑↓定位，空格切换",
                 "回车只刷新  ·  y 确认  ·  白色未选 / 绿色已选 / 红色锁定"],
                message, error, buffer)
            event = ui.read_event()
            kind, value = event
            old_focus = focus
            focus, clicked = ui.navigate(event, focus, len(rows))
            if (kind in ("up", "down", "home", "end", "page_up", "page_down")
                    or clicked or focus != old_focus):
                buffer = ""
            if clicked:
                toggle(str(focus + 1))
            if kind == "line":
                stripped = value.strip()
                if stripped.lower() == "y":
                    return selected
                if stripped:
                    toggle(stripped)
                else:
                    message, error = "状态已刷新；输入 y 才会进入版本选择", False
            elif kind == "char" and value.lower() == "y":
                if buffer:
                    toggle(buffer)
                    buffer = ""
                    if error:
                        continue
                return selected
            elif kind == "enter":
                if buffer:
                    toggle(buffer)
                    buffer = ""
                else:
                    message, error = "状态已刷新；按 y 才会进入版本选择", False
            elif kind == "space":
                # 修复: 空格键总是切换当前焦点项（清空 buffer）
                if buffer:
                    buffer = ""
                toggle(str(focus + 1))
            elif kind == "backspace":
                buffer = buffer[:-1]
            elif kind == "char":
                if value.isdigit() or value == ",":
                    buffer += value
                    if buffer.isdigit() and 1 <= int(buffer) <= len(rows):
                        focus = int(buffer) - 1
                else:
                    message, error = "请输入编号，用空格切换；按 y 确认", True
            elif kind == "escape":
                buffer = ""
                message, error = "已清空输入；按 y 确认参数", False


# ==================== version_menu ====================
def version_menu(flags):
    notice = ""
    while True:
        labels = []
        descriptions = []
        for i, v in enumerate(VERSIONS, 1):
            note = ("（旧布局，32位索引，不支持压缩属性）" if v <= 28
                    else "（32位索引）" if v == 29
                    else "（APK全部样本版本）" if v == 32 else "")
            labels.append(f"0x{v:02X} / {v}")
            descriptions.append(f"0x{v:02X}：{note or '支持外层LZ4与可选属性'}")
        a = _menu_choice(
            "GLB → mesh  /  版本单选",
            labels, descriptions, 7, "b", list(VERSIONS), notice)
        if a.lower() == "b":
            return None
        try:
            v = (32 if not a
                 else (VERSIONS[int(a) - 1] if a.isdigit() and 1 <= int(a) <= 8
                       else int(a, 0)))
        except (ValueError, IndexError):
            notice = "无效版本，请输入菜单编号或 0x19–0x20"
            continue
        if v not in VERSIONS:
            notice = "版本不受该 so 正常支持，请选择 0x19–0x20"
            continue
        if v < 29 and flags & {"ZipPos", "ZipUvs", "StripNorm"}:
            notice = "请先返回参数表取消 ZipPos / ZipUvs / StripNorm"
            continue
        return v


# ==================== 交互式入口 ====================
def _prompt_file_plain(cands, label):
    """多选文件（普通 input，非 TUI）。"""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    for i, c in enumerate(cands, 1):
        print(f"  {_Ansi.GREEN}{i:2}{_Ansi.RESET}) {os.path.relpath(c)}")
    print(f"  {_Ansi.GREEN}all{_Ansi.RESET}) 全部")
    print(f"{'='*60}")
    r = input("  选择（编号/范围/路径，回车=1）› ").strip()
    if not r:
        return [cands[0]]
    if r.lower() in ('all', 'a', '*', '全部'):
        return list(cands)
    picked = []
    for p in r.replace(',', ' ').split():
        if '-' in p and not os.path.exists(p):
            try:
                a, b = map(int, p.split('-', 1))
                picked += [cands[k - 1] for k in range(min(a, b), max(a, b) + 1)
                           if 1 <= k <= len(cands)]
            except ValueError:
                pass
        elif p.isdigit() and 1 <= int(p) <= len(cands):
            picked.append(cands[int(p) - 1])
        elif os.path.exists(p):
            picked.append(p)
    return picked or [cands[0]]


def _prompt_outdir_plain(src):
    sdir = os.path.dirname(os.path.abspath(src))
    cands = [c for c in (sdir, os.path.join(os.path.dirname(sdir), 'output'),
                         os.getcwd()) if os.path.isdir(c)]
    print(f"\n{'='*60}")
    print(f"  输出目录")
    print(f"{'='*60}")
    for i, c in enumerate(cands, 1):
        print(f"  {_Ansi.GREEN}{i:2}{_Ansi.RESET}) {c}")
    print(f"{'='*60}")
    r = input("  选择（编号/路径，回车=1）› ").strip()
    if not r:
        return cands[0]
    if r.isdigit() and 1 <= int(r) <= len(cands):
        return cands[int(r) - 1]
    os.makedirs(r, exist_ok=True)
    return r


def _find_files(base='.', depth=2, exts=SUPPORTED_IN):
    found = []
    base = os.path.abspath(base)
    skip = {'.git', '__pycache__', 'node_modules', '.sessions'}
    for root, dirs, files in os.walk(base):
        rel = root[len(base):].lstrip(os.sep)
        if rel.count(os.sep) >= depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in sorted(files):
            if fn.lower().endswith(exts):
                found.append(os.path.join(root, fn))
    return found


def interactive_menu():
    notice = ""
    while True:
        choice = _menu_choice(
            "主菜单  /  sky_mesh_tool v5",
            ["任意模型 → mesh  (FBX/OBJ/STL/PLY/glTF/DAE)",
             "GLB → mesh  (含参数/动画)",
             "mesh → GLB",
             "mesh → 任意格式  (FBX/OBJ/STL/PLY/glTF/DAE)",
             "检查 mesh 信息"],
            ["将 FBX/OBJ/STL/PLY/glTF/DAE 转换为游戏 .mesh",
             "将 GLB 转换为游戏 .mesh（可选参数和版本）",
             "将游戏 .mesh 转换为 GLB（保留原始数据）",
             "将游戏 .mesh 导出为多种通用格式",
             "显示 mesh 的结构/参数/警告"],
            message=notice).lower()
        if choice in ("q", "quit", "退出"):
            return
        notice = ""
        try:
            if choice == "1":
                srcs = _find_files('.', 2, MESH_INPUT_EXTS)
                if not srcs:
                    notice = "当前目录未找到支持的模型文件"
                    continue
                picked = _prompt_file_plain(srcs, "模型文件")
                outdir = _prompt_outdir_plain(picked[0])
                name = input("  模型名（回车=文件名）› ").strip()
                ver = version_menu(set())
                flags_sel = flags_menu_for_new(ver)
                for src in picked:
                    base = name or os.path.splitext(os.path.basename(src))[0]
                    dst = os.path.join(outdir,
                                       base + suffix(flags_sel) + '.mesh')
                    convert_any_to_mesh(src, dst, name=base, version=ver,
                                        flags=flags_sel, compress=ver >= 27)
            elif choice == "2":
                srcs = _find_files('.', 2, ('.glb',))
                if not srcs:
                    notice = "当前目录未找到 .glb"
                    continue
                picked = _prompt_file_plain(srcs, "GLB 文件")
                outdir = _prompt_outdir_plain(picked[0])
                for src in picked:
                    g = Glb(Path(src).read_bytes())
                    fs = None
                    while True:
                        fs = flags_menu(g, os.path.basename(src), fs)
                        v = version_menu(fs)
                        if v is not None:
                            break
                    result, fs_out, status = convert_glb(
                        g, os.path.basename(src), v, fs, v >= 27, None)
                    base, _ = split_name(src)
                    dst = os.path.join(outdir, base + suffix(fs_out) + '.mesh')
                    atomic_write(dst, result)
                    print(f'{_Ansi.GREEN}✓{_Ansi.RESET} {status} → {dst}')
            elif choice == "3":
                srcs = _find_files('.', 2, ('.mesh',))
                if not srcs:
                    notice = "当前目录未找到 .mesh"
                    continue
                picked = _prompt_file_plain(srcs, "Mesh 文件")
                outdir = _prompt_outdir_plain(picked[0])
                for src in picked:
                    m = read_mesh(Path(src).read_bytes(), os.path.basename(src))
                    fs = detect_flags(m)
                    base, _ = split_name(src)
                    dst = os.path.join(outdir, base + suffix(fs) + '.glb')
                    convert_mesh_to_glb(src, dst)
                    print(f'{_Ansi.GREEN}✓{_Ansi.RESET} → {dst}')
            elif choice == "4":
                srcs = _find_files('.', 2, ('.mesh',))
                if not srcs:
                    notice = "当前目录未找到 .mesh"
                    continue
                picked = _prompt_file_plain(srcs, "Mesh 文件")
                outdir = _prompt_outdir_plain(picked[0])
                fmt = _menu_choice(
                    "选择输出格式",
                    ["FBX (含蒙皮)", "OBJ (含权重侧车)",
                     "STL (仅几何)", "PLY (含颜色/UV)",
                     "glTF (分离式, 含动画)", "DAE (Collada)",
                     "FBX + OBJ 同时输出"],
                    ["ASCII FBX 7.4，含 Skin/Cluster",
                     "OBJ + weights.json 侧车文件",
                     "STL 二进制，无 UV/骨骼",
                     "PLY 二进制，含顶点颜色",
                     "glTF + .bin，含动画/骨骼",
                     "Collada 1.4.1（骨骼平铺）",
                     "同时导出 FBX 和 OBJ"],
                    default=0).lower()
                fmt_map = {"1": "fbx", "2": "obj", "3": "stl", "4": "ply",
                           "5": "gltf", "6": "dae", "7": "both"}
                if fmt in fmt_map:
                    fmt = fmt_map[fmt]
                if fmt not in MESH_OUTPUT_EXTS:
                    notice = f"未知格式: {fmt}"
                    continue
                for src in picked:
                    m = read_mesh(Path(src).read_bytes(), os.path.basename(src))
                    base = os.path.splitext(os.path.basename(src))[0]
                    stem = os.path.join(outdir, base)
                    export_mesh(m, stem, fmt=fmt)
            elif choice == "5":
                print("\n  输入 mesh 路径:")
                p = input("  › ").strip().strip("\"'")
                m = read_mesh(Path(p).read_bytes(), os.path.basename(p))
                print(json.dumps({
                    "version": f"0x{m.version:02X} / {m.version}",
                    "name": m.name,
                    "compressed": bool(m.compressed),
                    "animated": bool(m.animated),
                    "extra": bool(m.extra),
                    "flags": sorted(detect_flags(m)),
                    "filenameFlags": sorted(m.filename_flags),
                    "warnings": m.warnings,
                    "lods": [{"vertices": l.vertices,
                              "triangles": l.indices_count // 3,
                              "idx32": bool(l.idx32),
                              "zip": l.zipped} for l in m.lods],
                }, ensure_ascii=False, indent=2))
            else:
                notice = f"请输入 1-5：{choice}"
                continue
        except (OSError, ValueError, KeyError, IndexError, struct.error,
                ET.ParseError) as e:
            notice = f"失败: {e}"
            print(f'{_Ansi.RED}✗{_Ansi.RESET} {notice}')
        input("\n回车返回主菜单...")


def flags_menu_for_new(version):
    """新建 mesh 时的参数选择（无参考数据，仅可选结构无关项）。"""
    class _FakeGlb:
        doc = {"meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
               "nodes": [{"mesh": 0}]}
        bin = b""
        def accessor(self, i):
            return [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    selected = set()
    locked = {"StripGeo": False, "StripAnim": False, "UncompAnim": False}
    unavailable = set(REFERENCE_ONLY) | {"CompOcc"}
    if version < 29:
        unavailable |= {"ZipPos", "ZipUvs", "StripNorm"}
    return flags_menu(_FakeGlb(), "new", initial=selected) if False else _choose_flags_direct(version)


def _choose_flags_direct(version):
    """直接的参数多选（不依赖 GLB 上下文）。"""
    selected = set()
    unavailable = set(REFERENCE_ONLY) | {"CompOcc"}
    if version < 29:
        unavailable |= {"ZipPos", "ZipUvs", "StripNorm"}
    locked = {}
    focus = 0
    buffer, message, error = "", "", False

    def toggle(answer):
        nonlocal focus, message, error
        try:
            items = [int(x) for x in re.split(r"[ ,，]+", answer.strip())]
        except ValueError:
            message, error = "请输入菜单编号", True
            return
        notices = []
        error = False
        for i in items:
            if not 1 <= i <= len(FLAG_ORDER):
                notices.append(f"无效编号：{i}")
                error = True
                continue
            focus = i - 1
            f = FLAG_ORDER[focus]
            if f in unavailable:
                notices.append(f"{f} 当前不可选")
                error = True
            else:
                if f in selected:
                    selected.remove(f)
                else:
                    selected.add(f)
                notices.append(f'{f} {"已选择" if f in selected else "已取消"}')
        message = "；".join(notices)

    with _MenuTerminal() as ui:
        while True:
            rows = []
            for f in FLAG_ORDER:
                status = ("无来源 · 锁定" if f in unavailable
                          else "已选择" if f in selected else "未选择")
                rows.append({
                    "label": f,
                    "selected": f in selected,
                    "locked": f in unavailable,
                    "status": status,
                    "description": f"{f}：{FLAG_DESCRIPTIONS[f]}",
                })
            ui.draw(
                "新建 mesh  /  参数多选",
                rows, focus,
                ["编号 + 空格切换  ·  ↑↓定位",
                 "回车刷新  ·  y 确认"],
                message, error, buffer)
            event = ui.read_event()
            kind, value = event
            old_focus = focus
            focus, clicked = ui.navigate(event, focus, len(rows))
            if (kind in ("up", "down", "home", "end", "page_up", "page_down")
                    or clicked or focus != old_focus):
                buffer = ""
            if clicked:
                toggle(str(focus + 1))
            if kind == "line":
                if value.strip().lower() == "y":
                    return selected
                if value.strip():
                    toggle(value.strip())
            elif kind == "char" and value.lower() == "y":
                if buffer:
                    toggle(buffer)
                    buffer = ""
                    if error:
                        continue
                return selected
            elif kind == "enter":
                if buffer:
                    toggle(buffer)
                    buffer = ""
            elif kind == "space":
                if buffer:
                    buffer = ""
                toggle(str(focus + 1))
            elif kind == "backspace":
                buffer = buffer[:-1]
            elif kind == "char":
                if value.isdigit() or value == ",":
                    buffer += value
                    if buffer.isdigit() and 1 <= int(buffer) <= len(rows):
                        focus = int(buffer) - 1


# ==================== CLI ====================
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Sky mesh 全能转换器 v5（FBX/OBJ/STL/PLY/glTF/DAE/GLB ↔ mesh）")
    p.add_argument("input", nargs="?")
    p.add_argument("-o", "--output")
    p.add_argument("--version", type=lambda x: int(x, 0),
                   choices=VERSIONS, default=32)
    p.add_argument("--flags", help="逗号分隔；空=取消所有；不指定=自动")
    p.add_argument("--uncompressed", action="store_true")
    p.add_argument("--fps", type=int)
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--fmt", default="glb",
                   choices=MESH_OUTPUT_EXTS + ("glb",))
    a = p.parse_args(argv)
    try:
        if not a.input:
            interactive_menu()
            return 0
        if a.inspect:
            m = read_mesh(Path(a.input).read_bytes(), a.input)
            print(json.dumps({
                "version": f"0x{m.version:02X}",
                "name": m.name,
                "compressed": bool(m.compressed),
                "animated": bool(m.animated),
                "extra": bool(m.extra),
                "flags": sorted(detect_flags(m)),
                "lods": [{"vertices": l.vertices,
                          "triangles": l.indices_count // 3}
                         for l in m.lods],
                "warnings": m.warnings,
            }, ensure_ascii=False, indent=2))
            return 0
        fs = (None if a.flags is None
              else {s.strip().lstrip("_") for s in a.flags.split(",") if s.strip()})
        if a.fps is not None:
            require(1 <= a.fps <= 240, "fps 需 1-240")
        src = Path(a.input)
        require(src.is_file(), f"文件不存在: {src}")
        ext = src.suffix.lower()
        if ext == ".mesh":
            if a.fmt == "glb":
                m = read_mesh(src.read_bytes(), src.name)
                fs_det = detect_flags(m)
                base, _ = split_name(src)
                out = (Path(a.output) if a.output
                       else src.with_name(base + suffix(fs_det) + ".glb"))
                convert_mesh_to_glb(src, out)
            else:
                m = read_mesh(src.read_bytes(), src.name)
                stem = (Path(a.output).with_suffix("") if a.output
                        else src.with_suffix(""))
                export_mesh(m, str(stem), fmt=a.fmt)
                out = stem
            print(f"{_Ansi.GREEN}完成:{_Ansi.RESET} {out}")
            return 0
        if ext == ".glb":
            g = Glb(src.read_bytes())
            result, fs_out, status = convert_glb(g, src.name, a.version,
                                                 fs, not a.uncompressed, a.fps)
            base, _ = split_name(src)
            out = (Path(a.output) if a.output
                   else src.with_name(base + suffix(fs_out) + ".mesh"))
            atomic_write(out, result)
            print(f"{_Ansi.GREEN}✓{_Ansi.RESET} {status} → {out}")
            return 0
        if ext in MESH_INPUT_EXTS:
            base = src.stem
            out = (Path(a.output) if a.output
                   else src.with_name(base + suffix(fs or set()) + ".mesh"))
            convert_any_to_mesh(src, str(out), name=base, version=a.version,
                                flags=fs or set(),
                                compress=not a.uncompressed)
            return 0
        print(f"不支持: {ext}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, IndexError, struct.error,
            ET.ParseError) as e:
        print(f"{_Ansi.RED}错误:{_Ansi.RESET} {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已退出")
        return 0


# ==================== 动画命名空间 ====================
_sky_animation = SimpleNamespace(
    Clip=Clip,
    Animation=Animation,
    unit_quaternion=unit_quaternion,
    read_trs=read_trs,
    read_animation=read_animation,
    add_glb_animations=add_glb_animations,
    pack_trs=pack_trs,
    encode_animation=encode_animation,
    decompose_trs=decompose_trs,
    slerp=slerp,
    sampler_value=sampler_value,
    from_glb=from_glb,
    fingerprint=animation_fingerprint,
)


if __name__ == "__main__":
    sys.exit(main())