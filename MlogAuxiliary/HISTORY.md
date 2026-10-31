# 演进记录（0.1 – 0.12.5 的工程日志）

以下按版本记录当时怎么想、踩了哪些坑，保留原文不改。
当前用法见 [README.md](README.md)。

# MlogAuxiliary —— 游戏内Mlog辅助（P1 + P2 + 设置界面 + AI 面板 + 区域面板 + 作业队列 + 撤销落盘 + 大模型设置 + 事务写入 + 端点自测 + 模型思考回显 + 框选作业窗，v0.12.5）

把「写 mlog」这件事从玩家手里拿走：**说意图，不写代码**。

    [游戏内] 选中处理器 → 选模板/填参数 → POST 给本地 sidecar
                                              ↓
    [sidecar] tools/logicd.py: 意图 → mlog → 静态检查(41 条规则 + 成本) → 字节载荷
                                              ↓
    [游戏内] 面板显示 findings + 成本 → 确认 → build.configure(bytes) 写进处理器

P1 做 **API 接入** 与 **写入通道**；P2 把**快检**搬进游戏（没有 Python 也能先判一轮）；
v0.3 起调试入口是**设置 → Mlog辅助**一栏，不再依赖控制台。
v0.4 起有两个正面入口：**F4 的 AI 辅助面板**（一句话 -> sidecar 选模板并预检 -> 预览 -> 写入）
与**原版逻辑编辑器底部的审查栏**（打开原版编辑器就自动挂上，代码一改就重算）。
v0.4.2 起多一个**只读**入口：**F7 把鼠标周围 10 格扫成区域快照**，sidecar 算出每个处理器「还能连哪些方块、
该叫什么链接名」，用文本弹窗列出来 —— 先看清楚，再决定要不要按 F4 写。

生成层仍是确定性模板（模型只能选模板、填参数，不能直接产 mlog）；框选 + 队列是 P3/P4，路线见
`docs/Mlog辅助-规划.md`。

## 为什么写入走 config 通道

`LogicBlock` 的 `config(byte[].class, ...)` 就是原版「客户端把代码推给服务端」的通道
（`Mindustry-Source/core/src/mindustry/world/blocks/logic/LogicBlock.java:65-69`），
载荷格式是 `compress(code, links)`：版本字节 + 代码 + 链接表 deflate（同文件 `:137-166`）。
模组直接 `build.configure(bytes)`：

* 单机与联机同一条路径 —— 联机时由服务端执行 `readCompressed`，**不用自己造同步**；
* 载荷由 `tools/msch.py:447 build_logic_data()` 生成，和游戏自己的编码器同一格式，
  `tests/logicd/run_logicd_tests.py` 会把这段字节灌进真 v160.7 处理器逐条验。

## 安装

1. 先找到**真正在用的数据目录** —— 不要凭印象。Steam 版可能是桌面版（`%APPDATA%\Mindustry`），
   也可能是**便携版**（数据目录就是游戏目录下的 `saves`）。判据：游戏跑起来后看这两个位置的
   `last_log.txt` 谁的修改时间是刚刚，或者直接用游戏内「模组 → 打开文件夹」。
2. 把 `MlogAuxiliary/` **整个目录**放进那个 `mods` 下（本机实测是
   `E:/Program Files (x86)/Steam/steamapps/common/Mindustry/saves/mods/MlogAuxiliary/`）。
   **用 zip 也行，但 zip 里必须只包一层 `MlogAuxiliary/` 目录**：`Mods.resolveRoot()`（`Mods.java:1070-1074`）
   只在「根下恰好一个目录」时才下钻，否则把 zip 根当模组根，`scripts/main.js` 会 `exists()==false`，
   日志里就是 `[E] No main.js found for mod mlog-auxiliary.` —— 模组列表里看得见它、脚本却一行没跑，
   控制台自然找不到 `maiVersion`。`out/_dist/MlogAuxiliary.zip` 已经按这个形状打好了。
3. 启动 sidecar：`python tools/logicd.py`（默认 `http://127.0.0.1:8765`）。不开也能进游戏，
   只是没有生成/写入；**本地快检照常工作**。
4. 进游戏（模组是 `hidden` 的，不会出现在模组列表里，这是故意的：联机不会被当成不兼容模组）：
   **设置 → Mlog辅助**，这一栏会直接告诉你加载是否成功（版本 / 构建 / 快检模块 / sidecar / 两个目录）。
   把鼠标指向一个处理器再按 **F4** 打开 **AI 辅助面板**（一句话生成 + 预检 + 写入）；
  纯模板面板走那一栏的「模板面板」按钮。

## 控制台函数

| 函数 | 作用 |
| --- | --- |
| `maiVersion()` | 模组版本 + sidecar 地址 |
| `maiPing()` | 异步 GET `/health`，结果弹窗 + 写日志（返回「已发出…」） |
| `maiSel()` | 报告鼠标下的方块是不是处理器、它现在有几行代码几个链接 |
| `maiPanel()` | 打开面板 |
| `maiCompose(template, paramsJson)` | 异步 compose，结果弹窗并写日志（返回「已发出…」） |
| `maiApply(template, paramsJson)` | 异步 compose + 直接写进鼠标下的处理器（0 ERR 才写，结果弹窗） |
| `maiUndo()` | 撤销上一次写入（P1 是内存快照，重启游戏即失效） |
| `maiCheck([code])` | 本地快检：不给参数就查鼠标下的处理器，给了就查这段代码 |
| `maiScan()` | 全图扫描所有处理器跑本地快检，返回「N 个处理器 / 多少有命中 / 规则分布 / 耗时」 |
| `maiDiag()` | 诊断文本（就是设置界面里那段） |
| `maiSelfTest()` | 拿一张假 Table 建一遍设置表格，报告填了多少行/格（无 UI 环境的自检） |
| `maiSettings()` | 再尝试注册设置分类；返回 `registered` 或 `no ui` |
| `maiAi()` | 打开 AI 辅助面板（等同 F4） |
| `maiReview()` | 纯文本版审查：原版编辑器里的代码 + 快检结论 + findings（不弹窗） |
| `maiSnapshot(x1,y1,x2,y2)` | 区域快照 JSON 文本（tile 坐标，clamp 到地图内，上限 4096 格） |
| `maiSnapshotText(...)` | 同一份快照的人读摘要 |
| `maiRegion(x1,y1,x2,y2)` | 异步 `POST /region`，回包解析后弹窗（返回「已发出…」） |
| `maiRegionAround(r)` | 以鼠标下处理器为中心扫 `r` 格（F7 就是 `r=10`） |
| `maiHttpRaw(method,url,body)` | 调试用：同步发一个请求，返回 `code` 换行 `text`（v0.4.4 起走裸 socket，连不上回 `-1`） |

## 设置界面（v0.4）

`设置 → Mlog辅助`，一栏把调试要看的都摊开，不用再往控制台敲命令：

v0.4 把这一页按用途分段（`── 运行环境 ──` 这样的标题行 + 左标签右值两列）：

* **运行环境**：版本 / 游戏构建（`Version.build`）/ 快检模块是否加载（指令、op 版本门、链接槽各几条）/ 
  数据目录 / mods 目录 / 设置分类状态 —— 装错位置、规则表没加载，一眼看出来；
* **本地快检**：快检能否使用、规则表条数；
* **sidecar**：地址（可直接改，回车即存进 `Core.settings` 的 `mlogauxiliary-url`）、连接测试（GET `/health`）、**复制诊断**到剪贴板；
* **快捷动作**：AI 辅助编程 / 模板面板 / 快检鼠标下的处理器 / 打印诊断 / 重新注册分类 / 审查状态。

分类图标用原版逻辑徽标 `Icon.logic`（`Vars.ui.settings.addCategory(name, Drawable, Cons)`，
`SettingsMenuDialog.java:324`）：取不到图标（无头环境 `Core.atlas` 是 `null`）就退回纯文字重载（`:329`）。

排版只用了在真 v160.7 里实测能解析的 `Table` / `Cell` 方法（`add` / `labelWrap` / `button` / `field` +
`colspan` / `left` / `right` / `width` / `pad` / `padTop` / `padBottom` / `padRight` / `row`），
`run_ui_smoke.py` 会把 `maiSelfTest()` 报出的每个方法名拿到真引擎里调一遍。

注册走 `Vars.ui.settings.addCategory(name, Cons<SettingsTable>)`（`SettingsMenuDialog.java:329-330`），
分类只在显示设置菜单时 rebuild（`:357`、`:658`），所以脚本加载时机不影响它出现；脚本加载时 UI 还可能不存在，
`ClientLoadEvent` 上再补注册一次，之后热键钩子里还有一次**有界重试**（每 60 帧一次、最多 600 次）。
诊断里最后一行 `设置分类：` 会直接告诉你注册成功还是在哪个异常上失败。

**v0.3.0 那一栏没出现的原因**：设置表格用了 `t.table(function(tt){...})` 做按钮分组，真 v160.7 的 Rhino 在
**方法解析阶段**就抛 `InternalError: ... Table.table ... is ambiguous`（重载歧义），异常被注册函数吞掉，
于是游戏里根本没有这一栏 —— 而假表 smoke 还是绿的。v0.3.1 改成**平铺加行**（`t.button` / `t.labelWrap` /
`t.field`，实测都能解析），并且 `run_ui_smoke.py` 多了一道探针：把 `maiSelfTest()` 报出的方法名逐个拿到
真引擎里调用，出现 `ambiguous` 即 FAIL。**无头环境**（模拟器、`Vars.ui == null`）里 `maiSettings()` 返回
`no ui` 而不是抛错，设置表格的布线由 `maiSelfTest()` 用假表验证。

## AI 辅助面板（v0.4，F4）

把鼠标指向任意处理器按 **F4**（或设置页的「AI 辅助编程」、控制台 `maiAi()`）：

1. 一句话说清要什么，例如「让 @flare 从 10 10 来回巡逻到 30 12」「容器里铜够 250 就打印」；
2. 模组把这句话 + 该处理器的链接名列表 `POST /plan` 给 sidecar；
3. sidecar 用关键词路由选出模板（六个之一）并抽参数（数字、`@单位`、链接名），再走同一条
   确定性生成 + 全量检查，返回 `{template, params, reason, code, findings, cost, bytes_b64}`；
4. 面板显示**选中的模板与理由**、预检计数、前几条 findings、前 40 行代码；
5. 0 ERR 时出现「写入」按钮 —— 与模板面板同一个写入函数（`build.configure(bytes)`）。

有 ERR 就没有写入按钮：是拒绝写入，不是降级写入。意图太模糊（没命中任何关键词）时服务端
返回 `ok:false` + 六个候选模板名，面板照原样显示，不瞎猜。

**模型只有一个接入点**：`tools/logicd.py` 的 `_route()`。它是纯规则（关键词表 + 参数抽取），
换成模型调用时返回形状不变，模组这一侧一行都不用改 —— 所以模型永远没法把没检查过的 mlog 塞进存档。

