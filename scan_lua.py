#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正向扫描：Lua 脚本里写了要用的资源，磁盘上到底有没有。

用法：

    python scan_lua.py --root <UE 工程根>
    python scan_lua.py --root <UE 工程根> --quiet

产物（默认写到 <report_dir>）：

    lua_asset_refs.csv    全部引用明细（逐行）
    missing_refs.csv      未命中资源（按引用次数排序）
    summary.md            人读摘要 + 环境自检

零第三方依赖，仅标准库，Python 3.8+。

设计说明（为什么不用一行正则扫全文件）：

  1. Lua 注释里也会出现包路径（文档示例、被注释掉的旧代码），直接正则扫
     会产出大量误报。本脚本用手写词法状态机剥离行注释与块注释，只采集
     真正的字符串字面量。
  2. 资源引用的形态不统一：可能是 "/Game/A/B.B_C"（蓝图生成类，带 _C 后缀）、
     可能是 "/Game/A/L_Level"（关卡，实际落在 .umap）、可能被
     "/Script/Engine.Texture2D'/Game/...'" 这样的外层包裹。统一在归一化阶段
     处理，并保留 note 供人工复核。
  3. 字符串拼接（"/Game/UI/" .. name .. "_C"）静态无法求值。这类只报为
     「疑似拼接」，绝不当作死引用上报，避免制造噪音。
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import refcheck_lib as lib  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASSET_EXTS = lib.ASSET_EXTS
MISSING_RATIO_WARN = 0.25   # 唯一路径未命中率超过此值 -> 先怀疑环境不完整


# --------------------------------------------------------------------------
# Lua 词法：只提取字符串字面量，跳过注释
# --------------------------------------------------------------------------


def extract_string_literals(src, mount="/Game/"):
    """扫描 Lua 源码，返回 (literals, comment_paths)。

    literals      [(line, value, trailing)]  代码中的字符串字面量（含 [[ ]] 长字符串）
                    trailing 是字面量结束后紧跟的一小段源码，用于检测
                    ".../" .. var 这种「拼接发生在字符串外面」的写法
    comment_paths [(line, path)]             注释里的包路径（仅统计噪音量）
    """
    path_re = lib._path_re_str(mount)
    n = len(src)
    i = 0
    line = 1
    literals = []
    comment_paths = []

    while i < n:
        ch = src[i]

        if ch == "\n":
            line += 1
            i += 1
            continue

        # ---- 注释：行注释 或 块注释 --[[ ]] / --[==[ ]==] ----
        if ch == "-" and i + 1 < n and src[i + 1] == "-":
            j = i + 2
            eq = 0
            while j < n and src[j] == "=":
                eq += 1
                j += 1
            if j < n and src[j] == "[":
                close = "]" + "=" * eq + "]"
                start = j + 1
                end = src.find(close, start)
                if end == -1:
                    end = n
                    body = src[start:end]
                else:
                    body = src[start:end]
                    end += len(close)
            else:
                end = src.find("\n", i)
                if end == -1:
                    end = n
                body = src[i:end]
            for m in path_re.finditer(body):
                comment_paths.append((line, m.group(0)))
            line += src.count("\n", i, end)
            i = end
            continue

        # ---- 短字符串 "..." / '...' ----
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            buf = []
            while j < n:
                cj = src[j]
                if cj == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if cj == quote or cj == "\n":
                    break
                buf.append(cj)
                j += 1
            literals.append((line, "".join(buf), src[j + 1:j + 41]))
            line += src.count("\n", i, j)
            i = j + 1
            continue

        # ---- 长字符串 [[ ]] / [=[ ]=] （配置表大量使用）----
        if ch == "[":
            k = i + 1
            eq = 0
            while k < n and src[k] == "=":
                eq += 1
                k += 1
            if k < n and src[k] == "[":
                close = "]" + "=" * eq + "]"
                start = k + 1
                end = src.find(close, start)
                if end == -1:
                    value = src[start:]
                    end = n
                else:
                    value = src[start:end]
                    end += len(close)
                literals.append((line, value, src[end:end + 41]))
                line += src.count("\n", i, end)
                i = end
                continue

        i += 1

    return literals, comment_paths


