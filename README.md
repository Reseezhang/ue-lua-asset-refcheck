# ue-lua-asset-refcheck

> 检查 UE 项目里 Lua / 表格引用的资源是否真的存在，并支持反向影响面查询。
> 零第三方依赖、只读磁盘、不启动引擎。

English: A dependency checker for Unreal Engine projects that load assets by
string path from Lua (or from spreadsheet-driven config). It scans your Lua
scripts, `.xlsx` source tables and `.uasset` files, then tells you both
*"what is referenced but missing"* and *"what breaks if I change this asset"*.
Pure standard library, read-only, no engine launch required.

---

## 它解决什么问题

在 UE 项目里，代码和表格里写的资源路径**本质上只是一串字符**：

```lua
UE.UObject.Load("/Game/Art/UI/T_LoginBg")
UE.UClass.Load("/Game/Art/UI/WBP_Login.WBP_Login_C")
```

写错了、资源改名了、资源被删了——**编译不报错，启动不报错**，只有真正打开
那个界面或触发那段逻辑时才会黑屏、空白或崩。这类问题在测试之前几乎发现不了，
测的时候才发现，然后来回沟通定位。

这个工具做两件事：

| 方向 | 回答的问题 | 命令 |
|---|---|---|
| 正向 | 代码/表格里写了要用、磁盘上没有的是哪些 | `scan` |
| 反向 | 我要改 / 删 / 改名某个资源，会影响谁 | `query` |

反向查询会给出四段答案：

1. 直接引用它的**业务脚本**
2. 引用它的**表格源表字段**
3. 直接 / 间接依赖它的**资产**
4. 经资产链传递后**间接受影响的脚本**（例如：贴图 → 被某材质引用 → 材质被某界面使用 → 该界面的脚本）

## 它不做什么（请先读这段）

- **不读 pak / chunk**。若工程启用了打包分包或热更下发，资源可能不在磁盘上。
  因此工具输出的永远是「**待核查清单**」，不是「bug 清单」。
- **不启动引擎**，不联网，不读版本管理，不写任何工程文件。
- **不是 Asset Registry 的替代品**。资产引用是字节级提取（见下文），
  存在少量噪音，已用分类与 `hint` 标注出来。
- **不做语义推断**。字符串拼接出来的路径静态求不出值，只会标成 `DYNAMIC`。

## 30 秒上手（不需要 UE 工程）

会现场生成一个不含真实数据的迷你工程，跑通全流程：

```bash
python examples/make_demo.py
python refcheck.py all --config examples/demo/refcheck.ini
python refcheck.py query LoginBg --config examples/demo/refcheck.ini
```

`examples/demo/` 覆盖了工具要处理的各类情况：正常命中、资源不存在、
字符串拼接、注释里的路径、同名前缀族、改名残留、路径少写一级、
自动生成产物、导入元数据噪音。详见 `examples/demo/README.md`。

清理：`python examples/make_demo.py --clean`

## 接入自己的工程

要求 **Python 3.8+**，无第三方依赖。

1. 把 `refcheck.ini` 放到 UE 工程根目录（与 `Content/` 同级），按需修改：

```ini
[project]
; 留空 = 本配置文件所在目录
root =
content_dir = Content
mount = /Game/

[scan]
; 要扫的 Lua 目录，逗号分隔
lua_dirs = Content/Script
; 表格源表目录，没有就留空
design_dirs = Design/Excels
; 判定「自动生成产物」的路径特征
generated_markers = Configs/Design/

[classify]
; 已知的历史改名：旧前缀=新前缀（只影响候选提示，不影响分类）
rename_prefixes =
try_insert_top_dir = true

[output]
report_dir = out/report
index_dir = out/index
```

2. 跑：

```bash
python refcheck.py all                          # 建索引 + 正向扫描
python refcheck.py query /Game/Art/UI/T_LoginBg # 反向查询
```

也可以不写配置，直接 `--root <UE 工程根>`。完整参数见 `python refcheck.py --help`。

## 三个入口

