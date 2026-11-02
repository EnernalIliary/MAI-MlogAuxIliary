# MAI 工作区快照 · 界面改版方案

> 状态：**方案稿**（用户 m00325 选「1. 先写方案文档」：ASCII 稿 + 改动点 + 验收，看过再开工）。
> 细化了 `docs/Mlog辅助-工作区快照-规划.md` §5.1（界面）——数据模型与生命周期**不动**，本文只谈界面。
> 行号基准：v0.18.4 提交 `7444ec7` 的 `MlogAuxiliary/scripts/main.js`（实现时会漂移，以函数名为锚）。

## 1. 一句话结论

**A + C，版本 v0.19.0**：把「列表 → 点进 → 列表消失 → 纯文本堆」改成**左列表 300 / 右详情 440 双栏常驻**，列表行做成带状态色条的卡片，动作分层（主 / 次 / 危险 + 释放前确认），再把所有 active 工作区的矩形**画到世界上**、点行把镜头带过去。

- 不动 schema（仍是 1）、不动 `workspaces/<id>.json` 字段、不动 sidecar、不动作业流程 ⇒ **零迁移、老存档照读**。
- 只改 `main.js` 的工作区 UI 段 + 一个世界绘制钩子 + 探测口 + smoke 断言；预估 **+330 / −90 行**，2–2.5 天。
- 缩略图（方案 B）**不在本期**：要 `Vars.schematics.create` + `SchematicImage` 渲染，无图集环境还要退化，收益/风险比不如 A+C，留待真机看过 A+C 再定。

## 2. 现状取证（为什么现在难用）

| # | 现在 | 代码位置 | 难用在哪 |
|---|---|---|---|
| 1 | 列表和详情**共用同一个 pane**，进详情先 `host.clear()` | `renderWorkspaces` :4869 / `renderWorkspaceDetail` :4830（clear 在 :4832） | 点一个工作区，列表就没了；想换一个必须先「返回列表」(:4866)，来回跳 |
| 2 | 行文本 = `名字 · N 台 · 进行中 · 08-31 21:10` | `wsRowLabel` :4668 | 没有状态色、没有空间信息、没有最近作业结果 —— 8 个工作区要一行行读才知道哪个刚失败过 |
| 3 | 详情是纯文本堆：`--- 处理器（N 台）---` / `--- 最近的作业 ---` | :4838 / :4841 | 列不对齐（`@x,y 处理器 行数 · 描述` 全挤成一句），扫读困难 |
| 4 | 详情只显示 **8 台处理器 / 3 条作业**，多的一句「… 还有 N 台」 | `wsProcLines(ws,8)` :4839、`wsJobLines(ws,3)` :4842 | 一个 20 台的工作区看不到全景 |
| 5 | 范围只有一行数字 `地图 FrozenForest (283,225)-(290,232)` | `wsBoundsText` :4674 | 不知道这块地在图上哪儿，也没有「带我过去」 |
| 6 | 7 个按钮等宽 160px 平铺，**「释放」和「存成蓝图」长得一样** | :4844-:4865（释放 :4859-:4862） | 危险操作没有区分；「释放」直接 `wsRelease`(:2332)，**规划稿 §4 要求的确认弹窗根本没实现** |
| 7 | active / archived 只靠两行文字标题分隔 | :4873 / :4885 | 没有搜索、没有过滤、没有排序；工作区一多（上限 200）只能滚 |
| 8 | 对话框 pane 固定 600×320 | `openWorkspaceDialog` :5470（尺寸 :5475） | 双栏放不下，要放大到 760×420 |

## 3. 目标与边界

**做**：双栏常驻布局；卡片行（状态色 / 副行信息）；详情结构化（处理器表 + 作业时间线）；搜索/过滤/排序；动作分层 + 释放确认；世界矩形 + 镜头定位。

**不做**：数据模型/schema 改动；sidecar 索引（规划稿 W5）；缩略图（方案 B）；「继续改进」的语义（`wsImprovePayload` :4786 / `wsImproveBatch` :4812 保持原样）；作业管理窗（v0.12.5）的任何改动；底栏那排（刷新 / 撤销上次写入 / 撤销快照 / 从蓝图创建 / 关闭，:5476-:5487）保持原样。