# --------------------------------------------------------------------------
# 路径归一化与判定
# --------------------------------------------------------------------------


def extract_pkgs(raw, mount="/Game/", trailing=""):
    """从一个字符串字面量里提取所有包路径。

    返回 [(pkg, note, dynamic)]：
      pkg     归一化后的包路径
      note    归一化过程中的判断说明，供人工复核
      dynamic 是否疑似拼接，命中失败时不计入死引用。
              两种拼接都要认：字符串内部带 .. 或 %s（"a/%s"），
              以及拼接发生在字符串外面（"a/" .. level）—— 后者靠 trailing 判定。
    """
    out = []
    for m in lib._path_re_str(mount).finditer(raw):
        seg = m.group(0).rstrip("/")
        seg = seg if seg else ""
        dot = seg.find(".", len(mount))
        note = ""
        if dot == -1:
            pkg = seg
        else:
            pkg = seg[:dot]
            obj = seg[dot + 1:]
            if obj.endswith("_C"):
                note = "蓝图生成类 _C"
            elif obj:
                note = "对象名与文件名不同"

        if not pkg or pkg.rstrip("/") == mount.rstrip("/"):
            continue

        rest = raw[m.end():]
        dynamic = (
            rest[:1] in ("%", "{")
            or ".." in rest
            or bool(re.match(r"\s*\.\.", trailing or ""))
        )
        if dynamic:
            note = (note + " 疑似拼接").strip()

        out.append((pkg, note, dynamic))
    return out


def suggest_similar(pkg, content_root, mount="/Game/"):
    """未命中时，在同目录下找最相近的真实资源名。

    实测最有价值的一类问题就是「路径写错了但长得像」，靠人一个个猜很费劲。
    """
    parent, _, name = pkg.rpartition("/")
    rel_dir = parent[len(mount):]
    d = Path(content_root) / rel_dir
    if not d.is_dir():
        return ""
    names = []
    for ext in ASSET_EXTS:
        names.extend(p.stem for p in d.glob("*" + ext))
    if not names:
        return ""

    hits = []
    # UE 命名习惯：U 前缀是 C++ 类名写法，蓝图不需要
    if name[:1] == "U" and name[1:] in names:
        hits.append(name[1:])
    for alt in (name + "_C", name[:-2] if name.endswith("_C") else ""):
        if alt and alt in names and alt not in hits:
            hits.append(alt)
    for c in difflib.get_close_matches(name, names, n=3, cutoff=0.75):
        if c not in hits:
            hits.append(c)
    return ", ".join(hits[:3])


def pkg_candidates(pkg):
    """未命中时尝试的兜底变体。"""
    yield pkg, ""
    if pkg.endswith("_C"):
        yield pkg[:-2], "去 _C 兜底"


def resolve_pkg(pkg, content_root, mount, cache):
    """包路径 -> 磁盘文件（.uasset 或 .umap），带缓存。"""
    if pkg in cache:
        return cache[pkg]
    rel = pkg[len(mount):]
    found = None
    for ext in ASSET_EXTS:
        p = Path(content_root) / (rel + ext)
        if p.is_file():
            found = p
            break
    cache[pkg] = found
    return found


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def iter_lua_files(cfg):
    seen = set()
    for d in cfg.lua_dirs:
        d = cfg.root / d
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.lua")):
            if p not in seen:
                seen.add(p)
                yield p


