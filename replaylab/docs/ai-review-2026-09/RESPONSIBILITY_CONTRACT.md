# ReplayLab AgentHost — 职责契约 (Responsibility Contract)

> 已废止的旧架构职责说明。当前职责契约由 `../../ARCHITECTURE.md` 定义。

> 目标：明确 **Host / 主 Agent (Supervisor) / 子 Agent (Specialist/Child) / Skill** 四层的职责边界，
> 特别是"**谁决定路线 (route / mode / 方法)**"。以此为准绳完成剩余重构。
> 参考：Anthropic《Building Effective Agents》、Claude Code Subagents、OpenAI Agents SDK (handoffs/guardrails)、LangGraph supervisor。

---

## 0. 核心原则 (The one rule)

**路线 (route/method/approach) 属于 LLM，不属于确定性的 Host。**

在所有主流优秀设计里，决策按"**类型**"分工，而不是按"层级集中"：

- LLM 负责一切 **语义 / 规划** 决策：任务如何拆分、走哪个子 Agent、采用什么方法/路线。
- Host（确定性 harness）只负责 **机械 / 安全** 决策：状态流转、工具可用性、输入/输出校验、预算与停机。

> Anthropic：Agent 是"LLM 动态地掌控自己如何完成任务 (maintaining control over **how** they accomplish tasks)"的系统；
> 框架不应"增加额外抽象层、遮蔽底层的 prompt/response"。
> OpenAI Agents SDK：guardrails 只能 **校验 / 阻断 (block/halt)**，不能改道；改道 (handoff) 是以"工具"的形式交给 **LLM** 决定的。
> LangGraph：supervisor（一个 LLM）决定路由，graph runtime 只机械地搬运它发出的 `Command`/state。

**因此，Host 对"路线"只能有"否决权 (veto-only)"，不能有"生成权 (generative)"：**

- ✅ 可以：因为**权限/安全**而 **拒绝 (block)** 或 **降级 (downgrade)** 一条越权的路线。
- ❌ 不可以：**推断、默认、发明、改写、升级** 任何一条路线。

---

## 1. 职责矩阵 (Responsibility matrix)

| 职责 | 归属 | 说明 |
|---|---|---|
| 任务的 **路线 / mode / 方法**（如 `quick-analysis` / `readable-reconstruction` / `compile-only` / `compile-replace` …） | **主 Agent 编写，子 Agent 细化** | Host 绝不编写。 |
| **计划编排**（拆成哪些 task、依赖关系、走哪个 Skill 域） | **主 Agent** | Host 不新增/改写 task 来"补齐路线"。 |
| 子任务内部**怎么做**（选哪个工具、先后顺序、何时停） | **子 Agent** | 子 Agent 在自己的 context 内决定，可根据证据细化路线。 |
| (a) **安全 / 权限门禁**（effect 上限、危险操作） | **Host（只否决）** | 只能 block/downgrade，永不 upgrade/choose。 |
| (b) **域 / 工具可用性**（哪个 Skill 存在、暴露哪些 tool） | **Host（声明式）** | 提供可用集合；不替 Agent 选具体工具。 |
| (c) **结果 / 证据校验**（output 是否落地、是否有证据） | **Host（output guardrail）** | 校验/拒收，不改写内容。 |
| (d) **预算 / 循环控制**（重试次数、阶段上限、停机） | **Host（runtime）** | 机械控制，不涉及路线。 |
| **语言统一**（把子结果统一到用户显示语言） | **仅在主 Agent 面向用户输出的最终一步** | 中途子 Agent 用自己自然语言书写；Host 不在中途因语言而丢弃/失败。 |

Host 拥有"**边和门 (edges & gates)**"（控制流、状态搬运、否决检查）；LLM 拥有"**选哪条路 (choice of path)**"。

---

## 2. 当前代码中"Host 生成路线"的违规点 (Audit)

以下位置目前由 Host 主动 **推断/发明/默认** 了路线，违反核心原则，需在剩余重构中移除或改为 veto-only：

| # | 位置 | 现状（违规） | 应改为 |
|---|---|---|---|
| 1 | `adaptive_orchestration.py` · `canonical_shader_task` (~7232, 7240–7274) | 模型未写 route 时，`infer_shader_route_request(user_request)` 推断一个默认路线，并据此推导 effects | Host 不推断；route 取模型所写，缺失则留空 → 交给子 Agent（见 §3 待定项） |
| 2 | `adaptive_orchestration.py` · `_restored_shader_goal_proposal` (~7555) | "继续"恢复时 `infer_shader_route_request(goal.summary)` 重新推断路线 | 恢复只还原 **目标 + 权限上限 + 目标身份**；路线由模型在续跑计划里重新编写 |
| 3 | `adaptive_orchestration.py` 指纹 (~983) | 用 `infer_...` 补一个路线来算去重指纹 | 指纹只基于模型实际所写 route（None 安全） |
| 4 | `shader_routing.py` · `select_shader_route` (~3019) | `request is None` 时 `infer_shader_route_request(task_text)` | 调用方传入模型 route；None 表示"未指定"，不发明 |
| 5 | `shader_routing.py` · offer 补救 (~3311) | 同上，None 时推断 | 同上 |
| 6 | `planning.py` · `safe_fallback_plan` (~823–844) | 从 typed intent 构造一个 fallback event route | 最后兜底路径；评估是否保留为"纯安全兜底"或交回模型重规划 |

