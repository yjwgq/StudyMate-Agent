"""演示状态准备脚本（M10-2）。

一条命令把 demo 需要的状态准备好：
    1. 建（或复用）演示用户并登录；
    2. 上传 3 篇演示语料并等到 ready（知识问答场景要有东西可检索）；
    3. 预置一条待办（工具治理场景的旁证）；
    4. 打印**分镜里要用的三段提问**与**降级开关命令**（可直接复制粘贴）。

用法：
    uv run python scripts/demo_prepare.py
    uv run python scripts/demo_prepare.py --base-url http://127.0.0.1:8080

注意：本脚本只做"准备"，不改任何业务代码；重复运行是安全的（幂等）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "DemoPass123!"
CORPUS = [
    ("ml_basics.md", "监督学习 / 无监督学习 / 过拟合"),
    ("db_systems.md", "B+ 树索引 / MVCC"),
    ("rag_notes.md", "RAG / 混合检索 / RRF"),
]


async def _login_or_register(client: httpx.AsyncClient, base_url: str) -> str:
    r = await client.post(
        f"{base_url}/api/v1/auth/register",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "display_name": "Demo 用户"},
    )
    if r.status_code not in (200, 201, 409):
        raise SystemExit(f"注册失败 {r.status_code}: {r.text[:200]}")
    r = await client.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
    )
    if r.status_code != 200:
        raise SystemExit(f"登录失败 {r.status_code}: {r.text[:200]}")
    return r.json()["data"]["access_token"]


async def _upload_corpus(client: httpx.AsyncClient, base_url: str, docs_dir: Path) -> int:
    uploaded = 0
    for fname, _topics in CORPUS:
        path = docs_dir / fname
        if not path.exists():
            print(f"  ⚠ 语料缺失（先跑 evals.build_golden_v2 --write-corpus）：{path}")
            continue
        with path.open("rb") as f:
            r = await client.post(
                f"{base_url}/api/v1/kb/documents",
                files={"file": (path.name, f, "text/markdown")},
            )
        if r.status_code in (200, 201, 202):
            uploaded += 1
            dup = r.json().get("data", {}).get("duplicated")
            print(f"  ✓ {fname}{'（已存在，复用）' if dup else ''}")
        else:
            print(f"  ✗ {fname} 上传失败 {r.status_code}: {r.text[:160]}")
    return uploaded


async def _wait_ready(client: httpx.AsyncClient, base_url: str, timeout_s: int = 180) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = await client.get(f"{base_url}/api/v1/kb/documents")
        if r.status_code == 200:
            docs = r.json()["data"]["items"]
            ready = [d for d in docs if d["status"] == "ready"]
            pending = [d for d in docs if d["status"] == "processing"]
            print(f"  文档 {len(docs)} 篇：ready {len(ready)}，processing {len(pending)}")
            if docs and not pending:
                return all(d["status"] == "ready" for d in docs)
        await asyncio.sleep(3)
    return False


def _print_playbook(base_url: str) -> None:
    print("\n" + "=" * 72)
    print("演示playbook（复制粘贴即可）")
    print("=" * 72)
    print(f"""
【场景 1 · 带引用的问答】
  {base_url}  →  提问：
     监督学习和无监督学习的区别是什么？
     预期：答案逐字流出 + [1][2] 引用卡片（可展开看来源段落）

【场景 2 · 工具调用 + 人工审批（L2）】
  提问：
     请调用 send_email 工具，给 partner@corp.com 发邮件，主题「合作沟通」，正文「想约下周三讨论合作细节。」
     预期：弹出审批卡片（L2 / 收件人含外部域）→ 点「批准执行」→ 返回邮件 id

【场景 3 · 中断恢复（kill -9）】
  触发场景 2 的审批后**不要点批准**，切到终端：
     docker kill personal-agent-os-dev-api-1
     docker compose -f infra/docker-compose.dev.yml up -d api
     curl {base_url}/api/v1/ready            # 等 ready
  回浏览器刷新 → 审批卡片仍在 → 点「批准执行」→ 从断点续跑完成

【场景 4 · 降级角标】
  关精排（黄标：未精排）：
     curl -X POST {base_url}/api/v1/flags -H "Content-Type: application/json" \\
          -d '{{"name":"retrieval.rerank.enabled","value":false}}'
  再关向量路（红标：未走向量检索）：
     curl -X POST {base_url}/api/v1/flags -H "Content-Type: application/json" \\
          -d '{{"name":"retrieval.vector.enabled","value":false}}'
  提问：什么是交叉验证？          → 答案正常 + 角标
  逐路对比观察：{base_url}/kb/debug  （向量 0 条 / 关键词 28 条、生效开关）
  恢复（清除覆写）：
     curl -X POST {base_url}/api/v1/flags -H "Content-Type: application/json" \\
          -d '{{"name":"retrieval.rerank.enabled","value":null}}'
     curl -X POST {base_url}/api/v1/flags -H "Content-Type: application/json" \\
          -d '{{"name":"retrieval.vector.enabled","value":null}}'
""")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--docs-dir", default="data/evaldocs")
    args = ap.parse_args()
    base_url = args.base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=180.0) as client:
        # 先确认栈就绪（否则后面的报错会很难懂）
        try:
            r = await client.get(f"{base_url}/api/v1/ready", timeout=10)
            if r.status_code != 200:
                print(f"⚠ /ready 未就绪：{r.text[:200]}")
                return 2
        except httpx.HTTPError as exc:
            print(f"✗ 连不上 {base_url}：{exc}\n  先启动栈：docker compose -f infra/docker-compose.dev.yml up -d")
            return 2

        print(f"演示用户：{DEMO_EMAIL}")
        token = await _login_or_register(client, base_url)
        client.headers["Authorization"] = f"Bearer {token}"
        print("  ✓ 登录成功")

        print("上传演示语料：")
        await _upload_corpus(client, base_url, Path(args.docs_dir))
        ok = await _wait_ready(client, base_url)
        print(f"  {'✓ 全部就绪' if ok else '⚠ 超时未全部就绪（可稍后重跑本脚本）'}")

        # 预置一条待办：场景 2 之后可以顺带展示"写操作有审计"
        r = await client.post(
            f"{base_url}/api/v1/chat",
            headers={"Content-Type": "application/json", "Idempotency-Key": f"demo-{time.time()}"},
            json={"content": "帮我加一条待办：复习监督学习"},
        )
        print(f"预置待办（异步执行，HTTP {r.status_code}）")

    _print_playbook(base_url)
    print("准备完成。录制前请清屏（关闭无关标签与通知），分镜与旁白见 docs/demo/script.md")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
