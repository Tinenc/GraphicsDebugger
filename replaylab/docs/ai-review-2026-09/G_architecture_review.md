# G. 架构对标审查：主 Agent 委派 + 子 Agent 自我修复循环（2026-09-02）

> 历史审查。Custom Agent 与自研编排结论已失效；当前 API/订阅双路径见 `../../ARCHITECTURE.md`。

你关心的核心链路是:**主 Agent 分析需求 → 判断是否委派子 Agent(是否有合适 custom agent) → 子 Agent 自选路线 → 处理 → 检错 → 循环 → 自我修复**。本文把你的实现逐环对照当前公认的优秀 AI Agent 架构,给出结论、亮点、以及少量真正值得做的优化。

先说结论:**这条链路已经是"编排者-工作者 + 评估者-优化者"的成熟形态,映射到最佳实践几乎是一一对应的,而且在若干处比参考实现更严格(哈希绑定取证、确定性授权护栏)。** 现在主要的风险不是"缺能力",而是"复杂度/漂移"——这正是本轮 P1–P5 在收敛的。下面的优化都以"不增加无谓复杂度"为前提。

---

## 一、你的实现 ↔ 公认最佳实践(逐环对照)

| 你的环节 | 代码位置 | 对应的成熟模式 | 评价 |
|---|---|---|---|
| 主 Agent 分析需求 | `intent_contract.infer_task_intent_contract` + `planning` | 语义意图解析 → 计划 | 类型化意图契约,严谨 |
| 判断委派 / 选 custom agent | `runtime._custom_agent_relevance` 做**相关性排名**,`_registry_routing_prompt_context` 把候选投影给 supervisor,supervisor 在 plan 里绑 `custom_agent_id` | Anthropic **Routing** + OpenAI **Handoff** | Host 只排名+投影,LLM 决策——符合"Host 不决策路由"。✓ |
| 没有合适 custom agent | `runtime` 校验绑定不合法/缺失 → 回落 `GENERIC_CHILD_AGENT_ID`(runtime.py:3119) | 安全兜底 | 有明确的通用子 Agent 兜底。✓ |
| 子 Agent 自选路线 | 非绑定 offer(shader/event)+ child selection,offer 规范序不排名 | Anthropic **Orchestrator-workers**(worker 自主) | 子 Agent 在授权天花板内自选,Host 不代选。✓ |
| 处理 → 检错 | Host 校验(evidence/receipt/scope/effect)= 评估者 | Anthropic **Evaluator-optimizer** 的评估者 | 哈希绑定的确定性校验,比多数实现严格 |
| 纠错循环 | `_run_external_plan` 子循环:`_child_failure_disposition` 分类 → evidence_correction 保留进度 + 观测+指令回喂 → 子 Agent 自我修正 | **Evaluator-optimizer** + **Reflexion / self-refine** | 保留已完成工具/效果、回喂可执行观测,是标准的自我修复底座。✓ |
| 换技能 / 换路由 | `select_another_execution_skill`(换 offered skill)、`_mcp_route_recovery`(推进 MCP 路由队列) | 备选路线回退 | 覆盖"同技能修不好→换技能/换路由"。✓ |
| 防死循环 | `max_total_attempts=min(8,…)`、`local_attempt_limit`、`selection_round_limit`、`forbidden_operation_fingerprints`(不重复同一失败操作)、terminal vs correctable | 生产级 agent loop 的预算/终止控制 | 有界、防重复、区分终止/可纠正。✓ |
| 跨运行自我改进 | `learning_candidates` / `learning_promotion` / `learning_scheduler` | 经验沉淀/晋升 | 比多数参考实现多一层跨会话学习。✓(亮点) |

**一句话:参考架构里该有的环节你都有,而且 Host 侧的授权/取证护栏比它们更硬。**

---

## 二、真正的亮点(值得保留、别为了"统一"而削弱)

1. **确定性护栏与 LLM 决策的干净分层**:Host 只做授权/安全/取证/预算(确定性),路线/技能/委派都由 LLM 决策。这正是 Anthropic 反复强调的"把不确定的交给模型、把关的交给确定性代码"。
2. **进度可保留的纠错(evidence_correction)**:失败重试保留已完成的只读工具与已获效果,不从零重来——这是很多 self-repair 实现缺的。
3. **终止性 vs 可纠正的二分**:authority/safety/ledger 类直接 release,不浪费预算反复撞墙;其余才进纠错循环。
4. **跨会话学习层**:大多数开源 agent 框架没有。

