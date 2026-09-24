# ADR-3：前端 Next.js 15 + 原生 fetch 流式

> 状态：**已实现**（M0 起；M3 加引用脚注与降级角标；M5 改为**两步流**）
> 实现落点：`apps/web/app/{page.tsx,kb/page.tsx,kb/debug/page.tsx}`、`apps/web/lib/api.ts`
> 版本：Next.js 15.5.25 · React 19 · TypeScript 5.7（**未引入 shadcn/ui**，见"与设计文档的差异"）

## 背景

前端要渲染三类"非普通请求/响应"的东西：**逐 token 流式**、**工具调用过程**（折叠卡片）、**人工审批交互**（暂停中的任务）。通用聊天组件默认协议通常只覆盖第一类。

## 决策

Next.js 15（App Router）+ TypeScript；SSE 用**原生 `fetch` + `ReadableStream`** 自行解析自定义事件协议。

理由：流式与审批交互在 React 生态支持最好；自研解析让**事件协议由我们定义**（`token` / `citations` / `notice` / `degraded` / `approval` / `done` / `error`），前后端契约明确。

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| Vercel AI SDK `useChat`（默认 transport） | 它使用自家的 data stream protocol，无法承载**自定义事件**（`approval` / `citations` / `degraded`），硬塞会污染语义 |
| Streamlit | 交互能力弱，做不了审批卡片与细粒度流式 |
| Vue | 生态内 AI 组件较少 |

## 与设计文档的差异（按实际实现修正）

| 设计文档写的 | 实际做法 | 原因 |
|-------------|---------|------|
| shadcn/ui 组件库 | **未引入**：手写全局 CSS（`globals.css`） | 本项目前端页面少，引入组件库与其构建配置的收益低于维护成本；零构建配置让"3 分钟起栈"更容易 |
| 单条 SSE 长连接消费 | **M5 起改为两步流**：`POST /chat` 投递任务（返回 `message_id`）→ `GET /chat/{id}/stream` 订阅 | 单长连接绑定 HTTP 生命周期 → 关页面任务就没了。两步流让执行与传输解耦（详见 ADR-10） |

## 代价与局限

1. **协议解析要自己维护**：SSE 分帧（`\n\n` 分隔）、`data:` 单行 JSON、`id:` 行用于断线续读——都靠自己写，出问题时定位成本在自己身上（换来的是"知道每一层在做什么"）。
2. **UI 精致度靠手写**：没有组件库的设计系统，样式一致性靠自觉。
3. **未做 Markdown 渲染**：当前回答以纯文本 + `white-space: pre-wrap` 展示。设计文档 §13.11 要求"前端 Markdown 必须 sanitize（防检索内容注入 XSS）"——**引入 Markdown 渲染前必须同时引入 sanitize**，属未完成项。

## 影响

- 自定义事件协议是 M3 引用脚注、M5 审批卡片、M6 降级角标的共同基础：后端新增一种事件，前端加一个 case 即可。
- 两步流让"关闭页面后任务继续跑、重新打开看到完整答案"（M5 的 F4）成为可能，也让 F3（进程被杀后恢复）在 UI 侧无需特殊处理。