| 命令 | 做什么 | 典型耗时 |
|---|---|---|
| `python refcheck.py scan` | 正向扫描：脚本里写的资源在不在 | 秒级 |
| `python refcheck.py index` | 构建反向索引（脚本 + 表格 + 资产依赖图） | 约 10 秒 / 3500 资产 |
| `python refcheck.py query <资源>` | 反向查询影响面 | 秒级 |

`query` 的常用变体：

```bash
python refcheck.py query --top 20                # 哪些资源最「牵一发动全身」
python refcheck.py query --search 头像            # 先模糊找资源
python refcheck.py query --file targets.txt --out out/report/reverse.md
```

## 输出产物

`scan` 写到 `report_dir`：

| 文件 | 内容 |
|---|---|
| `lua_asset_refs.csv` | 全部引用明细（文件 / 行号 / 状态 / 路径 / 备注） |
| `missing_refs.csv` | 未命中资源（按引用次数排序，附同目录最相近资源） |
| `summary.md` | 人读摘要 + 环境自检 |

`index` 写到 `index_dir`：

| 文件 | 内容 |
|---|---|
| `assets.csv` | 磁盘资产清单 + 出度 / 入度 |
| `asset_deps.csv` | 资产 → 资产 的引用边，带 `kind` 分类与 `hint` 说明 |
| `lua_refs.csv` | 脚本 → 资源 的引用边 |
| `design_refs.csv` | 源表字段 → 资源 的引用边（含单元格原值） |
| `index_summary.md` | 人读摘要：分类分布、高风险资源、各类问题清单 |

CSV 用 UTF-8 with BOM（Excel 双击不乱码），Markdown 用 UTF-8 no BOM。

## 分类怎么读（最重要的一节）

`asset_deps.csv` 的 `kind` 列不是「对 / 错」，而是**可信度分档**。
做影响面判断时请按下面的顺序采信：

| kind | 含义 | 怎么用 |
|---|---|---|
| `HIT` | 磁盘上存在，真实依赖 | 可直接采信 |
| `DIR` | 指向目录，不是资源 | UE 的目录级引用，忽略 |
| `FAMILY` | 同名前缀族，编号后缀被剥离 | **多半有效**，要展开到整个族 |
| `STALE` | 磁盘上找不到 | 看 `hint`：多半是改名搬迁或路径写错 |
| `META` | 出现在资产内嵌的导入元数据里 | 基本是噪音，做影响面分析时剔除 |

以下是实测一个中等规模 UE5 项目得到的分布，**只有约六成可以直接采信**：

```
HIT     ██████████████████████████████  63%   可直接采信
FAMILY  █████████                         18%   多半有效，需展开到整族
META    ████████                          16%   噪音，剔除
STALE   █                                  2%   真的找不到
DIR                                        1%   目录引用，忽略
```

如果不做这个分档，只输出「存在 / 不存在」两个答案，将近四成结论会是错的。

### 关于 `FAMILY`（最容易误判的一类）

UE 的 FName 带编号机制会把 `T_Cloth_9` 拆成名字 `T_Cloth` + 编号 9，
落盘后字符串里只剩 `T_Cloth`。所以二进制里出现 `/Game/.../T_Cloth`
**不代表引用了不存在的资源**——磁盘上确实没有 `T_Cloth.uasset`，
但可能有 `T_Cloth_0 .. T_Cloth_55`。

工具会识别这种情况并把整族列出来。这类占实测引用边的一成以上，
不做区分就会大量误判成「资源被删」。

### 关于 `hint` 列

`STALE` 的 hint 会给出三类结论之一：

- `疑似路径少了一级，磁盘实际为 X` —— 在挂载点后补一层目录即可命中
- 同名目录下最相近的真实资产（改名搬迁的候选）
- 空 —— 确实找不到对应物（导入残留、临时目录、已删资源）

`rename_prefixes` 配置项可以登记「已知的历史改名」，
让 hint 直接给出改名后的候选。它**只影响提示文字，不影响分类判定**。

## 为什么要分三条引用链路

工具把「谁引用了资源」拆成三条链路分别扫描，因为**它们的出错方式和修法完全不同**：

