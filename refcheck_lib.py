#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ue-lua-asset-refcheck —— 共享库

四个职责：

    1. 配置加载          INI 配置 + 命令行覆盖
    2. 路径归一化        /Game/A/B.B_C  ->  /Game/A/B
    3. 磁盘资产索引      包路径 -> .uasset/.umap 文件
    4. 资产引用提取      从 .uasset/.umap 的原始字节里捞出它引用的其它包
    5. 表格源表解析      xlsx（zip + XML）里的资源路径

零第三方依赖，仅用标准库，Python 3.8+。

关于第 4 点（本库最核心、也最容易做错的部分）：

    UE5 的 .uasset 里，资产引用并不集中在 Name Table。实测 Name Table
    只装属性名与类名，甚至可能一条包路径都没有；真正的路径散落在文件
    各处，且字符串编码规则并不统一。

    因此本库不解析任何 UE 结构，改为「全文件字节扫描 + 上下文分类」。
    实测数千个资产 / 数 GB 只要十秒级，且能正确捞出真实依赖
    （例如动画蓝图 -> 骨骼、物理资产）。

    代价是必须处理两类噪音，见 iter_refs() 与 classify()。
"""

from __future__ import annotations

import configparser
import difflib
import re
import zipfile
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

ASSET_EXTS = (".uasset", ".umap")

# 引用分类：不是「对/错」，而是可信度分档
KIND_HIT = "HIT"        # 磁盘上存在对应资产 —— 真实依赖
KIND_DIR = "DIR"        # 是某个真实资产的父级目录 —— 目录级引用，不是资源
KIND_FAMILY = "FAMILY"  # 同名前缀族（T_Cloth 对 T_Cloth_0..55）—— 编号后缀被剥离
KIND_STALE = "STALE"    # 磁盘上找不到 —— 疑似改名 / 搬迁 / 删除
KIND_META = "META"      # 出现在导入元数据里 —— 不是运行期依赖

KIND_ORDER = (KIND_HIT, KIND_DIR, KIND_FAMILY, KIND_STALE, KIND_META)

KIND_MEANING = {
    KIND_HIT: "磁盘上存在，真实依赖",
    KIND_DIR: "指向目录而非资源",
    KIND_FAMILY: "同名前缀族，编号后缀被引擎剥离（多半有效）",
    KIND_STALE: "磁盘上找不到，疑似改名 / 搬迁 / 删除",
    KIND_META: "资产内嵌的导入元数据，不是运行期依赖",
}

# 导入元数据 JSON 的特征：路径前紧跟着 "key": "
JSON_KEY_RE = re.compile(rb'"[A-Za-z0-9_]+"\s*:\s*"$')

# 注意：这里**故意不做**「一个匹配里含多个挂载点就按挂载点切开」的处理。
# 因为完全合法的路径可能包含与挂载点同名的目录层 —— 举例（假设值）：
# 挂载点写 /Game/，而工程里恰好有一层目录也叫 Game，就会出现
# /Game/Game/... 这样完全合法的路径。盲目按挂载点切分会把它砍成
# /Game/...，凭空造出成百上千条「资源缺失」。
# 详见 README「踩过的坑」。


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

_DEFAULTS = {
    "project": {
        "root": "",                    # 工作区根；留空 = 配置文件所在目录
        "content_dir": "Content",      # UE 内容目录（相对 root）
        "mount": "/Game/",             # 包挂载点
    },
    "scan": {
        "lua_dirs": "Content/Script",          # 要扫的 Lua 目录（逗号分隔）
        "design_dirs": "Design/Excels",        # 表格源表目录（逗号分隔，可留空）
        "generated_markers": "Configs/Design/",  # 判定「自动生成产物」的路径特征
    },
    "classify": {
        # 已知的历史改名：旧前缀=新前缀，逗号分隔。
        # 只影响未命中项的「候选提示」，不影响分类判定。
        "rename_prefixes": "",
        # 未命中时是否尝试「补一层顶层目录」（对应工程里路径少写一级的情况）
        "try_insert_top_dir": "true",
    },
    "output": {
        "report_dir": "out/report",
        "index_dir": "out/index",
    },
}


def _split_list(s):
    return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]


def _as_bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on", "y")


class Config:
    """工具配置。字段全部来自 INI，命令行可覆盖 root。"""

    def __init__(self, values=None, config_path=None):
        v = {sec: dict(d) for sec, d in _DEFAULTS.items()}
        if values:
            for sec, kv in values.items():
                v.setdefault(sec, {}).update(kv)
        self.values = v
        self.config_path = Path(config_path).resolve() if config_path else None
        base = self.config_path.parent if self.config_path else Path.cwd()

        root = (v["project"].get("root") or "").strip()
        self.root = Path(root).expanduser().resolve() if root else base

        self.content_dir = (v["project"].get("content_dir") or "Content").strip()

        mount = (v["project"].get("mount") or "/Game/").strip()
        if not mount.startswith("/"):
            mount = "/" + mount
        if not mount.endswith("/"):
            mount += "/"
        self.mount = mount

        self.lua_dirs = _split_list(v["scan"].get("lua_dirs"))
        self.design_dirs = _split_list(v["scan"].get("design_dirs"))
        self.generated_markers = _split_list(v["scan"].get("generated_markers"))
        self.rename_prefixes = self._parse_rename(v["classify"].get("rename_prefixes"))
        self.try_insert_top_dir = _as_bool(
            v["classify"].get("try_insert_top_dir", "true"))

        self.report_dir = self._resolve_out(base, v["output"].get("report_dir"))
        self.index_dir = self._resolve_out(base, v["output"].get("index_dir"))

    @staticmethod
    def _parse_rename(s):
        out = []
        for item in _split_list(s):
            if "=" in item:
                a, b = item.split("=", 1)
                a, b = a.strip(), b.strip()
                if a and b:
                    out.append((a, b))
        return out

    @staticmethod
    def _resolve_out(base, p):
        p = Path(p or "out").expanduser()
        return p.resolve() if p.is_absolute() else (base / p).resolve()

    @property
    def content_root(self):
        return self.root / self.content_dir

    def rel(self, path):
        """把路径转成相对 root 的 posix 形式，便于写进报告。"""
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()


def load_config(path=None, root=None):
    """读取配置。

    path 为 None 时，依次尝试 ./refcheck.ini、本文件同目录的 refcheck.ini。
    root 非空时覆盖配置里的 root。
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / "refcheck.ini")
        candidates.append(Path(__file__).resolve().parent / "refcheck.ini")

    values, used = {}, None
    for c in candidates:
        if c.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(str(c), encoding="utf-8")
            except configparser.Error as e:
                raise SystemExit("[ERROR] 配置文件解析失败 %s: %s" % (c, e))
            values = {sec: dict(cp.items(sec)) for sec in cp.sections()}
            used = c
            break

    if root:
        values.setdefault("project", {})["root"] = root
    return Config(values, config_path=used)