## 原版编辑器审查栏（v0.4）

打开**原版逻辑编辑器**（点处理器 → 编辑）时，模组在对话框底部补一条 46 px 状态栏：

* 「审查」+ ERR/WARN/INFO 计数（`Pal.remove` / `Pal.lightOrange` 上色）+ 语句数 / 线性成本 / ipt；
* 「详情」= 全部 findings（`Vars.ui.showText`）；「AI 辅助」= 直接开 AI 面板；
* 代码一改（`canvas.save()` 变了）就重算，最多每 30 帧一次；编辑器没开时每帧只做两次属性读。

接法是最低侵入的：不自造对话框、不碰 canvas，只在 `Vars.ui.logic`（单例，`UI.java:79` / `:228`）
身上 `row()` 之后 `add(bar).growX()`。`LogicDialog` 的构造里 `clearChildren()` 之后只有
`add(canvas).grow()` / `row()` / `add(buttons).growX()` 三样（`LogicDialog.java:39-72`），
所以这一行是它的第四行，原版按钮一个都不动。
## v0.4.1：修掉「点生成就崩」+ 设置页按原版风格分隔

**崩溃根因（真机日志定的）**：v0.4.0 用 `d.cont.pane(function(t){...}).get()` 当结果区，但 `Table.pane(Cons)`
返回 `Cell<ScrollPane>`（Arc `Table.java:295/311`），`.get()` 拿到的是 ScrollPane。弹窗能开，一点「生成」就：

```
rhino.EcmaError: TypeError: Cannot find function add in object ScrollPane. (main.js#685)
```

因为 Group 有 `clear()`（`Group.java:357`）却没有公开的 `add`（真正的 API 是 `addChild(Element)`，`Group.java:251`），
`out.clear()` 过得去、`out.add(...)` 当场抛错，而模态窗口又没有关闭按钮（`Dialog.addCloseButton()` 是空实现，
`Dialog.java:453-455`），于是一路打到崩溃上报。

修法：结果区自己造一张 `new Table()` 交给 `pane(host)` 托管（Element 重载），之后只往 host 里加东西；
`pane()` 不可用时回落成 `d.cont.add(host).growX()`。AI 面板与模板面板两处都改了。

**设置页改成原版的样子**：不再手画「── 标题 ──」，改用原版自己的两样东西 —— 分段标题下一条 accent 强调线
（`titleImage = titleTable.image(Tex.whiteui, Pal.accent).growX().height(3f)`，`BaseDialog.java:25`），以及灰底行
（`Styles.grayPanel`，原版设置项 `CheckSetting` 用的同一个 Drawable，`SettingsMenuDialog.java:824-846`）。
行宽 540、内边距 8/5 也照它取。

`pane()` 这个坑现在有三道闸：`run_ui_smoke.py` 在真引擎里断言 ScrollPane 上没有 `add`；源码级禁止
`.pane(function` 与 `.pane(...).get()`；页面上每个 Table/Cell 方法名仍然逐个拿到真引擎里解析
（`margin` / `growX` / `height` / `color` / `image` 是这一版新用到的五个）。

## v0.4.2：区域快照 + F7 链接候选（P3 第一刀）

P3 原本是「框选 + 队列」，这一版只做**只读的那一半**：把鼠标周围一片区域读成快照，
让 sidecar 算清楚「每个处理器还能连哪些方块、链接名该叫什么」，人看完再决定写什么。
**它不写任何东西** —— 写入仍然只有 F4 面板那一条路（`build.configure(bytes)`）。

### 快照里有什么

把鼠标指向处理器按 **F7**（或控制台 `maiSnapshot(x1,y1,x2,y2)`）：

```json
{"schema":1,"mod":"0.4.2","build":160,"map":"...","world":{"width":...,"height":...},
 "bounds":{"x1":...,"y1":...,"x2":...,"y2":...},"cells":6,"truncated":false,
 "grid":[{"x":13,"y":10,"block":"container"}, ...],
 "processors":[{"x":10,"y":10,"block":"micro-processor","code":"...",
                "links":[{"name":"container1","x":13,"y":10}]}]}
```

* 坐标是 **tile 坐标**（`Vars.world.tile(x,y)` 那一套），空格子（`air`）不进 `grid`；
* 框会先交换大小值再 **clamp 到地图内** —— 处理器在 `(0,0)` 时问 `-1,-1` 是常态，不 clamp 会读到 null tile；
* 上限 `SNAP_MAX_CELLS = 4096`，超了置 `truncated:true` 并截断（prompt 装不下就不装）。

### sidecar 算出来的链接候选

`POST /region` 收这份快照，回答每个处理器「10 格内哪些方块能连、叫什么名」：

* **距离**：`LogicBlock.range = 8*10` 世界单位（`LogicBlock.java:49`），判定是
  `other.within(this, range + other.block.size*tilesize/2f)`（`LogicBlock.java:736`），tilesize=8
  → 中心距上限 `80 + size*4`。所以 2x2 的 container 到 10.5 格仍可连，1x1 的 switch 到 11 格就不行。
* **名字**：`LogicBlock.getLinkName`（`:123-135`，取方块名 `-` 的最后一段，末段是 `large` 或数字时取倒数第二段）
  加 `findLinkName`（`:331-360`，已有 `base<n>` 就取已占数字，否则从 1 起找最小空号）。`tools/mschgen.py:547`
  早就有同一套 `link_name_for()`，`tests/logicd/run_logicd_tests.py` 拿 `tools/block_sizes.json` 全表 446 个方块做零漂移比对。
* 候选按「代码里正在用的排前面、再按距离」排，并标 `[代码正在用]`；每个处理器最多显示 12 个。

F7 弹的是纯文本（`Vars.ui.showText`），不引入新的对话框/布局 —— v0.4.1 那次崩溃就是自造结果区引起的，
这一版刻意不再碰 UI 树。

### 引擎里踩到的两个坑（都写进了代码注释）

1. **`LogicBlock` 是方块类，建筑是它的内部类 `LogicBlock$LogicBuild`** —— `bld instanceof LogicBlock`
   恒为假，第一版快照的 `processors` 因此是空数组。判据改成鸭子类型（有 `executor` 且有 `code`），
   与既有的 `targetBuild()` 一致。
2. **`tile.block()` 是方法、`tile.build` 是字段** —— 写成 `tile.build()` 会得到
   `TypeError: Cannot call property build in object stone:air:air[2,0]`。

### 边界

只读、只回答「能连什么」，**没有**框选拖拽、没有队列、没有批量写入 —— 那些是 P3 后半段与 P4/P5。

## v0.4.3：F7 为什么弹出「sidecar 不可用」——同步等待在主线程上永远拿不到结果

v0.4.2 装进游戏后，F7 弹出的是一个空回复：

```
sidecar 不可用（ ）
pending
```

不是 sidecar 没起来，是**等待方式根本不可能成功**：

* `httpJson()` 把请求放到 daemon 线程，拿到结果后用 `Core.app.post(...)` 投递回调 ——
  也就是说回调**只能在主线程空闲后**执行；
* 控制台命令与热键分支都跑在**主线程**上，它们里面的 `while(got === "pending"){ waited += 50; }`
  既没有真的 sleep，又占着主线程 —— 计时器瞬间走完，函数带着字面量 `"pending"` 返回，
  于是 `regionCandidatesText()` 收到一段没有换行的文本：`code` 为空、`body` 是 `pending`。

四个同步入口（`maiPing` / `maiCompose` / `maiApply` / `maiRegion`）全是这个形状，一个都没法用。

**修法：全部改成异步。** 请求发出去就返回一句「已发出…（结果稍后弹窗）」，真正的处理放在回调里 ——
回调本身就在主线程（`Core.app.post`），所以照样能调 `Vars.ui.showText`、能 `build.configure(bytes)`：
F7 现在走 `regionAroundBox()`；`maiApply` 在回调里判断 0 ERR 再 `writeBuild()`。

错误文案也换了：连不上时直接说「连不上 sidecar（地址），先在本机跑 python tools/logicd.py」，
不再把空 code 印成一对空括号。`ping()` 这个同步探活函数整个删掉，探活统一走设置页的「连接测试」
（`probeSidecar()`，本来就是异步的）。

**这条现在有闸**：`run_ui_smoke.py` 的源码红线禁止 `while(x === "pending")` 与 `while(out === "?")`，
并要求 `regionAroundBox(` 存在、`regionCandidatesText(rtext)` 不再出现。


## v0.4.4：传输层换成裸 socket（顺带挖出 Rhino 的两个 codegen bug）

v0.4.3 修好等待方式之后，F7 换了新的报错，而且这回是完整的 Java 异常：

```
java.lang.IllegalAccessException: class rhino.MemberBox cannot access class
sun.net.www.protocol.http.HttpURLConnection (in module java.base) because module java.base does not
export sun.net.www.protocol.http to unnamed module
```

`java.net.URL.openConnection()` 交回来的对象是 `sun.net.www.protocol.http.HttpURLConnection` —— 
`java.base` 的内部类，没有 export。Rhino 绑成员时用的是**运行期类**（`rhino.MemberBox`），
于是 `setRequestMethod` / `getResponseCode` / `getInputStream` 这些被内部类覆写的方法一调就抛。

**修法：自己用 `java.net.Socket` 写 HTTP/1.1。** 请求行 + `Host` + `Connection: close`，有 body 时加
`Content-Type` / `Content-Length`（UTF-8 字节长度）；响应用 `InputStreamReader(..., "UTF-8")` 读到 EOF，
按 `\r\n\r\n` 切头与正文，状态码用 `substring` + `parseInt` 解析。sidecar 是 `BaseHTTPRequestHandler`
（`tools/logicd.py:717`），默认 `protocol_version = "HTTP/1.0"`，不 chunked 也不 keep-alive，读到 EOF 正好是正文尾。

**改的时候踩到两条真 v160.7 的 Rhino 编译期 bug**（两条都让整段脚本静默不执行）：

1. 状态行解析原本写 `/^HTTP\/1\.[01] (\d{3})/` —— Rhino 编译它时直接 `IllegalStateException: FAILED ASSERTION:
   invalid bytecode`。同一个文件里 `endpoint()` 那条长正则却没事，只能逐段 bisect 才定位到。
2. 收尾原本是 `try{ ... }finally{ try{ sock.close(); }catch(e1){} }` —— `finally` 体里再嵌 `try/catch`
   会让 Rhino 抛 `ArrayIndexOutOfBoundsException: arraycopy: last source index N out of bounds for object array[M]`。
   最小形是 `function t(){ try{ return 1; }finally{ try{ }catch(e){} } }`。现在改成 try/catch + 末尾独立的
   `try{ if(sock !== null){ sock.close(); } }catch(e1){}`。

