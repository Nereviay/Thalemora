"""低语只在明确授权的 private 域保存与浏览。"""
from thalemora.identity.registry import AccessDenied
from thalemora.inner_world.content import AgentContent


class Whisper(AgentContent):
    kind = "whisper"
    nature = "private_whisper"

    def __init__(self, store):
        if store.session.channel != "private":
            raise AccessDenied("Whisper 需要 private 通道授权")
        super().__init__(store)
