"""官方 MCP 薄桥；stdio 与受认证的 Streamable HTTP 共用 Application。"""
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

    @server.tool()
    def memory(agent_id: str, app_id: str, channel: str = "main", limit: int = 20, offset: int = 0) -> list:
        """分页读取已授权域的记忆及当前修订 ID。"""
        return service.memory(agent_id, app_id, channel, limit=limit, offset=offset)

    @server.tool()
    def evidence(agent_id: str, app_id: str, card_id: int, channel: str = "main", limit: int = 20, offset: int = 0) -> list:
        """读取记忆证据和当前来源状态；人工来源不冒充 verified。"""
        return service.evidence(agent_id, app_id, card_id, channel, limit=limit, offset=offset)

    @server.tool()
    def import_history(agent_id: str, app_id: str, channel: str, events: list[dict]) -> list:
        """导入 1..100 个保留来源的 envelope；app/channel 须匹配显式请求域。

        保留 source_event_id、version、role、text、occurred_at、conversation_id、
        epoch_id、content_hash、hash_verification、raw_event_id、deleted。
        这是授权档案导入，不会自动运行 Writer 或生成记忆。
        """
        return service.import_history(agent_id, app_id, events, channel)

    @server.tool()
    def remember(agent_id: str, app_id: str, source_event_id: str, channel: str = "main") -> dict:
        """显式将已有的有效来源事件原文存为人工记忆，保留来源证据。"""
        return service.remember(agent_id, app_id, source_event_id, channel)

    @server.tool()
    def revise_memory(agent_id: str, app_id: str, card_id: int, content: str,
                      expected_current_revision_id: int, channel: str = "main") -> dict:
        """明确的人工正文修订；须提供当前修订 ID，冲突失败，不充当 Agent 作者。"""
        return service.revise_memory(agent_id, app_id, card_id, content,
                                     expected_current_revision_id, channel)

    return server