**这两条最阴的地方是「静默」**：`Scripts.runConsole`（`Mindustry-Source/.../mod/Scripts.java:44-65`）
`catch(Throwable)` 之后**把错误当返回值返回**，不抛也不打印，所以控制台上看起来脚本「加载成功」，
实际一个函数都没定义。诊断时唯一的入口是把 `runConsole()` 的返回值打印出来。

**这条现在有闸**：`run_ui_smoke.py` 起一个真的本地 `http.server`，用 `maiHttpRaw` 打三枪 —— 
GET 200 且正文含 `MAISIDECAR-OK`、POST 200 且回显 `MAIPING`、死端口 `127.0.0.1:1` 必须回 `code -1`；
另有四条源码红线：禁 `openConnection(`、要求 `new Packages.java.net.Socket()`、禁 `}finally{`、
要求那句显式 `sock.close()`。

## v0.4.5：F7 不再贴 Java 栈，并把「代码在用但没绑上」的链接名点出来

v0.4.4 之后真机按 F7 看到的是：

    连不上 sidecar（http://127.0.0.1:8765）
    JavaException: java.net.ConnectException: Connection refused: getsockopt

传输层其实已经好了 —— 是 sidecar 没在跑。可这句话对用户没有用，所以 v0.4.5 做了两件事：

1. **三种失败分开说**（`main.js` 的 `httpWhy` + `lastLine`）：连接被拒绝 ⇒「sidecar 没在跑（连接被拒绝）」，超时 ⇒「sidecar 没回应（超时）」，其它 ⇒「连不上 sidecar」，各配一句启动命令；并且**只贴最后一行诊断**（超过 200 字截断），不再把 Java 栈整段糊上去。
2. **F7 点出「代码用到、但既没绑定也不在候选里」的链接名**（`/region` 新增 `missing`）。这是 `core` 那个老问题的机器化：`getLinkName`（`LogicBlock.java:123-135`）把 `core-shard` 变成 `shard`，游戏永远不会交给你一个叫 `core` 的链接名，可代码里写 `core` 很自然。现在面板会显示：

       ⚠ core 代码在用，但没绑上、也不在本区域候选里（同类可用 shard1）

判据在 `tools/logicd.py`（`_linkish` / `_code_idents` / `_result_names`）：

- 一个词要么是方块链接基名，要么是某个方块名的前缀（`core` → `core-shard`）；少于 3 个字母不算；
- 排除 `@内容`（`@copper` 是物品不是链接）、指令名与 op 名（`mlogcheck.SPECS` / `OP_*`）、结果槽里的名字（每行 token 1）、以及 `turretX` 这类大写后缀变量（`turret` 不因为 `turretX` 出现就被当成链接）；
- 同基名或同前缀的候选存在时给出替代建议。

`missing` 只报告，**不改代码、不写文件**；写入仍然只走 `build.configure(logic@config)`。

## v0.4.6：链接坐标是相对处理器的——不告诉 sidecar 处理器在哪，链接表就写到空地上

现象：v0.4.5 之后 F7 正常、面板能生成能写入，但写进去的程序一跑，`container1` / `message1` 这些链接全是 `null`，
游戏不报任何错。

根因：写入走的是 `LogicBlock.config(byte[].class, (build, data) -> build.readCompressed(data, true))`
（`LogicBlock.java:65-69`），第二个参数 `relative = true`，`readCompressed` 里
`if(relative){ x += tileX(); y += tileY(); }`（`:304-307`）—— 载荷里存的是**相对处理器**的偏移量；
而模组 `linkList(b)` 读出来的 `LogicLink.x/y`（`:102`）是**绝对** tile 坐标。原样塞进
`msch.build_logic_data()`，游戏就把绝对坐标当相对偏移又加了一次处理器坐标，整张链接表平移到地图另一边，
落在空地块上：`link.valid = false`，运行时读 `null`，全程无报错。

真 v160.7 实测（`out/_v46_probe2.py`）：处理器在 `(10,10)`、container 在 `(13,10)`。

- 载荷写绝对 `(13,10)` ⇒ 回读 `container1@23,20->null`
- 载荷写相对 `(3,0)` ⇒ 回读 `container1@13,10->container` ✓

（这个 bug 一直没被测出来，是因为测试里处理器都放在 `(0,0)`，绝对坐标和相对偏移数值相同。）

修法：

1. `tools/logicd.py` 新增 `_relative_links(links, origin)`；`POST /compose` 与 `POST /plan` 接受
   `origin: {x, y}`（处理器的 tile 坐标），把绝对坐标减成相对偏移再打包；`origin` 不是对象时返回 400；
   返回体回显 `origin` 便于对账。
2. `main.js` 新增 `originOf(b)`（`b.tile` 存在就给 `{x, y}`），`/plan` 与 `/compose` 两个请求体都带上。
3. 测试加两条：带 origin 时 `msch.parse_logic(..., relative_to=...)` 必须还原出绝对坐标、裸 parse 得到相对偏移；
   写入路径把目标处理器另放到 `base + (16,16)`（`(0,0)` 处绝对=相对，测不出这个 bug），四个链接方块就位后
   回读 `name>block` 必须全部解析，**负控**是同一组绝对坐标不带 origin 再写一次、必须出现 `>null`。

不传 `origin` 时行为不变（原样透传，视作已经是相对坐标），老的调用方不会被这次改动破坏。

## v0.4.7：审查栏的行内箭头 —— 以及「有链接也报无链接」的真凶

用户要的交互是：哪一行有问题，就在那一行贴个箭头，点箭头看这一行的原因。

### 为什么是覆盖层，不是往原版行里插一个格子

Arc 的 `Table.cells` 是私有字段（`arc/scene/ui/layout/Table.java:35`），插不到行内 `addressLabel` 前面；追加到行尾又会被行内按钮挤。所以做成一个铺在 `LCanvas.statements` 父容器上的 Table：箭头是它的普通孩子（`addChild`，不是 cell），手动摆位，`host.update()` 每帧跟着布局重算。

三条依据：

* `LCanvas.statements` 是 public 字段（`LCanvas.java:32`），子元素按语句顺序排；每个 `StatementElem` 的表头行高 38（`LCanvas.java:427-491`）。
* quickcheck 的 `finding.line` 是 1 起的语句序号（`quickcheck.js:97`），所以第 i 个孩子对应 `line = i + 1`。
* 覆盖层用 `Touchable.childrenOnly`（`arc/scene/event/Touchable.java`）：空白处不拦鼠标，原版画布照常拖行、点按钮；只有箭头自己被命中（`Group.hit` 先测孩子，`Element.hit` 对非 enabled 返回 null）。

箭头横向优先放进行左侧的空白（`statements.x + e.x - 18`），没有空白就压进行内左端（`+2`）；纵向取表头行中心。颜色 ERR 红（`Pal.remove`）/ WARN 橙（`Pal.lightOrange`）/ INFO 蓝（`Pal.accent`），同一行多条取最严重的那条，点开列出全部。

无头引擎没有图集（`Icon.right` 是 null，`new Image(null)` 还会因为 4 个重载歧义直接抛），所以找不到图标时只少画箭头，审查栏与统计照常工作。

### 「有链接也报无链接」的真凶：`LogicDialog.executor` 是包私有字段

真 v160.7 反射实测：`LogicDialog` 的字段可见性是 `canvas:public, consumer:pkg, privileged:pkg, executor:pkg, globalsDialog:pkg, wasRows:pkg, wasPortrait:pkg, forceRestart:pkg`。Rhino 只暴露 public 成员，所以 `L.executor` 读出来是 `undefined`（不是 null），取 `.build` 抛 TypeError，被 try/catch 吞掉 → `linkNames()` 返回空数组 → 快检把每个链接槽里的名字都判成「没链接」。

两处修：

1. `logicExecutor()` 走反射：`Class.forName("mindustry.logic.LogicDialog")` → `getDeclaredField("executor")` → `setAccessible(true)` → `get(L)`，再读 public 的 `ex.build`（`LExecutor.build` 是 public，`LogicBlock$LogicBuild.links` 也是 public）。
2. 「没有链接」和「读不到链接表」必须分开：`linkNames()` 读不到时返回 `null`（不是 `[]`），`quickcheck.check()` 收到 `null` 就整段跳过链接槽判据，并在 `stats.linkKnown` 里记下来；审查栏统计行显示「链接 ?（读不到处理器）」而不是「链接 0」。

真引擎对照探针（`out/_v47_probe.py`）：放处理器、用 `msch.build_logic_data` 写带两条链接的载荷，读回 `links.size=2`，把真链接名喂 `quickcheck.check` ⇒ 零命中；喂空数组 ⇒ 第 1、3 行各一条 WARN。所以快检逻辑本身没错，错的是喂进去的东西。

## v0.14.0：一个选框就是一个工作区 —— 写完逻辑它不会消失（W1 数据层）

以前 F3 框选是一次性的：框一次、投一次、写完、框消失，上下文全丢。现在每次框选会落到一个**工作区**上：

- 落盘在 `数据目录/mlogauxiliary/workspaces/<id>.json`（`数据目录` 就是设置页里那个，Steam 版在 `Mindustry/saves/`）。
- **同一块区域再框一次，复用同一个工作区**：判定是「新选框的中心落在已有工作区的 `bounds` 内，且地图一致」，
  复用时 `bounds` 取并集（所以先框一小块、再框一大块，还是一个工作区）。
- 每个处理器在 `procs[]` 里有自己的一行：坐标、方块名、最近一次描述、链接表、**最近一次写入的代码**、
  被接受过几次（`accepted`）、以及历史（最多 20 条，`WS_HISTORY`）。
- 写入成功后回写 `code`/`accepted`/`history` —— 这就是「保留上下文」的实体：下次继续改，模型能看到上次写了什么。
- 工作区数量上限 200（`WS_KEEP`），作业引用最多 50 条（`WS_JOBS`），超了按 `updated` 淘汰最旧的。
- 生命周期：`active`（在用）→ `archived`（归档，不再自动复用）→ 释放（删文件，什么都不留）。
  W1 只做了数据层，归档/释放的按钮在 W2 的界面上。

三个不显眼的细节：

1. 选框中心判定而不是「完全包含」—— 玩家拖框很难两次一模一样，中心点够稳。
2. 单台处理器（作业面板里对一台投递）也会记到工作区，但**不会凭空造工作区**：找不到就只写 `job` 记录，不建文件。
3. 三个存储原语（读/写/删）在探针里会被换成内存表，所以自检不需要真磁盘；无头环境里 `Vars.dataDirectory`
   是有的（`dir=yes`），但源码检出/异常路径仍然走降级，不会因为拿不到目录就崩。

自检口：`maiWorkspaceProbe()`（无头 ui smoke 的 `MAIWORKSPACE` 就是它），断言表 L2-66。

## v0.13.2：模型算错一行跳转，现在会被抓住 —— 而且它会重试

