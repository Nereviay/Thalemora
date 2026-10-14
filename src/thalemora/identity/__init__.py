"""本地 Agent 身份与来源授权。"""

from .registry import AccessDenied, Agent, IdentityRegistry, InvalidIdentity, OwnerPrincipal

__all__ = ["AccessDenied", "Agent", "IdentityRegistry", "InvalidIdentity", "OwnerPrincipal"]