**兼容**：`wsRowLabel`(:4668)、`wsBoundsText`(:4674)、`wsProcLines`(:4680)、`wsJobLines`(:4696) 全部**保留并保持返回字符串**——smoke 的 `MAIWS2:ok` 直接断言它们（`tests/mlogauxiliary/run_ui_smoke.py:561`，探针在 main.js :6958）。新界面改用新的**纯函数模型**，旧函数变成新函数的「一行摘要」实现。

## 4. 界面设计

### 4.1 整体布局（760×420，双栏常驻）

```
┌ Mlog辅助 · 工作区快照 v0.19.0 ─────────────────────────────── ✕ ┐
│ [搜索名字/描述/备注…        ]  [全部][进行中][已归档][地图不符]  [排序:最近更新 ▾] │
├─────────────────────────────┬───────────────────────────────────┤
│ ▎赤红核心 改进            ▸ │ 赤红核心 改进          [进行中]    │
│ ▎4 台 · 08-31 21:10 · 完成  │ 地图 FrozenForest (283,225)-(290,232) │
│                             │ 8×8 格                [⌖ 定位]      │
│ ▎硅厂流水线                │ ── 处理器 4 台 ────────────────────│
│ ▎12 台 · 08-30 19:02 · 失败 │ @283,225  microprocessor  24 行 写2 │
│                             │ @284,225  microprocessor  18 行 写1 │
│ ┃ 旧框（地图不符）          │ …                                 │
│ ┃3 台 · 08-20 11:40          │ ── 最近作业 ───────────────────────│
│                             │ ● 完成  #j9  改进赤红核心  21:10    │
│                             │ ● 失败  #j8  调整投放      20:44    │
│                             │ ○ 排队  #j7  …                      │
│                             ├───────────────────────────────────┤
│                             │ [恢复选框] [继续改进]              │
│                             │ 存成蓝图 归档 改名 导出            │
│                             │                    [释放工作区…]  │
├─────────────────────────────┴───────────────────────────────────┤
│ 刷新  撤销上次写入  撤销快照  从蓝图创建            关闭        │
└─────────────────────────────────────────────────────────────────┘
```

- 左列 300（固定宽），右列 440（`growX`），中间 1px 分隔（`Tex.whiteui` 竖条）。
- 左右各自 `ScrollPane`，**互不挤掉**：选中行 → 右侧刷新；右侧不重建左侧。
- 空态（左列）：
  ```
  还没有工作区。
  框选一片逻辑区域开始干活，
  或用下面「从蓝图创建」。
  ```
- 小屏降级：可用宽度 < 620 时退化成**单栏**（列表 → 点行进详情，详情顶部保留「‹ 返回列表」）。判定在 `openWorkspaceDialog` 建 pane 时做一次（`Core.graphics.getWidth()`），不做运行时重排 —— 重排是 bug 温床。

### 4.2 列表行（卡片）

单行放大（高 54）：

```
┌─┬───────────────────────────────────────────┬─┐
│▎│ 赤红核心 改进                    [地图不符] │▸│
│▎│ 4 台 · 08-31 21:10 · 最近作业 完成         │ │
└─┴───────────────────────────────────────────┴─┘
 4px  名字（状态色）               副行（灰）        选中标记
```

- 左侧 4px 竖条 = `row.image(Tex.whiteui).width(4).growY().color(statusColor)`（`Tex.whiteui` 用法已在 :621、:928）。
- 名字颜色 = 状态色；副行 = `Pal.gray`（副行文字用 `setColor`，不用 `[gray]` markup —— 本仓目前 0 处 markup，见 §5）。
- 选中行：副行末尾加 `▸`，并整行底色换成 `Styles.flatOver`（拿不到就只靠 `▸`，不抛）。
- 行高 54、行间 4px、圆角不做（arc Table 没有现成圆角，别自造）。

**状态色语义**（三态，够用且互斥）：

| 状态 | 判定 | 色 | 行尾 chip |
|---|---|---|---|
| 进行中 | `state === "active"` 且地图一致 | `themeColor()`（:928，用户可调） | — |
| 已归档 | `state === "archived"` | `Pal.gray` | — |
| 地图不符 | 任意 state，且 `ws.map !== 当前地图名` | `Pal.remove`（**本 checkout 没有 `Pal.scarlet`**，见 §9） | `[地图不符]` |

地图名取法：`Vars.state.map.name()`（try 包住；拿不到就当一致，不误报）。