旧版有个难看的洞：模型自己数跳转行号，`jump 12` 写成 `jump 12` 少算一行，就跳进了「停止射击」块的正中间，
后半段代码永远执行不到。检查器其实**算出了**这件事（成本报告里的 `UNREACHABLE`），但它躺在没人看的报告里：
不进 findings、不进作业 JSON、面板不显示、也不会触发重试。你看到的「生成出来的逻辑有问题」就是这么漏出去的。

这一版把这条通路接上了：

- **检查器**：`analyse()` 现在把「可达性分析到不了的语句」追加成 `WARN`，规则 id `unreachable-statement`，
  文案是 `this statement can never run: ... a jump target is usually off by one`。它进作业 JSON、进面板、也让模型重试。
- **作业**：`cost` 现在存进作业文件（以前只有 findings/explanation/assumptions 存了）。
- **面板**：作业详情里多了两段 —— 「模型假定的前提（它替你想的，错了就是逻辑不对的源头）」和
  「成本（含 UNREACHABLE：永远执行不到的语句）」。
- **生成侧**：prompt 不再允许数行号，要求用标签（`wait_ready:` 独占一行、`jump wait_ready always`），
  并给了一个 free tier 的骨架。标签是零成本的：写进蓝图的 logic data 就是代码原文，游戏加载时自己解析标签。

顺带一说，这条规则上线后当场抓到了我们**自己的 7 个内置模板里的 5 个** —— `patrol` / `item-threshold` /
`radar-fire` / `timer-flush` / `counter` 结尾都是 `jump loop always` 再跟一行 `end`，那个 `end` 永远执行不到。
已经全删了。

自检口：`maiJobPanelProbe(jobJson)`；断言表 L2-65。

## v0.13.1：装完就没有第三步了 —— 后端随包，模组自己把它拉起来

v0.13.0 的包能装，但装完还得双击 `启动逻辑后端.bat`、留一个黑窗口，源码检出还要玩家自己装 Python 3。
这一版把这件事去掉：

1. 包里多了 `mod/MlogAuxiliary/sidecar/bin/logicd.exe`（PyInstaller 打的独立程序，不需要 Python）；
2. 第一次按 F3（或打开面板）时，模组先探 `http://127.0.0.1:8765/health`，探不到就自己把那个 exe 拉起来，
   然后每 400 ms 轮询一次，屏幕上先出现「正在启动 sidecar …」、就绪后出现「sidecar 已就绪」；
3. 端口跟着设置页里的地址走（地址写 `http://127.0.0.1:9000` 就用 9000 拉起），不用改脚本。

`启动逻辑后端.bat` 还留着，但只在「想手动看后端日志」或「这是没有 exe 的源码检出」时用 ——
它优先跑随包的 exe，没有 exe 才退回 `python sidecar/logicd.py`。

四个不显眼但关键的细节（都是「看起来成功」的那一类）：

- **只能 onedir，不能 onefile。** onefile 会先把自己解包到 `%TEMP%` 再执行，这台机器上的策略直接拦成
  `[PYI-xxxx:ERROR] Could not create temporary directory!`（换个 `%TEMP%` 下的目录也一样，只有工作区内能跑）。
  所以包里是 `bin/logicd.exe` + `bin/_internal/`，一共 100 个文件、约 2.2 MB。
- **拉起进程必须用 `ProcessBuilder` 的字符串数组**，不能用 `Runtime.exec` 的单个命令行字符串 ——
  后者按空格切，而 `saves\mods` 常常就在 `Program Files (x86)` 下面。
- **子进程输出必须重定向到 `sidecar.log`。** 没人读的 stdout 管道攒满 64 KB 后，后端会卡死在写日志上，
  看起来像「后端随机卡住」。日志就在 `sidecar/bin/sidecar.log`。
- **找不到 exe 时必须安静退回。** 源码检出里本来没有 exe，这时只提示「先在仓库里跑 `python tools/logicd.py`」，
  不能抛异常 —— 同一帧里的其它钩子会被一起弄死。

自检口：`maiSidecarProbe()`（无头 ui smoke 的 `MAISIDECAR` 就是它）；断言表 L2-64。
sidecar 版本同步升到 0.13.1（冻结分支要认 `sys._MEIPASS` 与可执行文件所在目录，作业与快照的落盘根目录也跟着走）。

## v0.13.0：打包安装即用（双击 `安装.bat`，不再手拷两个文件夹）

以前「安装」是两处手拷：`MlogAuxiliary/` 进 `saves/mods`，再从仓库里开一个 `python tools/logicd.py` 窗口。
而旧的 `out/_dist/MlogAuxiliary.zip` 只是模组文件夹的压缩 —— 模组是前端，没有 sidecar 它只会说
「连不上 sidecar」，所以那个包从来没真的可用过。

现在有一个真的包：`python tools/package.py` 产出 `out/_dist/MAI-0.13.0/`（63 个文件）与
`MAI-0.13.0.zip`（233,560 B）。解压后：

```
MAI-0.13.0/
  mod/MlogAuxiliary/        模组本体（mod.hjson + scripts/main.js + README.md）
  sidecar/logicd.py         逻辑后端 + 它需要的 6 个模块、5 个 JSON 数据表、rules/ 40 条规则
  install.ps1 / uninstall.ps1 / stop-sidecar.ps1
  安装.bat / 卸载.bat / 启动逻辑后端.bat / 停止逻辑后端.bat
  使用说明.md / VERSION.txt（每个文件的 SHA256 + 两边版本）
```

三步：解压到有写权限的目录（比如 `D:\MAI`）→ 双击 `安装.bat`（自动找 `Mindustry\saves\mods`，旧版本改名成
`MlogAuxiliary.bak-<时间>`）→ 双击 `启动逻辑后端.bat` 留一个窗口。`启动逻辑后端.bat 8899` 可以换端口，
端口被占会直接拒绝而不是叠第二个。

四个不显眼但关键的细节（都是「看起来成功」的那一类）：

1. **sidecar 闭包按 AST 算，不是按行首正则。** `rule_ids` 是在函数里导入的
   （`logicd.py` 的 `_rid()` 里 `from rule_ids import rule_id`），行首锚定的正则漏掉它之后，包照样
   `/health` 200、`/capabilities` 200，只有 `/analyze` 会在玩家机器上 `ModuleNotFoundError`。
   现在 `tools/package.py` 的 `local_imports()` 走 `ast.walk`，并且 `tests/packaging/run_pack_tests.py`
   （gate 的 `package` 步）解包后真的起 sidecar 打 `/analyze`，缺一个模块就红。
2. **「装好了」是逐字节核对的。** `install.ps1` 拷完按 `VERSION.txt` 里 `mod/MlogAuxiliary/*` 的 SHA256
   再哈希一遍，不一致就退出码 1，而不是「拷贝命令没报错就算成功」。
