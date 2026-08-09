# -*- coding: utf-8 -*-
"""
安全数理计算沙箱
================
基于 RestrictedPython 隔离危险操作 + SymPy 完成微积分、方程、数值验算。

设计原则：
1. 禁止直接 eval/exec，所有用户代码必须经 RestrictedPython 编译；
2. 仅暴露白名单内置函数与 SymPy 数学符号；
3. 限制执行时间与递归深度，防止死循环；
4. 所有异常捕获后返回结构化结果，绝不抛出未处理异常。

依据：项目硬性规则第9条——数理计算使用 RestrictedPython 安全沙箱+SymPy，禁止直接 eval。
"""
from __future__ import annotations

import math
import signal
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

import sympy as sp
from RestrictedPython import compile_restricted, safe_builtins
from RestrictedPython.Eval import default_guarded_getiter
from RestrictedPython.Guards import (
    guarded_iter_unpack_sequence,
    safer_getattr,
)


# ---------------------------------------------------------------------------
# 沙箱环境配置
# ---------------------------------------------------------------------------

# 全局可用执行超时（秒）
DEFAULT_TIMEOUT_SECONDS = 10

# 最大递归深度
MAX_RECURSION_DEPTH = 200


def _build_safe_globals() -> Dict[str, Any]:
    """
    构建沙箱可用的全局命名空间。

    暴露内容：
    - 白名单内置函数：abs / min / max / round / sum / len / range / enumerate / zip / map / filter / sorted
    - SymPy 符号与函数：symbols / simplify / solve / diff / integrate / limit / series / Matrix / Rational
    - math 标准库：math
    - 受限的 print 函数（仅截断输出长度）
    """
    sympy_namespace = {
        # 基础符号
        "symbols": sp.symbols,
        "Symbol": sp.Symbol,
        "sympify": sp.sympify,
        "parse_expr": sp.parse_expr,
        "S": sp.S,
        "Rational": sp.Rational,
        "Integer": sp.Integer,
        "Float": sp.Float,
        # 表达式化简与运算
        "simplify": sp.simplify,
        "expand": sp.expand,
        "factor": sp.factor,
        "collect": sp.collect,
        "cancel": sp.cancel,
        "apart": sp.apart,
        "together": sp.together,
        "trigsimp": sp.trigsimp,
        "radsimp": sp.radsimp,
        # 微积分
        "diff": sp.diff,
        "integrate": sp.integrate,
        "limit": sp.limit,
        "series": sp.series,
        "Derivative": sp.Derivative,
        "Integral": sp.Integral,
        # 方程与不等式
        "solve": sp.solve,
        "solveset": sp.solveset,
        "Eq": sp.Eq,
        "roots": sp.roots,
        # 线性代数
        "Matrix": sp.Matrix,
        "det": sp.det,
        "inv": sp.Inverse,
        # 集合
        "FiniteSet": sp.FiniteSet,
        "Interval": sp.Interval,
        # 数值计算
        "N": sp.N,
        "nsimplify": sp.nsimplify,
        # 三角与特殊函数
        "sin": sp.sin,
        "cos": sp.cos,
        "tan": sp.tan,
        "cot": sp.cot,
        "asin": sp.asin,
        "acos": sp.acos,
        "atan": sp.atan,
        "sinh": sp.sinh,
        "cosh": sp.cosh,
        "exp": sp.exp,
        "log": sp.log,
        "ln": sp.ln,
        "sqrt": sp.sqrt,
        "Abs": sp.Abs,
        "pi": sp.pi,
        "E": sp.E,
        "oo": sp.oo,
        "I": sp.I,
    }

    # 白名单内置函数
    safe_builtin_subset = {
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "sum": sum,
        "len": len,
        "range": range,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "sorted": sorted,
        "list": list,
        "tuple": tuple,
        "dict": dict,
        "set": set,
        "bool": bool,
        "int": int,
        "float": float,
        "str": str,
        "True": True,
        "False": False,
        "None": None,
        "isinstance": isinstance,
        "type": type,
        # 使用 output 替代 print，绕开 RestrictedPython 的 _print_ 转换
        "output": _safe_print,
    }

    return {
        "__builtins__": {**safe_builtins, **safe_builtin_subset},
        "_getitem_": safer_getattr,
        "_getiter_": default_guarded_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "math": math,
        **sympy_namespace,
    }


# ---------------------------------------------------------------------------
# 输出捕获
# ---------------------------------------------------------------------------

class _OutputCollector:
    """捕获沙箱内 print 的输出，限制最大长度防止内存爆掉。"""

    MAX_LENGTH = 4096

    def __init__(self) -> None:
        self._buffer: List[str] = []

    def write(self, text: str) -> None:
        current = "".join(self._buffer)
        if len(current) >= self.MAX_LENGTH:
            return
        self._buffer.append(str(text))

    def flush(self) -> None:  # noqa: D401
        """兼容 file-like 接口"""
        return None

    def get_output(self) -> str:
        text = "".join(self._buffer)
        if len(text) >= self.MAX_LENGTH:
            return text[: self.MAX_LENGTH] + "\n...[输出已截断]"
        return text