**副行内容**（三段，用 `·` 连）：`N 台` · `最后更新 MM-DD HH:MM`（`wsTimeText` :4658）· `最近作业 <状态>`（无作业时省掉第三段）。最近作业取 `ws.jobs` 最后一条，状态用现成的 `stateCn()`（:4700 处已在用）。

### 4.3 详情面板

1. **头部**：名字（20 号，状态色）+ 状态 chip（进行中 / 已归档 / 地图不符）。
2. **范围**：`地图 X  (x1,y1)-(x2,y2)` + `W×H 格`（`wsBoundsText` 的字符串 + 新增尺寸）+ `[⌖ 定位]` 按钮（地图不符时禁用 + tooltip 说明）。
3. **处理器表**：表头 `位置 | 方块 | 代码 | 写入`，行 = `@x,y | microprocessor | 24 行 | 2 次`，列宽固定（位置 70 / 方块 110 / 代码 growX / 写入 60），**最多 12 行**，超出显示「… 还有 N 台（共 M 台）」。代码为空显示「未写入」。
4. **作业时间线**：最后 **6** 条，倒序；左侧 8×8 色点（完成 = 主题色、失败 = `Pal.remove`、进行中/排队 = gray），右侧 `状态  #id  描述（截 30 字）  MM-DD HH:MM`。空时显示「还没有投过作业」。
5. **备注**：`ws.notes` 非空时显示一行「备注：…」（截 120 字）。

### 4.4 动作分层

| 层级 | 按钮 | 位置 | 行为 |
|---|---|---|---|
| 主 | `恢复选框` `继续改进` | 详情底部第一行，高 40，主题色 | `wsRestoreBox` :4709 / 现有 improve 入口 |
| 次 | `存成蓝图` `归档`（已归档则显示 `恢复`）`改名` `导出 JSON` | 第二行，普通按钮 | `blueprintSaveBoxAsk` :4963 / `wsArchive` :2313 / `wsRestore` :2322 / `wsRename` :4722 / `wsExportToDisk` :4740 |
| 危险 | `释放工作区…` | 右下角，红字，右对齐 | `Vars.ui.showConfirm` → 确认后 `wsRelease` :2332 |
| 常驻 | `刷新` `撤销上次写入` `撤销快照` `从蓝图创建` `关闭` | 底栏（不变） | 原样 |

**释放确认**（补上规划稿 §4 的欠账）：

```
Vars.ui.showConfirm("释放工作区", "「赤红核心 改进」的 4 台处理器代码与 9 条作业记录会被删除，且不能恢复。地图上的逻辑方块不受影响。", function(){ wsRelease(id); refresh(); });
```

按钮文案直接 `释放`（`showConfirm` 自带「确定/取消」，不重复）。无头环境（`Vars.ui === null`）直接执行释放 —— 探测口要能跑通。

### 4.5 搜索 / 过滤 / 排序

- **搜索**：`new Packages.arc.scene.ui.TextField()`（设置页已用文本框，见 :619 附近），输入即筛，`change` 回调里重画左列。
  匹配范围：`name` / `notes` / 每个 `procs[i].desc`，全部转小写 `indexOf`。空串 = 全显示。
- **过滤**：4 个 chip 按钮 `全部 | 进行中 | 已归档 | 地图不符`，单选，选中 chip 用主题色。默认 `全部`（不再按 active/archived 分两段标题——排序和过滤已经把这件事说清楚了）。
- **排序**：一个按钮循环 `最近更新 ▾ → 名字 ▾ → 处理器数 ▾`（`SelectBox` 在 Rhino 里要处理 adapter，不如按钮循环稳）。
- 过滤后为空：「没有匹配的工作区（共 N 个）」+「清空筛选」按钮。

### 4.6 地图联动（方案 C）

