# rules/ — 这个工具的「我学到过什么」

一个规则是一个 JSON 文件。名字终身不变，因为报告、测试和文档都引用它。

## 行号约定

`Finding.line` 是**语句序号**，不是文件行号：它和 `jump N` 的编号一致。标签行、空行、纯注释行都不算语句，所以带标签的程序里两者会差几行 —— 读者拿我们的行号去核对 `jump 13` 时，只有语句序号是对得上的。

## 为什么是文件

我没有权重记忆。**对话结束而仓库没变，就等于没学到**。所以每一条被用户纠正过的、
从源码里挖出来的、或从语料统计出来的判断，都必须落成一个能重跑的东西：

| 字段 | 作用 | 缺了会怎样 |
|---|---|---|
| `id` | 稳定标识（kebab-case） | 报告与测试无法互相引用 |
| `kind` | `semantics` / `heuristic` / `negative` | 分不清「游戏就是这样」和「通常如此」 |
| `claim` | 一句话结论 | 三个月后只记得「当时改过」 |
| `why` | 证据：源码行、数字、或谁说的 | 无法判断这条还成立吗 |
| `source` | `user` / `source` / `corpus` / `reasoning` | 不知道能信几分 |
| `ref` | 消息号或 URL | 无法回溯到原始上下文 |
| `since` | 日期 | 无法按时间排出漂移 |
| `counter_example` | 一份**不该**命中的 fixture | 谓词会悄悄变宽 |
| `match` | `sevs` + `msg_any` 子串 | R2 数不到它 |
| `line_regex` | 可选：命中行应长什么样 | precision 无法测量 |
| `auto_downgrade` | 允许 R2 自动降级 | 高噪声规则要人工发现 |
| `observed` | R2 写回：命中数/程序数/占比 | 没人知道它现在有多吵 |
| `surface` | 可选，目前只有 `blueprint`：这条规则只在 `analyse_blueprint()` 里可能命中（要读别的处理器的变量表），`.mlog` fixture 永远碰不到 | `tests/ruleids/run_ruleids.py` 会把它当死规则报错 |

**每条规则都必须被某个 fixture 真的点火一次**（`tests/ruleids/run_ruleids.py`，
也在 gate 里）。写规则时顺手写 `tests/cases/<rid>.mlog`：只写反例的规则在
`rule_capture.py lint` 里也是绿的——lint 只证明「反例不命中」，不证明「正例能命中」。
改 `match.msg_any` 就必须重跑 `run_ruleids.py --write`，diff 就是这次改动的复核。

## 三种 kind，一条硬规矩

- **`semantics`** —— 游戏就是这样做（源码或用户确认）。**永不允许自动降级**，因为它不是统计量，是判决。
- **`heuristic`** —— 「通常意味着写错了」。可以随语料精确率升降。
- **`negative`** —— 看着像错，其实是对的。存在的唯一目的是**挡住那条很想写的规则**。
  本仓库最贵的三条都是 negative：`reactor-single-threshold-is-fine`、
  `true-false-are-numbers`、`invalid-parameter-is-not-an-error` 的残余。

硬规矩：`semantics` 里写 `auto_downgrade: true` 会被 `rule_capture.py lint` 直接拦下。

## 单向棘轮

- **降级**（WARN → INFO）可以由机器提议：一条规则吃掉了报告的一半以上，它描述的
  多半是我的实现细节，不是用户的问题。
- **升级**（INFO → WARN/ERR）**永远需要人点头**。历史上有一次 `noop` 被我降成
  WARN，用户一句「低版本开高版本语句，noop 程序基本就死了」又升回 ERR ——
  这种判断不在数据里。

## 工具

```bash
python tools/rule_seed.py                 # 写入种子规则（不覆盖已有文件）
python tools/rule_seed.py --force         # 覆盖（种子是唯一真源时用）
python tools/rule_capture.py list         # 每条一行，带 observed
python tools/rule_capture.py lint -v      # schema + 反例必须不命中
python tools/rule_audit.py                # 全语料噪声审计 → out/rule_audit.md
python tools/rule_audit.py --dir out/maps --serial
python tools/rule_audit.py --apply        # 把实测数字写回 rules/*.json
```

`lint` 是这套东西里唯一真正值钱的部分：它把「这条规则有反例」变成一句可以机器验证的
话。一条没有反例的规则不算错，但会被标成 `unguarded` —— 让「没测过」和「测过是对的」
在报告里长得不一样。