# --------------------------------------------------------------------------
# 路径归一化
# --------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _path_re_bytes(mount):
    return re.compile(re.escape(mount).encode("ascii") + rb"[A-Za-z0-9_/.\-]{2,200}")


@lru_cache(maxsize=8)
def _path_re_str(mount):
    return re.compile(re.escape(mount) + r"[A-Za-z0-9_\-/\.]+")


def norm_pkg(segment, mount="/Game/"):
    """把一段原始匹配归一化成包路径。

    "/Game/A/B.B_C" -> "/Game/A/B"（截到第一个 '.' 之前，即去掉对象名）
    "/Game/A/B/"    -> "/Game/A/B"
    """
    if isinstance(segment, bytes):
        segment = segment.decode("ascii", "replace")
    s = segment.rstrip("/")
    if len(s) <= len(mount.rstrip("/")):
        return ""
    dot = s.find(".", len(mount))
    if dot != -1:
        s = s[:dot]
    s = s.rstrip("/")
    return "" if len(s) <= len(mount.rstrip("/")) else s


def iter_refs(data, mount="/Game/"):
    """从 .uasset/.umap 的原始字节里提取引用的包路径。

    yield (pkg, is_meta)：
        pkg      归一化后的包路径
        is_meta  该路径是否出现在导入元数据 JSON 里（非运行期依赖）
    """
    for m in _path_re_bytes(mount).finditer(data):
        pre = data[max(0, m.start() - 40):m.start()]
        is_meta = bool(JSON_KEY_RE.search(pre))
        pkg = norm_pkg(m.group(0), mount)
        if pkg:
            yield pkg, is_meta


