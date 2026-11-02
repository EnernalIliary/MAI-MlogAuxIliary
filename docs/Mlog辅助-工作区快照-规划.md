# 工作区快照（Workspace Snapshot）规划

状态：规划稿，未实现。需求提出于用户 m03632；用户 m03723 要求「先完成两个小功能，然后规划一下工作区快照再继续」
（两个小功能 = v0.13.2 的 F1/F2，已完成并提交 `866589a`）。

## 1. 用户要什么（原话拆解）

> 「相当于每个选框就是一个工作区快照。即使写完一次逻辑也不会让工作区消失，保留上下文，可以选择归档或释放工作区，
> 释放工作区就不再留存上下文。仿照蓝图界面写一个工作区快照界面存放归档的工作区。可以直接从蓝图页面创建工作区。」

拆成四条可验收的行为：

1. **选框 = 工作区**：一次 F3 框选产生一个持久的工作区，而不是「一个用完即弃的批处理」。
2. **写完不消失、保留上下文**：写进处理器之后，这个工作区还在，下次在同一区域投递时模型能看到之前写过什么、
   试过什么、为什么失败 —— 也就是「允许继续改进」。
3. **两个显式动作**：归档（留着，但从活跃列表里收起来）与释放（不再留存上下文，等于删掉）。
4. **一个界面**：仿原版蓝图界面，列出归档的工作区；并且可以从蓝图页直接创建工作区。

## 2. 现状（这决定了改动面有多大）

| 现有件 | 位置 | 与工作区的关系 |
| --- | --- | --- |
| `jobBox`（当前框选的运行时态）`{r, ids, jobs, pend, started, termAt, lastPoll}` | `MlogAuxiliary/scripts/main.js:1909` | 工作区要**引用**它，而不是取代它 |
| `startJobBatch(targets, desc, rect)` | `main.js:2505` | 建工作区的时机（框选松手、描述确认） |
| `jobBody(b, desc)`（作业请求体，含 13×13 区域快照） | `main.js:1922` | 要加 `workspace` 字段 |
| `jobBoxTick()` / `jobBoxWritten(b)` / `closeJobBox(why)` | `main.js:2277 / 2270 / 2252` | 写入成功后要回写工作区（`code`/`accepted`/`history`） |
| `saveUndoTo(root, b, why, batch)` | `main.js:963` | **现成的磁盘持久化范式**：`Vars.dataDirectory.child("mlogauxiliary").child("undo")` + `writeString` + `root.list(".json")` + `delete()` |
| `openJobManager()` / `renderManager(host, d)` | `main.js:2666 / 2628` | 界面范式：`new Packages.arc.scene.ui.Dialog(...)` + `d.cont.pane(host)` + `d.buttons.button(...)`；异步列表用 `managerSeq` 防迟到回调 |
| `linkListOrNull(b)` / `procKey(b)` / `originOf(b)` | `main.js` 内 | 处理器身份：`"x,y"` |
| `regionSnapshot(x1,y1,x2,y2)` | `main.js:3067` | 工作区存世界事实时复用（v0.12.9 起五段齐全） |
| `api.mai*Probe` 系列 | `main.js` 尾部 | 自检口范式，工作区要有 `maiWorkspaceProbe` |
| `Core.settings.put/getString` | 已在用（设置项、`URL_KEY`） | 小状态（当前工作区 id）可以走这里 |

**关键结论**：模组已经有「文件级持久化 + 对话框 + 自检口」三套成熟范式，工作区不需要新的基础设施，
只是把 `jobBox` 的生命周期从「一批」拉长到「一个区域的历史」。

## 3. 数据模型（schema 1）

落盘位置：`Vars.dataDirectory/mlogauxiliary/workspaces/<id>.json`（与 undo 同级，跟着存档走，换存档自动隔离）。

```json
{
  "schema": 1,
  "id": "ws-20261031-2210-a1b2",
  "name": "东侧炮台",
  "state": "active",                    // active | archived | released(只在内存里短暂存在)
  "map": "FrozenForest",
  "world": {"width": 400, "height": 400},
  "bounds": {"x1": 283, "y1": 225, "x2": 290, "y2": 232},
  "created": 1761954600000,
  "updated": 1761955200000,
  "notes": "",                          // 玩家手写的备注（「炮台要省电」）
  "procs": [
    {"k": "286,227", "x": 286, "y": 227, "block": "logic-processor",
     "desc": "控制炮台攻击玩家",
     "links": [{"name": "salvo1", "x": 283, "y": 228, "block": "salvo"}],
     "code": "radar player ...",        // 最后一次成功写进去的代码（可能为空）
     "accepted": {"at": 1761955100000, "job": "43fe34c6", "statements": 16, "bytes": 420},
     "history": [                        // 最近 20 次投递，倒序
       {"at": 1761955100000, "job": "43fe34c6", "state": "done", "desc": "…",
        "statements": 16, "findings": 0}
     ]}
  ],
  "jobs": ["43fe34c6", "…"],             // 该工作区投过的作业 id，最多 50
  "archivedAt": null
}
```

设计取舍：

