#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一个可跑的迷你「UE 工程」，用来验证本工具。

**其中不含任何真实项目数据**：Lua 是现写的，.uasset/.umap 是自己合成的
字节（不是真的 UE 资产），xlsx 源表是用标准库拼出来的合法 xlsx。

生成的结构：

    examples/demo/
      refcheck.ini                  配置（演示 rename_prefixes 与生成产物标记）
      Content/Script/Main.lua           静态引用 / 拼接 / 注释噪音
      Content/Script/UI/LoginPanel.lua  同名前缀族 / 错误路径 / _C 后缀
      Content/Script/Configs/Design/DemoConfig.lua   自动生成产物
      Content/Art/UI/T_LoginBg.uasset
      Content/Art/UI/T_Cloth_0..3.uasset           同名前缀族
      Content/Art/UI/WBP_Login.uasset              引用贴图与骨骼网格
      Content/Art/UI/NoisyMeta.uasset              含导入元数据（META 噪音）
      Content/Character/SK_Hero.uasset
      Content/Character/SKM_Hero.uasset            引用 SK_Hero
      Content/Character/NewHero_Body.uasset        演示 OldHero_ 改名而来
      Content/Maps/L_Main.umap                     引用贴图与界面

用法：

    python examples/make_demo.py
    python refcheck.py all --config examples/demo/refcheck.ini
    python refcheck.py query LoginBg --config examples/demo/refcheck.ini
"""

from __future__ import annotations

import shutil
import struct
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "demo"


# --------------------------------------------------------------------------
# 合成的 uasset / umap
# --------------------------------------------------------------------------


def fake_asset(refs=(), meta=None):
    """合成一个假的 .uasset/.umap：头部 + 若干长度前缀字符串。

    只是给工具的字节扫描准备素材，并不是真的 UE 资产 —— 真实资产的引用
    散落在各种结构里，但对「扫字节 + 分类」这个策略来说，形态是一样的。
    """
    out = bytearray()
    out += b"\xc1\x83\x2a\x9e"                 # 假 magic
    out += struct.pack("<i", -9)               # 假版本号（负数 = UE5 风格）
    out += b"DEMO-ASSET\x00"
    for r in refs:
        b = r.encode("ascii")
        out += struct.pack("<i", len(b) + 1)
        out += b + b"\x00"
    for key, value in (meta or []):
        blob = ('{"%s": "%s"}' % (key, value)).encode("ascii")
        out += struct.pack("<i", len(blob) + 1)
        out += blob + b"\x00"
    out += b"\x00" * 16
    return bytes(out)


# --------------------------------------------------------------------------
# 合成的 xlsx（zip + XML，标准库即可）
# --------------------------------------------------------------------------


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path, rows, sheet_name="DemoTable"):
    cells_xml = []
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, v in enumerate(row):
            v = "" if v is None else str(v)
            if v == "":
                continue
            ref = "%s%d" % (_col_name(ci), ri)
            cells.append(
                '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t>'
                "</is></c>" % (ref, _xml_escape(v)))
        cells_xml.append('<row r="%d">%s</row>' % (ri, "".join(cells)))

    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_pkg = "http://schemas.openxmlformats.org/package/2006"

    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    sheet = (head + '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
             % (ns_main, "".join(cells_xml)))
    workbook = (head + '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
                '<sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
                % (ns_main, ns_rel, _xml_escape(sheet_name)))
    wb_rels = (head + '<Relationships xmlns="%s/relationships">'
               '<Relationship Id="rId1" Type="%s/worksheet" '
               'Target="worksheets/sheet1.xml"/></Relationships>'
               % (ns_pkg, ns_rel))
    root_rels = (head + '<Relationships xmlns="%s/relationships">'
                 '<Relationship Id="rId1" Type="%s/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>' % (ns_pkg, ns_rel))
    content_types = (head + '<Types xmlns="%s/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.'
                     'openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="'
                     'application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.worksheet+xml"/></Types>' % ns_pkg)

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


# --------------------------------------------------------------------------
# 内容
# --------------------------------------------------------------------------

MAIN_LUA = '''\
local Main = {}

local HERO_MESH = "/Game/Character/SKM_Hero"
local LEVEL_PATH = "/Game/Maps/L_Main"
local PANEL_CLASS = "/Game/Art/UI/WBP_Login.WBP_Login_C"

-- 注释里写路径不会被执行，工具应当把它排除掉
-- local OLD = "/Game/Art/UI/T_CommentedOut"

function Main.loadStatic()
    UE.UObject.Load(HERO_MESH)
    UE.UClass.Load(PANEL_CLASS)
end

function Main.loadDynamic(level)
    -- 字符串拼接：静态分析求不出值，工具只会标成「疑似拼接」
    local icon = "/Game/Art/UI/T_Icon_" .. level
    local fmt = string.format("/Game/Art/UI/T_Lv_%d", level)
    return icon, fmt
end

function Main.openLevel()
    UE.UObject.Load(LEVEL_PATH)
end

function Main.preload()
    UE.UObject.Load("/Game/Art/UI/T_Cloth_0")
    UE.UObject.Load("/Game/Art/UI/T_Cloth_1")
    UE.UObject.Load("/Game/Character/SK_Hero")
    UE.UObject.Load("/Game/Art/UI/WBP_Login")
end

function Main.loadDeadRef()
    -- 磁盘上并不存在这个资源
    UE.UObject.Load("/Game/Art/UI/T_NotExistAtAll")
end

return Main
'''

LOGIN_LUA = '''\
local LoginPanel = {}

-- 磁盘上只有 T_Cloth_0 .. T_Cloth_3，没有 T_Cloth 本身。
-- 这是引擎把 FName 的编号后缀剥离导致的，不是死引用。
local CLOTH_PREFIX = "/Game/Art/UI/T_Cloth"

function LoginPanel.onOpen()
    UE.UObject.Load(CLOTH_PREFIX)
    UE.UObject.Load("/Game/Art/UI/T_LoginBg")
    UE.UObject.Load("/Game/Art/UI/WBP_Login_C")
end

function LoginPanel.onRefresh()
    -- 已改名：现在是 NewHero_Body
    UE.UObject.Load("/Game/Character/OldHero_Body")
end

return LoginPanel
'''

GEN_LUA = '''\
-- Auto-generated by tools/gen_client_lua.py. DO NOT EDIT.
-- 真正的路径写在 Design/Excels/DemoTable.xlsx 里，改这里无效。
local DemoConfig = {
    [1] = {
        Id = 1,
        IconPath = [[/Game/Art/UI/T_LoginBg]],
    },
}
return DemoConfig
'''

DEMO_INI = """\
; 示例工程配置。root 留空 = 本文件所在目录，所以不必再传 --root。

