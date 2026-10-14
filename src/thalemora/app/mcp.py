"""官方 MCP stdio 薄桥；可信本地进程上下文与 HTTP 共用 Application。"""
from mcp.server.fastmcp import FastMCP


def create_mcp(service):
    server = FastMCP("Thalemora", instructions="授权历史与内在状态；app/channel 必须显式授权。")

    @server.tool()
    def list_agents(limit: int = 20, offset: int = 0) -> list:
        return service.list_agents(limit=limit, offset=offset)

    @server.tool()
    def search(agent_id: str, app_id: str, query: str, channel: str = "main", limit: int = 20, offset: int = 0) -> list:
        return service.search(agent_id, app_id, query, channel, limit=limit, offset=offset)

    @server.tool()
    def history(agent_id: str, app_id: str, channel: str = "main", limit: int = 20, offset: int = 0) -> list:
        return service.history(agent_id, app_id, channel, limit=limit, offset=offset)

    @server.tool()
    def inner_state(agent_id: str, app_id: str, kind: str, channel: str = "main", limit: int = 20, offset: int = 0) -> list:
        return service.inner_state(agent_id, app_id, kind, channel, limit=limit, offset=offset)

    return server
