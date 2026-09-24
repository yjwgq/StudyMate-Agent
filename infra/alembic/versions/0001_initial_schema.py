"""M1-3：初始 schema —— 16 张业务表（设计文档 v1.1 §6）+ RLS 策略（§6.10）

Revision ID: 0001_initial_schema
Revises: 无（初始迁移）
Create Date: 2026-09-22

说明
----
1. DDL 与设计文档 v1.1 §6.3–§6.8 逐条对应，采用原生 SQL 而非 op.create_table：
   本迁移包含 vector(1024) 列、hnsw 索引、tsvector 生成列与部分索引，
   这些用 op.* 表达只会徒增转换层；原生 SQL 让「文档 ↔ 迁移」可逐行对照。
2. 扩展（vector / pg_trgm / pgcrypto）由 infra/postgres/init/01-roles.sh
   以超级用户身份创建（vector 非 trusted 扩展，普通角色无权创建），本迁移不处理。
3. 3 张 LangGraph checkpoint 表（checkpoints / checkpoint_writes / checkpoint_blobs）
   由 scripts/setup_checkpoints.py 调用库自带迁移生成（§6.9），不在本迁移内。
4. RLS 覆盖 13 张含 user_id 的业务表：ENABLE + FORCE + 单策略
   （USING + WITH CHECK 同时给出，覆盖 SELECT/UPDATE/DELETE 与 INSERT）。
   例外及理由：
   - users / refresh_tokens：注册、登录、刷新都在「拿到用户身份之前」发生，
     无法预先设置 app.user_id；隔离由仓储层的参数化查询保证。
   - audit_logs：user_id 可空且不设 FK，供 worker/admin 跨租户写入与审计。
5. FORCE ROW LEVEL SECURITY 是关键一步（ADR-9 第 2 条）：不加则表 owner
   （app_owner）绕过策略；加了之后 owner 的 DML 也受约束，但迁移只做 DDL 不受影响。

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # =========================================================
    # §6.3 用户与认证
    # =========================================================
    op.execute("""
        CREATE TABLE users (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          email         TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          display_name  TEXT,
          role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
          settings      JSONB NOT NULL DEFAULT '{}',
          is_active     BOOLEAN NOT NULL DEFAULT TRUE,
          last_login_at TIMESTAMPTZ,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE refresh_tokens (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          token_hash TEXT NOT NULL,
          family_id  UUID NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          revoked    BOOLEAN NOT NULL DEFAULT FALSE,
          revoked_reason TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX refresh_tokens_hash_idx ON refresh_tokens(token_hash)")
    op.execute("CREATE INDEX refresh_tokens_user_idx ON refresh_tokens(user_id, expires_at DESC)")

    # =========================================================
    # §6.4 会话与消息
    # =========================================================
    op.execute("""
        CREATE TABLE conversations (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title         TEXT,
          last_message_at TIMESTAMPTZ,
          last_consolidated_at TIMESTAMPTZ,
          archived      BOOLEAN NOT NULL DEFAULT FALSE,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX conversations_user_idx ON conversations(user_id, last_message_at DESC)")

    op.execute("""
        CREATE TABLE messages (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          seq             INT NOT NULL,
          role            TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
          content         TEXT,
          status          TEXT NOT NULL DEFAULT 'completed'
                          CHECK (status IN ('streaming','completed','interrupted','cancelled','failed')),
          tool_calls      JSONB,
          citations       JSONB,
          degraded        JSONB,
          token_usage     JSONB,
          trace_id        TEXT,
          feedback        SMALLINT,
          feedback_note   TEXT,
          superseded_by   UUID,
          error           JSONB,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (conversation_id, seq)
        )
    """)
    op.execute("CREATE INDEX messages_conv_seq_idx ON messages(conversation_id, seq)")
    op.execute("CREATE INDEX messages_streaming_idx ON messages(status) WHERE status = 'streaming'")

    # =========================================================
    # §6.5 知识库
    # =========================================================
    op.execute("""
        CREATE TABLE documents (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title         TEXT NOT NULL,
          source        TEXT,
          source_type   TEXT NOT NULL CHECK (source_type IN ('upload','webclip','note')),
          status        TEXT NOT NULL DEFAULT 'processing'
                        CHECK (status IN ('processing','ready','failed')),
          content_hash  TEXT NOT NULL,
          parser_used   TEXT,
          chunk_count   INT,
          error_message TEXT,
          retry_count   INT NOT NULL DEFAULT 0,
          meta          JSONB NOT NULL DEFAULT '{}',
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (user_id, content_hash)
        )
    """)
    op.execute("CREATE INDEX documents_user_status_idx ON documents(user_id, status)")

    op.execute("""
        CREATE TABLE chunks (
          id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          parent_id      UUID REFERENCES chunks(id) ON DELETE CASCADE,
          chunk_type     TEXT NOT NULL CHECK (chunk_type IN ('parent','child')),
          ord            INT NOT NULL,
          content        TEXT NOT NULL,
          content_tokens TEXT,
          meta           JSONB NOT NULL DEFAULT '{}',
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # tsv 用生成列，避免「列永远为 NULL」的静默失效（v1.0 的 bug）
    op.execute("""
        ALTER TABLE chunks
          ADD COLUMN tsv tsvector
          GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content_tokens, ''))) STORED
    """)
    # 仅子块建向量（父块不参与召回，避免父子重复召回）
    op.execute("ALTER TABLE chunks ADD COLUMN embedding vector(1024)")
    op.execute("""
        ALTER TABLE chunks ADD CONSTRAINT chunks_child_has_embedding
          CHECK ((chunk_type = 'child') = (embedding IS NOT NULL))
    """)
    op.execute("""
        CREATE INDEX chunks_vec_idx ON chunks
          USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)
    """)
    op.execute("CREATE INDEX chunks_tsv_idx ON chunks USING gin(tsv)")
    op.execute("CREATE INDEX chunks_user_idx ON chunks(user_id)")
    op.execute("CREATE UNIQUE INDEX chunks_doc_ord_uidx ON chunks(document_id, chunk_type, ord)")
    op.execute("CREATE INDEX chunks_parent_idx ON chunks(parent_id) WHERE parent_id IS NOT NULL")

    # =========================================================
    # §6.6 三级记忆（拆表以同时满足「唯一」与「审计」）
    # =========================================================
    op.execute("""
        CREATE TABLE episodic_memories (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
          summary         TEXT NOT NULL,
          embedding       vector(1024),
          importance      REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
          happened_at     TIMESTAMPTZ NOT NULL,
          archived        BOOLEAN NOT NULL DEFAULT FALSE,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX episodic_vec_idx ON episodic_memories
          USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)
    """)
    op.execute("CREATE INDEX episodic_user_time_idx ON episodic_memories(user_id, happened_at DESC)")

    op.execute("""
        CREATE TABLE semantic_memories (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          kind          TEXT NOT NULL CHECK (kind IN ('profile','preference','fact','instruction')),
          key           TEXT NOT NULL,
          value         TEXT NOT NULL,
          embedding     vector(1024),
          confidence    REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
          hit_count     INT NOT NULL DEFAULT 0,
          version       INT NOT NULL DEFAULT 1,
          source_episode_id UUID REFERENCES episodic_memories(id) ON DELETE SET NULL,
          needs_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
          last_used_at  TIMESTAMPTZ,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (user_id, kind, key)
        )
    """)
    op.execute("""
        CREATE INDEX semantic_vec_idx ON semantic_memories
          USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)
    """)
    op.execute("CREATE INDEX semantic_user_kind_idx ON semantic_memories(user_id, kind)")
    op.execute("""
        CREATE INDEX semantic_confirm_idx ON semantic_memories(user_id)
          WHERE needs_confirmation = TRUE
    """)

    # 变更历史（审计）：不设 FK，保留被删记忆的历史
    op.execute("""
        CREATE TABLE semantic_memory_history (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          memory_id  UUID NOT NULL,
          user_id    UUID NOT NULL,
          kind       TEXT NOT NULL,
          key        TEXT NOT NULL,
          old_value  TEXT,
          new_value  TEXT,
          reason     TEXT NOT NULL,
          changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX semantic_hist_user_idx ON semantic_memory_history(user_id, changed_at DESC)")

    # =========================================================
    # §6.7 待办、审批、简报（补幂等约束）
    # =========================================================
    op.execute("""
        CREATE TABLE todos (
          id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          title      TEXT NOT NULL,
          detail     TEXT,
          due_at     TIMESTAMPTZ,
          status     TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','done','cancelled')),
          source     TEXT NOT NULL DEFAULT 'chat',
          dedupe_key TEXT,
          reminded_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (user_id, dedupe_key)
        )
    """)
    # 提醒任务每分钟扫描：部分索引，小且快
    op.execute("CREATE INDEX todos_due_idx ON todos(status, due_at) WHERE status = 'pending'")

    op.execute("""
        CREATE TABLE approvals (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          thread_id   TEXT NOT NULL,
          tool_call   JSONB NOT NULL,
          risk_level  SMALLINT NOT NULL CHECK (risk_level IN (1,2)),
          status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','expired','executed')),
          expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '1 hour',
          decided_at  TIMESTAMPTZ,
          executed_at TIMESTAMPTZ,
          exec_result JSONB,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX approvals_pending_idx ON approvals(status, expires_at) WHERE status = 'pending'")
    op.execute("CREATE INDEX approvals_user_idx ON approvals(user_id, created_at DESC)")

    op.execute("""
        CREATE TABLE briefings (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          deliver_date DATE NOT NULL,
          content_md   TEXT NOT NULL,
          sections     JSONB,
          delivered_at TIMESTAMPTZ,
          channel      TEXT,
          UNIQUE (user_id, deliver_date)
        )
    """)

    # =========================================================
    # §6.8 v1.1 新增表
    # =========================================================
    op.execute("""
        CREATE TABLE tool_invocations (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          message_id  UUID REFERENCES messages(id) ON DELETE CASCADE,
          trace_id    TEXT,
          step_id     INT,
          round       INT,
          tool_name   TEXT NOT NULL,
          risk_level  SMALLINT NOT NULL,
          args        JSONB,
          result_ok   BOOLEAN NOT NULL,
          error_code  TEXT,
          deduplicated BOOLEAN NOT NULL DEFAULT FALSE,
          latency_ms  INT NOT NULL,
          result_chars INT,
          truncated   BOOLEAN NOT NULL DEFAULT FALSE,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX tool_inv_user_idx ON tool_invocations(user_id, created_at DESC)")
    op.execute("CREATE INDEX tool_inv_name_idx ON tool_invocations(tool_name, result_ok)")

    op.execute("""
        CREATE TABLE usage_daily (
          id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          usage_date     DATE NOT NULL,
          prompt_tokens  BIGINT NOT NULL DEFAULT 0,
          completion_tokens BIGINT NOT NULL DEFAULT 0,
          request_count  INT NOT NULL DEFAULT 0,
          cost_estimate  NUMERIC(12,4) NOT NULL DEFAULT 0,
          by_feature     JSONB NOT NULL DEFAULT '{}',
          UNIQUE (user_id, usage_date)
        )
    """)

    op.execute("""
        CREATE TABLE audit_logs (
          id          BIGSERIAL PRIMARY KEY,
          user_id     UUID,
          actor_type  TEXT NOT NULL CHECK (actor_type IN ('user','worker','admin','system')),
          action      TEXT NOT NULL,
          target      TEXT,
          payload     JSONB,
          ip          INET,
          user_agent  TEXT,
          trace_id    TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX audit_user_time_idx ON audit_logs(user_id, created_at DESC)")
    op.execute("CREATE INDEX audit_action_idx ON audit_logs(action, created_at DESC)")

    op.execute("""
        CREATE TABLE user_credentials (
          id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          provider        TEXT NOT NULL,
          encrypted_dek   BYTEA NOT NULL,
          nonce           BYTEA NOT NULL,
          ciphertext      BYTEA NOT NULL,
          scopes          TEXT[],
          expires_at      TIMESTAMPTZ,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (user_id, provider)
        )
    """)

    # =========================================================
    # §6.10 RLS：13 张业务表 ENABLE + FORCE + 租户隔离策略
    # 策略读事务级 current_setting('app.user_id', true)；
    # 未设置时结果为 NULL → 空结果（fail-closed，默认拒绝）。
    # =========================================================
    rls_tables = [
        "conversations",
        "messages",
        "documents",
        "chunks",
        "episodic_memories",
        "semantic_memories",
        "semantic_memory_history",
        "todos",
        "approvals",
        "briefings",
        "tool_invocations",
        "usage_daily",
        "user_credentials",
    ]
    for table in rls_tables:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # 关键：不加 FORCE 则表 owner（app_owner）绕过策略，形同虚设
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
              USING (user_id = current_setting('app.user_id', true)::uuid)
              WITH CHECK (user_id = current_setting('app.user_id', true)::uuid)
        """)


def downgrade() -> None:
    # 先删策略与 RLS（顺序上其实随表级联，但显式列出更清晰）
    rls_tables = [
        "conversations", "messages", "documents", "chunks",
        "episodic_memories", "semantic_memories", "semantic_memory_history",
        "todos", "approvals", "briefings",
        "tool_invocations", "usage_daily", "user_credentials",
    ]
    for table in rls_tables:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # 逆依赖顺序删除（外键多为 CASCADE，逐表删即可）
    op.execute("DROP TABLE IF EXISTS user_credentials")
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP TABLE IF EXISTS usage_daily")
    op.execute("DROP TABLE IF EXISTS tool_invocations")
    op.execute("DROP TABLE IF EXISTS briefings")
    op.execute("DROP TABLE IF EXISTS approvals")
    op.execute("DROP TABLE IF EXISTS todos")
    op.execute("DROP TABLE IF EXISTS semantic_memory_history")
    op.execute("DROP TABLE IF EXISTS semantic_memories")
    op.execute("DROP TABLE IF EXISTS episodic_memories")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP TABLE IF EXISTS users")
