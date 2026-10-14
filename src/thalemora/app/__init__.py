"""独立应用唯一业务入口；HTTP、MCP 和可信 Python 宿主共用。"""

from .service import Application

__all__ = ["Application"]
