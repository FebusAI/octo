"""
Unit & Integration Tests for Swarm Protocol & Multiplayer Overlap Features (`tests/test_swarm_and_multiplayer.py`).
Tests:
- SwarmBoard monotonic sequencing, mention targeting, and channel routing
- OverlapDetector direct file collision and AST import interface dependency detection
- C2CCoordinator peer registration, message dispatch, and auto-negotiation
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.c2c_coordinator import C2CCoordinator
from orchestrator.config import TaskSpec
from orchestrator.github_bus import GitHubBus
from orchestrator.overlap_detector import OverlapDetector
from orchestrator.swarm_board import SwarmBoard


class TestSwarmAndMultiplayer(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="octo-swarm-test-"))
        self.mock_gh_dir = self.tmp_dir / "mock_gh"
        self.bus = GitHubBus(repo="test/repo", config={}, mock_dir=self.mock_gh_dir)
        self.board = SwarmBoard(repo_root=self.tmp_dir, swarm_id="test_swarm")
        self.detector = OverlapDetector(repo_root=self.tmp_dir)
        self.c2c = C2CCoordinator(
            repo_root=self.tmp_dir,
            bus=self.bus,
            board=self.board,
            coordinator_id="coordinator-alpha",
            node_id="macbook",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_swarm_board_routing_and_mentions(self):
        """Verifies that the Swarm Board delivers messages and filters by recipient and channel."""
        m1 = self.board.post(sender="coordinator", recipient="@parser", text="@parser implement lexer", channel="main")
        m2 = self.board.post(sender="parser", recipient="@verifier", text="@verifier [done] parser ready", channel="qa")
        m3 = self.board.post(sender="coordinator", recipient="@all", text="@all sync interface contract", channel="main")

        self.assertEqual(m1.seq, 1)
        self.assertEqual(m2.seq, 2)
        self.assertEqual(m3.seq, 3)
        self.assertTrue(m2.is_done)

        # Filter by recipient @parser
        msgs_for_parser = self.board.read(since_seq=0, recipient="parser", mentions_only=True)
        # Should receive m1 (@parser) and m3 (@all)
        recipients = [m.recipient for m in msgs_for_parser]
        self.assertIn("@parser", recipients)
        self.assertIn("@all", recipients)

        # Filter by channel "qa"
        qa_msgs = self.board.read(since_seq=0, channel="qa")
        self.assertEqual(len(qa_msgs), 1)
        self.assertEqual(qa_msgs[0].recipient, "@verifier")

    def test_02_overlap_detector_direct_collision(self):
        """Verifies that two tasks targeting the same file trigger a CRITICAL_COLLISION alert."""
        tasks = [
            {
                "number": 10,
                "title": "Auth Refactor",
                "body": "<!-- OCTO-META:TASK_SPEC {\"target_files\": [\"src/auth.py\", \"src/models.py\"]} -->",
                "labels": [{"name": "status:in-progress"}],
                "claim": {"node_id": "worker-node-1"},
            },
            {
                "number": 11,
                "title": "Add OAuth Endpoint",
                "body": "<!-- OCTO-META:TASK_SPEC {\"target_files\": [\"src/auth.py\", \"src/routes.py\"]} -->",
                "labels": [{"name": "status:ready"}],
                "claim": {"node_id": "worker-node-2"},
            },
        ]

        reports = self.detector.check_overlaps(tasks, worktrees_root=self.tmp_dir / "worktrees")
        collisions = [r for r in reports if r.level == "CRITICAL_COLLISION"]
        self.assertEqual(len(collisions), 1)
        self.assertIn(10, collisions[0].conflicting_tasks)
        self.assertIn(11, collisions[0].conflicting_tasks)
        self.assertIn("src/auth.py", collisions[0].conflicting_files)

    def test_03_overlap_detector_interface_dependency(self):
        """Verifies that if Task A modifies a module and Task B imports it, INTERFACE_DEPENDENCY is flagged."""
        # Create a mock source tree in tmp_dir
        src_dir = self.tmp_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
        (src_dir / "api.py").write_text("import models\n\ndef get_user(): return models.User()\n", encoding="utf-8")

        tasks = [
            {
                "number": 20,
                "title": "Schema Migration",
                "body": "<!-- OCTO-META:TASK_SPEC {\"target_files\": [\"src/models.py\"]} -->",
                "labels": [{"name": "status:in-progress"}],
                "claim": {"node_id": "worker-node-3"},
            },
            {
                "number": 21,
                "title": "API Controller",
                "body": "<!-- OCTO-META:TASK_SPEC {\"target_files\": [\"src/api.py\"]} -->",
                "labels": [{"name": "status:in-progress"}],
                "claim": {"node_id": "macbook"},
            },
        ]

        reports = self.detector.check_overlaps(tasks, worktrees_root=self.tmp_dir / "worktrees")
        deps = [r for r in reports if r.level == "INTERFACE_DEPENDENCY"]
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0].metadata["module"], "models")

    def test_04_c2c_coordinator_registration_and_negotiation(self):
        """Verifies peer coordinator discovery and automated overlap negotiation."""
        ident = self.c2c.register(active_epic=42, goal_title="Payment Gateway", owned_files=["src/payment.py"])
        self.assertEqual(ident.coordinator_id, "coordinator-alpha")

        active_coords = self.c2c.list_active_coordinators()
        self.assertEqual(len(active_coords), 1)
        self.assertEqual(active_coords[0].active_epic, 42)

        # Test sending C2C message
        msg = self.c2c.send_message(
            target_coordinator="coordinator-beta",
            kind="HEADS_UP",
            subject="Modifying Payment Webhook Schema",
            body="Be aware that webhook payload now contains token signature.",
            metadata={"epic_number": 42},
        )
        self.assertEqual(msg.kind, "HEADS_UP")

        # Check that it appeared on Swarm Board channel #c2c
        c2c_history = self.c2c.get_c2c_history()
        self.assertTrue(any("Payment Webhook" in str(m.get("text")) for m in c2c_history))


if __name__ == "__main__":
    unittest.main()
