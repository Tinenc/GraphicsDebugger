# F. 架构审计后续（2026-09-02）

> 历史审计记录。下述旧运行时已整体删除，勾选状态和待办不再适用。

本轮聚焦两件事：继续推进"Host 不参与任何路径设定"这条主线，并对标成熟 AI Agent 设计，找出剩余的潜在问题与优化点。下面按**严重度/价值排序**，每条都给出代码位置、根因、建议，以及是否已落地。

---

## 主线进展（已落地）

- **Shader 路由透传**：模型未写路由时 `shader_route=None`，由子 Agent 从"非排序 offer"里自选；Host 不再 infer/default/override（`adaptive_orchestration.py` 的 `_canonicalize_initial_shader_plan`、`safe_fallback_plan` 的 `fallback_shader_route=None`）。
- **Skill offer 规范序**：`SkillOffer` 校验强制"candidates 用规范 ID 序，不是偏好排名"（`skill_selection.py`），Host 只提供候选，child 选择。
- **Event 路由为非绑定 offer**：`select_event_route` 明确"build a non-binding route catalog and full authorized capability offer"，`route_id="auto"` 交给 child。
- **本轮清理（已提交）**：删除死导入 `infer_shader_route_request`（`adaptive_orchestration.py`、`planning.py`）——生产已无任何调用点。

---

## 落地状态（2026-09-02 第二轮）

| 项 | 状态 | 说明 |
|---|---|---|
| **P1** 跨层契约常量收敛 | ✅ 已落地 | 新增 `child_policy_contract.py`，`policy_failure_code` / `policy_allowed_actions` / 观测 code 的读写两侧统一 import；字符串值不变，测试不受影响。 |
| **P2** 委派改 `route=None` | ✅ 已落地 | `adaptive_orchestration.py` 委派分支不再铸造 `mode=quick-analysis`，改 `route=None + effects=[]`，靠零效果天花板保证 analysis-only。 |
| **P4** 删死的兼容分类器 | ✅ 已落地 | 删除 `infer_shader_route_request` / `shader_route_request_from_intent`（430 行）+ `__all__` 导出 + 死导入 + `test_shader_routing.py` 里 15 个只测死代码的用例。 |
| **P5** 事件 outcomes 推断短路 | ✅ 已落地 | supervisor 已声明 outcomes/obligations 时，`select_event_route` 给文本分类器喂空串，关闭所有 prose 猜测，仅保留结构性底线（baseline / 类型化 intent / 前置依赖）。 |
| **P3** 两套路由子系统统一 | ✅ 已落地 | 见下。 |

**P3 落地方式**：新增 `route_contract.py`，把"route request = 授权/outcome-contract + 可空 route 选择"这**一个骨架**用共享的 `RouteRequestBase` 基类 + `RouteRequest` 协议表达。`ShaderRouteRequest` 与 `EventRouteRequest` 都继承 `RouteRequestBase`、都满足 `RouteRequest` 协议,共享 `selection_basis` 字段规格与**唯一一份** outcome-contract 投影 `outcome_contract()`(Host 从它推导 effect ceiling)。两个域各自的严格字段与校验器**保持不变**——刻意不合并成单个 model,因为那会溶解掉两域各自的强校验(正是防漂移的关键)。行为零变化(容器内已用真实 pydantic 构造两模型并逐 mode 校验 `shader_goal_required_effects` 与 `select_event_route` 输出一致、序列化字节一致)。这样"共享形状"只有一处定义,两子系统不会再各写一份而漂移(即 P1 那类问题的根治)。

## 发现的问题（按优先级）

### P1 — 跨层契约用字符串字面量各写一份，发生漂移【系统性，最高价值】

本会话修掉的 **3 个测试失败，根因全是这一类**：

| 契约 | 写入方 | 读取方 | 后果 |
|---|---|---|---|
| 终止性策略 code | `project_failure_state` 写 `child-authority-or-safety-rejected` | `_child_failure_diagnostic` 读 `child-route-policy-rejected` | 诊断恒为 None |
| 恢复重试块字段 | 源码曾写 `suggestedCandidateRoutePairs` | 契约/测试要 `candidateRoutePairs` | 字段读不到 |
| 纠错上下文载体 | 源码改走 `evidence_correction` 参数 | 测试仍断言在 prompt 里 | 断言失配 |

**根因**：同一个协议被相邻两层各自用字符串常量或 dict key 表达，重构改一处、漏改另一处就漂移。这也是你反复遇到 "task 总失败" 的一大根子。

**建议**：把这些**跨层协议常量收敛到单一 source of truth**——一个 `contracts.py`（或按域拆分的 Enum/常量模块），生产者与消费者都 `import` 同一处。至少覆盖：`policy_failure_code` 家族、`policy_allowed_actions`、恢复动作的 `stopConditions`、route mode 名。对标 Anthropic《Building Effective Agents》强调的 "well-documented / consistent tool interfaces (ACI)" 与 "keep it simple"：契约只描述一次。