3. **存档目录不是猜的。** `Vars.loadSettings()` 里 `if(steam || Version.isSteam){ settings.setDataDirectory(Core.files.local("saves/")); }`（`Mindustry-Source/core/src/mindustry/Vars.java:494-495`）—— Steam 版的
   `saves/` 在**游戏目录旁边**，只有非 Steam 版才用 `%APPDATA%\Mindustry`。所以安装器先找「有 `saves\` 的
   游戏目录」，再退 `%APPDATA%`；反着排会把模组装进游戏从不打开的目录，玩家只看到「模组没反应」。
   卸载器同一个顺序，否则删错目录还会报成功。
4. **`.ps1` 必须带 UTF-8 BOM。** PowerShell 5.1 对没有 BOM 的 `.ps1` 按 ANSI（中文机器上是 GBK）解码，
   安装会成功、中文全变 `鍚姩閫昏緫鍚庣`。`.bat` 则相反：保持纯 ASCII，中文只出现在文件名里。
   这两条都由 `tests/packaging/run_pack_tests.py` 逐文件断言。

自检口：`python tools/package.py --verify`（建到临时目录、起 sidecar、跑 `install.ps1`/`uninstall.ps1` 往返）；
断言表 L2-63。

## v0.13.0：作业启用和完成时，状态直接进聊天框

以前作业跑到哪一步只能盯着面板或框右下角的徽标。现在投递成功和作业终态都会往聊天框塞一行，格式固定成

```
MAI<逻辑徽标> <状态> (x,y): <其它信息>
```

真机上长这样（`<逻辑徽标>` 是那个逻辑方块图标）：

```
MAI[logic] Generating (12,34): 给核心补铜（job 3f2a1b9c）
MAI[logic] Done (12,34): code · 12 条语句 · 240 字节 · 0 ERR
MAI[logic] Failed (12,34): 模型没写出可用代码
```

状态词：`Generating`（投递成功那一刻）、`Done` / `Rejected` / `Failed` / `Cancelled`（终态）。
`Done` 后面跟模板名、语句数、字节数、ERR 计数；失败跟模型给的原因（截 60 字）；信息为空就不留冒号。

三个引擎事实决定了写法：

1. 聊天框画字用的是 `Fonts.def`，图标必须是真 glyph。`UI.formatIcons(":logic:")` 会把 `:name:` 换成 `Iconc`
   里的字符（原版发送聊天时自己也这么干），所以不能写 `[logic]` —— 那是 Label 的标签语法，聊天框不认。
2. 原版 `ChatFragment` 的可见性是 `net.active() && ui.hudfrag.shown`，单机会话里聊天记录每帧被清空。
   所以播报按 `net.active()` 选通道：联机会话进聊天框，单机 / 无头退回原来的淡出提示，状态不会凭空消失。
3. `addMessage(String)` 只往本地 `messages` 里插一行，不广播、不过网 —— 播报是给自己看的日志，不会刷别人的屏。

终态按作业 id 去重（`CHAT_DONE`），单作业面板轮询和批量框轮询同时看到同一个作业也只报一次。
自检口：`maiChatProbe()`（无头 ui smoke 的 `MAICHAT` 就是它）；断言表 L2-62。

## v0.12.9：区域快照不只说「有几台处理器」，还说「这里有什么」

框选之后送给模型的区域快照，以前只有方块格子（`grid`）和处理器（`processors`）——模型能数出「这片区域有 4 个
容器」，但看不见容器里有没有铜，也不知道边上停着一队敌方单位。补给类、巡逻类、开火类的描述就全靠猜。

现在快照多带五段：

- `blocks`：按格计数（container 2x2 算 4 格）；
- `items` / `liquids`：区域里的存量。**按建筑身份去重**——一个 2x2 容器在四个格子上是同一个建筑，
  所以 100 铜只算一次 100，不会变成 400；
- `stores`：每台有库存的建筑一条明细（坐标 / 方块 / 队伍 / 物品 / 液体），最多 16 条；
- `units`：单位总数 + 按类型 + 按队伍（单位不在方块网格里，走 `Groups.unit` 再按 `u.x / Vars.tilesize`
  落 tile 筛边界）；
- `enemies`：敌方单位与敌方建筑计数（单机也分得清队伍；无头 / 没有本地玩家时两个计数都是 0，
  并带一句 `note` 说明「enemy 在这里没有意义」）。

面板里那行区域摘要跟着多一行「方块 N 种 物品 … 液体 … 单位 …（敌方单位 …，敌方建筑 …）」；sidecar 侧
`tools/mlogprovider.py` 的 `snapshot_facts()` 把这四类事实写成世界事实喂给模型：

```
items stored in the area: copper 100, lead 12.5
liquids stored in the area: water 3
units in the area: 2 (dagger x2; teams: sharded 2)
enemies in the area: 1 unit(s) flare x1, 1 enemy building(s)
```

自检口：`maiSnapshot(x1, y1, x2, y2)`（无头 ui smoke 的 `MAISNAP` 就是它）；断言表 L2-60 / L2-61。

顺带记一个引擎坑：`Groups.unit` 是 `EntityGroup`，它把继承来的 `Seq.get` 挡住了——`Groups.unit.get` 在
v160.7 上是 `undefined`，而 `size` 又是方法不是字段。所以单位计数先 `rawSeq()` 拿底层 Seq、`seqSize()` 两种
都试；按 `seq.get(i)` 直接写会一次都不循环，单位数静默变 0。

## v0.12.8：等作业时改了链接拦得住；联机里被服务端拒掉会明说「无权限」

两件事，都在「写入」这一步。

**冲突检测不再只看代码。** 投递作业的时候，模组记下处理器当时的代码，也记下它的链接表（每个链接名 +
绝对坐标，排序后成一个签名）。作业回来点「写入」时两张表一起比：

- 只有代码变了 → 「代码 3 行 → 5 行」；
- 只有链接变了（你趁等的时候拆了一条、加了一条，或者把同名链接换到另一台方块上）→
  「链接表 [iron@10,4] → [iron@10,4 copper@12,4]」；
- 两样都变 → 两条都列出来。

拒绝一次，再点一次「写入」就是确认覆盖，跟以前一样。加这一条是因为代码里的链接名是模型按**投递那一刻**的
链接表生成的：链接表一变，写进去的程序就满屏红箭头（链接名没绑上），比覆盖掉代码还难查。

**联机里被拒的写入会明说。** 联机时逻辑块由服务端执行——`configure()` 只是发一个包。服务端先查「这块
处理器是不是你的队伍 / 编辑模式」，不是就当场拒；模组这边也先拦一道（整批一台都不写，消息以「无权限：」
开头）。麻烦的是服务端拒绝时**不给客户端报错**，它只是偷偷把客户端刚设的配置撤销回去（原版源码注释：
`undo the config on the client`），从外面看就像写成功了。

所以现在写完会回读：1.5 秒后读一次处理器的 `code`，跟要写进去的文本比；对不上就再等，连续 3 次都对不上
才提示「无权限：… 的写入没生效（服务端没有接受）。联机里逻辑块由服务端执行，需要房主 / 管理员给权限，
或改本方队伍的处理器」。单机（`Vars.net` 为空）两道闸都不进，一次多余的判断都没有。

自检口：`maiLinkConflictProbe()` / `maiNetProbe()` / `maiWriteCheckProbe()`；断言表 L2-58 / L2-59。
真联机要开服才能验，本轮只在无头引擎里验了单机分支。

## v0.12.7：框选框不会自己消失

框住一片处理器、写一句描述、等作业跑完之后，框和右下角的徽标会一直留着——不会再「多留 8 秒然后
自己没了」。它只有两个消失理由：

- **写入成功**：框里某个处理器写入成功，框就收起来（那正是你框它的目的）；
- **你主动关**：作业管理窗里点「关闭框选框」，或者右键点框右下角的徽标。

生成完还没写的时候框不会跑，可以对着框里那几个处理器慢慢挑。

## v0.12.6：还在重试的时候，别把「上一次的问题」当成结论

推理模型偶尔会把回复预算全花在思考上，然后一个字都没写出来（`finish_reason=length`）。这时候作业会**自己重试**
（sidecar 把预算从 4096 加到 16000 再问一次），多数情况下第二次就成了——真机上那次 `attempts: 2` 就是这么来的。

但重试期间作业的 `findings` 里躺着上一次的错误，作业管理窗照实渲染，于是玩家看到的是：

```
ERR provider-error model endpoint answered with an empty message (finish_reason=length; ...)
```

看着就是「生成失败」，其实还在跑。现在：

- 非终态作业的 findings 一律抬一个抬头：`--- 上一次尝试的问题（第 2 次，还在重试）---`；
- 消息里出现 `finish_reason=length` 时补一句人话：`（模型把回复预算用在思考上了，sidecar 会自动加大预算再试一次）`；
- sidecar 侧同步把预算写进消息本身：`... (budget=16000 tokens; finish_reason=length; ...; the model ran out of room mid-thought, before it wrote an answer)`，
  这样就算只在盘上的作业文件里看，也能分清「被截断」和「被拒绝」。

## v0.12.5：F3 只弹一个描述框，框留到作业做完，角标点开作业管理

以前 F3 拖一个框松手，弹的是「区域快照」面板：框本身画完就消失，作业状态只挂在某一个 AI 面板里——
换个处理器就看不见了，一次想改一片处理器只能一台一台来。

现在 F3 松手只有一件事：一个描述框。

```
Mlog辅助 · 描述 v0.12.5
目标：区域里 3 个处理器，每个处理器生成一段
[                                    ]   ← 就这一行，写一句话
                    [生成] [取消]
