"""梦境明确标记为梦境，不能充当真实经历。"""
from thalemora.inner_world.content import AgentContent


class Dream(AgentContent):
    kind = "dream"
    nature = "dream_not_real_experience"