**收益**：一次性消灭一整类回归——以后重命名一个 code，读写两侧要么一起编译通过，要么一起报错，不会静默漂移。

---

### P2 — 委派安全路径仍由 Host 授权路由（主线最后一处）【建议落地，需你确认】

`adaptive_orchestration.py:7511-7520`，Event→Shader 委派时：

```python
if task.delegation is not None:
    route = ShaderRouteRequest(mode="quick-analysis", ...)  # Host 在铸造路由
    effects = []
```

`effects=[]`（零效果天花板）**已经**把这个任务锁死成 analysis-only；再写 `mode="quick-analysis"` 既冗余，又是全模块里**唯一一处 Host 仍在 author 一条 shader 路由**，与主线口径直接冲突。

**建议**：改成 `route = None` + `effects = []`。零效果天花板保证 analysis-only（child 选任何路由都产生不了效果），Host 不碰路由，和"模型没写路由就 route=None"的通用模型完全一致。

**前提/风险**：需确认委派任务在下游对 `route=None` 无隐式依赖（Decision A 已在通用路径允许 route=None），并跑 `test_adaptive_orchestration.py` 里委派相关用例。属行为改动，我没有擅自改——你点头我就做并验证。

---

### P3 — Shader 与 Event 两套路由子系统形状不对称【技术债，中期】

- **Shader**：`route`（选择）与 `effects`（授权）干净分离，`route=None` 表"未指定"。
- **Event**：把 `required_outcomes` / `artifact_obligations`（交付契约）塞进 `EventRouteRequest`，靠 `route_id="auto"` 表"未指定"。

两者都能工作，但"授权 vs 选择"的表达方式不一致，增加认知负担，也是 P1 那类漂移的温床（两套模型 = 两套字符串）。对标 LangGraph / OpenAI Agents SDK 的 handoff：交接契约应当**统一形状**。建议中期把二者对齐到同一个 "outcome-contract(授权) + route(选择, 可空)" 骨架。

---

### P4 — 死的兼容分类器仍在（生产不调用，仅测试在测）【可选清理，需你拍板】

`infer_shader_route_request` / `shader_route_request_from_intent`（`shader_routing.py:2568/2610`）：生产**零调用点**，只有 `test_shader_routing.py` 里约 10 个用例在测它。等于在为死代码做覆盖，且它是旧的"从文本推断路由"逻辑——正是主线要清掉的东西。

**建议**（二选一）：
1. 整体删除函数 + 对应测试 + `__all__` 导出（推荐，彻底）；或
2. 保留但从公共 `__all__` 移除并标注 `# legacy, not wired`，防止被重新接线。

我倾向 (1)，但它会删掉一批测试，属你的决定。

---

### P5 — 事件侧仍启用"从文本推断 outcomes"的兼容分类器【口径一致性】

`select_event_route` → `_compatibility_outcomes(task_text, ...)`（`event_routing.py:704/886`）在生产仍启用（不像 shader 的 infer 已死）。它只推断 "required outcomes" 且 offer 非绑定，属较轻的推断，但与"Host 不从文本推断"的口径不完全一致。

**建议**：确认它只在 pre-v1 计划兜底时触发；当 supervisor 正常产出了 `required_outcomes`/`artifact_obligations` 时应短路掉这条推断，避免 Host 的文本猜测与模型的显式声明叠加。

---

## 对标成熟设计：正向确认与机会

- 你的架构本质是 **orchestrator-workers**（supervisor 编排 + child 执行）**加一个确定性的 Host 控制面**（授权 / 安全 / 取证 / 预算）。这正是 Anthropic《Building Effective Agents》里 orchestrator-workers + routing 的推荐分工，也是 LangGraph supervisor 模式的核心。方向是对的。
- **"Host 对路由 veto-only"** 对应最佳实践里 "deterministic guardrail 只把关、不决策"——保留下来。
- **`evidence_correction` 走参数而非 prompt** = evaluator-optimizer 的 out-of-band 状态回传，是个好模式，继续保持。
- 最主要的可优化面（对标 Anthropic 的 simplicity / transparency 原则）就是 **P1/P3：同一契约不要有多份表达**。你反复遇到的 task 失败，绝大多数是这类漂移，而不是逻辑本身错。

---

## 建议执行顺序

1. **P1**（收敛跨层协议常量）——投入产出比最高，一次性堵住一整类回归。
2. **P2**（委派 `route=None`）——补齐主线最后一块。
3. **P4 / P5**（兼容分类器）——你决定删或留。
4. **P3**（两套路由子系统统一）——中期重构，单独排期。

---

## 参考

- Anthropic, *Building Effective Agents*（orchestrator-workers / routing / evaluator-optimizer；simplicity 与一致的工具接口）— https://www.anthropic.com/engineering/building-effective-agents
- LangGraph 多智能体：Supervisor vs Swarm 的分工与 handoff 权责 — https://dev.to/focused_dot_io/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture-1b7e
- Agent handoffs 的上下文/控制权移交 — https://towardsdatascience.com/how-agent-handoffs-work-in-multi-agent-systems/