- 对话框打开期间，把**所有 active 工作区**的矩形画到世界上：`Lines`（:3795 `Packages.arc.graphics.g2d.Lines`）+ `Lines.rect(x,y,w,h)`（左下角 + 宽高，:6372 注释），坐标换算照现有选框：格 × 8。
- 线宽/颜色：普通 active = `stroke(1)` + 主题色 35% alpha；当前选中行 = `stroke(2)` + 主题色全亮。**不画 archived**（地图上已无意义），**不画填充**（原版选框就无填充，:6382）。
- 与 `jobBox` 选框共存：jobBox 是粗双描边（:6382 那套），工作区矩形是细单线 + 半透明，二者可同屏；工作区矩形**只在对话框打开时画**，关闭即停（`Events.run(ET.Trigger.draw)` 里判 `wsDialogOpen` 标志）。
- 绘制实现：新函数 `drawWorkspaceGhosts()`，挂在 **现有 draw 钩子**里（`Events.run(ET.Trigger.draw, ...)` :7101，:7099-:7104）—— `try{ drawSelection(); drawWorkspaceGhosts(); }catch(e){}`，**每帧回调绝不外抛**（照 :7095 的约定）。
- **定位**：详情「⌖ 定位」+ 行内 `▸` 旁的「⌖」小按钮 → `Core.camera.position.set(cx*8+4, cy*8+4)`（先例 :5581，已 try/catch）。地图不符时禁用。
- **悬停高亮**（选中即高亮的加强版）：`Table.addListener(new Packages.arc.scene.event.InputListener())` 覆写 `entered`/`exit`。
  ⚠ 本仓**没有 InputListener 子类化的先例**（:515-:520 只用了现成的 `Tooltip` 当 listener）⇒ **标记为「需真机验证」**，失败时退化为「选中行高亮」（不依赖悬停，一定能用）。

## 5. 技术落点

### 5.1 复用（不重写）

| 用途 | 现有实现 |
|---|---|
| 灰底行 | `realRow()` :914（`Styles.grayPanel`，无头退化） |
| 分区标题 | `section(t,title)` :919 |
| 主题色 | `themeColor()` :928 |
| 悬浮提示 | `tipOn(el,text)` :513（先判 `Core.scene`，无头安全） |
| 时间 | `wsTimeText` :4658 |
| 状态中文 | `stateCn`（:4700 已在用） |
| 世界线框 | `Lines.rect` / `Lines.stroke`（:6372-:6395 一套） |
| 镜头 | `Core.camera.position.set`（:5581） |
| 世界绘制钩子 | `Events.run(ET.Trigger.draw)` :7101 |

### 5.2 新增（全部纯函数优先，方便无头测）

```js
// —— 纯函数（无 UI 依赖，探测口直接调）——
wsStatusOf(ws, mapName)        // "active" | "archived" | "mismatch"
wsRowParts(ws, mapName)        // {name, state, colorKey, procs, when, lastJob, mismatch}
wsDetailModel(ws, mapName)     // {header, chip, boundsLine, cells, procs:[{pos,block,code,written}], jobs:[{state,id,desc,at}], notes}
wsFilterList(list, opt)        // opt = {q, state}  → 过滤后的数组（不改原数组）
wsSortList(list, key)          // key = "updated" | "name" | "procs"
wsReleaseConfirmText(ws)       // 释放确认的正文（含名字、处理器数、作业数）
wsGhostRects(list, mapName)    // [{x1,y1,x2,y2,selected}]，只含 active 且地图一致

// —— 渲染（Vars.ui 为 null 时直接 return）——
renderWorkspaceList(host, list, sel)   // 左列
renderWorkspaceDetailPane(host, ws)    // 右列（替代 renderWorkspaceDetail 的文本堆）
drawWorkspaceGhosts()                  // 世界线框，挂 Trigger.draw
wsLocate(ws)                           // 镜头跳转（try/catch，地图不符返回 "地图不符"）

// —— 探测口 ——
api.maiWorkspace3Probe()   // 返回 "ok k=v k=v" 或 "bad a,b"
```

`wsRowLabel` / `wsBoundsText` / `wsProcLines` / `wsJobLines` **保留**，内部改成从 `wsRowParts` / `wsDetailModel` 拼字符串 ⇒ `MAIWS2:ok` 不动。

### 5.3 探测口契约（`MAIWS3:`）

```
ok rows=3 filt=2 sort=name detail=4 ghosts=2 release=1 mismatch=1
```

| 键 | 断言什么 |
|---|---|
| `rows` | `wsRowParts` 造出 3 个 active 工作区后行模型数量对，且含名字 |
| `filt` | `wsFilterList` 用 `{q:"探测", state:"active"}` 过滤出预期条数（含按 desc / notes 命中各一次） |
| `sort` | `wsSortList` 三种 key 排序结果首元素正确（名字升序 / 更新降序 / 处理器数降序） |
| `detail` | `wsDetailModel` 的 procs 条数与 jobs 条数对，且超 12 台被截断成 12 |
| `ghosts` | `wsGhostRects` 只返回 active + 地图一致者（造一个 archived、一个异地图，验证被剔除） |
| `release` | `wsReleaseConfirmText` 含工作区名与「不能恢复」 |
| `mismatch` | `wsStatusOf` 对异地图返回 `"mismatch"`，对同图 active 返回 `"active"` |