- **不存世界快照**：`regionSnapshot` 每次投递现算（v0.12.9 起足够便宜），工作区只存「上下文」，避免文件膨胀。
- **`procs[].code` 是「工作区的当前版本」**：写入成功后从 `b.code` 回读（联机里 `armWriteCheck` 那条路已经保证
  只有服务端真收下才更新）。它同时是「继续改进」的 basis。
- **`history` 与 `jobs` 分开**：`history` 是给模型/面板看的人话摘要，`jobs` 是给 sidecar 查详情用的 id 列表。
- 单文件估算 2–6 KB；上限 **200 个工作区**、每工作区 `history ≤ 20`、`jobs ≤ 50`，超限按 `updated` 淘汰
  （先淘汰 `released` 残留，再淘汰最老的 archived）。

## 4. 生命周期

```
F3 框选松手
   │  选框中心/集合落在某个 active 工作区的 bounds 内，且 map+world 一致？
   ├─ 是 → 复用该工作区（更新 bounds 并集、procs 并集）
   └─ 否 → 新建 active 工作区（名字自动：地图名 + 序号，可改）
        │
        ├─ 投递作业（job 带 workspace.id）
        ├─ 作业终态 → 写 history；done 且写入成功 → 写 procs[].code / accepted
        │
        ├─「归档」 → state=archived，从活跃列表移到归档列表（磁盘不动）
        │      └─「恢复」 → state=active，把选框重新画到地图上
        │
        └─「释放」 → 删 <id>.json，内存里清掉（= 不再留存上下文）
```

三个必须写清的边界：

1. **地图变了怎么办**：`map` 或 `world` 对不上 → 工作区标 `stale`（只读，不能写入，可以看历史），
   恢复时提示「这张图已经不一样了，上下文只作参考」。
2. **方块没了怎么办**：`procs` 里某台处理器拆了 → 保留它的历史（那是上下文），但投递时跳过它。
3. **释放是破坏性的**：释放前弹一次确认（`arc.scene.ui.Dialog` 的两按钮确认框，无头环境走 `hint` 文案）。

## 5. 界面

### 5.1 工作区快照对话框（仿蓝图界面）

> v0.19.0 界面改版（双栏 + 卡片行 + 世界联动）的细化方案见 [`Mlog辅助-工作区快照-界面改版-方案.md`](Mlog辅助-工作区快照-界面改版-方案.md)；下面这段 ASCII 是 v0.14 的初版，留作对照。

```
┌ Mlog辅助 · 工作区快照 v0.14.x ─────────────────────────────┐
│ [活跃] 东侧炮台     4 台  21:10 更新                        │
│ [活跃] 铜矿传送带   2 台  20:58 更新                        │
│ [归档] 旧炮台       4 台  昨天                              │
│ ─────────────────────────────────────────────────────────── │
│ 预览：FrozenForest (283,225)-(290,232) · 4 台处理器         │
│   @286,227  控制炮台攻击玩家   16 条 · 0 ERR · 已写入       │
│   @287,227  控制炮台攻击玩家   16 条 · 0 ERR · 已写入       │
│ 最近 3 次投递：done(16 条) / failed(模型没写出可用代码) …   │
│ [恢复选框] [归档] [释放] [改名] [导出 JSON]                 │
└─────────────────────────────────────────────────────────────┘
```

- 列表按 `updated` 倒序，`active` 在前、`archived` 分组在后；每行 `名字 · N 台 · 时间`。
- 右侧预览复用作业面板的排版习惯（`host.add(...).row()`），代码只显示前 8 行摘要。
- 「恢复选框」= `jobBox` 重新指向该工作区的 bounds 与处理器集合，并把矩形画到世界上（复用 v0.12.7 的常驻框）。

### 5.2 蓝图页入口

第一版**不 hook 原版 `SchematicsDialog`**（改它的 cont 风险大、且原版会重建）：
在我们的对话框里加一节「从蓝图创建工作区」，用 `Vars.schematics.all()` 列蓝图，
选一个 → 用蓝图的方块包围盒作 `bounds`、`procs` 留空（等玩家真的框到处理器再填）。
第二版再考虑在原版蓝图页加按钮（需要确认 v160.7 的 `SchematicsDialog` 是否提供插槽）。

### 5.3 与作业管理窗打通

`openJobManager()` 的作业行前缀加所属工作区名（`[东侧炮台] 43fe34c6 done 16 条`），
点进去能看到「这个工作区的历史」。

## 6. 「继续改进」怎么落地（这才是用户真正要的）

现在投递是 `mode: "new"`，模型从零写。有了工作区之后新增 `mode: "improve"`：

- `jobBody()` 里带 `workspace: {id, name, notes, prevCode, prevJob, prevFindings}`；
- sidecar `tools/mlogprovider.py:build_prompt()` 新增一段（限 1200 字符）：
  「这是同一块区域上一次的版本，玩家要求继续改进：… 上一次的检查器结论：… 不要退回旧写法」；
- 面板上「继续改进」按钮 = 同一工作区、同一批处理器、把上一版代码当 basis 投一单；
- 拒绝/失败也进 `history`，所以「上次为什么没成」在 prompt 里是可见的。

