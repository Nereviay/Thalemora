"""本人日记，按事件分段、版本化保存。"""
from thalemora.inner_world.content import AgentContent


class Diary(AgentContent):
    kind = "diary"
    nature = "diary"
