"""SSE 事件序列化的单元测试（M0 的第一批测试）。

为什么第一批测试选它：SSE 的格式是本里程碑**最容易写错、又最难排查**的一环 ——
格式差一个换行，前端就是一个字都收不到，而且浏览器不会报任何错。
把它钉死在测试里，后面改动 sse.py 时能立刻发现回归。
"""

import json

from apps.api.sse import EVENT_ERROR, EVENT_TOKEN, sse_event


def test_event_ends_with_blank_line() -> None:
    """SSE 以空行分隔消息：结尾必须是两个换行，否则浏览器收不到完整事件。"""
    out = sse_event(EVENT_TOKEN, {"delta": "你好"})
    assert out.endswith("\n\n")


def test_event_has_event_and_data_lines() -> None:
    """标准块形如：event: token / data: {...} / 空行"""
    out = sse_event(EVENT_TOKEN, {"delta": "你"})
    lines = out.split("\n")
    assert lines[0] == "event: token"
    assert lines[1].startswith("data: ")
    assert lines[2] == ""
    assert lines[3] == ""


def test_data_stays_on_single_line() -> None:
    """data 必须是单行 JSON —— 真实换行会被 SSE 解析成多个 data 字段。"""
    out = sse_event(EVENT_TOKEN, {"delta": "第一行\n第二行"})
    data_line = out.split("\n")[1]
    payload = data_line[len("data: ") :]
    assert "\n" not in payload
    assert json.loads(payload)["delta"] == "第一行\n第二行"


def test_chinese_is_not_escaped() -> None:
    """中文原样输出（ensure_ascii=False），便于直接看日志排查问题。"""
    out = sse_event(EVENT_TOKEN, {"delta": "中文内容"})
    assert "中文内容" in out


def test_error_event_name_constant() -> None:
    """事件名走常量，避免前后端字符串漂移。"""
    out = sse_event(EVENT_ERROR, {"code": "LLM_OFFLINE", "message": "boom"})
    assert out.startswith("event: error\n")
