# ADR-9：多租户隔离采用"共享库 + 行级 user_id + RLS"

> 状态：**已实现**（M1 落地，B4 验收跨租户隔离）
> 实现落点：`infra/alembic/versions/0001_initial_schema.py`（RLS 策略）、`apps/api/core/db.py`（`tenant_session`）、`apps/api/repositories/*`、`infra/postgres/init/01-roles.sh`

## 背景

个人助理场景租户数有限，但**每个租户的数据都是私密的**（会话、知识库、待办、审批）。隔离方案的错误代价不是"性能差"，而是**数据泄露**。

## 决策

**共享库 + 每表强制 `user_id` + Repository 层显式过滤 + PostgreSQL RLS 双保险**；三个 DB 角色分离（ADR 细节见下）。

| 角色 | 权限 | 用途 |
|------|------|------|
| `app_api` | 受 RLS 约束 | API 进程（租户上下文内） |
| `app_worker` | **BYPASSRLS** | Celery worker / checkpoint（合法跨租户） |
| `app_owner` | 表 owner | 仅用于迁移 |

## 替代方案

| 方案 | 放弃理由 |
|------|---------|
| schema-per-tenant | 租户数增长时迁移脚本数量爆炸；连接池按 schema 切换复杂 |
| database-per-tenant | 运维成本随租户线性增长，个人项目不可接受 |
| 仅应用层过滤（无 RLS） | 一次漏写 `WHERE user_id` 就是全量泄露；RLS 是"忘了也不会泄露"的兜底 |

## 必须注意的实现细节（**"是否真做过"的必考点，全部已落地**）

1. **`SET LOCAL` 的作用域**：RLS 策略读 `current_setting('app.user_id', true)`，必须在**事务内**设置——asyncpg 是连接池复用，会话级 `SET` 会**泄漏到下一个请求**（隐蔽且严重）。实现用 `SELECT set_config('app.user_id', :uid, true)`（第三参 `true` 等价 `SET LOCAL`，且支持参数绑定——`SET LOCAL` 本身不支持 `$1`）。
2. **必须 `FORCE ROW LEVEL SECURITY`**：表 owner 默认**绕过** RLS，不加 FORCE 则策略形同虚设。
3. **后台任务用专用角色**：worker 使用独立的 `BYPASSRLS` 角色，连接串不与 API 共享。
4. **迁移用另一个 owner 角色**：避免 RLS 阻碍 DDL。
5. **跨租户越权用例纳入必过项**：`tests/security/test_tenant_isolation.py`（B 用户读 A 的数据 → 404/空结果）。

## 实测踩坑（本 ADR 最有价值的部分）

### 1. RLS 之外的**第二套权限体系**：checkpoint 表的 schema 权限（M5）

LangGraph 的 checkpointer 以 `app_worker` 连接，但该角色在 `schema public` 上**没有 CREATE 权限** → `saver.setup()` 抛 `InsufficientPrivilege` → **初始化失败被上层吞掉** → 图退化为"无持久化运行"，**中断恢复静默失效**（直到 M5 验收 F3 的 `kill -9` 演练才暴露）。

教训：**"降级被静默吞掉"比"降级"危险得多**。现已改为：建表脚本补授权 + 运行时 setup 失败不致命（表已由 migrate 创建），并改用**连接池**（单连接会被并发轮次踩坏，表现为 `connection is closed` + 答案为空）。

### 2. 跨租户设计的"不泄露存在性"

查不到他人资源时返回 **404 而非 403**——不区分"不存在"与"不属于你"，避免通过状态码枚举他人资源 ID。B4 验收即按此断言。

## 代价与局限

1. **每次请求多一次 `set_config` 往返**（事务内第一条语句），成本可忽略。
2. **RLS 与 BYPASSRLS 的边界要人工维护**：新增后台任务时必须确认用哪个角色的连接串，错用 `app_api` 会导致"扫描所有用户"的任务静默返回空集。
3. **`audit_logs` / 系统级表未纳入 RLS 全覆盖**：审计表以 `actor_type='system'` 写入 NULL user_id 的场景需要单独判断策略（当前实现允许）。
