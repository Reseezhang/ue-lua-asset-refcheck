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