def scan(cfg, quiet=False):
    content_root = cfg.content_root
    lua_files = list(iter_lua_files(cfg))

    if not lua_files:
        raise SystemExit("[ERROR] 没有找到任何 Lua 文件。检查 refcheck.ini 的 "
                         "scan.lua_dirs（当前：%s），以及 --root 是否指向 UE 工程根。"
                         % "、".join(cfg.lua_dirs))

    cache = {}
    rows = []
    comment_noise = []
    generated_files = 0
    literal_count = 0

    for fp in lua_files:
        rel = cfg.rel(fp)
        try:
            src = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print("[WARN] 读取失败 %s: %s" % (rel, e))
            continue

        is_gen = any(m and m in rel for m in cfg.generated_markers)
        if is_gen:
            generated_files += 1

        literals, cpaths = extract_string_literals(src, cfg.mount)
        literal_count += len(literals)
        if cpaths:
            comment_noise.extend((rel, ln, p) for ln, p in cpaths)

        for line, value, trailing in literals:
            for pkg, note, dynamic in extract_pkgs(value, cfg.mount, trailing):
                hit = None
                hit_pkg = pkg
                for cand, cnote in pkg_candidates(pkg):
                    found = resolve_pkg(cand, content_root, cfg.mount, cache)
                    if found:
                        hit = found
                        hit_pkg = cand
                        if cnote:
                            note = (note + " " + cnote).strip()
                        break

                if hit:
                    status = "OK"
                elif dynamic:
                    status = "DYNAMIC"
                else:
                    status = "MISSING"

                rows.append({
                    "lua_file": rel,
                    "line": line,
                    "status": status,
                    "pkg": pkg,
                    "hit_pkg": hit_pkg if hit else "",
                    "note": note,
                    "raw": value[:120].replace("\n", "\\n"),
                    "generated": "Y" if is_gen else "",
                })

    if not quiet:
        print("已扫描 Lua 文件 %d 个，字符串字面量 %d 条，命中包路径引用 %d 处"
              % (len(lua_files), literal_count, len(rows)))

    return {
        "root": cfg.root,
        "lua_files": lua_files,
        "rows": rows,
        "comment_noise": comment_noise,
        "generated_files": generated_files,
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def write_csv(path, fieldnames, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def build_missing(rows, content_root, mount):
    """按包路径聚合未命中项，并为每项找同目录下最相近的真实资源名。"""
    agg = defaultdict(lambda: {"refs": 0, "files": set(), "sample": "",
                               "line": 0, "raw": ""})
    for r in rows:
        if r["status"] != "MISSING":
            continue
        a = agg[r["pkg"]]
        a["refs"] += 1
        a["files"].add(r["lua_file"])
        if not a["sample"]:
            a["sample"] = r["lua_file"]
            a["line"] = r["line"]
            a["raw"] = r["raw"]
    return sorted(
        ({"pkg": pkg, "refs": a["refs"], "file_count": len(a["files"]),
          "sample_file": a["sample"], "sample_line": a["line"],
          "sample_raw": a["raw"],
          "suggest": suggest_similar(pkg, content_root, mount)}
         for pkg, a in agg.items()),
        key=lambda d: (-d["refs"], d["pkg"]),
    )


def health_check(cfg, lua_files, rows, missing, comment_noise):
    lines = []
    ok = True

    if cfg.content_root.is_dir():
        lines.append("- 内容目录：存在（`%s`）" % cfg.rel(cfg.content_root))
    else:
        lines.append("- 内容目录：**缺失**（`%s`）" % cfg.rel(cfg.content_root))
        ok = False

    if not lua_files:
        lines.append("- Lua 文件：**一个都没找到**，检查 scan.lua_dirs")
        ok = False

    uniq = {r["pkg"] for r in rows}
    ratio = (len(missing) / len(uniq)) if uniq else 0.0
    lines.append("- 唯一包路径 %d 个，未命中 %d 个，未命中率 **%.1f%%**"
                 % (len(uniq), len(missing), ratio * 100))
    if ratio > MISSING_RATIO_WARN:
        lines.append("  - :warning: 未命中率偏高。先确认版本管理是否已同步完整、"
                     "工程二进制是否已部署 —— 否则未命中清单里会混入大量假阳性，"
                     "不要直接当 bug 报。")
        ok = False

    if comment_noise:
        lines.append("- 注释中出现 %d 处包路径，已被词法分析排除"
                     "（直接正则扫全文件会全部误报）" % len(comment_noise))

    lines.append("")
    lines.append("> 本工具只读磁盘，**不读 pak / chunk**。若工程启用打包分包或热更下发，"
                 "磁盘缺失不等于运行时加载失败。")
    return ok, lines


def write_summary(cfg, data, missing):
    rows = data["rows"]
    status_counter = Counter(r["status"] for r in rows)
    uniq = {r["pkg"] for r in rows}
    gen_rows = [r for r in rows if r["generated"] == "Y"]

    ok, health = health_check(cfg, data["lua_files"], rows, missing,
                              data["comment_noise"])

    L = []
    L.append("# 资源引用扫描报告")
    L.append("")
    L.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("- 工作区根：`%s`" % cfg.root)
    L.append("- 扫描范围：%s" % "、".join("`%s`" % d for d in cfg.lua_dirs))
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| Lua 文件数 | %d |" % len(data["lua_files"]))
    L.append("| 其中自动生成产物 | %d |" % data["generated_files"])
    L.append("| 包路径引用总处数 | %d |" % len(rows))
    L.append("| 唯一包路径 | %d |" % len(uniq))
    L.append("| 命中磁盘（OK） | %d |" % status_counter.get("OK", 0))
    L.append("| 未命中（MISSING） | %d |" % status_counter.get("MISSING", 0))
    L.append("| 疑似拼接（DYNAMIC） | %d |" % status_counter.get("DYNAMIC", 0))
    L.append("| 来自生成产物的引用 | %d |" % len(gen_rows))
    L.append("")

    if gen_rows:
        L.append("> 有 %d 处引用来自自动生成的配置文件（路径特征：%s）。"
                 % (len(gen_rows), "、".join("`%s`" % m
                                            for m in cfg.generated_markers) or "未配置"))
        L.append("> 这些路径的真正源头是表格源表，**改生成的 Lua 无效**，"
                 "要改表格后重新生成。")
        L.append("")

    L.append("## 环境自检")
    L.append("")
    L.extend(health)
    L.append("")

    if missing:
        L.append("## 未命中清单（Top 30）")
        L.append("")
        L.append("按引用次数排序。**先排除打包分包、热更与同步不全，"
                 "再判定是否为真死引用。**")
        L.append("")
        L.append("| # | 包路径 | 引用次数 | 同目录最相近的资源 | 首个位置 |")
        L.append("|---|---|---|---|---|")
        for idx, m in enumerate(missing[:30], 1):
            sug = ("`%s`" % m["suggest"]) if m["suggest"] else "—"
            L.append("| %d | `%s` | %d | %s | `%s:%d` |"
                     % (idx, m["pkg"], m["refs"], sug,
                        m["sample_file"], m["sample_line"]))
        L.append("")

    md_path = Path(cfg.report_dir) / "summary.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    return md_path, ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="扫描 Lua 脚本中引用的资源路径")
    ap.add_argument("--root", help="UE 工程根目录")
    ap.add_argument("--config", help="配置文件路径（默认 ./refcheck.ini）")
    ap.add_argument("--out", help="报告输出目录")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args(argv)

    cfg = lib.load_config(args.config, args.root)
    if args.out:
        cfg.report_dir = Path(args.out).expanduser().resolve()

    data = scan(cfg, quiet=args.quiet)
    rows = data["rows"]
    missing = build_missing(rows, cfg.content_root, cfg.mount)

    write_csv(Path(cfg.report_dir) / "lua_asset_refs.csv",
              ["lua_file", "line", "status", "pkg", "hit_pkg", "note", "raw",
               "generated"], rows)
    write_csv(Path(cfg.report_dir) / "missing_refs.csv",
              ["pkg", "refs", "file_count", "sample_file", "sample_line",
               "suggest", "sample_raw"], missing)
    md_path, ok = write_summary(cfg, data, missing)

    status_counter = Counter(r["status"] for r in rows)
    print("")
    print("引用总处数 %d | 唯一包路径 %d | OK %d | MISSING %d | DYNAMIC %d"
          % (len(rows), len({r["pkg"] for r in rows}),
             status_counter.get("OK", 0), status_counter.get("MISSING", 0),
             status_counter.get("DYNAMIC", 0)))
    print("报告已写入: %s" % cfg.report_dir)
    print("  - lua_asset_refs.csv")
    print("  - missing_refs.csv")
    print("  - %s" % md_path.name)
    if not ok:
        print("")
        print("[注意] 环境自检未通过，见 summary.md。未命中清单可能含假阳性。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