```

「生成」把框里的处理器逐个投一个作业（一个处理器一个作业，各写各的），然后：

- **矩形留在原地**，直到这一批作业全部终态；右上角不用管，右下角挂一行小字：
  `作业 1/3 · 生成中（点开看）` → 全部完成 `作业 3/3 · 完成（点开看）`；有失败就是 `作业 3/3 · 2 成 1 败（点开看）`。
- 全终态之后框还会**多留 8 秒**（`JOB_BOX_LINGER_MS`），免得你还没看清就没了。
- **点右下角那行字**，开「作业管理」窗：
  - 上半是**本次框选**的作业（点一条看详情）；
  - 下半是 sidecar 里**最近的 24 个作业**（`GET /jobs?all=1&limit=24`，跨重启也在），谁被拒了、拒在哪条规则，一眼看到；
  - 详情页：状态行 + `--- 模型思考（N 字符）---`（v0.12.4 的回显）+ findings（最多 6 条）+ 代码；`done` 的可以直接「写入到 @x,y」，还在飞的可以「取消这个作业」。

没拖框时点它不会开窗（只在真有作业框的时候抢这次点击），也不会挡住框里的格子——徽标画在框底边外侧。

- 控制台：`maiJobBox()` 看状态机的 JSON（含 `linger`/`badge`）、`maiJobBoxPin(idsJson, statesJson)` 不碰 sidecar 注入假作业、
  `maiJobBoxClear()` 清框、`maiJobManager()` 开窗（无头引擎回 `no ui`）。
- sidecar 侧同版加的是「历史」这一问：`Queue.history()` + `GET /jobs?all=1&limit=N`（剥掉 snapshot/code/reasoning 这些重字段），
  `SIDECAR_VERSION` 是 `0.12.5`——**改过 `tools/logicd.py` 要重启那个进程**，否则管理窗下半永远空着（`GET /health` 的 `version` 会告诉你跑的是哪一版）。
- 旧的区域快照面板没删：控制台 `maiDump(x1,y1,x2,y2)` 和 AI 面板那条老路还在。

## v0.12.4：模型在想什么，面板里直接看

推理模型（`deepseek-flash` 之类）先写 `reasoning_content` 再写 `content`。以前点「生成」后只看到
「作业 …：被门禁拒绝（尝试 3 次，ERR 1 条）」加一行 message —— 是模型想歪了、还是根本没照做，看不出来。

现在 sidecar 把回复里的推理内容存进作业的 `reasoning` 字段，面板在状态行下面多一段：

```
作业 …：完成（4 条语句，60 字节，尝试 1 次）
--- 模型思考（1832 字符，只显示开头）---
（分段显示开头 16 行，每行 40 字符）
```

- 端点不返回推理内容时，**这一段整个不出现**（模板档、不支持推理的模型都是这样）。
- 控制台 `maiJobReasoning(jobJson)` 取原文，`maiDiag()` 里的版本号变成 0.12.4。
- 只有 sidecar 侧和这一段渲染是新的；作业状态机、门禁、写入通道都没动。
- 源码红线两条：`function jobReasoningLines(`、`api.maiJobReasoning = function`。

## v0.12.1：模型档自己会说话 —— 地址补全、端点自测、重启后自动重同步

### 症状

玩家填了地址与密钥，投递作业只拿到两句英文：先是
`ERR L0 model endpoint unreachable: HTTP Error 404: Not Found`，换了地址之后又变成
`ERR L0 empty reply`。两句都不指向真正的原因，玩家只能猜。

### 三处改动

1. **地址可以只填到 API base**。`tools/logicd.py` 新增 `completion_url(url)`：以
   `/chat/completions`、`/completions`、`/responses` 结尾的原样放行，否则补上
   `/chat/completions`。`POST /llm` 与 `POST /llm/probe` 都走它，所以填
   `https://api.deepseek.com/v1` 也能直接用（原来会 404）。
2. **设置页多了「测试端点」**（四个按钮：获取模型 / 测试端点 / 启用模型档 / 回到模板档）。
   它 `POST /llm/probe`，sidecar 替模组发一次最小 chat 调用（`max_tokens` 128、
   `temperature` 0），把 **HTTP 状态 / finish_reason / 回复长度 / message keys /
   reasoning 长度 / 回复原文（≤800 字符）** 摊在面板上。于是：
   - 404 → 地址形状错（现在也会自动补全）；
   - `finish_reason=length` + content 空 → token 被截断；
   - `reasoning_len>0` + content 空 → 模型把话都说在 reasoning 里；
   - 401 → 密钥错。
3. **sidecar 重启不再悄悄退回模板档**。配置只存在 sidecar 进程内存里，重启就没了，
   玩家看到的是「找不到样板」。模组侧新增 `llmSync()`：载入时与每次开面板都
   `GET /llm`，发现 `kind` 不是 `llm` 或模型名不一致，就用 `Core.settings` 里的三件套
   `POST /llm` 推回去（日志 `llm tier re-synced from settings: <url>`）。

顺带：`mlogprovider.HttpProvider` 的 `max_tokens` 默认 900 → **1600**（推理模型常常把
900 全花在 reasoning 上），404 的错误文案补上「这个地址可能是 API base，不是
/chat/completions」，空回复的错误文案带上 `finish_reason`、message keys 与 usage。

### 验证

`tests/logicd/run_logicd_tests.py`：`llm` 6 → **8** 项（base url 归一、满 URL 不被改写、
`/llm/probe` 打到 stub 并带回回复原文），`provider` 9 → **11** 项（空回复必须带
`finish_reason=length`、404 必须带 `/chat/completions` 提示）。`run_ui_smoke.py` 加
v0.12.1 源码红线三条（`function llmSync(`、`function llmProbe(`、`"/llm/probe"`）。

### 边界

- 模组的裸 Socket 只有明文 HTTP，模型列表与自测都必须经 sidecar（https 由 Python 侧发）。
- 密钥仍然只存在 `Core.settings` 与 sidecar 内存里，面板只显示尾巴。
- 自动重同步只覆盖「模组里有、sidecar 里没有」这一个方向；在 sidecar 命令行里换档仍然
  以模组设置为准（游戏里改过就是游戏里赢）。

## v0.12.0：一次写多台 —— 要么全写，要么全不写（事务写入核心）

**症状**：一句话想写到一片处理器上时，之前只能一台一台点。中途某台失败，存档就停在一半新一半旧的状态，撤销也只能一台一台退。

**改动**（`scripts/main.js`，P5 第三刀的**上半**：写入核心，面板入口下一刀）：

- 新增 `writeMany(items, why)`：每台处理器一个作业（链接表、门禁、修复循环各自独立），但**写入是一次事务** ——
  先整批预检（目标在、0 ERR、有载荷），再整批问冲突（有一台被改过就整批拒绝一次，再调一次 = 确认覆盖），
  然后**先把每台的原代码落盘**（同一个 `batch`），最后逐台 `configure`。
- 任一台 `configure` 抛错：把**已经写过的按快照 `updateCode` 回滚**，返回「写入失败（@x,y：…）；已回滚 N 台：…」，
  存档不会停在一半。
- 撤销是整批的：内存栈里一条 `{many:[…], batch}`；磁盘快照 JSON 多一个 `batch` 字段，
  `undoFromDisk()` 见到带 batch 的最新快照就调 `undoBatchFromDisk()` 把整批还原（处理器已被拆掉的跳过并删掉该文件）。
- 控制台 `maiWriteManyProbe(jobJson, x, y)`：现放一台处理器（结束时拆掉），验三件事 —— 整批写进去、整批撤销、
  第二台 `configure` 抛错时第一台被回滚。

**验证**：`python -X utf8 tests/mlogauxiliary/run_ui_smoke.py` → EXIT=0，摘要行
`batch write: ok 已写入 2 台（事务 1793382167907_b）：@0,0 @3,3 | 已回滚 2 台（事务 1793382167907_b）：@0,0 @3,3`；
`MAIDIAG:版本：0.12.0`。

**边界**：这一刀只有核心与探针，**没有面板按钮**（多台一起投递需要面板同时轮询 N 个作业，是下一刀）；
`writeMany` 只吃「已经 done 的作业结果」，不负责投递。单台写入（`writeBuild`）的路径一行未改。

## v0.11.0：设置页里填模型地址 / 密钥 / 模型，点一下就换档

**症状**：模型档其实早就能用（`--jobs-provider llm`），但只有命令行能开。玩家在游戏里
套不上样板时只看到「no template matched」——不知道还有模型档，更没法填端点。

**改动**（`scripts/main.js` + `tools/logicd.py` + `tools/jobqueue.py`）：

- 设置 → Mlog辅助 新增「大模型」段：地址 / 密钥 / 模型三行输入 + 三个按钮。
  值存在 `Core.settings`（键 `mlogauxiliary-llm-url` / `-llm-key` / `-llm-model`），
  密钥那一行用 `TextField.setPasswordMode(true)` 打成圆点。
- 「获取模型」→ `POST /models` 由 sidecar 代取模型列表（**模组自己打不了 https**：
  传输层是裸 `java.net.Socket` 手写的明文 HTTP），拿到后弹一个可滚动列表点选。
- 「启用模型档」→ `POST /llm {url, model, key}`：sidecar 在**进程内**把队列的 provider
  换成 `mlogprovider.HttpProvider`，不用重启；「回到模板档」→ `POST /llm {kind: "template"}`。
- 诊断里多一行「大模型：模型档 · <地址> · <模型> · 密钥 已填 ******1234」——
  **只显示尾巴**，明文密钥不出现在面板、日志、任何响应里（`GET /llm` 也只回 `key_set`/`key_hint`）。
- 模板档套不上样板时的报错后面追加一句中文：当前是模板档，去设置里填模型再点「启用模型档」。

**验证**：

1. `tests/logicd/run_logicd_tests.py` 新增 `llm: 6 check(s)`：起一个假 OpenAI 端点（`/v1/models`
   回两条、`/v1/chat/completions` 回一段干净代码），验 `/models` 的 URL 推导与 Bearer 透传、
   `/llm` 换档后密钥不回显、换档后**真作业**能走到 `done`、`ftp://` 被 400、`{kind: template}` 能回去。
2. `tests/mlogauxiliary/run_ui_smoke.py` 新增 `MAILLM:ok`：真引擎里存三件套 → 状态文本显示模型、
   **不含密钥明文也不含尾巴**、把「还没提交给 sidecar」说出来 → 原样恢复；另加五条源码红线
   （`function llmText(`、`function showPickList(`、`setPasswordMode(true)`、`llmSave(LLM_KEY_KEY`、
   `httpJson("POST", "/models"`）。

**边界**：模型档的质量取决于端点和模型，模组不判断模型好坏；换档只影响之后的作业，
已经在跑的作业用换档前的 provider 跑完。密钥存在游戏存档目录的 `settings.json` 里（明文，
和原版所有设置一样）——要更严就别在共享机器上填。

## v0.10.1：投递作业之后处理器被改过 —— 第一次写入会被拒绝

**症状**：作业是异步的（模型档可能要等几十秒）。等结果的时候顺手在原版编辑器里改了代码，
面板上一点「写入」，作业结果把这段改动**静默覆盖**掉了。

**改动**（`scripts/main.js`）：

- 投递那一刻把处理器代码记成基线：`JOB_ORIG["x,y"] = String(b.code)`，两条投递路径都记
  （`jobSubmit()` 与 `jobSync()` —— 后者是同步跑完的测试/控制台入口，一开始漏了，ui smoke 的端到端用例把它抓出来了）。
- 写入前比对：不一致就**拒绝一次**，文案点明「提交时 N 行，现在 M 行」，并提示再点一次「写入」才算确认覆盖
  （`JOB_FORCE` 只放行一次，第三次又会问）。
- 写成功之后基线过期（`forgetOrig()`），不会误报下一次写入。
- **不用对话框是有意的**：无头环境里 `Core.scene` 是 null，弹窗直接 NPE；
  而「拒绝一次 + 再点一次」在真机与测试里走的是同一条路径，不用为测试造第二条代码路径。
- 控制台新增 `maiConflictProbe()`：用假处理器（只有 tile 与 code）验冲突闸的三个分支，不碰存档、不写撤销快照。

**验证**：`tests/mlogauxiliary/run_ui_smoke.py` 里两条 ——

1. `MAICONFLICT:ok`：拒绝 → 放行一次 → 第三次又问 → 代码没动过就不问。
2. 端到端（真 sidecar + 真 v160.7 处理器）：投递作业 → `updateCode` 改代码 → 第一次 `maiJobWrite` 返回
   「被改过」且**处理器代码没被动**，第二次「已写入」。摘要行：
   `job conflict gate: first write refused, second click overwrote`。

## v0.10.0：撤销能跨重启了 —— 写入前把原代码落盘（P5 第一刀）

**症状**：写错了想回滚，可是内存里的撤销栈只活到关游戏。第二天开游戏，原来的代码已经没了。

**改动**（`scripts/main.js`）：

- 每次写入前，先把「处理器坐标 + 原代码 + 链接表 + 时间」写成
  `<数据目录>/mlogauxiliary/undo/<毫秒时间戳>_<x>_<y>.json`（文件名可排序 = 时间顺序），最多留 40 份，旧的自动删。
  快照失败不影响写入（只写一行日志），但写入成功的提示里会带上快照名。
- `undo()` 改成两级：内存栈有就先用内存（快，联机也一样只是本地生效），没有就从磁盘上**最新的一份**快照回滚。
  磁盘回滚会跳过「处理器已经不存在」的快照（拆了、换图了），继续往前找。
- 设置 → Mlog辅助 → 快捷动作 多一行两个按钮：**撤销上次写入** / **撤销快照**（后者只报份数和目录）。
  控制台也多了 `maiUndo()` 与 `maiUndoInfo()`。

**为什么只还原代码**：写入走 `build.configure(bytes)`，动的是代码 + 链接表，但模板生成的链接表来自处理器
**当前**的链接，所以回滚只需要把 `b.code` 换回去（`updateCode`）。联机下 `updateCode` 仍然只是本地生效，
这一版不假装能替服务器回滚。

**验证**：`tests/mlogauxiliary/run_ui_smoke.py` 在真 v160.7 引擎里跑 `maiUndoProbe()` —— 把一份假快照写进
`<数据目录>/mlogauxiliary/selftest`，读回来比对 x/y/code/links，再删掉文件与目录，输出 `MAIUNDO:ok <文件名>`；
另外 `maiSelfTest()` 里加了一次纯序列化往返（`撤销快照 ok`）。三条源码红线盯着
`saveUndoTo(` / `undoFromDisk(` / `writeBuild` 里记快照名那行。

**没做（P5 剩下的）**：一个作业产出多段代码时的事务写入与整体回滚、写前「处理器被改过」的冲突提示。
这两件要等 sidecar 的作业契约扩展，见 `docs/Mlog辅助-规划.md` §5。

## v0.9.1：侧车进程太旧时，面板把「重启 sidecar」直接说出来

**症状**：投递失败，面板上只有一行 `no such endpoint: /jobs`。

**根因不在模组，而在进程**：模组连的是已经跑了几个小时的 `python tools/logicd.py`，
那是一个 *在 /jobs 出现之前* 启动的进程 —— Python 把模块加载进内存之后，磁盘上再改代码它也不知道。
所以同一个模组版本，重启侧车前是 404，重启后立刻正常。v0.9.1 只做一件事：把这个诊断变成面板上的一句话。

**改动**（`scripts/main.js`）：

- 新增 `sidecarStaleHint()` / `jobWhy()`：错误文本里出现 `no such endpoint` 时，换成
  「sidecar 进程是旧版本，不认识这个接口 → 关掉旧进程再 `python tools/logicd.py --port 8765`」，
  并点明「改过 `tools/logicd.py` 之后必须重启」。
- 三个作业接口（`POST /jobs`、`GET /jobs?id=`、`DELETE /jobs?id=`）的错误回调统一走 `jobWhy()`，
  其它错误（连接被拒/超时/sidecar 返回码）行为不变。

**验证**：`tests/mlogauxiliary/run_ui_smoke.py` 新增源码红线（`function sidecarStaleHint(` 必须在、
`no such endpoint` 分支必须在）；活体复验用的是真 sidecar：重启前 `GET /jobs` 404，
重启后 `POST /jobs`（container1 + message1）→ `done`，1 次尝试、184 B 载荷、7 条语句、0 条 finding。

**顺带确认的边界**：模板档要打印就必须有消息块链接。处理器只有 `container1` 没有 `message1` 时，
作业会 `rejected` 并报 `template-needs-link`，文案里已经写明「代码里写的 message1，处理器没有这个链接」——
这是对的：mlog 没有 printflush 的对象就什么都看不到，模板不应该凭空写一个不存在的链接。
## v0.9.0：F4 键彻底删除 —— 以及「处理器一个链接都没接」时门禁漏判的真凶

v0.8.1 把 F4 并进 F3 之后，F4 只是留了个别名；这一版按玩家要求**整把删掉**：
`KEY_PANEL` 常量、`toggleAiPanel()` 函数、update 钩子里那 6 行 `keyTap` 全部消失，
钩子外层那个 `if(KEY_PANEL !== null){` 也退化成匿名块（不再有任何按键开关门）。
现在开 AI 面板只有一条路：**按住 F3 在处理器上点一下**。ui smoke 里原来的正向红线
（必须有 `toggleAiPanel();`）改成了**反向红线**：源码里再出现 `KEY_PANEL`、`toggleAiPanel`
或字符串 `"f4"` 就判失败 —— 免得以后有人「顺手」把热键加回来。

### 门禁漏判：空链接表被当成了「不知道」

sidecar 的写入门禁靠 `link-name-not-bound` 拦幻觉链接名（`jobqueue.FATAL_RIDS`）。
但这条规则在 `mlogcheck` 里是这么写的：只有 `links is not None` 才检查「这个名字在不在链接表里」。
而 `logicd.analyse()` 原来把模组送来的 `links` 用 `_link_names(links) or None` 过了一道 ——
**空列表被折成了 None**。于是「这台处理器一个链接都没接」（模组读到的权威空表 `[]`）和
「模组读不到链接表」（`null`）在分析器眼里变成了同一件事，`sensor x container9 @copper`
这种指名道姓要连 `container9` 的程序在零链接处理器上直接判干净、写进去。

修法是把「权威空表」和「不知道」分开：

- `mlogcheck.analyse(..., links_authoritative=False)`：新增开关，只影响「程序里有大量形似链接名的
  变量、但一条链接都没声明」时的碎片启发式分支（`len(stmts) > 200` 那条），默认关，语料与 golden 行为不变。
- `logicd.analyse()`：`links is not None` 时把 `links_authoritative=True` 传下去，同时 `names` 保留空列表本身。
- `jobqueue`：`normalize()` 允许 `links` 为 `null`（表示「不知道」，门禁跳过链接名判据），
  `_materialize()` 里**不再** `or []`（这一处把 null 又变回权威空表，是第一次改完测试仍红的真凶），
  只有给 `msch` 造载荷时才 `links or []`。
- 模组侧新增 `linkListOrNull(b)`：读得到链接表就发数组（可能是 `[]`），读不到才发 `null`。

验收（三处，都跑在真 v160.7 引擎上）：作业队列契约测试从 9 项涨到 **12 项**（`links: []` 必 rejected、
`links: null` 必 done、`gate()` 单元三连）；ui smoke 多一路 `MAIJOBZERO` —— 引擎里那台处理器**一个链接都没接**，
假 provider 让它输出 `sensor x container9 @copper`，实测 `job zero-link gate: rejected, rule=link-name-not-bound`。
`python -X utf8 tests/run_tests.py` 44/44（改 `mlogcheck` 后要先 `tools/sync_mlogagent.py --mirror all`）。

## v0.8.1：F4 并入 F3 —— 单格点一下处理器就开 AI 面板

原来 F4 是「开 AI 面板」的入口，F3 是「框选」的入口，两把键干两件事、还得先想起哪把是哪把。
v0.8.1 把它们合成一把：**按住 F3 在处理器上点一下（不拖）= 开这台处理器的 AI 面板**，
**按住 F3 拖出矩形 = 区域快照**（不变）。空格子单点给一句提示，不再假装「没有拖动」。

- `processorAt(x, y)`：这一格是不是处理器（`buildAt()` + `isProcessor()`，鸭子类型判据）。
- `selFinish()`：1×1 先问 `processorAt()`，是处理器就 `showAiPanel(one)`；不是才提示。
- **F4 键仍然能用**（老习惯的键盘入口，走同一个 `toggleAiPanel()`），只是不再需要记它。
- 控制台 `maiPick(x,y)`：这一格能选中处理器就返回 `"x,y"`，否则空串（无头测试用）。

真引擎验的：`MAIPICK` 在处理器格子上返回坐标、在相邻的容器格子上返回空串；
源码红线钉住 `processorAt(` / `showAiPanel(one);` / `var one = processorAt(r.x1, r.y1);` / `api.maiPick`。
## v0.8.0：生成走作业队列（可取消、有落盘、门禁更严）

F4 面板原来点「生成」是直接 `POST /plan` —— 同步语义、不落盘、不能取消，而且分析器对
`link-name-not-bound` 只给 WARN。v0.8.0 把它接到 P4 的作业队列上：

- **投作业**：面板把「一句话 + 处理器链接表 + 相对坐标 + 13×13 框选快照」打包成 job，
  `POST /jobs` 交给 sidecar（`jobBody()` / `jobSubmit()`）。
- **每秒轮询**：`jobTick()` 挂在每帧钩子里，`JOB_POLL_MS = 1000`，查 `GET /jobs?id=`；
  面板关掉就停止轮询（`aiJob = null`），不会漏掉也不会白耗。
- **完成/拒绝都渲染**：`renderJob()` —— done 时列代码并把按钮换成「写入 / 关闭」；
  rejected / failed / cancelled 时列 findings 与「被门禁拒绝」的原因（`jobProgressText()`）。
- **取消**：面板上的「取消作业」走 `DELETE /jobs?id=`，正在跑的作业也会在检查点退出。
- **保留退路**：「直接生成」按钮仍在（旧的 `/plan` 同步路径），老 sidecar 没有 `/jobs` 时面板照样能用。
- **控制台入口**：`maiJob(desc)` / `maiJobSync(desc,ms)` / `maiJobSyncAt(x,y,desc,ms)` /
  `maiJobWrite(json,x,y)` / `maiJobProgress(json)` / `maiJobCancel()` / `maiJobState()`。

测试把这条链在**真 v160.7 引擎**里跑通（`tests/mlogauxiliary/run_ui_smoke.py` 起一个真
`tools/logicd.py` + 假 provider）：干净作业 → `done`/attempts 1/60 字节载荷被写进处理器；
坏代码作业 → `rejected`/attempts 3/带 ERR findings。

## v0.7.0：F2 —— 全图处理器清单

之前「哪台处理器有问题」只能一台一台把鼠标挪过去按 F4，或者去控制台敲 `maiScan()` 看一段纯文本。
v0.7.0 把这件事做成面板：

- **F2** 扫一遍全图（`Groups.build` 里用 `isProcessor()` 的鸭子类型筛处理器，和 `targetBuild()` 同一套判据），
  对每台跑一次本地快检，然后按 **ERR 多 → WARN 多 → 坐标** 排序。
- 每行一个按钮：`@x,y  12 行  ERR 2  WARN 1`（干净的写「干净」）。点一行 = 相机跳到那台处理器
  并把面板关掉、直接开 F4 面板 —— 也就是「从清单直接进到能改它的地方」。
- 底部「复制清单」把同样的文本放进剪贴板（`Core.app.setClipboardText`），贴给 AI 或存档都行。
- 面板建不出来时回落成纯文本弹窗；控制台退路是 `maiList()`（文本摘要）与 `maiListRows()`（JSON 行）。

只读：不写方块、不写代码，跑的是和审查栏同一个快检函数（`api.q.check`）。

键位：F2 是这张图上唯一还空着的功能键 —— `Binding.java` 里 F1（方块信息）/F5（电力线）/F6（方块状态）/
F8（控制台）/F11（全屏）/F12（截图）都被游戏占着，F3/F4/F7 是我们自己的。

## v0.6.1：选框偏了半格 —— Tile.worldx() 是格子中心

玩家实测（v0.5.0/v0.6.0）：「选框位置偏右上角越半格」。

根因在坐标口径，不在画法：`Tile.worldx()` 返回的是 `x * tilesize`
（`Mindustry-Source/core/src/mindustry/world/Tile.java:166-172`），也就是**格子中心**的世界坐标；
格子 `(x1,y1)` 的左下角其实是 `(x1 - 0.5) * 8`。v0.5.0 直接把 `r.x1 * 8` 当左下角传给了
`Lines.rect(x,y,w,h)`（它按左下角 + 宽高画），于是整框向右上各偏 4 px = 半格。

原版也是先减半格的：`Placement.java:380-390`
`drawResult.x = result.x * tilesize - block.size * scaling * tilesize / 2`（air：size 1、scaling 1 ⇒ 减 4）。

修法一行：`selDrawArgs(r)` 改成
`{x: (r.x1 - 0.5) * 8, y: (r.y1 - 0.5) * 8, w: r.w * 8, h: r.h * 8}`。

测试：`MASELDRAW` 的期望值跟着改（`(bx+5)*8-4`），并新增源码红线
`(r.x1 - 0.5) * 8` / `(r.y1 - 0.5) * 8` 必须同时出现 —— 少了半格就红。

## v0.6.0：框选之后有面板了 —— 候选链接 + 一键交给 AI

v0.5.0 的 F3 松手只做了一件事：把快照 POST 到 sidecar 落盘，然后弹一段纯文本说「已落盘 xxx.json」。
对玩家来说这等于「框了半天，只得到一个文件路径」。v0.6.0 把松手之后的出口换成**区域面板**：

- 顺序：`POST /snapshot`（落盘）→ `POST /region`（每个处理器 10 格内的可连方块与链接名）→ 开对话框。
- 面板内容：区域摘要（bounds / 方块数 / 处理器数）+ 落盘路径 + 每个处理器的链接候选与
  「代码在用但没绑上」的告警（复用 v0.4.5 的 `missing`）。
- 每个处理器一行按钮「用 @x,y 生成」：点了直接开 F4 的 AI 面板，**并且以那个处理器为目标**
  （`showAiPanel(target)` 新参数），不用再把鼠标挪过去。
- 底部两个按钮：「复制快照 JSON」（`Core.app.setClipboardText`）、「关闭」。
- 面板建不出来时（`pane()` 重载缺失等）回落到旧的纯文本弹窗，不会连候选都看不到。

代码位置：`buildRegionTable()` 在脚本顶层（只碰 table 接口，能用假表在无 UI 环境数行数），
`showRegionPanel()` / `buildAt()` 在引擎作用域里。`dumpRegion()` 从「一次请求 + popup」
改成「两次请求串起来 + 面板」。

测试：ui smoke 的 `maiSelfTest()` 现在多建一张区域表（假表 8 行），并新增四条源码红线
（`buildRegionTable`/`showRegionPanel`、`buildAt`+`showAiPanel(b)`、`labelWrap(String(candText))`、
`function showAiPanel(target)`）。无头引擎里 F3 那条路径本来就直接返回摘要、不发请求，所以面板只在真机上出现。

## v0.5.0：F3 就是鼠标左键，高亮改成原版选框样子

用户原话（m11493）：「框选区域是ohno，尝试做成原版的矩形选框风格。而且我要的是按下快捷建后就直接拉矩形，相当于f3就是鼠标左键。」

两件事：

1. **F3 是按住式的左键，不再有「就绪」这个中间态。**
   按住 F3 = 按下左键（起点 = 此刻鼠标格），移动鼠标改终点，**松开 F3 = 抬起左键并出快照**。
   v0.4.9 的“按 F3 就绪 → 再按住左键拖”已经去掉：框选不再碰鼠标左键。
2. **高亮照抄原版选框**（`InputHandler.java:1758-1795` `drawSelection`）：
   `Lines.stroke(2)` 两次描边，先 `Pal.accentBack` 画一条 `y-1` 的阴影，再 `Pal.accent` 画本体，
   坐标用**左下角 + 宽高**（`Lines.rect(x,y,w,h)`）；另外在光标旁写「宽x高 (面积)」
   （`mindustry.ui.Fonts.outline` + `1/camerascale` 缩放 + `Draw.z(Layer.endPixeled)`，画完逐项还原）。
   原版**没有半透明填充**，所以 v0.4.8/v0.4.9 那个 `Draw.alpha(0.35)` 的方块去掉了。

顺带修掉一个潜在错误：`Fonts` 是 **`mindustry.ui.Fonts`**，不是 `mindustry.gen.Fonts`
（`out/_server160.jar` 里只有 `mindustry/ui/Fonts.class`）—— 写错包名会被 `catch` 吞掉，字永远画不出来。

测试：`tests/mlogauxiliary/run_ui_smoke.py` 的 `select_probe()` 直接驱动状态机
（`maiSelPress` 就是按下、`maiSelRelease` 就是抬起，中间没有 arm），并用反射钉住
`Lines.rect(float,float,float,float)` / `Lines.stroke(float)` / `Draw.color(Color)` / `Pal.accentBack` / `mindustry.ui.Fonts.outline`；
源码红线：必须有 `L.stroke(2);`、`Pal.accentBack`、`function drawSizeText(`、`Core.input.keyDown(selKey)`，
不得出现 `selArmed`、`D.alpha(0.35)`、`keyTap(Packages.arc.input.KeyCode[KEY_SELECT`。

## v0.4.9：修掉 F3 的两个手感 bug

v0.4.8 装进游戏后用户立刻报了两条，都是真 bug：

1. **按 F3 弹出提示框，结果拖不动。** 提示用的是 `showText`（Dialog），而 Dialog 会吃掉鼠标
   （`Core.scene.hasDialog()` 为真，世界收不到点击），于是「就绪提示」本身把框选废了。
   现在改成 `showInfoFade`（`Mindustry-Source/core/src/mindustry/core/UI.java:373`，
   `Touchable.disabled` 的淡出标签，不挡输入），而且**按 F3 只置就绪、什么都不弹**。
2. **预览框的中心落在指针上。** `Draw.rect(region, x, y, w, h)` 的 x,y 是**中心**不是左上角
   （`Arc-Source/arc-core/src/arc/graphics/g2d/Draw.java:378`：`batch.draw(region, x - w/2f, y - h/2f, …)`），
   v0.4.8 传的是角点，框就被画到指针的左上。现在传 `(x1 + w/2) * 8` 这样的中心坐标。

顺手改了一处语义：**拖拽起点取「按下左键的那一格」**，不是按 F3 的位置 —— 否则按完 F3
鼠标还没动就已经有一个框，松手就是 1×1 的「以指针为中心」的小方块。只点一下不拖会提示
「没有拖动」，不再落盘。

控制台新增拖拽状态机的测试钩子：`maiSelState()`、`maiSelPress(x,y)`、`maiSelMove(x,y)`、
`maiSelRelease()`、`maiSelDraw(x1,y1,x2,y2)`；断言见 L2-37。
## v0.4.8：F3 拖拽框选 —— 框选矩形终于能自己画，快照直接落盘

规划里 F7 一直是「框选矩形」（`docs/Mlog辅助-规划.md:99`），但 v0.4.2 只落地了只读的一半：
以鼠标下的处理器为中心扫固定 10 格。v0.4.8 把拖拽补上，但**没有用 F7 也没有用 F8** ——
游戏里 F8 是控制台（`Mindustry-Source/core/src/mindustry/input/Binding.java:107`
`console = KeyBind.add("console", KeyCode.f8)`），F9/F10 也被占，所以框选落在 **F3**。

用法：按 **F3** 进入就绪 → 按住左键拖出矩形（半透明高亮）→ 松手弹摘要。再按 F3 取消。

- 高亮画在**世界坐标**里（`Events.run(ET.Trigger.draw)` + `Draw.rect(Tex.whiteui, …)`），
  不往场景树里塞元素 —— v0.4.1 那次「点生成就崩」就是自造结果区引起的，能画的地方就不建 UI。
- 松手先给区域摘要（方块数 / 处理器数 / 每个处理器的链接），**不依赖 sidecar**；
  再把快照 JSON `POST /snapshot` 落盘成 `out/snapshots/snap-<时间>-<sha1 前 8>.json`，
  弹窗里补一行「已落盘 <路径>（N B）」。sidecar 没起也能看摘要，只是不落盘。
- `selRect()` 只做归一化（从右下往左上拖 = 同一个矩形），裁到地图边界仍由 `regionSnapshot()` 负责。
- 无头引擎（`Vars.ui === null`）不发请求，只返回摘要 —— 自检不该连网络。
- 控制台：`maiSelect()`（等同按 F3）、`maiDump(x1,y1,x2,y2)`（直接出摘要并落盘）、
  `maiSelRect(ax,ay,bx,by)`（拖拽数学，测试钩子）。
## 接口契约（sidecar v1）


| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/health` | – | `{ok, protocol}` |
| GET | `/capabilities` | – | `{ok, protocol, game, instructions, ops, templates[], limits{ipt_max, link_range_tiles}}` |
| POST | `/analyze` | `{code, links, ipt}` | `{ok, findings[{sev,line,stmt,rid,msg}], errors, warns, statements, cost}` |
| POST | `/compose` | `{template, params, links, ipt}` | 同上 + `{template, code, links, bytes_b64, bytes_len}` |
| POST | `/plan` | `{intent, links, ipt}` | `/compose` 的全部字段 + `{intent, params, reason}`；没命中时 `{ok:false, template:null, reason, candidates[]}` |
| POST | `/region` | `{snapshot}` | `{ok, bounds, cells, truncated, processors[{x,y,block,links[],statements,in_range,used_unlinked,candidates[{name,block,x,y,dist,used}]}]}`；**不写文件、不生成代码** |

* `ok` 的语义是 **没有 ERR**。有 ERR 时模组拒绝写入，WARN/INFO 照常写入但会列在面板上。
* `findings[].line` 是代码里的行号（1 起），`stmt` 是指令槽位序号 —— 两者不同，因为标签行不是语句
  （`LParser.java:117-125`）。
* 模板声明要用某个链接名而请求里没有 → 服务端补一条 `rid=template-needs-link` 的 ERR。

## 游戏内快检（P2）

`scripts/quickcheck.js` 是纯函数、无游戏依赖的 ES5，回答五类**单行**问题：指令是否存在、
`ucontrol`/`draw`/`ulocate` 子命令是否拼错（拼错整行会被解析成 `NoopI`，静默不执行）、
是否用到比当前 build 更新的指令、链接槽里的名字是否真的连着、语句数是否超过 1000（尾部会被截断）。

规则表 `scripts/rules.generated.js` 由 `python tools/rules_export.py` 从
`tools/instr_semantics.json` + `tools/mlogcheck.py` 生成；`gate.py` 跑 `--check` 防漂移，
`tests/mlogauxiliary/` 跑 14 个用例、`require()` 契约、以及「quickcheck 说断链时全量检查也必须同意」的单向验证。

真 Rhino 与 node 的**逐条比对**是独立一步：`python tests/mlogauxiliary/run_rhino_parity.py`
（无 JRE 时 SKIP）。理由是真 v160.7 的 Rhino 里 `"abc def".split(/\s+/)` 会按**每个字符**切开
（带 `+` 量词的正则在它的 `String.prototype.split` 里等价于空匹配，只有单字符模式正常），
node 侧永远发现不了 —— 所以本文件的分词、裁剪、标识符判断全部手写扫描，不用 `split(正则)`。

两条源码事实（v160.7 实测）：

* 游戏只执行 `scripts/main.js`（`Mods.java:809`），其余文件必须 `require()` 进来；
  `Scripts.java:30-32` 装了 CommonJS 模块提供器，实测 `typeof require` 是函数。
* 版本号取 `Packages.mindustry.core.Version.build`（`Version.java:22`，自定义构建是 `-1`，
  此时版本门不启用）。

**边界**：快检只许少报，不许瞎报。未赋值变量的槽位分级、成本 CFG、值域这些需要数据流
推理的判据不在游戏内，仍由 sidecar 回答。

## 明确没做

* 撤销只在内存里，不落盘（P3：`<dataDir>/mlogauxiliary/undo/`）
* 没有 diff 预览，整段替换（P3）
* 框选只做到「读」：F7 扫的是鼠标周围固定 10 格（`maiRegionAround(r)` 可换半径），没有拖拽矩形、也没有队列
* 自然语言已经能用，但只是**关键词路由**：`/plan` 的 `_route()` 是规则表，没接模型（P4）
* 多处理器批量写入与撤销落盘（P5）：区域快照只回答「能连什么」，不生成代码
* 真界面的**像素**没法机器验证（无头模拟器里 `Vars.ui` 是 null）：`run_ui_smoke.py` 只验到
  「main.js 整段初始化不抛错 + 设置表格能建到假 Table 上 + 页面用到的每个 Table 方法在真引擎里能解析」，
  按钮点下去的实际效果、这一栏在设置菜单里的位置与排版仍要人看
* 两个面板的模板按钮只用默认参数（改参数走控制台 `maiCompose(id, paramsJson)` 或 AI 面板的一句话）
* AI 面板只覆盖六个模板能表达的意图，多处理器 / 多步流程它做不到
