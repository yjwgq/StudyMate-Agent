#!/bin/bash
# ============================================================
#  M1-2：数据库初始化（docker-entrypoint-initdb.d，仅首次建库时执行）
# ============================================================
#  职责（普通角色无权完成、必须以超级用户身份做的事）：
#    1. 创建扩展：vector（pgvector，hnsw/iterative_scan 依赖，见 M1-3 特别要求）
#                 pg_trgm、pgcrypto
#    2. 创建三角色（ADR-9）：
#         app_owner   迁移角色，拥有表所有权（alembic 用它连库）
#         app_api     API 角色，受 RLS 约束
#         app_worker  Worker 角色，BYPASSRLS（跨租户扫描定时任务，M9 用）
#    3. 授权：app_owner 成为库 owner（PG15+ 下 public schema 建表需要）；
#       为 app_owner 未来创建的表/序列预授 DML 权限给两个运行角色。
#
#  密码说明（开发环境简化）：三角色统一使用 POSTGRES_PASSWORD 的值，
#  与 .env 中三条 DATABASE_URL 里的密码保持一致即可。
#  生产环境应由 secrets 管理注入各自独立的强密码。
# ============================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v dbname="$POSTGRES_DB" \
     -v pw="$POSTGRES_PASSWORD" <<'EOSQL'

-- 扩展必须由超级用户创建（vector 非 trusted 扩展），迁移脚本不再处理
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 三角色（ADR-9）
CREATE ROLE app_owner  LOGIN PASSWORD :'pw';
CREATE ROLE app_api    LOGIN PASSWORD :'pw';
CREATE ROLE app_worker LOGIN PASSWORD :'pw' BYPASSRLS;

-- 库所有权交给迁移角色：PG15+ 的 public schema 由 pg_database_owner 持有，
-- app_owner 成为库 owner 后才能在其中建表
ALTER DATABASE :"dbname" OWNER TO app_owner;
GRANT CONNECT ON DATABASE :"dbname" TO app_api, app_worker;

GRANT ALL ON SCHEMA public TO app_owner;
GRANT USAGE ON SCHEMA public TO app_api, app_worker;

-- app_owner 未来创建的表/序列，自动授予两个运行角色 DML 权限
-- （必须在建表之前设置默认权限才生效）
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_api, app_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_api, app_worker;

EOSQL

echo "[init] extensions created; roles app_owner / app_api / app_worker ready"
