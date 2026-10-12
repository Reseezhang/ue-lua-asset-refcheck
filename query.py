#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向查询：给我一个资源，告诉我动它会影响谁。

这是本工具真正的价值所在 —— 正向扫描只能告诉你「有没有」，
反向查询才能回答「改之前要回归哪些东西」。

用法：

    # 直接查（可给完整包路径，也可只给资源名）
    python query.py --root <UE 工程根> /Game/UI/Styles/TextStyle_A
    python query.py --root <UE 工程根> TextStyle_A

    # 先模糊找资源
    python query.py --search 头像
    python query.py --search T_Icon

    # 看看全工程最「牵一发动全身」的资源
    python query.py --top 30

    # 批量查（一行一个），并把结果写成 Markdown
    python query.py --file targets.txt --out out/report/reverse.md

输出四段：

    [1] 直接引用它的业务脚本    —— 改资源前必须回归的脚本
    [2] 引用它的配置表字段      —— 改前要同步的表格源表
    [3] 直接 / 间接依赖它的资产 —— 受影响的界面 / 关卡 / 材质
    [4] 间接受影响的脚本        —— 经资产链传递后的第二跳

前置：需要先跑 build_index.py 生成索引。零第三方依赖。
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import refcheck_lib as lib  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REQUIRED = ("assets.csv", "asset_deps.csv", "lua_refs.csv", "design_refs.csv")


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Index:
    """把索引 CSV 载入内存，并提供反向图。"""

    def __init__(self, idx_dir, cfg):
        idx_dir = Path(idx_dir)
        missing = [n for n in REQUIRED if not (idx_dir / n).is_file()]
        if missing:
            raise SystemExit(
                "[ERROR] 索引不完整，缺少 %s\n"
                "        请先运行：python build_index.py --root <UE 工程根>"
                % "、".join(missing))

        self.cfg = cfg
        self.generated_markers = cfg.generated_markers

        self.assets = read_csv(idx_dir / "assets.csv")
        self.deps = read_csv(idx_dir / "asset_deps.csv")
        self.lua = read_csv(idx_dir / "lua_refs.csv")
        self.design = read_csv(idx_dir / "design_refs.csv")

        self.asset_by_pkg = {a["pkg"]: a for a in self.assets}
        self.asset_lower = {a["pkg"].lower(): a["pkg"] for a in self.assets}

        # 反向图：只保留真实依赖边（HIT）
        self.rev = defaultdict(list)
        self.repair_hint = {}
        self.family_of = {}
        self.alias_rev = defaultdict(list)

        for d in self.deps:
            # HIT 是真实依赖；FAMILY 指的是「同名前缀族」这个虚拟节点
            # （磁盘上没有这个名字），把它一起纳入反向图，查族前缀时
            # 才能看到谁引用了这一族。
            if d["kind"] in ("HIT", "FAMILY"):
                self.rev[d["dst_pkg"]].append(d["src_pkg"])
            if d["kind"] == "FAMILY":
                self.family_of.setdefault(d["dst_pkg"], d["hint"])
            hint = d.get("hint", "")
            if "少了一级" in hint:
                self.repair_hint.setdefault(
                    d["dst_pkg"], hint.split("实际为", 1)[-1].strip())
            # 错路径 -> 实际资产：把「引用里写错了路径」的引用者也算到
            # 正确资产头上，否则查一个实际有十几处引用的资源会得到
            # 「0 处引用」的错误结论。
            if "实际为" in hint:
                for real in hint.split("实际为", 1)[-1].split(","):
                    real = real.strip()
                    if real:
                        self.alias_rev[real].append((d["src_pkg"], d["dst_pkg"]))

        self.lua_by_pkg = defaultdict(list)
        for r in self.lua:
            self.lua_by_pkg[r["pkg"]].append(r)

        self.design_by_pkg = defaultdict(list)
        for r in self.design:
            self.design_by_pkg[r["pkg"]].append(r)

    def is_generated(self, lua_file):
        return any(m and m in lua_file for m in self.generated_markers)

    def resolve(self, text):
        """把用户输入解析成磁盘上的包路径。返回 (pkg, 说明列表)。"""
        notes = []
        t = (text or "").strip().strip('"').strip("'")
        if not t:
            return "", notes
        # 防御：POSIX shell（Git Bash、MSYS）会把 /Game/... 当路径转换，
        # 这里截回真正的挂载点。
        cut = t.find(self.cfg.mount)
        if cut > 0:
            notes.append("已从被 shell 转换的路径中截回挂载点")
            t = t[cut:]
        if not t.startswith(self.cfg.mount):
            # 允许直接给资源名（甚至名字的一部分），自动补挂载点
            exact = [a["pkg"] for a in self.assets
                     if a["pkg"].rsplit("/", 1)[-1] == t]
            if exact:
                t = exact[0]
                if len(exact) > 1:
                    notes.append("资源名 %s 有 %d 个同名候选，取第一个：%s"
                                 % (t.rsplit("/", 1)[-1], len(exact), t))
                else:
                    notes.append("按资源名匹配到 %s" % t)
            else:
                # 同名前缀族：磁盘上没有这个名字，但它确实被引用过
                fam = [p for p in self.family_of if p.rsplit("/", 1)[-1] == t]
                if fam:
                    t = fam[0]
                    notes.append("匹配到同名前缀族前缀 %s" % t)
                else:
                    kw = t.lower()
                    loose = [a["pkg"] for a in self.assets
                             if kw in a["pkg"].lower()]
                    if loose:
                        notes.append("关键字 %s 匹配 %d 个资源，取第一个：%s"
                                     "（用 --search %s 查看全部）"
                                     % (t, len(loose), loose[0], t))
                        t = loose[0]

        pkg = t.rstrip("/")
        if pkg in self.asset_by_pkg:
            return pkg, notes
        real = self.asset_lower.get(pkg.lower())
        if real:
            notes.append("大小写规范化：%s -> %s" % (pkg, real))
            return real, notes
        hint = self.repair_hint.get(pkg)
        if hint:
            real = hint.split(",")[0].strip()
            notes.append("输入的路径在磁盘上不存在，疑似少了一级；"
                         "已按实际路径 %s 分析" % real)
            return real, notes
        return pkg, notes

    def search(self, keyword, limit=40):
        k = keyword.lower()
        hits = [a["pkg"] for a in self.assets if k in a["pkg"].lower()][:limit]
        if len(hits) < limit:
            for p in self.family_of:
                if k in p.lower() and p not in hits:
                    hits.append(p)
                    if len(hits) >= limit:
                        break
        return hits