| 链路 | 来源 | 出问题时要改哪里 |
|---|---|---|
| 脚本引用 | `lua_dirs`（默认 `Content/Script/**/*.lua`） | 改 Lua |
| 源表引用 | `design_dirs`（Luban 等导表工具的 `.xlsx` 源表） | **改表格后重新生成**，改生成的 Lua 无效 |
| 资产引用 | 每个 `.uasset` / `.umap` 内部 | 在编辑器里改资产 |

第二行是最容易被做错的地方：很多工程的 Lua 配置表是**导表工具生成的产物**，
路径真正写在 Excel 里。只扫 Lua 会得出「该改脚本」的错误结论。
配置项 `generated_markers` 就是用来标记这类产物目录的。

## 实现要点

1. **Lua 侧用手写词法状态机剥离注释**，只采集真正的字符串字面量。
   实测注释里有不少被注释掉的旧路径和文档示例，直接正则扫全文件会全部误报。
2. **拼接识别**既认字符串内部的 `..` / `%s`，也认 `".../" .. level`
   这种「拼接发生在字符串外面」的写法。这类一律标 `DYNAMIC`，
   **绝不当死引用上报**——宁可少报，不能制造噪音。
3. **关卡是 `.umap`**。只查 `.uasset` 会把关卡引用全部误报成缺失，
   两种扩展名都要查。
4. **资产引用不解析 UE 结构，只扫原始字节。** UE5 的包路径并不集中在
   Name Table（实测 Name Table 只装属性名与类名，甚至可能一条路径都没有），
   结构解析性价比极低。改成「字节扫描 + 上下文分类」后，
   约 3500 个资产 / 数 GB 只要十秒级，且能正确捞出真实依赖
   （例如动画蓝图 → 骨骼、物理资产）。
5. **路径大小写不敏感**：UE 的包路径不区分大小写，索引里做了小写归一化，
   避免把大小写差异误报成「资源缺失」。
6. **未命中时给候选**：先试命名变体（配置登记的历史改名、`SK_`/`SKM_`
   这类引擎命名习惯、`_C` 后缀），再用 difflib 兜底。
   实测最有价值的一类问题就是「路径写错了但长得像」，靠人一个个猜很费劲。
7. **未命中时还会试「补一层顶层目录」**：对应把 `/Game/A/UI/X`
   写成 `/Game/UI/X` 这类少写一级的批量问题。

## 踩过的坑

**绝不能按挂载点切分「疑似粘连」的路径。**

第一版想处理一个匹配里粘连两条路径的情况（`/Game/A/Game/B`），
于是按 `/Game/` 切开。结果这个工程里恰好有一层目录也叫 `Game`，
`/Game/Game/UI/X` 这类完全合法的路径被砍成了 `/Game/UI/X`，
**凭空造出 1500 多条「资源缺失」**，让缺失数虚高十倍以上。

用原始字节交叉验证才发现：字符串长度前缀精确对应完整路径，
而切出来的短路径根本不存在。修正后缺失数从约 1900 降到 151。

教训：**当工具报出的「系统性大问题」数量级异常时，先怀疑工具自己。**
宁可漏报，也不要把正常路径切坏。

## 已知限制

- 只覆盖配置里的挂载点（默认 `/Game/`），不含引擎与插件挂载点
- 只扫配置指定的目录，不递归整个工程
- 只读磁盘，不读 pak / chunk；热更资源在本地看不到
- 拼接出来的路径无法静态求值，只能标 `DYNAMIC`
- 资产引用是字节级提取，个别边可能是噪音（已用分类与 `hint` 标注，
  但不要把它当成引擎 Asset Registry 的等价物）
- 「路径清单」型资产（内部存了大量历史路径字符串）出度异常高，
  会把影响面放大，需要人工确认
- 表格解析针对「首行为字段名标记」的导表约定做了优化，
  也保留了退化路径（把首行当字段名），但对复杂多表头结构支持有限

## 目录结构

```
refcheck.py           统一入口（scan / index / query / all / init）
refcheck_lib.py       共享库：配置、路径归一化、资产索引、引用提取、xlsx 解析
scan_lua.py           正向扫描
build_index.py        构建反向索引
query.py              反向查询
refcheck.ini          配置模板
examples/
  make_demo.py        生成迷你示例工程（不含真实数据）
  demo/               生成物
```

## 许可证

MIT