[project]
root =
content_dir = Content
mount = /Game/

[scan]
lua_dirs = Content/Script
design_dirs = Design/Excels
generated_markers = Configs/Design/

[classify]
; 演示「历史改名」提示：OldHero_ 已改名为 NewHero_
rename_prefixes = OldHero_=NewHero_
try_insert_top_dir = true

[output]
report_dir = out/report
index_dir = out/index
"""

DEMO_README = """\
# 示例工程（合成数据）

这里的 `Content/` 与 `Design/` **不是真实项目**，由 `../make_demo.py` 现场生成：

- Lua 脚本是现写的
- `.uasset` / `.umap` 是合成的字节（不是真的 UE 资产）
- `DemoTable.xlsx` 是用标准库拼出来的合法 xlsx

它覆盖了工具要处理的各类情况：

| 情况 | 在哪 |
|---|---|
| 正常命中 | `Main.lua` 里的 `SKM_Hero`、`L_Main` |
| 资源不存在 | `Main.lua` 的 `T_NotExistAtAll` |
| 字符串拼接（静态求不出） | `Main.lua` 的 `T_Icon_ .. level` |
| 注释里的路径（应当被排除） | `Main.lua` 的 `T_CommentedOut` |
| 同名前缀族（编号被剥离） | `LoginPanel.lua` 的 `T_Cloth` |
| 改名残留 | `LoginPanel.lua` 的 `OldHero_Body` |
| 路径少写一级 | `DemoTable.xlsx` 的 `/Game/UI/T_LoginBg` |
| 自动生成产物 | `Configs/Design/DemoConfig.lua` |
| 导入元数据噪音 | `Content/Art/UI/NoisyMeta.uasset` |

