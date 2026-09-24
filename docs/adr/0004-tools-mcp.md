# ADR-4：工具体系采用 MCP + 本地治理层

> 状态：**已实现**（M4 落地治理层与三个 server；M5 增 email server 支撑 HITL 演示）
> 实现落点：`agent/tools/{base,policy,registry,mcp_client}.py`、`mcp_servers/{sandbox,todo,search,email}_server.py`
> 依赖版本：`mcp 1.30.0`（SDK v1；SDK 2.x 把 FastMCP 改名为 MCPServer，本项目锁定 <2）

## 背景

工具要能被复用、能被治理。开放协议（MCP）解决了"复用"，但**开放协议同时意味着你无法信任工具的自述**：第三方 server 不会遵守我们的 `risk_level` 约定，工具描述还会被塞进 function calling schema（本身就是注入载体）。

## 决策

工具统一为 **MCP 协议（stdio）**，并在**客户端侧强制治理层**：

1. **风险等级来自本地策略注册表，不信任工具自述** —— 未注册工具默认拒绝（fail-closed）。
2. **工具描述净化**：`ToolMeta.description` 限长 ≤500 字符（schema 注入面收敛）。
3. **Server 白名单 + 固定版本**：禁用自动发现，工具列表显式声明。
4. **权限最小化**：`sandbox` 只拿 CPU/内存限额与禁用 fork 的子进程；`todo` 只拿 worker DB。
5. **生命周期治理**：调用超时（`ToolMeta.timeout_s`）+ 崩溃重试一次 + **重启后校验工具列表是否变化**（变化即告警，供应链篡改信号）。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| 全部用进程内 Python 函数（不上 MCP） | 失去"一次封装多端复用"与行业标准接口；MCP 也是本项目的差异化展示点 |
| 全部走 MCP（包括薄封装 CRUD） | todo CRUD 这类薄封装走 MCP 只增加进程开销；ADR 允许兜底但**必须同样过治理层** |
| 信任工具自述风险等级 | 开放协议下不可行（fail-open 是安全事故的常见根因） |

## 与设计文档的差异（按实际实现修正）

| 设计文档写的 | 实际做法 | 原因 |
|-------------|---------|------|
| 工具集：`sandbox` / `todo` / `search` | **四个**：多了 `email`（模拟发件） | HITL 需要 L2 真实动作载体（外发邮件），`email` 的参数级升级策略正好演示"收件人含外部域 → 升级 L2 → 审批"（§7.5 的原始例子） |
| 常驻 stdio 会话 + 健康检查 | **按调用开短会话**（spawn → call → close） | 实测：常驻会话的 anyio cancel scope 与建立它的任务绑定，跨请求使用会触发 `cancel scope in a different task`（表现为流随机 500）。短会话代价是每次 100–300ms 进程拉起，当前调用频次下可接受 |
| 崩溃自动重启 | 短会话模型下**每次调用都是新进程**，无需"重启" | 同上；`restart()` 保留为接口兼容 |

## 代价与局限

1. **每次工具调用有进程冷启动开销**（约 100–300ms）。M7 若成瓶颈，可引入 supervisor-task 常驻会话模式（已记录为优化项）。
2. **`sandbox` 的隔离边界要说清楚**：RestrictedPython ≠ OS 隔离。进程级 rlimit 能防死循环与内存炸弹，**不能防容器逃逸类攻击**——本项目不处理该威胁模型（无多租户代码执行需求）。主动披露比含糊带过更可信。
3. `list_tools` 在每次调用时也会跑一遍（用于列表变化校验），是重复开销。

## 影响

- **新增工具零成本继承横切能力**：策略校验 / 参数校验 / 参数级风险升级 / 幂等去重 / 并发限流 / 超时 / 结果截断（含 `full_ref` 回取）/ 埋点 / 审计，全部收敛在 `ToolRegistry.invoke`。
- 治理层是 M5 审批闭环的地基：L2 → `approvals` 落库 → LangGraph `interrupt()` → 人工决策 → 从 checkpoint 续跑。
