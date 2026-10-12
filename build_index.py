#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建资源引用索引：把「谁引用了什么」扫成可反向查询的表。

三路来源分别扫描，因为它们的出错方式和修法完全不同：

    Lua 引用    脚本里直接写的资源路径          -> 改脚本
    表格引用    xlsx 源表里的资源路径            -> 改表格后重新生成（改生成的 Lua 无效）
    资产引用    .uasset/.umap 内部引用的其它包   -> 在编辑器里改资产

产物（默认写到 <index_dir>）：

    assets.csv        磁盘资产清单 + 出入度
    asset_deps.csv    资产 -> 资产 的引用边（带分类与候选提示）
    lua_refs.csv      脚本 -> 资源 的引用边
    design_refs.csv   源表字段 -> 资源 的引用边
    index_summary.md  人读摘要

用法：

    python build_index.py --root <UE 工程根>
    python build_index.py --root <UE 工程根> --quiet

零第三方依赖，仅标准库，Python 3.8+。
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import refcheck_lib as lib          # noqa: E402
import scan_lua                     # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def write_csv(path, fieldnames, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# 资产依赖图
# --------------------------------------------------------------------------


def scan_asset_deps(cfg, index, quiet=False):
    """扫描所有 .uasset/.umap，提取并分类其引用的包路径。"""
    content = cfg.content_root
    files = sorted(p for p in content.rglob("*")
                   if p.suffix.lower() in lib.ASSET_EXTS)

    edges = []
    degree_out = Counter()
    degree_in = Counter()
    failed = []
    kind_counter = Counter()
    classify_cache = {}

    t0 = time.time()
    for n, p in enumerate(files, 1):
        try:
            data = p.read_bytes()
        except OSError as e:
            failed.append((cfg.rel(p), str(e)))
            continue

        self_pkg = cfg.mount + p.relative_to(content).with_suffix("").as_posix()

        seen = set()
        for pkg, is_meta in lib.iter_refs(data, cfg.mount):
            if pkg == self_pkg or pkg in seen:
                continue
            seen.add(pkg)

            if is_meta:
                kind, hint = lib.KIND_META, ""
            else:
                cached = classify_cache.get(pkg)
                if cached is None:
                    cached = lib.classify(pkg, index)
                    classify_cache[pkg] = cached
                kind, hint = cached

            edges.append({"src_pkg": self_pkg, "dst_pkg": pkg,
                          "kind": kind, "hint": hint})
            kind_counter[kind] += 1
            degree_out[self_pkg] += 1
            if kind == lib.KIND_HIT:
                degree_in[pkg] += 1

        if not quiet and n % 500 == 0:
            print("  资产扫描 %d/%d ..." % (n, len(files)))

    dt = time.time() - t0
    if not quiet:
        print("  资产扫描完成：%d 个文件，%.1f 秒，读取失败 %d 个"
              % (len(files), dt, len(failed)))

    return {"files": files, "edges": edges, "degree_in": degree_in,
            "degree_out": degree_out, "kind_counter": kind_counter,
            "failed": failed, "seconds": dt}


# --------------------------------------------------------------------------
# 表格源表
# --------------------------------------------------------------------------


def scan_design(cfg, index, quiet=False):
    if not cfg.design_dirs:
        return []
    found_any = False
    rows = []
    for rec in lib.scan_design_tables([cfg.root / d for d in cfg.design_dirs], cfg):
        found_any = True
        if rec.get("error"):
            rows.append({"xlsx": rec["xlsx"], "sheet": "", "row_id": "",
                         "excel_row": 0, "field": "", "pkg": "", "kind": "ERROR",
                         "hint": "", "value": ""})
            continue
        for pkg in rec["pkgs"]:
            kind, hint = lib.classify(pkg, index)
            rows.append({"xlsx": rec["xlsx"], "sheet": rec["sheet"],
                         "row_id": rec["row_id"], "excel_row": rec["excel_row"],
                         "field": rec["field"], "pkg": pkg, "kind": kind,
                         "hint": hint, "value": rec["value"][:200]})
    if not quiet:
        if not found_any:
            print("  [跳过] 找不到源表目录：%s" % "、".join(cfg.design_dirs))
        else:
            print("  源表扫描完成：%d 条资源引用" % len(rows))
    return rows


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------