## 目前的空白（诚实记录）

- `precision` 只对 4 条规则可测（它们的 finding 里带的是**源码行**）。程序级 finding
  （版本闸门、`@dead` 极性、常量 null 守卫）带的是 token 而不是行，所以 precision 显示 `—`。
  这不是 100%，是「不知道」。
- `rule_capture.py` 无法读聊天记录。它只能把**已经写成一句话**的纠正落成文件；
  「我意识到用户刚才否掉了什么」这一步仍然是手动的。
- R3（源码差分）、R4（与 mindcode 模拟器对拍）、R5（覆盖挖掘）还没实现，
  但零件都有：`tools/corpus/_sweep_saves.py`（分桶）、`<mindcode-dist>/mindcode.jar`（第二实现）。

## 审计之后改过的

用户逐条判完了 `out/audit_review.md` 的前 55 条，四类改动：

| 判定 | 改动 |
|---|---|
| `unary-op-extra-operand` 29 条全判「不值得报」 | `analyse` 不再上报，规则记成 `state: silent` |
| `version-gate` 「为什么是第零行，不应该指向 format 吗」 | finding 的 `line` 改成真正用到该特性的那一行 |
| `getblock` 「146 就已经有 getblock 了」 | `instr_semantics.json` 的 `min_build` 157 → 146（157.4 加的是 `query`） |
| 「状态效果改版，裸名变 null」 | 新 `status_names.json` + INFO `is the name of a status effect` |
| 「A 是无效参数，不影响」（`fetch ...Count`） | `uses_of` 不再把 count 类型的 `index` 当变量读 |
| 外部语料反馈（ref m02993）：0 链接且 >200 行降 INFO / 同名字索引操作数汇总 / 模组内容文案 | `link-name-not-bound` 拆出 INFO 版 `link-name-unbound-fragment`（Anon 语料 896 条/44 程序）；`undefined-var` 的纯索引变量按名字汇总（11,165 → 4,093）；两条 `sensor-not-a-property` 文案点明「原版 v160 快照，模组内容不认识」 |
| 未赋值变量两条规则合并（ref m07644）：{B}undefined-var-read{B}（ERR）与 {B}undefined-var-is-not-automatically-an-error{B}（INFO） | 合成单条 {B}undefined-var-read{B}，严重度改由**槽位类型**决定：num/bool 槽 → INFO（{B}numi(){B}/{B}bool(){B} 把 null 读成 0/false），其它槽 → ERR（{B}obj(){B} 把 null 对象交给指令，要么自己跳过要么把 null 写下去）。{B}bool-param-constantly-zero{B} 随之 WARN → INFO；{B}rule_ids.rule_id(){B} 平票改由规则 id 决定（原来取决于「该规则最长的那条 key」，会让宽规则抢走窄规则的命中） |

两条教训：

1. **精确率高只证明「报出来的不是错的」，不证明「报出来的值得看」**。`unary-op-extra-operand`
   精确率 100%，29 条全是「我知道，懒得删」——那叫噪声，不叫发现。
2. 用户判的不是「这行代码对不对」，是「这条规则该不该存在」。规则表里每次改动都要带 `ref`（哪一句
   话让我改的），否则半年后没人知道为什么。

## 作者复核（2026-10-19，ref m15486）

作者通读了 `out/audit_review.md` 的全部 86 行，逐条判 `ok`（"剩下的都是OK，我看过了没问题"）。
此前唯一被判 `bad` 的一类（`@dead` 极性）已经整条删除，见 `dead-polarity.json` 的 `state`。

| 规则（发现类别） | 行数 | 判 ok |
| --- | ---: | ---: |
| `version-gate` | 24 | 24 |
| `undefined-var-is-not-automatically-an-error` | 23 | 23 |
| `link-name-not-bound` | 13 | 13 |
| `status-name-needs-at` | 11 | 11 |
| `jump-to-jump-chain` | 10 | 10 |
| `constant-null-guard` | 4 | 4 |
| `printflush-clears-buffer` | 1 | 1 |

注：上表第 2 行的 `undefined-var-is-not-automatically-an-error` 已在 2026-10-29 合并进 `undefined-var-read`（ref m07644，见上表最后一行）；这里保留当时的审计记录，不改写历史。

含义：这 7 类在 20 张社区地图的样本上**没有假阳性**。它不等于"这些规则的证明是对的"——样本只覆盖这 20 张图，换语料仍要重跑 `review.py run`。