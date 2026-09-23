"""SSE 事件协议。

M0 只实现三种事件：token / done / error。
完整协议（tool_start · tool_end · approval · citation · degraded）见设计文档
v1.1 §11.3，将在 M5 随「执行与传输解耦 + 事件重放」一并落地。

协议要点（踩坑记录）：
    1. SSE 以「空行」作为消息分隔符 —— 每条消息必须以 \\n\\n 结尾，
       否则浏览器收不到完整事件。
    2. data 必须是单行 JSON。多行会被 SSE 解析成多个 data 字段。
    3. 响应头必须带 X-Accel-Buffering: no，否则经过 Nginx 时整个流
       会被缓冲住，前端看到的是「转圈几十秒然后一次性吐出全文」。
       Caddy 侧的对应配置是 flush_interval -1（见 infra/caddy/Caddyfile）。
"""

import json
from typing import Any

EVENT_TOKEN = "token"
EVENT_DONE = "done"
EVENT_ERROR = "error"
# M3（§11.3 完整协议的逐步落地）：
EVENT_CITATIONS = "citations"   # 检索命中：{citations: [{n,title,page,snippet,...}]}
EVENT_NOTICE = "notice"         # 过程提示：{code, message, clear?}（clear=true 时前端清空当前气泡重生成）
EVENT_DEGRADED = "degraded"     # 显式降级标记（§8.3）：{degraded: [...], message}

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    # 关键：禁止中间层缓冲。
    # 不写 Connection: keep-alive —— 那是 HTTP/1.1 的连接头，
    # 由 uvicorn/Starlette 自行管理；在 HTTP/2 下它还是非法首部。
    "X-Accel-Buffering": "no",
}

SSE_MEDIA_TYPE = "text/event-stream"


def sse_event(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    """把一条事件序列化为 SSE 文本块。

    Args:
        event: 事件名，见本模块顶部的事件常量。
        data:  事件负载，会被序列化为单行 JSON。
        event_id: 可选的 SSE 事件 id（Redis Stream entry id）。浏览器 EventSource
            会自动把它作为 Last-Event-ID 在重连时回传；自研 fetch 流也靠它续读
            （§7.7 第 3/4 条）。**M5 验收实锤**：不输出 id 行时，审批暂停后再
            resume 的订阅只能从 0 读，会立刻撞上上一轮的 done，误判
            「续跑没执行」。

    Returns:
        形如 ``id: 1790-0\\nevent: token\\ndata: {"delta":"你"}\\n\\n`` 的字符串。
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    id_line = f"id: {event_id}\n" if event_id else ""
    return f"{id_line}event: {event}\ndata: {payload}\n\n"