风险：prompt 变长 → token 预算（`DEFAULT_MAX_TOKENS = 4096`）要重新评估；`workspace` 段必须截断。

## 7. 分阶段与验收

| 阶段 | 内容 | 验收（机器可验） | 估时 |
| --- | --- | --- | --- |
| **W1 数据层**（v0.14.0，已交付） | 工作区读写（`wsRoot()`/`wsList()`/`wsLoad()`/`wsSave()`/`wsRelease()`）、F3 自动建/复用、写入成功回写 `code/accepted/history`、`api.maiWorkspaceProbe` | ui smoke `MAIWORKSPACE:ok dir=yes mem=yes`、磁盘上真有 `<id>.json`、`node --check` | 2–3 天 |
| **W2 界面**（v0.14.1，已交付） | 工作区快照对话框 `openWorkspaceDialog()`（列表按 active/archived 分组、预览、恢复选框/归档/释放/改名/导出 JSON） | ui smoke `MAIWS2:ok`（rowlabel/bounds/proclines/joblines/export/import/rename/restorebox）+ 红线 `function openWorkspaceDialog(` | 2–3 天 |
| **W3 继续改进**（v0.14.1，已交付） | `mode:"improve"`（模组 `wsImprovePayload()` → 作业 `workspace{...}`）+ `job_id()` 只在 improve 计入 prevCode + prompt 的 `=== previous version (same area) ===` 段（≤ 1200 字符）+ 面板「继续改进」/「改进上一版」按钮 | `tests/logicd` 的 `test_improve()` 7 组断言（protocol 26 / jobs 16 / provider 12 / improve 7 / llm 12）、`MAIWS2:ok` 的 payload 项 | 2 天 |
| **W4 蓝图页 + 导入导出**（v0.14.1，已交付） | 「从蓝图创建工作区」（`blueprintList()` 吃 `Vars.schematics === null` 回空数组）、导出 `wsExportText()`/`wsExportToDisk()`、导入 `wsImportText()`（复制、撞 id 换新 id） | `MAIWS2:ok` 的 blueprint / blueprint-miss / export / import / import-id 项 | 1–2 天 |
| **W5（可选）** | sidecar 索引工作区（`out/workspaces` 镜像），CLI/工具链可读 | 门禁新步或 logicd 测试 | 1–2 天 |

合计约 **8–12 个工作日**。W1 单独上线也有价值（上下文落盘 + 框选不再是一次性的），W3 是用户感知最强的一步。

## 8. 风险与边界

- **`Vars.dataDirectory` 在无头环境**：`saveUndoTo` 已在用同一路径，但工作区代码要全程 try/catch，
  失败时降级到 `Core.settings`（单键字符串，上限 32 KB，只够存当前工作区）。
- **文件膨胀**：见第 3 节上限；`wsList()` 每次只读文件名 + 首 200 字节的元数据（避免全量 parse）。
- **多工作区重叠**：两个工作区的 bounds 相交时，按「选框中心落在谁里面」判定归属，不合并（合并留给玩家显式操作）。
- **联机**：工作区是本地上下文，不广播；联机里写入仍走 `permWhy()` + `armWriteCheck()` 两条闸。
- **与 undo 的关系**：工作区记上下文，undo 记回滚点；`accepted.job` 让「回到某个已接受的版本」可行（W3 可加）。
- **不做**：跨存档共享工作区、云端同步、把工作区当蓝图导出成 `.msch`（蓝图导出已有 `msch.py`，不重复）。

## 9. 断言表预留

实现时新增两条：

- **L2-66**（W1，已写入 `docs/断言表.md`）工作区必须落盘且可复用：F3 在同一区域第二次框选必须复用同一个 `<id>.json`（不是新建），
  写入成功后 `procs[].code` 必须等于处理器回读的代码；验法 `MAIWORKSPACE:ok` + 磁盘断言。
- **L2-67**（W2/W3/W4，已写入 `docs/断言表.md`）工作区要有界面、要能「接着上一版改进」、要能搬走：
  界面 `MAIWS2:ok`；「继续改进」必须带上一版上下文（prompt 里出现 `prevCode` 片段且整段 ≤ 1200 字符，
  `job_id()` 只在 improve 时把 prevCode 计入 blob）；导入导出往返（导入换新 id，不覆盖）。

**实施记录**：W1 → v0.14.0（提交 `5df6ece`）；W2+W3+W4 → v0.14.1（模组侧 `main.js` + sidecar 侧
`tools/jobqueue.py`/`tools/mlogprovider.py`/`tools/logicd.py`，`SIDECAR_VERSION='0.14.0'`）；v0.14.2 修掉 W1/W2 在 UI 层被抹掉的一半（写入成功不再 `closeJobBox()`，框角换成「查看 / 取消」两个固定像素按钮，`jobBox.written` 计数），并把 `@aigenerated` 签名并入 sidecar 的写入口（作业 `code` 就是写进处理器的那份，带 `sig`）。W5（sidecar 索引）仍可选、未做。
