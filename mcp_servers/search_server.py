"""联网搜索 MCP server（M4-9，ADR-4 / W3 三件套之一）。

Provider 策略：
    1. SEARCH_PROVIDER=tavily + SEARCH_API_KEY → Tavily API（生产推荐）；
    2. 缺省 → DuckDuckGo HTML 端点解析（无 key，够验收与演示；页面结构
       变化会失效 —— 那是这类无协议抓取的固有脆弱性，如实告知）。

SSRF 说明：查询词发往固定第三方域名（白名单性质），非用户可控 URL ——
用户可控 URL 的 fetch 工具在 M9 落地（§13.4 DNS 后校验 + 内网段黑名单）。
"""

import json
import os
import re
from html import unescape
from typing import Any

import httpx
from pydantic import BaseModel, Field

from mcp_servers.common import make_server, run

app = make_server(
    "search",
    "联网搜索公开网页。用于查询知识库之外的时效性/公开信息。返回标题、链接与摘要。",
)

_UA = "Mozilla/5.0 (compatible; PersonalAgentOS/1.0; +https://localhost)"


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=400, description="搜索关键词")
    max_results: int = Field(default=5, ge=1, le=10)


def _clip(text: str, n: int = 300) -> str:
    text = unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
    return text[:n] + ("…" if len(text) > n else "")


async def _tavily(query: str, max_results: int) -> list[dict[str, Any]]:
    api_key = os.environ.get("SEARCH_API_KEY", "")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results},
        )
        r.raise_for_status()
        data = r.json()
    return [
        {"title": _clip(item.get("title", ""), 120), "url": item.get("url", ""), "snippet": _clip(item.get("content", ""))}
        for item in data.get("results", [])
    ]


async def _duckduckgo(query: str, max_results: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}, follow_redirects=True) as client:
        r = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
        r.raise_for_status()
    results: list[dict[str, Any]] = []
    for m in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.DOTALL
    ):
        url = m.group(1)
        # DDG 的跳转链接 //duckduckgo.com/l/?uddg=<encoded>
        uddg = re.search(r"uddg=([^&]+)", url)
        if uddg:
            from urllib.parse import unquote

            url = unquote(uddg.group(1))
        results.append({"title": _clip(m.group(2), 120), "url": url, "snippet": ""})
        if len(results) >= max_results:
            break
    # 补充摘要
    snippets = re.findall(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', r.text, re.DOTALL)
    for i, item in enumerate(results):
        if i < len(snippets):
            item["snippet"] = _clip(snippets[i])
    return results


@app.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索公开网页信息。"""
    provider = os.environ.get("SEARCH_PROVIDER", "duckduckgo")
    try:
        if provider == "tavily" and os.environ.get("SEARCH_API_KEY"):
            results = await _tavily(query, max_results)
            used = "tavily"
        else:
            results = await _duckduckgo(query, max_results)
            used = "duckduckgo"
    except Exception as exc:  # noqa: BLE001 —— 搜索失败转结构化结果
        return json.dumps(
            {"ok": False, "error": f"搜索失败（{provider}）：{exc}", "results": []}, ensure_ascii=False
        )
    return json.dumps(
        {"ok": True, "provider": used, "results": results, "note": "网络信息可能过时，注意核对"},
        ensure_ascii=False,
    )


if __name__ == "__main__":
    run(app)