无头环境下 `wsRowParts` 里 `colorKey` 只返回字符串（实现取 `"active"`/`"archived"`/`"mismatch"`，即 `state` 本身），不碰 `Pal`；`wsStatusColor()` 才把它换成 `Color`。

## 6. 改动清单

| 文件 | 改动 | 预估 |
|---|---|---|
| `MlogAuxiliary/scripts/main.js` | 新增 §5.2 函数；重写 `renderWorkspaces`(:4869) / `renderWorkspaceDetail`(:4830)；`openWorkspaceDialog`(:5470) 尺寸与双栏骨架；draw 钩子(:7101) 加 `drawWorkspaceGhosts`；`api.maiWorkspace3Probe` | +330 / −90 |
| `tests/mlogauxiliary/run_ui_smoke.py` | 打 `MAIWS3:` 并断言 `MAIWS3:ok`（:561 旁）；前缀白名单（:400、:1846 两处）；新增 code_only 红线（:1156 那组里） | +25 |
| `docs/断言表.md` | 追加 L2-94（界面重排 + 释放确认）、L2-95（地图联动），口径行数字同步 | +2 |
| `CHANGELOG.md` / `docs/Mlog辅助-规划.md` | 0.19.0 段 / 版本表一行 | +6 |
| `out/takeover-20261024/00-HANDOVER.md` + 新报告 `101-v0190-wsui.md` | 收尾记录 | +60 |

`mod.hjson` version → 0.19.0；`tools/logicd.py` `SIDECAR_VERSION` 不动（后端没改）。

**code_only 红线**（防以后误删）：`function wsRowParts(`、`function wsDetailModel(`、`function wsFilterList(`、`function wsGhostRects(`、`function drawWorkspaceGhosts(`、`api.maiWorkspace3Probe = function`、`showConfirm("释放工作区"`。

## 7. 验收

**门禁**：`python gate.py --full` 全绿（v0.18.4 是 17/17，52.8s）。

**smoke**：`MAIWS3:ok` + `MAIWS2:ok` 仍绿 + 7 条 code_only 红线命中。

**真机手测**（装进 `saves/mods/`，清单逐条打勾）：
1. 控制面板 →「工作区快照」：左列表 / 右详情同时可见；点第 2 行，左列表**不消失**。
2. 状态色：active 主题色、archived 灰、异地图 `Pal.remove`（红） + 名字后缀「地图不符」。
3. 搜索「赤」只剩名字含「赤」的行；点「已归档」只剩归档行；排序按钮三次循环顺序都对。
4. 打开对话框 → 世界里出现所有 active 工作区细线矩形；点行 → 对应矩形变粗；关对话框 → 矩形消失。
5. 点「⌖ 定位」→ 镜头到该框中心（异地图禁用）。
6. 点「释放工作区…」→ 弹出确认，正文含工作区名与「不能恢复」；取消 = 什么都不删；确定 = 列表与磁盘都少一个。
7. 小屏 1280×720 下双栏不重叠、按钮不溢出。
8. 悬停行 → 世界矩形高亮（**若失败**：记录到报告，退化路径必须仍可用）。

## 8. 分阶段（每阶段独立可提交、独立可验证）

| 阶段 | 内容 | 验收 | 预估 |
|---|---|---|---|
| A1 | 双栏骨架 + 选中态 + 小屏单栏降级；详情先用旧文本内容填右栏 | 手测 1；`MAIWS2:ok` 不变 | 0.5 天 |
| A2 | `wsRowParts`/`wsDetailModel` + 卡片行 + 处理器表 + 作业时间线 | `MAIWS3:ok`（rows/detail 两项） | 0.5 天 |
| A3 | 搜索/过滤/排序 + 动作分层 + 释放确认 | `MAIWS3:ok`（filt/sort/release）；手测 2/3/6 | 0.5 天 |
| C1 | `wsGhostRects` + `drawWorkspaceGhosts` + draw 钩子 | `MAIWS3:ok`（ghosts）；手测 4 | 0.5 天 |
| C2 | `wsLocate` 镜头 + 悬停高亮（可降级） | 手测 5/8 | 0.5 天 |

