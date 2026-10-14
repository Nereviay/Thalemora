"""仅供可信宿主调用的身份核心；principal 必须来自宿主认证，不能取自请求体。"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4


class AccessDenied(PermissionError):
    """主体不拥有 Agent，或来源没有指定域的授权。"""


class InvalidIdentity(ValueError):
    """身份或授权参数无效。"""


@dataclass(frozen=True)
class OwnerPrincipal:
    """宿主已认证的用户主体；此对象不是客户端身份声明或认证凭据。"""

    principal_id: str

    def __post_init__(self):
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise InvalidIdentity("principal_id 必须是非空字符串")


@dataclass(frozen=True)
class Agent:
    agent_id: str
    display_name: str


class IdentityRegistry:
    """持久保存身份与显式授权；root 由宿主配置，所有路径均为绝对路径。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.sqlite"
        with closing(sqlite3.connect(self.registry_path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    archive_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                    app_id TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('main', 'private')),
                    PRIMARY KEY (agent_id, app_id, channel)
                );
            """)

    @staticmethod
    def _principal(owner: OwnerPrincipal) -> str:
        if not isinstance(owner, OwnerPrincipal):
            raise InvalidIdentity("owner 必须是宿主提供的 OwnerPrincipal")
        return owner.principal_id

    @staticmethod
    def _source(app_id: str, channel: str) -> None:
        if not isinstance(app_id, str) or not app_id.strip():
            raise InvalidIdentity("app_id 必须是非空字符串")
        if channel not in ("main", "private"):
            raise InvalidIdentity("channel 必须是 main 或 private")

    def _agent_path(self, db, owner: OwnerPrincipal, agent_id: str) -> Path:
        principal_id = self._principal(owner)
        try:
            if not isinstance(agent_id, str) or str(UUID(agent_id)) != agent_id:
                raise ValueError
        except ValueError as exc:
            raise InvalidIdentity("agent_id 必须是规范 UUID") from exc
        row = db.execute(
            "SELECT archive_path FROM agents WHERE agent_id = ? AND principal_id = ?",
            (agent_id, principal_id),
        ).fetchone()
        if row is None:
            raise AccessDenied("主体不拥有该 Agent")
        return Path(row[0])

    def register_agent(self, owner: OwnerPrincipal, display_name: str) -> Agent:
        principal_id = self._principal(owner)
        if not isinstance(display_name, str) or not display_name.strip():
            raise InvalidIdentity("display_name 必须是非空字符串")
        agent_id = str(uuid4())
        archive_path = self.root / "agents" / agent_id / "archive.sqlite"
        archive_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(archive_path)):
            pass
        with closing(sqlite3.connect(self.registry_path)) as db, db:
            db.execute(
                "INSERT INTO agents VALUES (?, ?, ?, ?)",
                (agent_id, principal_id, display_name, str(archive_path)),
            )
        return Agent(agent_id, display_name)

    def grant_source(self, owner: OwnerPrincipal, agent_id: str, app_id: str,
                     channel: str = "main") -> None:
        self._source(app_id, channel)
        with closing(sqlite3.connect(self.registry_path)) as db, db:
            self._agent_path(db, owner, agent_id)
            db.execute(
                "INSERT INTO sources VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (agent_id, app_id, channel),
            )

    def assert_source(self, owner: OwnerPrincipal, agent_id: str, app_id: str,
                      channel: str = "main") -> None:
        self._source(app_id, channel)
        with closing(sqlite3.connect(self.registry_path)) as db:
            self._agent_path(db, owner, agent_id)
            row = db.execute(
                "SELECT 1 FROM sources WHERE agent_id = ? AND app_id = ? AND channel = ?",
                (agent_id, app_id, channel),
            ).fetchone()
        if row is None:
            raise AccessDenied("来源未获该 Agent 的指定域授权")

    def archive_db_path(self, owner: OwnerPrincipal, agent_id: str) -> Path:
        with closing(sqlite3.connect(self.registry_path)) as db:
            return self._agent_path(db, owner, agent_id)
