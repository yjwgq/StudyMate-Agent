"""PII 脱敏单元测试（M3-4 / D4 的自动化断言）。

D4 验收口径：Langfuse trace 里搜不到测试手机号 —— 前提是本模块全绿。
「密钥紧贴中文」的用例是真实踩坑：Python 的 \\b 把 CJK 当 word 字符，
中英贴身组合下 \\b 不成立，会静默漏报（详见 redact.py 注释）。
"""

import pytest

from agent.obs.redact import MASK, mask_any, mask_text


class TestMaskText:
    @pytest.mark.parametrize(
        "raw",
        [
            "13812345678",
            "手机13812345678请查收",       # 中文贴身（\b 陷阱）
            "电话：15987654321。",
        ],
    )
    def test_phone_masked(self, raw: str) -> None:
        assert MASK in mask_text(raw)
        assert "13812345678" not in mask_text(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "110101199003077858",           # 18 位身份证
            "身份证号11010119900307785X",
        ],
    )
    def test_id_card_masked(self, raw: str) -> None:
        assert MASK in mask_text(raw)

    def test_email_masked(self) -> None:
        out = mask_text("联系 zhang.san@example.com 获取")
        assert "zhang.san@example.com" not in out
        assert MASK in out

    @pytest.mark.parametrize(
        "raw",
        [
            "sk-abcdefghijklmnop1234",
            "密钥sk-abcdefghijklmnop1234这是说明",   # 中文前后贴身
            "key: sk-proj-AbCdEfGhIjKlMnOpQrStUv",
        ],
    )
    def test_secret_masked(self, raw: str) -> None:
        out = mask_text(raw)
        assert "abcdefghijklmnop" not in out
        assert MASK in out

    def test_bearer_and_jwt_masked(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
        out = mask_text(f"Authorization: Bearer {jwt}")
        assert jwt not in out

    def test_short_sk_not_masked(self) -> None:
        """sk-1 这种非密钥片段不应误伤（宁多脱也不误伤可读性的平衡点）。"""
        assert "sk-1" in mask_text("纯文本不受影响 sk-1")

    def test_plain_text_untouched(self) -> None:
        assert mask_text("监督学习需要标注数据") == "监督学习需要标注数据"

    def test_empty(self) -> None:
        assert mask_text("") == ""


class TestMaskAny:
    def test_nested_dict(self) -> None:
        payload = {
            "query": "手机13812345678",
            "messages": [{"role": "user", "content": "邮箱a@b.com"}],
            "meta": {"count": 3, "flag": True},
        }
        out = mask_any(payload)
        assert "13812345678" not in str(out)
        assert "a@b.com" not in str(out)
        assert out["meta"] == {"count": 3, "flag": True}  # 标量不被字符串化

    def test_list(self) -> None:
        assert MASK in str(mask_any(["13812345678", "ok"]))

    def test_none_passthrough(self) -> None:
        assert mask_any(None) is None


class TestMaskOtelSpans:
    """Langfuse mask 回调（§13.3：「必须配置 redaction 回调，并做自动化测试断言」）。

    这是 trace 通道的最后一道闸门：SDK 导出前逐个 span 回调，
    覆盖 mask_hook 覆盖不到的自动插桩通道。

    M3 验收实锤的两种失败模式（单测锁死）：
      1. 返回普通 dict → SDK 判为非法 → **丢弃整个 export batch**（trace 全丢）；
      2. 签名不匹配 → SDK 抛错后退回内置 fallback（脱敏静默失效）。
    """

    @staticmethod
    def _span(attrs: dict) -> dict:
        return {
            "trace_id": "t" * 32,
            "span_id": "s" * 16,
            "parent_span_id": None,
            "name": "chat",
            "instrumentation_scope_name": None,
            "instrumentation_scope_version": None,
            "attributes": attrs,
            "resource_attributes": {},
        }

    @staticmethod
    def _ident() -> tuple[str, str]:
        return ("t" * 32, "s" * 16)

    def test_returns_sdk_result_type(self) -> None:
        """返回值必须是 MaskOtelSpansResult 实例 —— dict 会导致整批 trace 被丢弃。"""
        from langfuse.types import MaskOtelSpansResult

        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {ident: self._span({"langfuse.observation.input": '{"q": "13812345678"}'})}
        result = mask_otel_spans(spans=spans)
        assert isinstance(result, MaskOtelSpansResult)
        patch = result.span_patches[ident]
        assert "13812345678" not in patch.set_attributes["langfuse.observation.input"]

    def test_masks_observation_input_json(self) -> None:
        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {
            ident: self._span(
                {
                    "langfuse.observation.input": '{"query": "我的手机号是13812345678"}',
                    "langfuse.observation.output": '["邮箱 a@b.com"]',
                }
            )
        }
        result = mask_otel_spans(spans=spans)
        patch = result.span_patches[ident].set_attributes
        assert MASK in patch["langfuse.observation.input"]
        assert "a@b.com" not in patch["langfuse.observation.output"]

    def test_masks_trace_level_attrs(self) -> None:
        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {ident: self._span({"langfuse.trace.input": "身份证110101199003077858"})}
        patch = mask_otel_spans(spans=spans).span_patches[ident].set_attributes
        assert "110101199003077858" not in patch["langfuse.trace.input"]

    def test_plain_string_attr_masked(self) -> None:
        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {ident: self._span({"langfuse.observation.input": "手机13812345678"})}
        patch = mask_otel_spans(spans=spans).span_patches[ident].set_attributes
        assert "13812345678" not in patch["langfuse.observation.input"]

    def test_clean_payload_not_patched(self) -> None:
        """无 PII 的 span 不产生 patch（避免无谓的上报体积）。"""
        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {ident: self._span({"langfuse.observation.input": '{"query": "什么是监督学习"}'})}
        assert mask_otel_spans(spans=spans).span_patches == {}

    def test_unrelated_attrs_ignored(self) -> None:
        from agent.obs.langfuse import mask_otel_spans

        ident = self._ident()
        spans = {ident: self._span({"langfuse.observation.model.name": "13812345678"})}
        assert mask_otel_spans(spans=spans).span_patches == {}

    def test_malformed_input_returns_none_not_raise(self) -> None:
        """异常时返回 None（SDK 语义：不做修改），绝不抛、也不丢数据。"""
        from agent.obs.langfuse import mask_otel_spans

        assert mask_otel_spans(params=object()) is None  # 非法入参 → None

    def test_accepts_params_object(self) -> None:
        """SDK 传的是 params 对象（params.spans），不是裸 mapping。"""
        from langfuse.types import MaskOtelSpansParams

        from agent.obs.langfuse import mask_otel_spans

        params = MaskOtelSpansParams(
            spans={
                self._ident(): self._span(
                    {"langfuse.observation.output": '{"a": "a@b.com"}'}
                )
            }
        )
        result = mask_otel_spans(params=params)
        patch = result.span_patches[self._ident()].set_attributes
        assert "a@b.com" not in patch["langfuse.observation.output"]

    def test_mask_hook_signature(self) -> None:
        """`mask=` 由 SDK 以 data= 关键字调用 —— 签名写错会静默退回 fallback。"""
        from agent.obs.langfuse import mask_hook

        out = mask_hook(data={"query": "手机13812345678", "n": 3})
        assert "13812345678" not in str(out)
        assert out["n"] == 3