# --------------------------------------------------------------------------
# 磁盘资产索引
# --------------------------------------------------------------------------


class AssetIndex:
    """磁盘上的资产索引：包路径 -> 文件，以及反向的同目录文件名表。

    by_dir 用于给「未命中」的引用找同目录最相近的真实资产 —— 实测这是
    最有价值的功能：项目里大量问题是改名搬迁，而不是资源凭空消失。
    """

    def __init__(self, content_root, mount="/Game/", rename_prefixes=(),
                 try_insert_top_dir=True):
        self.content_root = Path(content_root)
        self.mount = mount
        self.rename_prefixes = tuple(rename_prefixes)
        self.try_insert_top_dir = bool(try_insert_top_dir)

        self.paths = {}        # pkg -> Path
        self.lower = {}        # lower(pkg) -> 磁盘上的真实写法
        self.by_dir = {}       # 目录 pkg -> [直接位于该目录下的资产名]
        self.all_dirs = set()  # 所有目录前缀（含中间层）
        self.top_dirs = []     # 内容目录下的一级目录名
        self.built = False

    def build(self, progress=None):
        root = self.content_root
        n = 0
        for ext in ASSET_EXTS:
            for p in root.rglob("*" + ext):
                rel = p.relative_to(root).with_suffix("").as_posix()
                pkg = self.mount + rel
                self.paths[pkg] = p
                self.lower.setdefault(pkg.lower(), pkg)
                parent, _, name = pkg.rpartition("/")
                self.by_dir.setdefault(parent, []).append(name)
                # 逐级登记目录前缀：/Game/A/B/C -> /Game/A, /Game/A/B
                depth = self.mount.rstrip("/").count("/") + 1
                parts = pkg.split("/")
                for i in range(depth + 1, len(parts)):
                    self.all_dirs.add("/".join(parts[:i]))
                n += 1
                if progress and n % 1000 == 0:
                    progress(n)
        top = self.mount.rstrip("/").count("/") + 1
        self.top_dirs = sorted({p.split("/")[top] for p in self.paths
                                if len(p.split("/")) > top + 1})
        self.built = True
        return self

    def __contains__(self, pkg):
        return pkg in self.paths

    def __len__(self):
        return len(self.paths)

    @property
    def pkgs(self):
        return self.paths.keys()

    def real_case(self, pkg):
        """大小写不敏感命中，返回磁盘上的真实写法（None 表示不存在）。

        UE 的包路径大小写不敏感，而 Python 的 Path 大小写敏感。若不处理，
        会把同一条路径的大小写差异误报成「资源缺失」。
        """
        return self.lower.get(pkg.lower())

    def is_dir(self, pkg):
        """是否为目录级引用（磁盘上存在该目录，但它不是资产）。"""
        return pkg in self.all_dirs

    def family(self, pkg, limit=6):
        """判断 pkg 是否是某目录下「同名前缀族」的公共前缀。

        UE 的 FName 带编号机制会把 T_Cloth_9 拆成名字 T_Cloth + 编号 9，
        落盘后字符串只剩 T_Cloth。所以磁盘上找不到 T_Cloth 通常不是死引用，
        而是后缀编号丢了。返回 (族成员数, 成员样例)。
        """
        parent, _, name = pkg.rpartition("/")
        names = self.by_dir.get(parent)
        if not names:
            return 0, ""
        prefix = name + "_"
        hits = [x for x in names
                if x.startswith(prefix) and x[len(prefix):].isdigit()]
        if not hits:
            return 0, ""
        hits.sort(key=lambda x: int(x.rsplit("_", 1)[1]))
        return len(hits), ", ".join(hits[:limit])

    def repair_prefix(self, pkg, limit=2):
        """尝试在挂载点之后补一层目录，看能否命中。

        工程里可能存在「路径少写一级」的批量问题：写 /Game/UI/X，实际是
        /Game/<某个一级目录>/UI/X。逐个试内容目录的一级目录即可，成本极低。
        """
        if not self.try_insert_top_dir or not pkg.startswith(self.mount):
            return []
        tail = pkg[len(self.mount):]
        hits = []
        for top in self.top_dirs:
            real = self.lower.get((self.mount + top + "/" + tail).lower())
            if real:
                hits.append(real)
                if len(hits) >= limit:
                    break
        return hits

    def suggest(self, pkg, limit=3):
        """给未命中的包路径找真实资产，返回完整包路径（便于直接复制核对）。

        先试命名变体（配置登记的历史改名；以及 SK_/SKM_ 这类 UE 骨架命名
        习惯），再用 difflib 兜底。若原目录不存在，会在相近目录里找 ——
        这恰好能暴露「路径层级写错」这类问题。
        """
        parent, _, name = pkg.rpartition("/")
        base = parent
        names = self.by_dir.get(parent)
        if not names:
            hits = difflib.get_close_matches(parent, list(self.by_dir), n=1,
                                             cutoff=0.85)
            if not hits:
                return ""
            base = hits[0]
            names = self.by_dir[base]

        variants = []
        # 配置里登记的历史改名（双向都试，方便判断是改名前还是改名后）
        for a, b in self.rename_prefixes:
            if name.startswith(a):
                variants.append(b + name[len(a):])
            if name.startswith(b):
                variants.append(a + name[len(b):])
        # UE 命名习惯：SK_ = Skeleton，SKM_ = Skeletal Mesh
        if name.startswith("SKM_"):
            variants.append("SK_" + name[4:])
        if name.startswith("SK_"):
            variants.append("SKM_" + name[3:])
        # 蓝图生成类后缀
        variants.append(name + "_C")
        if name.endswith("_C"):
            variants.append(name[:-2])

        out = [v for v in variants if v in names]
        for c in difflib.get_close_matches(name, names, n=limit, cutoff=0.7):
            if c not in out:
                out.append(c)
        return ", ".join(base + "/" + v for v in out[:limit])


