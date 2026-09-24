# ADR-10：执行与传输解耦（投递任务 + Redis Stream 事件订阅）

> 状态：**已实现**（M5 落地；验收 F3/F4 实测）
> 实现落点：`apps/api/api/v1/chat.py`（POST 投递 + GET 订阅 + cancel/regenerate）、`apps/api/api/v1/chat_runtime.py`（后台任务 + 事件通道）、`apps/api/sse.py`
> 这是**实现期新增**的 ADR（不在设计文档 §3 的 9 条里）

## 背景

M0–M4 的 `POST /chat` 是一条**长连接 SSE**：请求进来到流结束之间，Agent 的执行完全绑定在这个 HTTP 连接上。于是有两个问题无法解决：

1. **用户关掉页面 → 任务消失**（浏览器断开会取消服务端任务），答案也丢了；
2. **进程重启 → 暂停中的审批永远等不到**（执行状态在进程内存里）。

而 M5 要交付的 HITL 审批天然是"暂停—等人工决策—继续"的形态，暂停可能持续几分钟到几小时，**长连接模型根本装不下**。

## 决策

把"执行"与"传输"分开：

```
POST /api/v1/chat                 投递任务 → 立刻返回 {message_id, stream_url}
   └─ asyncio.create_task(...)    后台推进 Agent（不绑定 HTTP 生命周期）
        └─ 事件写 Redis Stream    chat:ev:{message_id}（TTL 1h）
GET  /api/v1/chat/{id}/stream     订阅事件（XRANGE 补历史 + XREAD 阻塞实时）
   每条 SSE 带 id:              断线/续跑用 Last-Event-ID 续读
```

配套：assistant 消息在**流开始前**即落库（`status='streaming'`），执行结束更新为 `completed` / `interrupted` / `cancelled` / `failed`。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| 保持长连接 + 前端断线重连 | 服务端任务仍随连接取消；"关页面任务继续跑"做不到 |
| 执行结果只落库，不推送（纯轮询） | 失去逐字流式体验；轮询延迟与负载都不可接受 |
| 引入外部消息队列（Kafka/NATS） | 为"单机个人项目"引入运维负担；Redis Stream 已具备消费组与阻塞读 |
| 事件存表（Postgres）而非 Stream | 需要自己实现"轮询增量 + 阻塞等待"，Stream 一条命令就有 XREAD BLOCK |

## 代价与局限（实测踩坑）

1. **SSE 必须带 `id:` 行**，否则续读无从下手。M5 验收一度假失败：审批 resume 后订阅从 0 读，立刻撞上上一轮的 `done{awaiting_approval}`，看起来像"续跑没执行"——实际是**事件流是追加的，必须用 `Last-Event-ID` 续读**。
2. **token 增量不承诺重放**：断线重连后以**落库内容**为准（重放大量 token 碎片既不经济也无意义）。这意味着前端重开页面必须加载会话历史（已实现）。
3. **Stream TTL 1h**：超时后订阅端显式报 `STREAM_GONE`（而不是挂死），提示刷新页面取落库内容。
4. **启动宽限期**：后台任务在首个事件前要完成 MCP 工具发现（spawn 多个子进程，数秒），订阅端若不设宽限期会误判"流不存在"并要求重启。当前宽限 60s。
5. **锁与消息的时序**：会话锁在 POST 时获取、后台任务结束时释放；锁的持有者与"谁在跑任务"必须一致，否则并发保护失效。

## 影响

- **F3（kill -9 后恢复）成为可能**：中断状态完全在 Postgres（checkpoint + `approvals` 表），新进程批准审批后从 checkpoint 续跑——实测在容器被 `docker kill` 后成功完成发信。
- **F4（关页面不丢答案）成为可能**：轮询消息状态即可看到 `completed`，与浏览器是否在线无关。
- 取消（cancel）变成"标记 + 后台任务在下一个事件处退出"，粒度到 token 级；重新生成变成"新消息 + 旧消息 `superseded_by`"。