重跑：

```
python examples/make_demo.py
python refcheck.py all --config examples/demo/refcheck.ini
python refcheck.py query LoginBg --config examples/demo/refcheck.ini
```
"""


def main():
    if "--clean" in sys.argv[1:]:
        if DEMO.exists():
            shutil.rmtree(DEMO)
            print("已删除 %s" % DEMO)
        return 0

    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)

    c = DEMO / "Content"
    ui = c / "Art" / "UI"
    ch = c / "Character"
    maps = c / "Maps"
    for d in (ui, ch, maps):
        d.mkdir(parents=True, exist_ok=True)

    # ---- Lua ----
    scripts = c / "Script"
    (scripts / "UI").mkdir(parents=True)
    (scripts / "Configs" / "Design").mkdir(parents=True)
    (scripts / "Main.lua").write_text(MAIN_LUA, encoding="utf-8", newline="\n")
    (scripts / "UI" / "LoginPanel.lua").write_text(
        LOGIN_LUA, encoding="utf-8", newline="\n")
    (scripts / "Configs" / "Design" / "DemoConfig.lua").write_text(
        GEN_LUA, encoding="utf-8", newline="\n")

    # ---- 合成资产 ----
    (ui / "T_LoginBg.uasset").write_bytes(fake_asset())
    for i in range(4):
        (ui / ("T_Cloth_%d.uasset" % i)).write_bytes(fake_asset())
    (ui / "WBP_Login.uasset").write_bytes(fake_asset([
        "/Game/Art/UI/T_LoginBg",
        "/Game/Character/SKM_Hero",
        # 同名前缀族：磁盘上只有 T_Cloth_0..3，没有 T_Cloth 本身
        "/Game/Art/UI/T_Cloth",
        # 改名残留：实际已叫 NewHero_Body
        "/Game/Character/OldHero_Body",
    ]))
    (ui / "NoisyMeta.uasset").write_bytes(fake_asset(
        refs=[],
        meta=[("SourceFile", "/Game/Imported/FromFBX")],
    ))
    (ch / "SK_Hero.uasset").write_bytes(fake_asset())
    (ch / "SKM_Hero.uasset").write_bytes(fake_asset([
        "/Game/Character/SK_Hero",
    ]))
    (ch / "NewHero_Body.uasset").write_bytes(fake_asset())
    (maps / "L_Main.umap").write_bytes(fake_asset([
        "/Game/Art/UI/T_LoginBg",
        "/Game/Art/UI/WBP_Login",
    ]))

    # ---- 表格源表 ----
    rows = [
        ["##var", "Id", "Name", "IconPath", "ModelPath"],
        ["##type", "string", "string", "string", "string"],
        ["##group", "c", "c", "c", "c"],
        ["##", "说明", "名字", "图标路径", "模型路径"],
        ["", "1", "Login", "/Game/Art/UI/T_LoginBg", "/Game/Character/SKM_Hero"],
        ["", "2", "Broken", "/Game/Art/UI/T_MissingIcon",
         "/Game/Character/NewHero_Body"],
        ["", "3", "ShortPath", "/Game/UI/T_LoginBg", ""],
    ]
    write_xlsx(DEMO / "Design" / "Excels" / "DemoTable.xlsx", rows)

    # ---- 配置与说明 ----
    (DEMO / "refcheck.ini").write_text(DEMO_INI, encoding="utf-8", newline="\n")
    (DEMO / "README.md").write_text(DEMO_README, encoding="utf-8", newline="\n")

    n_assets = len(list(c.rglob("*")))
    print("示例工程已生成：%s" % DEMO)
    print("  Content/ 下条目 %d 个，Design/Excels/DemoTable.xlsx 1 张表" % n_assets)
    print("")
    print("跑一遍试试：")
    print("  python refcheck.py all --config examples/demo/refcheck.ini")
    print("  python refcheck.py query LoginBg --config examples/demo/refcheck.ini")
    print("")
    print("清理：python examples/make_demo.py --clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
