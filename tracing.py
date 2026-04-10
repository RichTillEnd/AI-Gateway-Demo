"""
Trace ID — 跨模組共用的 ContextVar。
由 main.py TraceIdMiddleware 在每個 request 開始時設定，
其他模組只需 import get_trace_id() 讀取。
"""
import contextvars

_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


def get_trace_id() -> str:
    return _trace_id_var.get()
