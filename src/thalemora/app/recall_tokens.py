"""独立召回凭据；明文仅签发时返回，作用域复用身份 registry。"""
from contextlib import closing
from hashlib import sha256
import secrets
import sqlite3


class InvalidRecallToken(PermissionError):
    pass


class RecallTokens:
    def __init__(self, registry, owner):
        self.registry, self.owner = registry, owner
        with closing(sqlite3.connect(registry.registry_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS recall_tokens (
                token_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                agent_id TEXT NOT NULL, app_id TEXT NOT NULL, channel TEXT NOT NULL)""")

    def issue(self, agent_id, app_id, channel="main"):
        self.registry.assert_source(self.owner, agent_id, app_id, channel)
        token_id, token = secrets.token_hex(8), secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.registry.registry_path)) as db, db:
            db.execute("INSERT INTO recall_tokens VALUES (?,?,?,?,?)",
                       (token_id, sha256(token.encode()).hexdigest(), agent_id, app_id, channel))
        return dict(token_id=token_id, token=token, agent_id=agent_id, app_id=app_id, channel=channel)

    def revoke(self, token_id):
        if not isinstance(token_id, str) or not token_id:
            raise ValueError("token_id 须为非空字符串")
        with closing(sqlite3.connect(self.registry.registry_path)) as db, db:
            deleted = db.execute("DELETE FROM recall_tokens WHERE token_id=?", (token_id,)).rowcount
        return {"revoked": bool(deleted)}

    def resolve(self, token):
        with closing(sqlite3.connect(self.registry.registry_path)) as db:
            row = db.execute("SELECT agent_id,app_id,channel FROM recall_tokens WHERE token_hash=?",
                             (sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise InvalidRecallToken("召回凭据无效或已撤销")
        self.registry.assert_source(self.owner, *row)
        return dict(zip(("agent_id", "app_id", "channel"), row))
