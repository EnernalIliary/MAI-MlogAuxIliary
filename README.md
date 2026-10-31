# MAI · MlogAuxiliary

Mindustry 的逻辑助手模组：**F3 框选处理器 → 用一句话描述意图 → 模型写 mlog → 静态预检 → 写进处理器**。
生成、审查、写入全在游戏内完成，模型只负责「写」，能不能写由本地检查器说了算。

- 模组 id：`mlog-auxiliary`
- 当前版本：`0.14.6`（Mindustry v160.7 / server jar 160.7）
- 后端：随包的 `sidecar/bin/logicd.exe`（Python 打的冻结程序，`127.0.0.1:8765`，模组自己拉起）
- 模型：OpenAI 兼容的 `/chat/completions` 端点（自填地址 / 密钥 / 模型名，密钥只存本机）

## 装

两种方式选一种，**不要同时用**（同一个模组 id 只会加载一个，谁赢看目录遍历顺序）。

**A. 游戏内导入（推荐给玩家）**

下载 Release 里的 `MAI-x.y.z-mod.zip` → 游戏主菜单 模组 → 导入 → 选中该 zip → 重启游戏。
首次使用时模组会把后端解到数据目录（`mlogauxiliary/sidecar/<版本>/`，约 40 MB），hint 会提示进度。

**B. 双击装机（解压发行包）**

下载 Release 里的 `MAI-x.y.z.zip`，解压后双击 `安装.bat`（或 `powershell -ExecutionPolicy Bypass -File install.ps1`）。
装机脚本自动找存档目录，把旧版本改名成 `MlogAuxiliary.bak-<时间戳>` 再装新的。
卸载跑 `卸载.bat`；`停止sidecar.bat` 用来掐掉卡住的后端进程。

## 用

1. 设置 → Mlog辅助 → 大模型：填地址（`https://…/v1` 即可，sidecar 会自动补全）、密钥、模型名，点「启用模型档」。
2. 游戏内按 **F3**，在处理器上按下拖出一个框 → 松手弹描述框 → 写「每 5 秒把容器 1 的铜送到核心」这类意图 → 确认。
3. 框内每台处理器各得一个作业，右下角徽标显示进度；点徽标看思考过程、问题清单、模型假定的前提与成本。
4. 预检通过才写入；写入失败（越权、联机服务端拒绝）会明确提示。写入成功后框自动消失。

框选同时是一个**工作区快照**：写完不消失，可以继续「改进」，也可以归档 / 释放（归档的进「工作区快照」对话框）。
AI 写出的代码带签名（`aigenerated:<作者>:<id>`）与可解析的载荷，便于事后追查是哪个作业、哪个工作区生成的。

## 仓库里有什么

```
MlogAuxiliary/            模组本体（mod.hjson、scripts/main.js、游戏内快检 quickcheck.js + 规则表）
tools/                    后端与工具链源码（sidecar、检查器、IR、蓝图、模拟器、打包器）
rules/*.json              检查规则（41 条，32 条已启用）
tests/                    行为级测试：检查器、规则覆盖、游戏内快检、UI 冒烟、sidecar 契约、打包
packaging/                装机脚本与使用说明
```

`tools/` 里没有的东西（游戏源码、原始贴图、个人存档、历届产物）一律不进本仓。

## 开发

```powershell
node --check MlogAuxiliary\scripts\main.js     # 模组语法
python tests\run_tests.py                      # 检查器 46 条摘要（当前 46/46）
python tests\shapes\run_shapes.py              # 检查器自身的语句形状不变量
python tests\ruleids\run_ruleids.py            # 每条规则必须在真实用例上开火
python tests\mlogauxiliary\run_quickcheck_tests.py  # 游戏内快检与 tools/ 规则表不漂移
python tests\logicd\run_logicd_tests.py        # sidecar 协议契约（写入路径需要 JRE + server jar，缺则 SKIP）
python tests\packaging\run_pack_tests.py       # 打包/装机回环
python tools\package.py                        # 打发行包（需要 PyInstaller）
```

需要 JRE 21 与 `out/_server160.jar` 的步骤会自己 SKIP，不会假装通过。

## 许可

MIT，见 [LICENSE](LICENSE)。本仓不含任何 Mindustry 原始素材或反编译源码。

分析器与 DSH 技能的那一份在 [EnernalIliary/mlog-agentskill](https://github.com/EnernalIliary/mlog-agentskill)。