def write_summary(cfg, index, deps, lua_rows, design_rows):
    edges = deps["edges"]
    kind_counter = deps["kind_counter"]
    total = len(edges)

    L = []
    L.append("# 资源引用索引报告")
    L.append("")
    L.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L.append("- 工作区根：`%s`" % cfg.root)
    L.append("- 资产扫描耗时：%.1f 秒" % deps["seconds"])
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append("| 磁盘资产（.uasset/.umap） | %d |" % len(index))
    L.append("| 资产间引用边 | %d |" % total)
    L.append("| 脚本 -> 资源 引用 | %d |" % len(lua_rows))
    L.append("| 源表字段 -> 资源 引用 | %d |" % len(design_rows))
    L.append("| 资产读取失败 | %d |" % len(deps["failed"]))
    L.append("")
    L.append("## 资产引用分类")
    L.append("")
    L.append("| 分类 | 含义 | 条数 | 占比 |")
    L.append("|---|---|---|---|")
    for k in lib.KIND_ORDER:
        c = kind_counter.get(k, 0)
        L.append("| `%s` | %s | %d | %.1f%% |"
                 % (k, lib.KIND_MEANING[k], c, 100.0 * c / max(1, total)))
    L.append("")
    L.append("> 判定精度参考：只有 `HIT` 才是可直接采信的依赖；"
             "`FAMILY` 是编号后缀被剥离的同名族（多半有效，需展开到整族）；"
             "`STALE` 要看 hint 列是不是改名搬迁；`META` 基本是噪音，"
             "做影响面分析时应剔除。")
    L.append("")

    L.append("## 被引用最多的资源（Top 20）")
    L.append("")
    L.append("这些是「动一个牵一片」的高风险资产，改它们之前务必先跑反向查询。")
    L.append("")
    L.append("| # | 资源 | 被引用次数 |")
    L.append("|---|---|---|")
    for i, (pkg, c) in enumerate(deps["degree_in"].most_common(20), 1):
        L.append("| %d | `%s` | %d |" % (i, pkg, c))
    L.append("")

    L.append("## 依赖最多的资产（Top 15）")
    L.append("")
    L.append("> :warning: 依赖数异常高的资产（尤其上百）往往是「路径清单」类资产，"
             "内部存的是历史路径字符串而非真实依赖，会把噪音带进影响面分析，"
             "请人工确认。")
    L.append("")
    L.append("| # | 资产 | 引用出去的数量 |")
    L.append("|---|---|---|")
    for i, (pkg, c) in enumerate(deps["degree_out"].most_common(15), 1):
        L.append("| %d | `%s` | %d |" % (i, pkg, c))
    L.append("")

    fam = {}
    for e in edges:
        if e["kind"] == lib.KIND_FAMILY:
            fam.setdefault(e["dst_pkg"], e["hint"])
    if fam:
        refs = Counter(e["dst_pkg"] for e in edges
                       if e["kind"] == lib.KIND_FAMILY)
        ordered = sorted(fam.items(), key=lambda kv: -refs[kv[0]])
        L.append("## 同名前缀族（Top 20）")
        L.append("")
        L.append("> UE 的 FName 带编号机制会把 `T_Cloth_9` 拆成名字 `T_Cloth` + 编号 9，"
                 "落盘后字符串只剩 `T_Cloth`。这类引用**不是死引用**，"
                 "做影响面分析时应展开到整个族。")
        L.append("")
        L.append("| # | 被引用的前缀 | 被引用 | 族人样例 |")
        L.append("|---|---|---|---|")
        for i, (pkg, hint) in enumerate(ordered[:20], 1):
            L.append("| %d | `%s` | %d | %s |" % (i, pkg, refs[pkg], hint))
        L.append("")

    repair = {}
    for e in edges:
        if e["kind"] == lib.KIND_STALE and "少了一级" in e["hint"]:
            repair.setdefault(e["dst_pkg"], e["hint"])
    if repair:
        rrefs = Counter(e["dst_pkg"] for e in edges
                        if e["kind"] == lib.KIND_STALE and "少了一级" in e["hint"])
        L.append("## 疑似路径少了一级（Top 20）")
        L.append("")
        L.append("> 引用的路径在挂载点后少了一层目录，补上就能命中。"
                 "这类问题改起来成本最低、收益最直接。")
        L.append("")
        L.append("| # | 引用里写的路径 | 被引用 | 磁盘实际路径 |")
        L.append("|---|---|---|---|")
        for i, (pkg, hint) in enumerate(
                sorted(repair.items(), key=lambda kv: -rrefs[kv[0]])[:20], 1):
            real = hint.split("实际为", 1)[-1].strip()
            L.append("| %d | `%s` | %d | `%s` |" % (i, pkg, rrefs[pkg], real))
        L.append("")

    stale = [e for e in edges if e["kind"] == lib.KIND_STALE and e["hint"]
             and "少了一级" not in e["hint"]]
    uniq_stale = {}
    for e in stale:
        uniq_stale.setdefault(e["dst_pkg"], e["hint"])
    if uniq_stale:
        L.append("## 疑似改名 / 搬迁（Top 25，附同目录最相近资产）")
        L.append("")
        L.append("已排除上面那批「路径少一级」的条目。候选给的是完整包路径，"
                 "可直接复制核对。")
        L.append("")
        refs = Counter(e["dst_pkg"] for e in stale)
        ordered = sorted(uniq_stale.items(), key=lambda kv: -refs[kv[0]])
        L.append("| # | 被引用的路径（磁盘上没有） | 被引用 | 同目录最相近的真实资产 |")
        L.append("|---|---|---|---|")
        for i, (pkg, hint) in enumerate(ordered[:25], 1):
            L.append("| %d | `%s` | %d | `%s` |" % (i, pkg, refs[pkg], hint))
        L.append("")

    if design_rows:
        kc = Counter(r["kind"] for r in design_rows)
        by_table = Counter(r["xlsx"] for r in design_rows)
        L.append("## 源表里的资源引用")
        L.append("")
        L.append("共 %d 条，分布在 %d 张表。分类：%s"
                 % (len(design_rows), len(by_table),
                    "，".join("`%s` %d" % (k, v) for k, v in kc.most_common())))
        L.append("")
        L.append("| 表 | 资源引用条数 |")
        L.append("|---|---|")
        for name, c in by_table.most_common(15):
            L.append("| `%s` | %d |" % (name, c))
        L.append("")
        bad = [r for r in design_rows if r["kind"] == lib.KIND_STALE]
        if bad:
            L.append("### 源表中指向不存在资源的字段（前 20）")
            L.append("")
            L.append("| 表 | 行 | 字段 | 路径 | 最相近资源 |")
            L.append("|---|---|---|---|---|")
            for r in bad[:20]:
                hint = r["hint"] or ""
                if "少了一级" in hint:
                    hint = "少了一级，实际为 `%s`" % hint.split("实际为", 1)[-1].strip()
                elif hint:
                    hint = "`%s`" % hint
                else:
                    hint = "—"
                L.append("| `%s` | %s | `%s` | `%s` | %s |"
                         % (r["xlsx"], r["row_id"], r["field"], r["pkg"], hint))
            L.append("")

    L.append("## 说明")
    L.append("")
    L.append("- 资产间引用通过扫描 .uasset/.umap 原始字节得出（UE5 的包路径"
             "不在 Name Table 里，故不做结构解析），已用真实依赖抽检核对。")
    L.append("- 只覆盖配置里的挂载点（当前 `%s`），不含引擎与插件挂载点。" % cfg.mount)
    L.append("- 只读磁盘，**不读 pak / chunk**；若工程启用分包与热更下发，"
             "磁盘缺失不等于运行时加载失败。")
    L.append("")

    md = Path(cfg.index_dir) / "index_summary.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
    return md


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build(cfg, quiet=False):
    if not quiet:
        print("[1/4] 建立磁盘资产索引 ...")
    index = lib.AssetIndex(cfg.content_root, cfg.mount, cfg.rename_prefixes,
                           cfg.try_insert_top_dir).build()
    if not quiet:
        print("  磁盘资产 %d 个" % len(index))

    if not quiet:
        print("[2/4] 扫描资产依赖 ...")
    deps = scan_asset_deps(cfg, index, quiet=quiet)

    if not quiet:
        print("[3/4] 扫描脚本引用 ...")
    lua_data = scan_lua.scan(cfg, quiet=True)
    lua_rows = [{"pkg": r["pkg"], "lua_file": r["lua_file"], "line": r["line"],
                 "status": r["status"]}
                for r in lua_data["rows"] if r["status"] in ("OK", "MISSING")]
    if not quiet:
        print("  脚本引用 %d 条" % len(lua_rows))

    if not quiet:
        print("[4/4] 扫描表格源表 ...")
    design_rows = scan_design(cfg, index, quiet=quiet)

    out_dir = Path(cfg.index_dir)
    write_csv(out_dir / "assets.csv",
              ["pkg", "disk_path", "ext", "size_kb", "deps_out", "refs_in"],
              [{"pkg": pkg, "disk_path": cfg.rel(p), "ext": p.suffix.lower(),
                "size_kb": round(p.stat().st_size / 1024.0, 1),
                "deps_out": deps["degree_out"].get(pkg, 0),
                "refs_in": deps["degree_in"].get(pkg, 0)}
               for pkg, p in sorted(index.paths.items())])

    write_csv(out_dir / "asset_deps.csv",
              ["src_pkg", "dst_pkg", "kind", "hint"], deps["edges"])
    write_csv(out_dir / "lua_refs.csv",
              ["lua_file", "line", "status", "pkg"], lua_rows)
    write_csv(out_dir / "design_refs.csv",
              ["xlsx", "sheet", "row_id", "excel_row", "field", "pkg",
               "kind", "hint", "value"], design_rows)

    md = write_summary(cfg, index, deps, lua_rows, design_rows)

    kc = deps["kind_counter"]
    print("")
    print("资产 %d | 依赖边 %d | 脚本引用 %d | 源表引用 %d"
          % (len(index), len(deps["edges"]), len(lua_rows), len(design_rows)))
    print("引用分类：" + " | ".join("%s %d" % (k, kc.get(k, 0))
                                   for k in lib.KIND_ORDER))
    print("索引已写入: %s" % out_dir)
    for n in ("assets.csv", "asset_deps.csv", "lua_refs.csv",
              "design_refs.csv", md.name):
        print("  - %s" % n)

    if deps["failed"]:
        print("")
        print("[注意] %d 个资产读取失败，已跳过。" % len(deps["failed"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="构建资源引用索引")
    ap.add_argument("--root", help="UE 工程根目录")
    ap.add_argument("--config", help="配置文件路径（默认 ./refcheck.ini）")
    ap.add_argument("--out", help="索引输出目录")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args(argv)

    cfg = lib.load_config(args.config, args.root)
    if args.out:
        cfg.index_dir = Path(args.out).expanduser().resolve()
    return build(cfg, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