**注意**：以下**不算违规**、应保留：

- Host **域绑定**（shader 请求必须走 shader Skill、纠正模型选错的 Skill）——这是 (b) 工具可用性。
- Host **effect 上限**的 block/downgrade——这是 (a) 安全门禁（只否决）。
- `offer_shader_routes`：只要它是**无排序、建议性 (advisory/unranked)** 地把可选路线**呈现**给子 Agent、由子 Agent 选，就符合原则（Host 呈现，LLM 选择）。

---

## 3. 待你拍板的一个关键问题 (Open decision — governs the rewrite)

移除 Host 推断后，**当主 Agent 没有为一个 shader task 写 route（或 Host 纠正了它选错的 Skill、原 task 本就没有 route）时，该怎么办？** 三选一（见对话里的选择卡）：

- **A. 留空 route，交给子 Agent 从 offer 里选**（最贴合"主 Agent 编写、子 Agent 细化"；不加往返、不制造新失败面）。
- **B. Host 拒收欠指定的计划，要求主 Agent 重新编写带 route 的计划**（最严格；但多一次往返、可能循环）。
- **C. Host 用最小权限默认（analysis-only）兜底**——注意这**仍是 Host 在设路线**，与你的要求相悖，一般不选。

`infer_shader_route_request` 本质是"关键词启发式"，是 Host 当初替弱模型兜底的拐杖。正确做法是：**强化 Supervisor 指令**让模型可靠地自己写 route，Host 退回到只做域绑定 + 权限否决 + 证据校验 + 预算控制。

> **已定 (2026-09)：选 A** —— route=None 时交给子 Agent 从无排序 offer 里选；Host 完全不碰路线。

---

## 4. 实施进度与剩余计划 (Rollout)

**关键澄清（来自代码走查）**：effect 上限 (`authorized_effects`) 由 Host 从 **用户请求/intent** 推出（`_requested_workflow_effects` / `_resume_effect_ceiling`，已在 runtime 计算并传给 supervisor 指令）——这是**权限**，属 Host 职责，保留。Host 要停的只是推 **route/mode**。因此"无 route 的 task"应当：`route=None` + `effects = 该 effect 上限`，让子 Agent 在授权范围内自选路线。

### 已完成 (committed, 已随本次 pytest 校验/待你复跑)

- **违规 #2 — resume**：`_restored_shader_goal_proposal` 不再 `infer`；直接还原模型当初写下的 `goal.shader_route`（恢复=还原模型决定，不是 Host 重新决定；防重试循环交给预算/loop 控制）。已用夹具验证：`继续` 精确还原 `compile-only + [organized-source, replay-abi-validation]`。
- **Supervisor 指令强化**：明确"每个 shader task 你**必须**自己写 `shader_route`；Host 永不 infer/default/override；`shader_route` 只表达**结果 (mode)**，不选执行路线/工具顺序"。

### 剩余（重活，需你本地 pytest 逐步把关；子 Agent 选路循环这里跑不了）

1. **违规 #1/#3/#4/#5 — 移除 admission/offer 层的 route 推断**：
   - `_canonicalize_initial_shader_plan` 增参 `authorized_shader_effects`；`canonical` 由"推断路线"改为 `None`；无 route 的 shader task → `route=None, effects=该上限`；`model_dump` 处对 `None` 做保护。
   - 给 `admit_initial_decision` 增参 `authorized_effects` 并在 runtime 调用点传入（runtime 已算好），另一个调用点用 `state["authorized_effects"]`。
   - `select_shader_route` / offer 补救：`request is None` 时**不 infer**，改为按 effect 上限构建**无排序**候选 envelope，交子 Agent 选。
   - 指纹 (#3) 改为对 `task.shader_route` 直接取值（None 安全），不 infer。
2. **测试改写**：约 15 处 admission 层断言从"Host 推断出的具体 mode"改为 `shader_route is None`（+ `effects == 授权上限`）；`infer_shader_route_request` 的**单元**测试保留（它成为子 Agent/模型可用的工具，不再由 Host 在准入期调用）。
3. **落地方式**：每落一片 → 你跑 `.venv\Scripts\python -m pytest -q` → 把红的贴回 → 迭代到绿。VM (`device_bash`) 一旦恢复我就能自己跑，不用你手动复跑。
