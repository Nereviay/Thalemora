"""每 Agent 一个 Persona authority；external 仅存引用，永不接管正式 Prompt。"""
import json

from thalemora.identity.registry import AccessDenied
from thalemora.inner_world.host import page_size, timestamp


class Persona:
    def __init__(self, store):
        self.store = store
        self.session = store.session
        self._check()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS persona_versions (
                    version INTEGER PRIMARY KEY, authority TEXT NOT NULL,
                    content TEXT, external_ref TEXT, external_revision TEXT,
                    audit TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS persona_epochs (
                    epoch_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    audit TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
            """)

    def _check(self):
        self.session.check()
        if self.session.channel != "main":
            raise AccessDenied("Agent 的 Persona 配置须由 main 授权访问")

    def _audit(self, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Persona 变更/绑定须有理由")
        return json.dumps({"principal_id": self.session.owner.principal_id,
                           "app_id": self.session.app_id, "role": self.session.role,
                           "reason": reason}, ensure_ascii=False)

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError("Persona 尚未配置或绑定")
        result = dict(zip(("version", "authority", "content", "external_ref",
                           "external_revision", "audit", "recorded_at"), row))
        result["audit"] = json.loads(result["audit"])
        return result

    def configure(self, *, authority, expected_version, reason, recorded_at,
                  content=None, external_ref=None, external_revision=None):
        self._check()
        timestamp(recorded_at)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version 无效")
        if authority == "managed":
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 16000 or external_ref is not None or external_revision is not None:
                raise ValueError("managed 仅接受非空本地配置")
        elif authority == "external":
            if content is not None or any(not isinstance(x, str) or not x.strip() for x in (external_ref, external_revision)):
                raise ValueError("external 仅接受外部 authority 引用和版本")
        else:
            raise ValueError("authority 必须为 managed 或 external")
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM persona_versions").fetchone()[0]
            if version != expected_version:
                raise ValueError("Persona 版本冲突")
            db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?)",
                       (version + 1, authority, content, external_ref, external_revision,
                        self._audit(reason), recorded_at))
        return self.get(version=version + 1)

    def get(self, *, version=None):
        self._check()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM persona_versions ORDER BY version DESC LIMIT 1").fetchone() if version is None else db.execute(
                "SELECT * FROM persona_versions WHERE version=?", (version,)).fetchone()
        return self._decode(row)

    def history(self, *, after=0, limit=20):
        self._check()
        page_size(limit)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM persona_versions WHERE version>? ORDER BY version LIMIT ?",
                              (after, limit)).fetchall()
        return [self._decode(row) for row in rows]

    def bind_epoch(self, epoch_id, *, reason, recorded_at):
        self._check()
        self.session.require_epoch(epoch_id)
        timestamp(recorded_at)
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT MAX(version) FROM persona_versions").fetchone()[0]
            if version is None:
                raise KeyError("Persona 尚未配置")
            db.execute("INSERT OR IGNORE INTO persona_epochs VALUES(?,?,?,?)",
                       (epoch_id, version, self._audit(reason), recorded_at))
        return self.for_epoch(epoch_id)

    def for_epoch(self, epoch_id):
        self._check()
        with self.store.connect() as db:
            row = db.execute("SELECT p.* FROM persona_epochs e JOIN persona_versions p USING(version) "
                             "WHERE e.epoch_id=?", (epoch_id,)).fetchone()
        return self._decode(row)
