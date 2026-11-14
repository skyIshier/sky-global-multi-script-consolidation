#!/usr/bin/env python3
"""bank2audio.py - FMOD .bank 音频提取工具（交互菜单版）"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave


def ensure_deps(need_ffmpeg=False):
    missing = []
    if not shutil.which("vgmstream-cli"):
        missing.append("vgmstream")
    if need_ffmpeg and not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if missing:
        print(f"[*] 安装依赖: {' '.join(missing)} ...")
        for pkg in missing:
            subprocess.run(["pkg", "install", pkg, "-y"], capture_output=True)
    if not shutil.which("vgmstream-cli"):
        sys.exit("[!] vgmstream-cli 安装失败")


def wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return None


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s)


def analyze_bank(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if len(data) < 12:
        return None
    return {"size": len(data), "fsb5": data.count(b"FSB5"),
            "fev": data[:4] == b"RIFF" and data[8:12] == b"FEV "}


# ─────────────────────────── 功能 1：列出统计 ───────────────────────────
def menu_list():
    d = ask("bank 目录（回车=当前）: ") or os.getcwd()
    banks = sorted(glob.glob(os.path.join(d, "*.bank")))
    if not banks:
        print(f"[!] {d} 下没有 .bank")
        return
    print(f"\n{len(banks)} 个 bank\n")
    print(f"{'Bank':<48} {'Size':>10} {'FSB5':>5} {'Type':>6}")
    print("-" * 72)
    audio = 0
    for b in banks:
        s = analyze_bank(b)
        if s is None:
            continue
        n = os.path.basename(b)
        t = "FSB5" if (n.endswith(".streams.bank") or n.endswith(".assets.bank")) \
            else ("FEV" if s["fev"] else "?")
        if s["fsb5"] > 0:
            audio += 1
        print(f"{n:<48} {s['size']:>10,} {s['fsb5']:>5} {t:>6}")
    print(f"\n{audio} 个含 FSB5，{len(banks)-audio} 个无音频")


# ─────────────────────────── 功能 2：提取 ───────────────────────────
def menu_extract():
    d = ask("bank 目录（回车=当前）: ") or os.getcwd()
    if not os.path.isdir(d):
        print(f"[!] 目录不存在: {d}")
        return
    out = ask(f"输出目录（回车={os.path.join(d,'out')}）: ") or os.path.join(d, "out")
    min_s = ask("最短时长秒（回车=0）: ")
    max_s = ask("最长时长秒（回车=不限）: ")
    pat = ask("只保留文件名匹配（正则，回车=全部）: ")
    bpat = ask("只处理 bank 匹配（正则，回车=全部）: ")
    mp3 = ask("转成 MP3？(y/N): ").lower() == "y"

    min_sec = float(min_s) if min_s else 0.0
    max_sec = float(max_s) if max_s else float("inf")
    ensure_deps(need_ffmpeg=mp3)

    banks = sorted(glob.glob(os.path.join(d, "*.bank")))
    if bpat:
        bp = re.compile(bpat, re.I)
        banks = [b for b in banks if bp.search(os.path.basename(b))]
    if not banks:
        print("[!] 没有符合条件的 bank")
        return

    os.makedirs(out, exist_ok=True)
    pat_re = re.compile(pat, re.I) if pat else None
    total = 0
    skipped = 0

    for i, bank in enumerate(banks, 1):
        name = os.path.basename(bank)
        info = analyze_bank(bank)
        if info is None:
            continue
        if info["fsb5"] == 0:
            skipped += 1
            continue
        prefix = safe_name(name.replace(".bank", ""))
        print(f"[{i}/{len(banks)}] {name}  ({info['fsb5']} FSB5)")
        tmp = tempfile.mkdtemp(prefix="vgm_")
        try:
            r = subprocess.run(
                ["vgmstream-cli", "-S", "0", "-o", os.path.join(tmp, "?n.wav"), bank],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  └ 失败: {r.stderr.strip()[:80]}")
                continue
            kept = 0
            for fn in sorted(os.listdir(tmp)):
                if not fn.lower().endswith(".wav"):
                    continue
                p = os.path.join(tmp, fn)
                if pat_re and not pat_re.search(fn):
                    continue
                dur = wav_duration(p)
                if dur is None or dur < min_sec or dur > max_sec:
                    continue
                base = f"{prefix}__{safe_name(fn)}"
                dst = os.path.join(out, base)
                if os.path.exists(dst):
                    stem, ext = os.path.splitext(base)
                    k = 1
                    while os.path.exists(dst):
                        dst = os.path.join(out, f"{stem}_{k}{ext}")
                        k += 1
                shutil.copy2(p, dst)
                kept += 1
                if mp3:
                    mp = dst[:-4] + ".mp3"
                    rr = subprocess.run(
                        ["ffmpeg", "-y", "-i", dst, "-codec:a", "libmp3lame",
                         "-qscale:a", "2", mp], capture_output=True)
                    if rr.returncode == 0:
                        os.remove(dst)
            total += kept
            print(f"  └ 保留 {kept}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n完成：{total} 个音频 → {out}")
    if skipped:
        print(f"跳过 {skipped} 个无音频 bank")


# ─────────────────────────── 功能 3：搜索输出 ───────────────────────────
def menu_search():
    d = ask("搜索目录（回车=./out）: ") or "./out"
    if not os.path.isdir(d):
        print(f"[!] 目录不存在: {d}")
        return
    pat = ask("文件名正则（如 click|tap|Bobo）: ")
    if not pat:
        return
    pr = re.compile(pat, re.I)
    hits = []
    for root, _, files in os.walk(d):
        for fn in files:
            if pr.search(fn):
                full = os.path.join(root, fn)
                hits.append((fn, os.path.getsize(full),
                             wav_duration(full) if fn.endswith(".wav") else None))
    if not hits:
        print(f"[!] 没有匹配 '{pat}' 的文件")
        return
    hits.sort()
    print(f"\n{len(hits)} 个匹配：\n")
    for fn, sz, dur in hits:
        ds = f"{dur:.2f}s" if dur is not None else "?"
        print(f"  {fn:<60} {sz:>9,}B  {ds}")


# ─────────────────────────── 功能 4：只解一个 bank ───────────────────────────
def menu_one():
    d = ask("bank 目录（回车=当前）: ") or os.getcwd()
    banks = sorted(glob.glob(os.path.join(d, "*.bank")))
    if not banks:
        print(f"[!] {d} 下没有 .bank")
        return
    for i, b in enumerate(banks, 1):
        print(f"  {i:3d}. {os.path.basename(b)}")
    c = ask("\n输入编号: ")
    if not c.isdigit() or not (1 <= int(c) <= len(banks)):
        print("[!] 无效编号")
        return
    bank = banks[int(c) - 1]
    out = ask(f"输出目录（回车={os.path.join(d,'out')}）: ") or os.path.join(d, "out")
    pat = ask("只保留文件名匹配（正则，回车=全部）: ")
    os.makedirs(out, exist_ok=True)
    pat_re = re.compile(pat, re.I) if pat else None
    tmp = tempfile.mkdtemp(prefix="vgm_")
    prefix = safe_name(os.path.basename(bank).replace(".bank", ""))
    try:
        print(f"[*] 提取 {os.path.basename(bank)} ...")
        r = subprocess.run(
            ["vgmstream-cli", "-S", "0", "-o", os.path.join(tmp, "?n.wav"), bank],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[!] 失败: {r.stderr.strip()[:120]}")
            return
        kept = 0
        for fn in sorted(os.listdir(tmp)):
            if not fn.lower().endswith(".wav"):
                continue
            if pat_re and not pat_re.search(fn):
                continue
            shutil.copy2(os.path.join(tmp, fn),
                         os.path.join(out, f"{prefix}__{safe_name(fn)}"))
            kept += 1
        print(f"[√] 保留 {kept} 个 → {out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─────────────────────────── 工具 ───────────────────────────
def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def main():
    print("=" * 56)
    print("  FMOD .bank 音频提取工具")
    print("=" * 56)
    while True:
        print("\n  1. 列出 bank 统计（看哪些有音频）")
        print("  2. 批量提取（可设时长/搜索/MP3）")
        print("  3. 在输出目录搜索文件")
        print("  4. 提取单个 bank")
        print("  0. 退出")
        c = ask("\n选择: ")
        if c == "0":
            break
        elif c == "1":
            menu_list()
        elif c == "2":
            menu_extract()
        elif c == "3":
            menu_search()
        elif c == "4":
            menu_one()
        else:
            print("[!] 无效选择")


if __name__ == "__main__":
    main()