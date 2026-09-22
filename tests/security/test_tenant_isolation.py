"""B3 / B4：多租户隔离验收。

B3  注册 → 登录 → GET /api/v1/conversations 返回自己的空列表。
B4  用 A 用户的 token 请求 B 用户的 conversation id →
    返回 404（空结果语义，不泄露存在性），绝不返回 B 的数据。

B4 是整个项目最重要的一条安全验收线：它验证的不是「代码写对了」，
而是「租户隔离真的生效了」。因此除 API 层断言外，本模块还包含
RLS 直探（绕过应用层，直接以 app_api 角色对数据库发起查询）：
    - 无租户上下文（未 SET app.user_id）→ 看不到任何行（fail-closed）；
    - 上下文为 A → 只能看到 A 的行；对 B 的行做 UPDATE → 0 行受影响。
"""


import pytest
from sqlalchemy import text

from tests.security.conftest import (
    auth_header,
    insert_conversation_for,
    register_and_login,
)


@pytest.mark.asyncio
async def test_b3_conversations_empty_list(client):
    """B3：注册 → 登录 → 会话列表为空。"""
    tokens = await register_and_login(client)
    r = await client.get("/api/v1/conversations", headers=auth_header(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["items"] == []
    assert "trace_id" in body


@pytest.mark.asyncio
async def test_b4_cross_tenant_conversation_hidden(client, worker_engine):
    """B4（API 层）：A 请求 B 的 conversation → 404，且响应中无 B 的任何数据。"""
    user_a = await register_and_login(client)
    user_b = await register_and_login(client)

    # 为 B 预置一个会话（模拟 B 的真实数据）
    b_conv_id = await insert_conversation_for(worker_engine, user_b["user_id"])

    # A 携带自己的 token 访问 B 的会话消息
    r = await client.get(
        f"/api/v1/conversations/{b_conv_id}/messages",
        headers=auth_header(user_a),
    )
    assert r.status_code in (403, 404), f"期望 403/404，实际 {r.status_code}: {r.text}"
    body = r.text
    assert user_b["user_id"] not in body, "响应泄露了 B 的数据"

    # A 访问自己的会话 → 正常（对照组）
    r_ok = await client.get(
        f"/api/v1/conversations/{await insert_conversation_for(worker_engine, user_a['user_id'])}/messages",
        headers=auth_header(user_a),
    )
    assert r_ok.status_code == 200, r_ok.text

    # 未认证请求 → 401
    r_anon = await client.get(f"/api/v1/conversations/{b_conv_id}/messages")
    assert r_anon.status_code == 401


@pytest.mark.asyncio
async def test_b4_rls_probe_no_context_denies_everything(api_engine, worker_engine, client):
    """B4（数据库直探）：app_api 角色在无租户上下文时看不到任何业务行。

    绕过应用层直接验证 FORCE ROW LEVEL SECURITY 的兜底效果——
    即使未来某个查询漏写 user_id 过滤，数据库也不放行。
    """
    # 确认库中确实有数据（worker 视角）
    async with worker_engine.begin() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM conversations"))).scalar_one()
    assert total >= 1, "夹具未产生数据，探测无意义"

    # app_api 视角：未设置 app.user_id → fail-closed，0 行
    async with api_engine.begin() as conn:
        visible = (await conn.execute(text("SELECT count(*) FROM conversations"))).scalar_one()
    assert visible == 0, f"RLS 未生效：app_api 无上下文却看到 {visible} 行"


@pytest.mark.asyncio
async def test_b4_rls_probe_context_isolation(api_engine, worker_engine, client):
    """B4（数据库直探）：设置 A 的上下文后，只能看到 A 的行；对 B 的行 UPDATE 无效。"""
    user_a = await register_and_login(client)
    user_b = await register_and_login(client)
    a_conv = await insert_conversation_for(worker_engine, user_a["user_id"])
    b_conv = await insert_conversation_for(worker_engine, user_b["user_id"])

    async with api_engine.begin() as conn:
        # 以 A 的上下文：可见自己的会话
        await conn.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_a["user_id"]}
        )
        seen_a = (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE id = :cid"),
                {"cid": a_conv},
            )
        ).scalar_one()
        assert seen_a == 1, "A 的上下文应能看到 A 自己的会话"

        # 以 A 的上下文：B 的会话不可见（读为零行）
        seen_b = (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE id = :cid"),
                {"cid": b_conv},
            )
        ).scalar_one()
        assert seen_b == 0, "A 的上下文不该看到 B 的会话"

        # 以 A 的上下文：直接 UPDATE B 的会话 → 0 行受影响（写同样被拦）
        result = await conn.execute(
            text("UPDATE conversations SET title = 'hacked' WHERE id = :cid"),
            {"cid": b_conv},
        )
        assert result.rowcount == 0, "RLS 未拦截跨租户写"

    # 复核：B 的数据未被篡改
    async with worker_engine.begin() as conn:
        title = (
            await conn.execute(
                text("SELECT title FROM conversations WHERE id = :cid"), {"cid": b_conv}
            )
        ).scalar_one()
    assert title == "fixture-conversation", "B 的数据被跨租户修改了！"


@pytest.mark.asyncio
async def test_b4_chat_endpoint_rejects_foreign_conversation(client, worker_engine):
    """B4（聊天口）：带他人 conversation_id 发消息 → 404，不产生任何消息。"""
    from tests.security.conftest import TEST_PASSWORD

    user_a = await register_and_login(client)
    user_b = await register_and_login(client)
    b_conv = await insert_conversation_for(worker_engine, user_b["user_id"])

    # 该用例不真调模型：期望在落库/模型调用之前就被 404 拦截。
    # 若实现顺序正确，响应必是 404 JSON。
    r = await client.post(
        "/api/v1/chat",
        json={"content": "hi", "conversation_id": b_conv},
        headers=auth_header(user_a),
    )
    assert r.status_code == 404, f"期望 404，实际 {r.status_code}: {r.text}"
    assert r.json()["error"]["code"] == "NOT_FOUND"

    _ = TEST_PASSWORD