def classify(pkg, index, self_pkg=""):
    """给一条引用分类，返回 (kind, hint)。

    hint 是给人看的补充说明：改名候选、大小写差异、同名族规模等。
    """
    if pkg == self_pkg or pkg in index:
        return KIND_HIT, ""
    real = index.real_case(pkg)
    if real:
        return KIND_HIT, "大小写与磁盘不一致（实际为 %s）" % real
    if index.is_dir(pkg):
        return KIND_DIR, ""
    n, sample = index.family(pkg)
    if n:
        return KIND_FAMILY, "同名前缀族 %d 个：%s（编号后缀被剥离）" % (n, sample)
    repair = index.repair_prefix(pkg)
    if repair:
        return KIND_STALE, "疑似路径少了一级，磁盘实际为 %s" % ", ".join(repair)
    return KIND_STALE, index.suggest(pkg)


# --------------------------------------------------------------------------
# xlsx 源表解析
# --------------------------------------------------------------------------

_XML_ENT = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
            ("&apos;", "'"), ("&#39;", "'"), ("&amp;", "&"))


def _unescape(s):
    for a, b in _XML_ENT:
        s = s.replace(a, b)
    return s


def _col_index(cell_ref):
    """把 "AB12" 的列部分转成 0 基下标。"""
    letters = re.match(r"([A-Z]+)", cell_ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx_grid(path):
    """把 xlsx 的第一个工作表读成二维数组（字符串）。

    xlsx 就是 zip + XML，标准库足够，不需要 openpyxl。
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
                shared.append(_unescape("".join(
                    re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))

        sheets = sorted(n for n in names
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            return [], ""
        sheet_xml = z.read(sheets[0]).decode("utf-8", "replace")
        wb_name = ""
        if "xl/workbook.xml" in names:
            wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
            m = re.search(r'<sheet[^>]*name="([^"]+)"', wb)
            wb_name = _unescape(m.group(1)) if m else ""

    rows = []
    for row_m in re.finditer(r"<row[^>]*>(.*?)</row>", sheet_xml, re.S):
        row = []
        for c in re.finditer(r"<c\b([^>]*)>(.*?)</c>|<c\b([^>]*)/>",
                             row_m.group(1), re.S):
            attrs = c.group(1) or c.group(3) or ""
            body = c.group(2) or ""
            ref = re.search(r'r="([A-Z]+\d+)"', attrs)
            col = _col_index(ref.group(1)) if ref else len(row)
            while len(row) < col:
                row.append("")
            t = re.search(r't="([^"]+)"', attrs)
            t = t.group(1) if t else ""
            if t == "s":
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                idx = int(v.group(1)) if v else -1
                val = shared[idx] if 0 <= idx < len(shared) else ""
            elif t == "inlineStr":
                val = _unescape("".join(
                    re.findall(r"<t[^>]*>(.*?)</t>", body, re.S)))
            else:
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                val = _unescape(v.group(1)) if v else ""
            row.append(val)
        rows.append(row)
    return rows, wb_name


def parse_table_rows(rows, marker="##var"):
    """解析表格结构，返回 (fields, data_rows)。

    优先识别「字段名行」：A 列写着标记（默认 `##var`），B 列起是字段名。
    这是 Luban / 部分自研导表工具的常见约定，其后以标记开头的行属元信息。

    若找不到标记行，则退化为「首行为字段名」，仍然可用 —— 这样对自研
    表格工具也能开箱即用。

    data_rows 的元素是 (excel_row, cells)，excel_row 为 1 基 Excel 行号。
    """
    var_row = None
    for i, r in enumerate(rows[:8]):
        if r and (r[0] or "").strip() == marker:
            var_row = i
            break

    if var_row is None:
        # 退化路径：没有标记行，把第一行当字段名，其余当数据
        first = None
        for i, r in enumerate(rows):
            if any((c or "").strip() for c in (r or [])):
                first = i
                break
        if first is None:
            return [], []
        fields = [(c or "").strip() for c in rows[first][1:]]
        data = [(i, r) for i, r in enumerate(rows[first + 1:], start=first + 2)
                if r and any((c or "").strip() for c in r)]
        return fields, data

    fields = [(c or "").strip() for c in rows[var_row][1:]]
    data = []
    for i, r in enumerate(rows[var_row + 1:], start=var_row + 2):
        if not r:
            continue
        head = (r[0] or "").strip()
        if head.startswith(marker[:2]):
            continue
        if not any((c or "").strip() for c in r):
            continue
        data.append((i, r))
    return fields, data


def scan_design_tables(design_dirs, cfg):
    """扫描表格源表目录，提取所有资源路径引用。

    design_dirs 可以是单个路径或路径列表。
    yield dict(xlsx, sheet, row_id, excel_row, field, value, pkgs)
    """
    if isinstance(design_dirs, (str, Path)):
        design_dirs = [design_dirs]

    for d in design_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for xlsx in sorted(d.rglob("*.xlsx")):
            if xlsx.name.startswith("~$"):
                continue
            try:
                rows, sheet = read_xlsx_grid(xlsx)
            except (zipfile.BadZipFile, OSError, KeyError, ValueError):
                yield {"xlsx": xlsx.name, "sheet": "", "row_id": "",
                       "excel_row": 0, "field": "", "value": "", "pkgs": [],
                       "error": "读取失败"}
                continue

            fields, data = parse_table_rows(rows)
            id_col = fields.index("Id") if "Id" in fields else 0
            path_re = _path_re_str(cfg.mount)

            for excel_row, r in data:
                # Id 列位置在各表间不统一，依次兜底，最后退回 Excel 行号
                row_id = ""
                for idx in (1 + id_col, id_col, 0):
                    if len(r) > idx and (r[idx] or "").strip():
                        row_id = r[idx].strip()
                        break
                if not row_id:
                    row_id = "行%d" % excel_row

                for ci, cell in enumerate(r[1:], start=0):
                    cell = cell or ""
                    if cfg.mount not in cell:
                        continue
                    pkgs = sorted({p for p in (
                        norm_pkg(m.group(0), cfg.mount)
                        for m in path_re.finditer(cell)) if p})
                    if not pkgs:
                        continue
                    field = fields[ci] if ci < len(fields) else "col%d" % ci
                    yield {"xlsx": xlsx.name, "sheet": sheet, "row_id": row_id,
                           "excel_row": excel_row, "field": field,
                           "value": cell.strip(), "pkgs": pkgs, "error": ""}
