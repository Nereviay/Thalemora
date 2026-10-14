"""P2-A：仅使用临时目录与合成身份、事件。"""

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thalemora.identity import AccessDenied, IdentityRegistry, InvalidIdentity, OwnerPrincipal


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = IdentityRegistry(self.temp.name)
        self.owner = OwnerPrincipal("owner-a")
        self.other = OwnerPrincipal("owner-b")
        self.agent1 = self.registry.register_agent(self.owner, "同名 Agent")
        self.agent2 = self.registry.register_agent(self.owner, "同名 Agent")

    def test_same_name_has_distinct_ids_and_isolated_databases(self):
        self.assertEqual(self.agent1.display_name, self.agent2.display_name)
        self.assertNotEqual(self.agent1.agent_id, self.agent2.agent_id)
        paths = []
        for agent, value in ((self.agent1, "合成甲"), (self.agent2, "合成乙")):
            self.assertEqual(str(UUID(agent.agent_id)), agent.agent_id)
            path = self.registry.archive_db_path(self.owner, agent.agent_id)
            self.assertTrue(path.is_absolute())
            self.assertEqual(path, Path(self.temp.name) / "agents" / agent.agent_id / "archive.sqlite")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE synthetic_events (source_event_id TEXT PRIMARY KEY, body TEXT)")
                db.execute("INSERT INTO synthetic_events VALUES (?, ?)", ("same-event", value))
            paths.append(path)
        self.assertNotEqual(*paths)
        for path, value in zip(paths, ("合成甲", "合成乙")):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT body FROM synthetic_events WHERE source_event_id = ?", ("same-event",)).fetchone(), (value,))

    def test_cross_principal_is_denied(self):
        agent_id = self.agent1.agent_id
        self.registry.grant_source(self.owner, agent_id, "app-a")
        for call in (
            lambda: self.registry.archive_db_path(self.other, agent_id),
            lambda: self.registry.grant_source(self.other, agent_id, "app-b"),
            lambda: self.registry.assert_source(self.other, agent_id, "app-a"),
        ):
            with self.assertRaises(AccessDenied):
                call()

    def test_multiple_apps_require_agent_specific_grants(self):
        for app in ("app-a", "app-b"):
            self.registry.grant_source(self.owner, self.agent1.agent_id, app)
            self.registry.assert_source(self.owner, self.agent1.agent_id, app)
            with self.assertRaises(AccessDenied):
                self.registry.assert_source(self.owner, self.agent2.agent_id, app)
        with self.assertRaises(AccessDenied):
            self.registry.assert_source(self.owner, self.agent1.agent_id, "app-c")
        self.registry.grant_source(self.owner, self.agent1.agent_id, "app-a")

    def test_private_is_explicit_and_scoped(self):
        agent_id = self.agent1.agent_id
        self.registry.grant_source(self.owner, agent_id, "app-a")
        with self.assertRaises(AccessDenied):
            self.registry.assert_source(self.owner, agent_id, "app-a", "private")
        self.registry.grant_source(self.owner, agent_id, "app-a", "private")
        self.registry.assert_source(self.owner, agent_id, "app-a", "private")
        for other_id, app in ((agent_id, "app-b"), (self.agent2.agent_id, "app-a")):
            with self.assertRaises(AccessDenied):
                self.registry.assert_source(self.owner, other_id, app, "private")

    def test_restart_preserves_ids_paths_and_grants(self):
        agent_id = self.agent1.agent_id
        path = self.registry.archive_db_path(self.owner, agent_id)
        self.registry.grant_source(self.owner, agent_id, "app-a", "private")
        restarted = IdentityRegistry(self.temp.name)
        self.assertEqual(restarted.archive_db_path(self.owner, agent_id), path)
        restarted.assert_source(self.owner, agent_id, "app-a", "private")
        with self.assertRaises(AccessDenied):
            restarted.assert_source(self.owner, agent_id, "app-a")

    def test_invalid_ids_and_unknown_agents_are_rejected(self):
        for agent_id in ("../outside", "", None):
            with self.assertRaises(InvalidIdentity):
                self.registry.archive_db_path(self.owner, agent_id)
        with self.assertRaises(AccessDenied):
            self.registry.archive_db_path(self.owner, str(uuid4()))
        with self.assertRaises(InvalidIdentity):
            self.registry.archive_db_path("owner-a", self.agent1.agent_id)
        with self.assertRaises(InvalidIdentity):
            self.registry.grant_source(self.owner, self.agent1.agent_id, "app-a", "unknown")


if __name__ == "__main__":
    unittest.main()
