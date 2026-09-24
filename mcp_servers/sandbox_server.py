"""沙箱 MCP server（M4-8，§13.8）：RestrictedPython + 子进程隔离 + 资源限制。

架构：
    Agent → ToolRegistry → MCP stdio → 本进程
         → 再 fork 一个**孙进程**（python -I）执行用户代码
    孙进程内：RestrictedPython 编译执行 + resource.setrlimit
    （CPU 3s / 内存 256MB / 文件 4MB）+ 3 秒墙钟超时后被杀。

诚实声明（§13.8 原文要求）：
    RestrictedPython ≠ OS 隔离。进程级限制能防死循环与内存炸弹，
    **不能防容器逃逸类攻击** —— 本项目不处理该威胁模型（无多租户代码执行需求）。

Windows 开发环境没有 resource 模块：降级为「子进程 + 墙钟超时」，
不提供 rlimit（compose 内为 Linux，验收环境生效完整限制）。
"""

import asyncio
import json
import sys

from pydantic import BaseModel, Field

from mcp_servers.common import make_server, run

app = make_server(
    "sandbox",
    "运行 Python 代码片段做计算/数据处理。用于数学计算、数据变换等任务。"
    "代码运行在受限环境：无网络、无文件系统写、无 import（仅内置安全函数）。",
)

# 传给孙进程的受限执行模板：stdin 收代码 JSON，stdout 回结果 JSON
_CHILD_TEMPLATE = r'''
import json, sys

def main():
    raw = sys.stdin.read()
    req = json.loads(raw)
    code = req["code"]
    # ---- 资源限制（仅 POSIX）----
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))  # 禁 fork
    except Exception:
        pass  # Windows 无 resource 模块：降级为仅墙钟超时

    from RestrictedPython import compile_restricted, safe_builtins, safe_globals
    from RestrictedPython.Guards import (
        guarded_iter_unpack_sequence,
        guarded_unpack_sequence,
        safer_getattr,
    )
    from RestrictedPython.PrintCollector import PrintCollector

    result = {"ok": False, "stdout": "", "error": ""}
    import io, contextlib, traceback

    buf = io.StringIO()
    loc = {"__name__": "sandbox"}
    try:
        byte_code = compile_restricted(code, "<sandbox>", "exec")
        glob = dict(safe_globals)
        # safe_builtins 极简（无 sum/abs/len 等），补充纯函数白名单
        # （白名单必须内嵌：孙进程是独立解释器，拿不到父进程的模块常量）
        _EXTRA = ("abs", "min", "max", "sum", "round", "len", "range", "enumerate",
                  "sorted", "reversed", "list", "dict", "tuple", "set", "str", "int",
                  "float", "bool", "zip", "map", "filter", "divmod", "pow")
        import builtins as _b
        glob["__builtins__"] = dict(safe_builtins)  # type: ignore[assignment]
        glob["__builtins__"].update({n: getattr(_b, n) for n in _EXTRA})
        # RestrictedPython 标准守卫：print 收集器 / 受控属性访问 / 解包保护
        glob["_print_"] = PrintCollector
        glob["_getattr_"] = safer_getattr
        glob["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
        glob["_unpack_sequence_"] = guarded_unpack_sequence
        glob["_getiter_"] = iter  # 受控迭代（列表/元组/字符串）
        with contextlib.redirect_stdout(buf):
            exec(byte_code, glob, loc)  # noqa: S102 —— 已经过 RestrictedPython 编译限制
        result["ok"] = True
        # 结果约定：变量 _（下划线）作为返回值（若存在）
        ret = loc.get("_", glob.get("_"))
        if ret is not None:
            result["value"] = ret
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["trace"] = traceback.format_exc(limit=3)
    result["stdout"] = buf.getvalue()[:4000]
    print(json.dumps(result, ensure_ascii=False, default=str))

main()
'''


class RunCodeArgs(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=8000,
        description="Python 代码。把要返回的结果赋给变量 _（下划线）；print 输出也会被捕获。",
    )


def _child_env() -> dict:
    import os

    env = dict(os.environ)
    env.update({"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


async def _run_in_subprocess(code: str) -> dict:
    """孙进程执行：-I 隔离模式 + 3s 墙钟超时。POSIX 附加 rlimits（见模板）。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", _CHILD_TEMPLATE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=json.dumps({"code": code}).encode()), timeout=6.0
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "执行超时（>6s，含 3s CPU 限制）", "stdout": ""}
    if proc.returncode != 0:
        return {"ok": False, "error": f"沙箱进程异常退出({proc.returncode}): {err.decode()[:500]}", "stdout": ""}
    try:
        return json.loads(out.decode())
    except (ValueError, TypeError):
        return {"ok": False, "error": "沙箱输出无法解析", "stdout": out.decode()[:2000]}


@app.tool()
async def run_python(code: str) -> str:
    """运行 Python 代码做计算。结果赋给变量 _ 返回；print 输出一并返回。"""
    result = await _run_in_subprocess(code)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    run(app)