---

## 三、值得考虑的优化(按价值排序,均为可选;已标注"是否真的划算")

### O1 — 纠错回合加一个轻量"反思"步(Reflexion 的核心)【中价值,推荐小做】

现状:纠错回合把 Host 观测 + 指令 + allowedActions 回喂给子 Agent,子 Agent 直接再试(靠它自己的内部推理)。Reflexion 论文的关键发现是:让模型**先显式写一句"上次为什么没过、这次改什么"再动手**,恢复率显著高于只给错误信息。

建议:在纠错回合的 child 契约里加一个**极小**的必填字段(例如 `revision_reason`:一句话说明本次相对上次改了什么假设/操作),Host 不校验其内容、只要求存在。这把"隐式反思"变"显式反思",几乎零成本,却能提升难例的自愈率。
**是否划算**:划算,但**必须极简**——只加一句话字段,不要加校验/评分,否则就成了你最反感的"过度验证"。

### O2 — custom agent 相关性排名的健壮性【低价值,按规模决定】

现状:`_custom_agent_relevance` 是**关键词词项匹配**(term ∈ id/title/description/...)。它只做排名、LLM 最终选,所以排错也不致命。但两点小隐患:(a) 中英混合/同义词命中差;(b) 如果 agent 数量多、投影有截断,真正相关的 agent 可能被排到截断线以下、supervisor 看不到。

建议:确保投影给 supervisor 的 custom agent 列表**在候选不多时不截断**(全量呈现),或给"曾被 skill 匹配命中"的 agent 一个不被截断的保底位;再顺手做一次中英词项归一化。
**是否划算**:agent 数量少时不用管;等你 custom agent 多起来再做。

### O3 — 平凡请求的快路径【设计取舍,先讨论再定】

现状:即便"解释这个 shader"这类只读请求,也走完整的 plan → child → offer → 校验 链路。好处是取证/授权模型统一;代价是简单请求也扛全套编排开销。Anthropic 明确建议"一次调用能解决的别上 agentic 复杂度"。

选项:给**纯只读、零效果、单步**的请求一条轻量快路径(仍然只读、仍然要求严谨不编造,但跳过多阶段编排)。
**是否划算**:不一定。它会引入"两条路径"的分叉(又是一种潜在漂移源),与你这轮"减少重复"的方向相反。建议**先不做**,除非你观测到简单请求延迟明显是痛点。列在这里是为了完整。

### O4 — 循环状态对用户的透明度【低价值,可选】

现状:纠错循环有 `_emit_progress`(phase=child-review,status=retrying/failed,带 public_detail),用户能看到"在重试"。可考虑把"这次改了什么(O1 的 revision_reason)"也投影给用户,让自愈过程更可读。Anthropic 把 transparency 列为核心原则。
**是否划算**:做了 O1 就顺带能做,几乎免费。

---

## 四、我的整体判断

- **能力维度**:你要的"分析需求→判断委派→子 Agent 自选路线→处理→检错→循环→自我修复"**已经完整实现**,且对标成熟架构不缺环节。
- **质量维度**:Host/LLM 分层干净、护栏硬、进度可保留、有跨会话学习——多处强于参考实现。
- **真正的持续风险是复杂度与跨层漂移**(不是缺功能),这也是 P1–P5 的主战场。后续新增东西时,优先问"这会不会又制造一处需要两层同步的重复",而不是"还能加什么能力"。

如果只做一件事,我建议 **O1(纠错回合加一句话显式反思)**——价值/成本比最高,且完全贴合你"自我修复"的诉求。要的话我来实现(极简版,不加校验)。

---

## 参考

- Anthropic, *Building Effective Agents*(orchestrator-workers / routing / evaluator-optimizer;"keep it simple") — https://www.anthropic.com/engineering/building-effective-agents
- Reflexion: 语言化自我反思提升 agent 自愈(Shinn et al., 2023)——O1 的依据
- LangGraph 多智能体 supervisor / handoff 权责 — https://dev.to/focused_dot_io/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture-1b7e
- Agent handoffs 的上下文/控制权移交 — https://towardsdatascience.com/how-agent-handoffs-work-in-multi-agent-systems/