def analyze(index, pkg, max_depth=4):
    # 把所有「写错但指向它」的路径一起当作起点，否则影响面会漏掉一大半
    alias = {}
    for src, wrong in index.alias_rev.get(pkg, ()):
        alias.setdefault(wrong, []).append(src)

    roots = [pkg] + [w for w in alias if w != pkg]
    dist = {}
    frontier = []
    for r in roots:
        dist[r] = 0
        frontier.append(r)

    # 引用了「错路径」的资产，等效于直接引用它，且要参与后续向上的传播
    for srcs in alias.values():
        for s in srcs:
            if s not in dist:
                dist[s] = 1
                frontier.append(s)

    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for node in frontier:
            for src in index.rev.get(node, ()):
                if src not in dist:
                    dist[src] = depth
                    nxt.append(src)
        frontier = nxt

    direct_lua = []
    design = []
    for r in roots:
        direct_lua += [dict(x, ref_pkg=r) for x in index.lua_by_pkg.get(r, [])]
        design += [dict(x, ref_pkg=r) for x in index.design_by_pkg.get(r, [])]

    up = sorted(((p, d) for p, d in dist.items() if d > 0),
                key=lambda kv: (kv[1], kv[0]))

    direct_keys = {(r["lua_file"], r["line"]) for r in direct_lua}
    indirect = {}
    for p, d in up:
        for r in index.lua_by_pkg.get(p, []):
            key = (r["lua_file"], r["line"])
            if key in direct_keys:
                continue
            prev = indirect.get(key)
            if prev is None or d < prev[0]:
                indirect[key] = (d, p)

    return {"pkg": pkg, "asset": index.asset_by_pkg.get(pkg), "roots": roots,
            "alias": alias, "direct_lua": direct_lua, "design": design,
            "upstream": up, "indirect": indirect,
            "family": index.family_of.get(pkg)}


def _split_lua(index, rows):
    """把脚本引用拆成「业务脚本」与「自动生成产物」两桶。

    生成产物与表格源表是同一份数据的两种视图，两边都报会让结果看起来翻倍。
    """
    biz, gen = [], []
    for r in rows:
        (gen if index.is_generated(r["lua_file"]) else biz).append(r)
    return biz, gen


def max_depth_cap(up):
    return max((d for _, d in up), default=0)