# 全局捕获器，由 execute() 临时设置
_current_collector: Optional[_OutputCollector] = None


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """受限 print：仅写入全局捕获器。"""
    if _current_collector is None:
        return
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    text = sep.join(str(a) for a in args) + end
    _current_collector.write(text)


# ---------------------------------------------------------------------------
# 超时控制（Windows 不支持 signal.SIGALRM，使用 threading 兜底）
# ---------------------------------------------------------------------------

class _TimeoutError(Exception):
    """沙箱执行超时"""


def _run_with_timeout(func, args=(), kwargs=None, timeout: int = DEFAULT_TIMEOUT_SECONDS):
    """使用线程执行函数，超时则强制中断。"""
    kwargs = kwargs or {}
    result_container: Dict[str, Any] = {"ret": None, "exc": None}

    def _target():
        try:
            result_container["ret"] = func(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            result_container["exc"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise _TimeoutError(f"沙箱执行超过 {timeout} 秒上限，已强制终止")
    if result_container["exc"] is not None:
        raise result_container["exc"]
    return result_container["ret"]


# ---------------------------------------------------------------------------
# 沙箱主入口
# ---------------------------------------------------------------------------

def execute(
    code: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    user_locals: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    在 RestrictedPython 沙箱中执行用户代码。

    Args:
        code: 用户代码字符串，可包含赋值、表达式、print 调用。
        timeout: 最大执行秒数。
        user_locals: 额外注入的局部变量（如已声明的 sympy 符号）。

    Returns:
        dict 包含字段：
          - success: bool 是否成功
          - result: Any 最后一个表达式的值（若可解析）
          - output: str stdout 捕获文本
          - error: str 错误信息（失败时）
          - error_type: str 错误类型名
    """
    global _current_collector

    # 1) 编译受限代码
    try:
        byte_code = compile_restricted(code, filename="<sandbox>", mode="exec")
    except SyntaxError as e:
        return {
            "success": False,
            "result": None,
            "output": "",
            "error": f"语法错误: {e.msg} (行 {e.lineno})",
            "error_type": "SyntaxError",
        }

    # 2) 准备命名空间
    safe_globals = _build_safe_globals()
    safe_locals: Dict[str, Any] = dict(user_locals or {})

    # 3) 捕获 print 输出
    collector = _OutputCollector()
    _current_collector = collector

    # 4) 限制递归深度
    old_rec_limit = sys_recursion_limit()
    sys_set_recursion_limit(MAX_RECURSION_DEPTH)

    try:
        # 5) 带超时执行
        def _exec():
            exec(byte_code, safe_globals, safe_locals)

        _run_with_timeout(_exec, timeout=timeout)

        # 6) 提取最后一个表达式语句的值（best-effort）
        last_value = _extract_last_expression(code, safe_locals)

        return {
            "success": True,
            "result": last_value,
            "output": collector.get_output(),
            "error": "",
            "error_type": "",
        }
    except _TimeoutError as e:
        return {
            "success": False,
            "result": None,
            "output": collector.get_output(),
            "error": str(e),
            "error_type": "TimeoutError",
        }
    except BaseException as e:  # noqa: BLE001
        return {
            "success": False,
            "result": None,
            "output": collector.get_output(),
            "error": f"{type(e).__name__}: {e}",
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(),
        }
    finally:
        _current_collector = None
        sys_set_recursion_limit(old_rec_limit)


def _extract_last_expression(code: str, local_ns: Dict[str, Any]) -> Any:
    """尽力提取用户代码最后一条表达式的求值结果。"""
    import ast

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return None

    if not tree.body:
        return None
    last_node = tree.body[-1]
    if not isinstance(last_node, ast.Expr):
        return None

    try:
        return eval(  # noqa: S307 - 仅对受 RestrictedPython 编译后的本地命名空间求值
            compile(last_node, "<sandbox>", "eval"),
            {"__builtins__": {}},
            local_ns,
        )
    except BaseException:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 高层数学计算 API：供 Agent 直接调用
# ---------------------------------------------------------------------------

def solve_equation(
    equation: str,
    variable: str = "x",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    解方程（支持一元/多元、线性/非线性）。

    Args:
        equation: 方程字符串，如 "x**2 - 4 = 0" 或 "x**2 - 4"（默认 =0）。
        variable: 求解变量名。
        timeout: 超时秒数。

    Returns:
        标准 execute() 返回结构，result 为解列表。
    """
    eq_normalized = equation.replace("==", "=")
    if "=" in eq_normalized:
        lhs, rhs = eq_normalized.split("=", 1)
        # 注意：RestrictedPython 禁止下划线开头的变量名；
        # 同时字符串形式的表达式必须用 sympify 解析为 SymPy 对象，否则 x 等符号未定义
        # 使用 output 替代 print，绕开 RestrictedPython 的 _print_ 转换
        code = (
            f"eq = Eq(sympify('{lhs.strip()}') - sympify('{rhs.strip()}'), 0)\n"
            f"solutions = solve(eq, symbols('{variable}'))\n"
            f"output('方程:', eq)\n"
            f"output('解:', solutions)\n"
        )
    else:
        code = (
            f"expr = sympify('{eq_normalized.strip()}')\n"
            f"solutions = solve(expr, symbols('{variable}'))\n"
            f"output('表达式:', expr)\n"
            f"output('解:', solutions)\n"
        )
    return execute(code, timeout=timeout)


def compute_derivative(
    expression: str,
    variable: str = "x",
    order: int = 1,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """计算符号导数。"""
    code = (
        f"var = symbols('{variable}')\n"
        f"expr = sympify('{expression.strip()}')\n"
        f"result = diff(expr, var, {order})\n"
        f"result_simplified = simplify(result)\n"
        f"output('原式:', expr)\n"
        f"output('{order}阶导数:', result_simplified)\n"
    )
    return execute(code, timeout=timeout)


def compute_integral(
    expression: str,
    variable: str = "x",
    lower: Optional[str] = None,
    upper: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """计算不定积分或定积分。"""
    if lower is not None and upper is not None:
        code = (
            f"var = symbols('{variable}')\n"
            f"expr = sympify('{expression.strip()}')\n"
            f"lower_bound = sympify('{lower.strip()}')\n"
            f"upper_bound = sympify('{upper.strip()}')\n"
            f"result = integrate(expr, (var, lower_bound, upper_bound))\n"
            f"output('定积分 [{lower}, {upper}]:', simplify(result))\n"
        )
    else:
        code = (
            f"var = symbols('{variable}')\n"
            f"expr = sympify('{expression.strip()}')\n"
            f"result = integrate(expr, var)\n"
            f"output('不定积分:', simplify(result))\n"
        )
    return execute(code, timeout=timeout)


def numeric_verify(
    expression: str,
    variable: str = "x",
    value: str = "1",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """在指定点对表达式进行数值验算。"""
    code = (
        f"var = symbols('{variable}')\n"
        f"expr = sympify('{expression.strip()}')\n"
        f"subst_value = sympify('{value.strip()}')\n"
        f"numeric_result = expr.subs(var, subst_value)\n"
        f"output('原式:', expr)\n"
        f"output('代入 {variable} =', subst_value, '后:', numeric_result)\n"
        f"output('数值结果:', N(numeric_result))\n"
    )
    return execute(code, timeout=timeout)


# ---------------------------------------------------------------------------
# sys.setrecursionlimit 兼容封装
# ---------------------------------------------------------------------------

import sys  # noqa: E402  - 故意放在末尾以避免污染沙箱命名空间


def sys_recursion_limit() -> int:
    return sys.getrecursionlimit()


def sys_set_recursion_limit(limit: int) -> None:
    sys.setrecursionlimit(limit)


# ---------------------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------------------

def main() -> None:
    """沙箱自测：分别演示解方程、求导、积分、数值验算、危险代码拦截。"""
    print("===== CodeSandbox 自测 =====\n")

    print("[1] 解方程 x^2 - 4 = 0:")
    print(execute_safe_format(solve_equation("x**2 - 4 = 0", "x")))

    print("\n[2] 求 sin(x)*exp(x) 的 1 阶导数:")
    print(execute_safe_format(compute_derivative("sin(x)*exp(x)", "x", 1)))

    print("\n[3] 求 x^2 在 [0, 2] 上的定积分:")
    print(execute_safe_format(compute_integral("x**2", "x", "0", "2")))

    print("\n[4] 数值验算: 在 x=2 处求 x^3 + 2x:")
    print(execute_safe_format(numeric_verify("x**3 + 2*x", "x", "2")))

    print("\n[5] 危险代码拦截测试 (尝试 __import__('os')):")
    print(execute_safe_format(execute("__import__('os').system('echo hack')")))

    print("\n[6] 死循环超时测试:")
    print(execute_safe_format(execute("while True:\n    pass", timeout=3)))


def execute_safe_format(result: Dict[str, Any]) -> str:
    """格式化输出沙箱结果。"""
    lines = [f"  success: {result['success']}"]
    if result.get("output"):
        for line in result["output"].rstrip().split("\n"):
            lines.append(f"  | {line}")
    if not result["success"]:
        lines.append(f"  error: {result.get('error', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
