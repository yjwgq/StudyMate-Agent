"""仓储层：数据访问的唯一入口（M1）。

约定：
    - 函数接收已开启事务的 AsyncSession，不自行 commit；
      事务边界由调用方的 session_scope / tenant_session 控制；
    - 用户身份相关的隔离有两道防线：SQL 里的 user_id 过滤（第一道）
      + 数据库 RLS 策略（第二道，兜底）；
    - users / refresh_tokens 无 RLS（注册、登录、刷新发生在拿到身份之前），
      隔离完全依赖本层显式的 user_id 条件 —— 因此这里的每个查询都必须带全。
"""

from . import auth, conversations  # noqa: F401  (re-export)
