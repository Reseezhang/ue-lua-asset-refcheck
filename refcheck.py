#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一入口。

    python refcheck.py scan   --root <UE 工程根>     # 正向：脚本里写的资源在不在
    python refcheck.py index  --root <UE 工程根>     # 建索引（反向查询的前置）
    python refcheck.py query  <资源> --root <...>    # 反向：改它会牵到谁
    python refcheck.py all    --root <UE 工程根>     # index + scan
    python refcheck.py init                          # 在当前目录生成 refcheck.ini

    # 没有 UE 工程也能先试：生成一个迷你示例工程并跑通全流程
    python examples/make_demo.py
    python refcheck.py all --config examples/demo/refcheck.ini

任何子命令都接受 --config / --root，其中 --root 优先级更高。
零第三方依赖，仅标准库，Python 3.8+。
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

USAGE = """ue-lua-asset-refcheck —— 检查 Lua / 表格引用的资源是否真的存在，并支持反向影响面查询

用法：
  python refcheck.py scan  [--root <UE 工程根>]           正向扫描 Lua 引用
  python refcheck.py index [--root <UE 工程根>]           构建反向索引
  python refcheck.py all   [--root <UE 工程根>]           index + scan
  python refcheck.py query <资源路径或名字> [--root <...>] 反向查询影响面
  python refcheck.py init                                 生成 refcheck.ini 模板

query 追加参数：
  --search <关键字>      模糊查找资源
  --top <N>              列出被引用最多的 N 个资源
  --file <路径>          批量查询（一行一个目标）
  --depth <N>            资产链最大展开深度（默认 4）
  --out <路径.md>        把结果写入 Markdown

通用参数：
  --config <refcheck.ini>  指定配置文件（默认 ./refcheck.ini）
  --out <目录>             覆盖输出目录
  --quiet                  不打印进度
"""

INI_TEMPLATE = """\
; ue-lua-asset-refcheck 配置
; 放到 UE 工程根目录，直接 `python refcheck.py all` 即可，无需 --root。

[project]
; 工作区根。留空 = 本配置文件所在目录
root =
; UE 内容目录（相对 root）
content_dir = Content
; 包挂载点。若工程用了自定义挂载点，改这里
mount = /Game/

[scan]
; 要扫的 Lua 目录，逗号分隔（相对 root）
lua_dirs = Content/Script
; 表格源表目录，逗号分隔（相对 root）。没有就留空
design_dirs = Design/Excels
; 判定「自动生成产物」的路径特征，逗号分隔。
; 命中这些特征的脚本，其路径真正的源头是表格源表 —— 改脚本无效
generated_markers = Configs/Design/

[classify]
; 已知的历史改名：旧前缀=新前缀，逗号分隔。
; 只影响未命中项的「候选提示」，不影响分类判定。例如：
;   rename_prefixes = OldHero_=NewHero_, SKM_=SK_
rename_prefixes =
; 未命中时是否尝试「补一层顶层目录」
; （对应工程里把 /Game/A/UI/X 写成 /Game/UI/X 这类少一级的情况）
try_insert_top_dir = true

[output]
report_dir = out/report
index_dir = out/index
"""


def cmd_init(argv):
    for a in argv:
        if a in ("-h", "--help"):
            print("用法：python refcheck.py init [--force]\n"
                  "在当前目录生成 refcheck.ini 模板。")
            return 0
    path = Path.cwd() / "refcheck.ini"
    if path.exists() and "--force" not in argv:
        print("[跳过] %s 已存在。要覆盖请加 --force" % path)
        return 0
    path.write_text(INI_TEMPLATE, encoding="utf-8", newline="\n")
    print("已生成 %s" % path)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-v", "--version"):
        print("ue-lua-asset-refcheck 1.0.0")
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "scan":
        import scan_lua
        return scan_lua.main(rest)
    if cmd == "index":
        import build_index
        return build_index.main(rest)
    if cmd == "query":
        import query
        return query.main(rest)
    if cmd == "all":
        import build_index
        import scan_lua
        rc = build_index.main(rest)
        if rc:
            return rc
        print("")
        return scan_lua.main(rest)
    if cmd == "init":
        return cmd_init(rest)

    print("[ERROR] 未知子命令：%s\n" % cmd)
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
