"""Unit tests for the worker claim assignee filter (GitHubBus.list_issues assignee=)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.github_bus import GitHubBus  # noqa: E402


def _make_bus(tmp: Path) -> GitHubBus:
    mock_dir = tmp / "mock"
    mock_dir.mkdir()
    store = {
        "prs": {},
        "labels": {},
        "projects": {},
        "next_id": 4,
        "issues": {
            "2": {
                "number": 2,
                "title": "[E1-T1] lab task",
                "body": "AREA: lab",
                "state": "OPEN",
                "labels": [{"name": "status:ready"}, {"name": "type:task"}],
                "comments": [],
                "assignees": [{"login": "alice"}],
            },
            "3": {
                "number": 3,
                "title": "[E1-T10] studio task",
                "body": "AREA: studio",
                "state": "OPEN",
                "labels": [{"name": "status:ready"}, {"name": "type:task"}],
                "comments": [],
                "assignees": [{"login": "thomas"}],
            },
            "4": {
                "number": 4,
                "title": "[E1-T11] unassigned task",
                "body": "no assignee",
                "state": "OPEN",
                "labels": [{"name": "status:ready"}, {"name": "type:task"}],
                "comments": [],
            },
        },
    }
    (mock_dir / "github_state.json").write_text(json.dumps(store))
    return GitHubBus(repo="acme/widgets", cwd=tmp, mock_dir=mock_dir)


class AssigneeFilterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bus = _make_bus(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_filter_returns_only_matching_assignee(self):
        nums = [i["number"] for i in self.bus.list_issues(label="status:ready", assignee="alice")]
        self.assertEqual(nums, [2])

    def test_filter_is_case_insensitive(self):
        nums = [i["number"] for i in self.bus.list_issues(label="status:ready", assignee="ALICE")]
        self.assertEqual(nums, [2])

    def test_no_filter_returns_all(self):
        nums = [i["number"] for i in self.bus.list_issues(label="status:ready")]
        self.assertEqual(nums, [2, 3, 4])

    def test_unassigned_issue_never_matches_filter(self):
        nums = [i["number"] for i in self.bus.list_issues(label="status:ready", assignee="nobody")]
        self.assertEqual(nums, [])


if __name__ == "__main__":
    unittest.main()