def render(res, index):
    pkg = res["pkg"]
    L = []
    a = res["asset"]
    L.append("=" * 78)
    L.append("资源：%s" % pkg)
    if a:
        L.append("磁盘：%s（%s KB，被 %s 个资产引用）"
                 % (a["disk_path"], a["size_kb"], a["refs_in"]))
    else:
        L.append("磁盘：**未找到**（可能是已删资源，或引用的路径写错了）")
    if res["family"]:
        L.append("注意：该名字是同名前缀族的公共前缀 —— %s" % res["family"])

    alias = res["alias"]
    if alias:
        L.append("")
        L.append("!! 另有引用写的是错误路径，已一并计入下面各段：")
        for wrong, srcs in alias.items():
            L.append("     `%s`（%d 处引用，来源见下）" % (wrong, len(srcs)))
    L.append("")

    biz, gen = _split_lua(index, res["direct_lua"])
    L.append("[1] 直接引用它的业务脚本：%d 处" % len(biz))
    for r in biz[:40]:
        flag = "" if r["status"] == "OK" else "  <- 引用路径未命中磁盘"
        L.append("    %s:%s%s" % (r["lua_file"], r["line"], flag))
    if len(biz) > 40:
        L.append("    ...（其余 %d 处见 index/lua_refs.csv）" % (len(biz) - 40))
    if gen:
        L.append("    （另 %d 处来自自动生成产物，源头见 [2]）" % len(gen))
    L.append("")

    de = res["design"]
    L.append("[2] 引用它的配置表字段：%d 处" % len(de))
    for r in de[:40]:
        L.append("    %s 行 %s 字段 %s" % (r["xlsx"], r["row_id"], r["field"]))
        if r.get("value"):
            L.append("        值：%s" % r["value"][:110])
    if len(de) > 40:
        L.append("    ...（其余 %d 处见 index/design_refs.csv）" % (len(de) - 40))
    L.append("")

    up = res["upstream"]
    direct = [x for x in up if x[1] == 1]
    deeper = [x for x in up if x[1] > 1]
    L.append("[3] 依赖它的资产：直接 %d 个，加上传递共 %d 个"
             % (len(direct), len(up)))
    for p, d in direct[:25]:
        L.append("    [直接] %s" % p)
    if len(direct) > 25:
        L.append("    ...（直接依赖共 %d 个）" % len(direct))
    if deeper:
        L.append("    更深层（最多 %d 跳，共 %d 个）："
                 % (max_depth_cap(up), len(deeper)))
        for p, d in deeper[:8]:
            L.append("    [%d跳] %s" % (d, p))
    L.append("")

    ind = res["indirect"]
    L.append("[4] 间接受影响的脚本（经资产链传递）：%d 个" % len(ind))
    for (f, ln), (d, via) in sorted(ind.items(),
                                    key=lambda kv: (kv[1][0], kv[0]))[:40]:
        mark = "（自动生成产物）" if index.is_generated(f) else ""
        L.append("    %s:%s%s   <- 经 %s" % (f, ln, mark, via.rsplit("/", 1)[-1]))
    if len(ind) > 40:
        L.append("    ...（其余 %d 个）" % (len(ind) - 40))
    L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="反向查询：一个资源被谁引用（脚本 / 配置表 / 资产 / 间接脚本）")
    ap.add_argument("targets", nargs="*", help="资源包路径或资源名")
    ap.add_argument("--root", help="UE 工程根目录")
    ap.add_argument("--config", help="配置文件路径（默认 ./refcheck.ini）")
    ap.add_argument("--index", help="索引目录（默认取配置里的 index_dir）")
    ap.add_argument("--search", help="按关键字模糊查找资源")
    ap.add_argument("--top", type=int, help="列出被引用最多的 N 个资源")
    ap.add_argument("--file", help="批量查询文件（一行一个目标，# 开头为注释）")
    ap.add_argument("--depth", type=int, default=4, help="资产链最大展开深度")
    ap.add_argument("--out", help="把结果写入 Markdown 文件")
    args = ap.parse_args(argv)

    cfg = lib.load_config(args.config, args.root)
    idx_dir = Path(args.index).expanduser().resolve() if args.index else cfg.index_dir
    index = Index(idx_dir, cfg)

    if args.search:
        hits = index.search(args.search)
        print("匹配 %d 个资源：" % len(hits))
        for p in hits:
            a = index.asset_by_pkg.get(p)
            if a:
                print("  %-4s  %s" % (a["refs_in"], p))
            else:
                print("     -  %s   （同名前缀族，磁盘上没有这个名字）" % p)
        return 0

    if args.top:
        ranked = sorted(index.assets,
                        key=lambda a: -int(a["refs_in"] or 0))[:args.top]
        print("被引用最多的 %d 个资源：" % len(ranked))
        print("%6s  %s" % ("被引用", "资源"))
        for a in ranked:
            print("%6s  %s" % (a["refs_in"], a["pkg"]))
        return 0

    targets = list(args.targets)
    if args.file:
        p = Path(args.file)
        if not p.is_file():
            raise SystemExit("[ERROR] 找不到 %s" % p)
        targets += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]

    if not targets:
        ap.print_help()
        print("\n提示：先跑一次 `python build_index.py --root <UE 工程根>` 建索引，"
              "再用 `--top 20` 看看哪些资源最动不得。")
        return 0

    blocks = []
    for t in targets:
        pkg, notes = index.resolve(t)
        if not pkg:
            blocks.append("跳过空输入")
            continue
        res = analyze(index, pkg, max_depth=args.depth)
        text = render(res, index)
        if notes:
            lines = text.splitlines()
            lines.insert(2, "说明：" + "；".join("（%s）" % n for n in notes))
            text = "\n".join(lines)
        blocks.append(text)

    output = "\n".join(blocks)
    print(output)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        md = ["# 资源影响面反向查询", ""]
        for b in blocks:
            md.append("```text")
            md.append(b)
            md.append("```")
            md.append("")
        out.write_text("\n".join(md), encoding="utf-8", newline="\n")
        print("\n结果已写入 %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