版本在 A3 后即可发 **v0.19.0**（A+C1 已完整）；C2 若悬停验证不过，退化版并入同版发布并在报告里注明。

## 9. 风险与边界

1. **Rhino 监听无先例**：`InputListener` 子类化在 arc + Rhino 下未验证（本仓只用过现成 `Tooltip` 当 listener，:515-:520）。⇒ 悬停只做「增强」，失败退化为选中高亮，不阻塞验收。
2. **无头环境**：`Vars.ui === null` 时所有渲染函数第一行 return；`wsRowParts` 等纯函数不碰 `Pal`/`Styles`/`Tex`（:912-:916 的教训：静态字段要 atlas，无头会炸）。
3. **性能**：每帧画矩形，上限 = active 工作区数（总量上限 200，实际 <10）；只画 active + 选中，且整段 `try/catch`（:7095 约定）。
4. **小屏/触摸**：760×420 在 1280×720 可用；<620 宽降级单栏。触摸设备没有 hover，C2 悬停自动失效（退化路径本来就兼容）。
5. **地图不符**：不允许定位/恢复选框，按钮禁用 + tooltip；不静默失败。
6. **与 jobBox 共存**：工作区矩形细线半透明，jobBox 粗双描边；对话框关闭立即停画，避免「关了窗地图上还留着框」。
7. **i18n**：全中文，与现状一致；不引入 markup（本仓 0 处），颜色一律走 `setColor`。

## 10. 待你拍板

1. 列表默认排序：**最近更新**（我的建议）还是按名字？
2. 搜索是否要连 `procs[].desc` 一起搜（能搜到「改投放」这种描述）？我建议**搜**。
3. 缩略图（方案 B）确认放到 v0.19.x 之后，不在本期？

## 11. 实现回填（v0.19.0，2026-11-02）

按 A1+A2+A3+C1 落地，C2 只做了「选中高亮」这一半，悬停没做（理由见下）。与上面方案的差异：

| 项 | 方案 | 实现 |
|---|---|---|
| 状态色 | `Pal.scarlet` | **`Pal.remove`**（本 checkout `Pal.java:76-88` 只有 remove=e55454，没有 scarlet） |
| `colorKey` | `"accent"/"gray"/"scarlet"` | 直接返回状态 key `"active"/"archived"/"mismatch"`，`wsStatusColor()` 再映射成 `Color` |
| 行尾 chip | `[地图不符]` | 名字本身用状态色 + 副行含「地图不符」；不加 chip（arc 里没有现成 chip 样式） |
| 选中态 | 整行 `Styles.flatOver` 底色 + `▸` | 只做 `▸` 前缀 + 左色条加亮（`flatOver` 要 `Styles` 静态字段，无头环境会炸） |
| 处理器表列宽 | 位置 70 / 方块 110 / 代码 growX / 写入 60 | 位置 78 / 方块 118 / 代码 88 / 写入 50（`growX` 会让四列挤在一起） |
| 空代码 | 「未写入」 | 「（空）」 |
| 悬停行高亮 | C2 做，可降级 | **未做**：`InputListener` 子类化在 Rhino 下本仓 0 先例（JavaAdapter 也是 0 命中），风险大于收益；选中高亮已能表达「这个矩形是这行」 |
| 定位按钮禁用 | 禁用 + tooltip | 不置灰，点了直接提示「地图不符（工作区在「X」），跳不过去」（`wsLocate` 返回值） |
| 释放确认 | `Vars.ui.showConfirm` | 同；另外无 `Vars.ui`（无头）或异常时**直接执行**，避免确认窗缺失导致功能卡死 |
| 双栏宽度 | 左 300 / 右 440，`wide = Core.graphics.getWidth() >= 900` | 同方案；窄屏单 pane 360 宽，靠「← 返回列表」翻页 |

无头自检口：`api.maiWorkspace3Probe()` 返回
`ok rows=3 filt=2 sort=3 detail=2 ghosts=1 release=1 mismatch=1 locate=1`
（smoke 里断言 `MAIWS3:ok`），造 3 个假工作区覆盖 进行中/已归档/异地图 三态。

待真机验的只有 4 条：双栏不重叠、世界里细框与选中加粗、定位镜头、释放确认。
