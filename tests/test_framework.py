"""
Comprehensive Verification Suite (`tests/test_framework.py`).
Tests the Distributed Multi-Agent GitHub Kanban & Multi-Harness Orchestrator
in an isolated sandbox (`OCTO_MOCK_DIR` + temporary worktrees).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import ProtocolParser, load_config, qualify_task_id
from orchestrator.dag_planner import DAGPlanner
from orchestrator.github_bus import GitHubBus
from orchestrator.harness_runner import HarnessRunner
from orchestrator.qa_auditor import QAAuditor
from orchestrator.worker_daemon import WorkerDaemon


SAMPLE_PLAN_MARKDOWN = """
# Architectural Overview
This plan builds a two-stage input validation pipeline.

## Goals & Non-Goals
- G1: Implement strict input sanitization
- G2: Verify edge cases with automated tests

```mermaid
flowchart TD
    T1["T1: Core Validator"] --> T2["T2: QA Test Suite"]
```

```json
{
  "epic_title": "Two-Stage Validation Pipeline",
  "summary": "Build core validator followed by automated QA test suite.",
  "goals": ["G1: Implement strict input sanitization", "G2: Verify edge cases"],
  "tasks": [
    {
      "id": "T1",
      "title": "Implement Core Input Validator",
      "role": "worker",
      "goal_refs": ["G1"],
      "depends_on": [],
      "target_files": ["validator.py"],
      "interfaces": ["sanitize_input(raw: str) -> str"],
      "edge_cases": ["Reject empty strings and null bytes"],
      "verification_cmd": "python3 -c 'print(123)'",
      "description": "Create validator.py with strict input sanitization.",
      "acceptance_criteria": ["Sanitizes SQL/Shell characters"]
    },
    {
      "id": "T2",
      "title": "Run Comprehensive QA Tests on Validator",
      "role": "qa",
      "goal_refs": ["G2"],
      "depends_on": ["T1"],
      "target_files": ["test_validator.py"],
      "interfaces": ["TestValidator(unittest.TestCase)"],
      "edge_cases": ["Verify malformed UTF-8 and oversized inputs"],
      "verification_cmd": "python3 -c 'print(456)'",
      "description": "Verify validator against edge cases and malformed inputs.",
      "acceptance_criteria": ["Zero test failures"]
    }
  ]
}
```
"""


class TestOrchestratorFramework(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="octo-test-"))
        self.mock_gh_dir = self.tmp_dir / "mock_gh"
        self.repo_root = Path(__file__).resolve().parent.parent
        self.config = load_config(self.repo_root)
        # Override worktree_dir and plans_dir so tests never mutate the real repo
        self.config.setdefault("project", {})["worktree_dir"] = str(self.tmp_dir / "worktrees")
        self.config.setdefault("project", {})["plans_dir"] = str(self.tmp_dir / "plans")
        # Add deterministic mock profiles
        self.config.setdefault("harnesses", {}).setdefault("profiles", {})["mock-worker"] = {
            "adapter": "mock",
            "script": "print('Implemented feature cleanly and verified all tests.')",
        }
        self.config["harnesses"]["profiles"]["mock-question"] = {
            "adapter": "mock",
            "script": "print('OCTO_QUESTION: Should token expiry be 15m or 60m?')",
        }

        self.bus = GitHubBus(repo="test/repo", config=self.config, mock_dir=self.mock_gh_dir)
        self.runner = HarnessRunner(config=self.config, repo_root=self.tmp_dir)
        self.planner = DAGPlanner(bus=self.bus, runner=self.runner, repo_root=self.tmp_dir, config=self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_plan_approval_gate_and_dag_spawning(self):
        """Verifies Phase 1 (awaiting-approval) and Phase 2 (Approve -> Epic-qualified ready/blocked DAG)."""
        tasks = self.planner.parse_tasks_from_markdown(SAMPLE_PLAN_MARKDOWN)
        self.assertEqual(len(tasks), 2)

        epic_num = self.planner.create_epic_for_approval(
            goal_title="Two-Stage Validation Pipeline",
            plan_markdown=SAMPLE_PLAN_MARKDOWN,
            tasks=tasks,
        )
        epic = self.bus.get_issue(epic_num)
        epic_labels = [l["name"] for l in epic["labels"]]
        self.assertIn("type:epic", epic_labels)
        self.assertIn("status:awaiting-approval", epic_labels)

        # Ensure no worker tasks exist before approval
        ready_before = self.bus.list_issues(label="status:ready")
        self.assertEqual(len(ready_before), 0)

        # Approve Epic (Phase 2 transition)
        spawned = self.planner.approve_and_spawn_dag(epic_num)
        self.assertEqual(set(spawned.keys()), {"T1", "T2"})

        t1_issue = self.bus.get_issue(spawned["T1"])
        t2_issue = self.bus.get_issue(spawned["T2"])

        self.assertTrue(t1_issue["title"].startswith(f"[E{epic_num}-T1] "))
        self.assertTrue(t2_issue["title"].startswith(f"[E{epic_num}-T2] "))

        t1_labels = [l["name"] for l in t1_issue["labels"]]
        t2_labels = [l["name"] for l in t2_issue["labels"]]

        self.assertIn("status:ready", t1_labels)
        self.assertIn(f"epic:{epic_num}", t1_labels)
        self.assertIn("status:blocked", t2_labels)
        self.assertIn(f"epic:{epic_num}", t2_labels)

    def test_02_multi_node_worker_claim_lease_and_recovery(self):
        """Verifies that Node A can claim a task, and if Node A's lease expires, Node B reaps and completes it."""
        tasks = self.planner.parse_tasks_from_markdown(SAMPLE_PLAN_MARKDOWN)
        epic_num = self.planner.create_epic_for_approval("Pipeline", SAMPLE_PLAN_MARKDOWN, tasks)
        spawned = self.planner.approve_and_spawn_dag(epic_num)
        t1_num = spawned["T1"]

        # Node A claims T1 with an already-expired TTL (-10s) to simulate a node going offline
        self.bus.try_claim_issue(
            issue_number=t1_num,
            node_id="worker-node-1",
            harness_profile="pi-local",
            model_name="openai/qwen2.5-coder:32b",
            ttl_seconds=-10,
            optimistic_wait=0,
        )
        self.bus.transition_issue_state(t1_num, "status:in-progress")

        # Node B runs worker daemon, reaps stale lease from Node A, claims T1, and submits PR
        worker_b = WorkerDaemon(
            bus=self.bus,
            runner=self.runner,
            repo_root=self.tmp_dir,
            config=self.config,
            node_id="worker-node-2",
            profile_name="mock-worker",
        )
        processed = worker_b.process_next_available_issue()
        self.assertEqual(processed, t1_num)

        t1_after = self.bus.get_issue(t1_num)
        labels_after = [l["name"] for l in t1_after["labels"]]
        self.assertIn("status:in-review", labels_after)

        # QA Auditor reviews T1, merges PR, closes T1, and DAG Planner unblocks T2
        auditor = QAAuditor(
            bus=self.bus,
            runner=self.runner,
            repo_root=self.tmp_dir,
            config=self.config,
            node_id="qa-node-1",
            qa_profile="mock-worker",
        )
        passed, _ = auditor.audit_issue_in_review(t1_num, run_llm_audit=False)
        self.assertTrue(passed)

        reconciled = self.planner.reconcile_dag_and_unblock()
        self.assertIn(spawned["T2"], reconciled["unblocked_tasks"])

        t2_after = self.bus.get_issue(spawned["T2"])
        t2_labels = [l["name"] for l in t2_after["labels"]]
        self.assertIn("status:ready", t2_labels)

    def test_03_harness_command_generation_across_adapters(self):
        """Verifies CLI command generation for `pi`, `claude`, and inline `model_override`."""
        cmd_pi, model_pi = self.runner.build_command(
            profile_name="pi-local",
            prompt="Implement feature X",
            workdir=self.tmp_dir,
            session_id="issue-42",
        )
        self.assertIn("--provider", cmd_pi)
        self.assertIn("openai", cmd_pi)
        self.assertIn("qwen2.5-coder:32b", cmd_pi)
        self.assertEqual(model_pi, "openai/qwen2.5-coder:32b")

        cmd_claude, model_claude = self.runner.build_command(
            profile_name="claude-cli",
            prompt="Audit PR #42",
            workdir=self.tmp_dir,
            read_only=True,
            model_override="opus",
        )
        self.assertIn("--model", cmd_claude)
        self.assertIn("opus", cmd_claude)
        self.assertIn("--tools", cmd_claude)
        self.assertEqual(model_claude, "opus")

    def test_04_code_safety_guardrail(self):
        """Verifies that QA Auditor catches destructive commands or leaked tokens in diffs."""
        auditor = QAAuditor(
            bus=self.bus,
            runner=self.runner,
            repo_root=self.tmp_dir,
            config=self.config,
            node_id="qa-auditor",
        )
        bad_diff = "+ os.system('rm -rf /')\n+ token = 'ghp_1234567890abcdefghijklmnopqrstuvwxyz'\n"
        violations = auditor.scan_code_safety(bad_diff)
        self.assertEqual(len(violations), 2)

    def test_05_agent_question_clarification_flow(self):
        """Verifies that a worker outputting OCTO_QUESTION transitions the issue to status:needs-clarification."""
        issue_num = self.bus.create_issue(
            title="[E1-T99] Ambiguous Spec Task",
            body="Implement token expiry.",
            labels=["type:task", "status:ready"],
        )
        worker = WorkerDaemon(
            bus=self.bus,
            runner=self.runner,
            repo_root=self.tmp_dir,
            config=self.config,
            node_id="worker-1",
            profile_name="mock-question",
        )
        processed = worker.process_next_available_issue()
        self.assertEqual(processed, issue_num)
        iss = self.bus.get_issue(issue_num)
        labels = [l["name"] for l in iss["labels"]]
        self.assertIn("status:needs-clarification", labels)

    def test_06_agent_markdown_loading_and_local_override(self):
        """Verifies loading `agents/*.md` frontmatter + prompt and merging `orchestrator.local.yaml`."""
        agents_dir = self.tmp_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "planner-architect.md").write_text(
            "---\n"
            "name: planner-architect\n"
            "harness: pi\n"
            "provider: local\n"
            "model: qwen-base\n"
            "thinking: high\n"
            "endpoint: http://localhost:11434/v1\n"
            "read_only: true\n"
            "---\n"
            "You are the test architect prompt.\n",
            encoding="utf-8",
        )
        (self.tmp_dir / "orchestrator.yaml").write_text(
            "version: '1.0'\nplanning:\n  rounds: 2\n",
            encoding="utf-8",
        )
        (self.tmp_dir / "orchestrator.local.yaml").write_text(
            "agents:\n  planner-architect:\n    model: qwen-local-override\n",
            encoding="utf-8",
        )

        merged_cfg = load_config(self.tmp_dir)
        runner = HarnessRunner(config=merged_cfg, repo_root=self.tmp_dir)
        info = runner.describe_target("planner")
        self.assertEqual(info["name"], "planner-architect")
        self.assertEqual(info["adapter"], "pi")
        self.assertEqual(info["model"], "qwen-local-override")
        self.assertEqual(info["endpoint"], "http://localhost:11434/v1")

        cmd, model_id = runner.build_command("planner", "Plan X", self.tmp_dir)
        self.assertEqual(model_id, "local/qwen-local-override")
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("--tools", cmd)
        self.assertIn("read,bash", cmd)

    def test_07_multi_agent_planning_loop_explorer_architect_critic(self):
        """Verifies Octo orchestrates Explorer -> Architect -> Critic -> Refiner loop and records trace."""
        agents_dir = self.tmp_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        draft_flag = self.tmp_dir / "architect_calls.txt"
        draft_json = json.dumps({
            "epic_title": "Refined Auth Module",
            "summary": "Modular auth",
            "tasks": [{
                "id": "T1",
                "title": "Build Auth Module",
                "role": "worker",
                "goal_refs": ["G1"],
                "depends_on": [],
                "target_files": ["auth.py"],
                "interfaces": ["verify_token(t: str) -> dict"],
                "edge_cases": ["Expired token raises ValueError"],
                "verification_cmd": "python3 -c 'print(1)'",
                "description": "Detailed auth module implementation.",
                "acceptance_criteria": ["Validates tokens"],
            }],
        })

        # Explorer mock agent
        (agents_dir / "planner-explorer.md").write_text(
            "---\nname: planner-explorer\nharness: mock\nmodel: explorer-m1\n"
            "script: \"print('Repository uses Python unittest.')\"\n---\nExplorer\n",
            encoding="utf-8",
        )
        # Architect mock agent (outputs plan JSON)
        (agents_dir / "planner-architect.md").write_text(
            f"---\nname: planner-architect\nharness: mock\nmodel: arch-m2\n"
            f"script: |\n  import pathlib\n  p = pathlib.Path({str(draft_flag)!r})\n"
            f"  n = int(p.read_text()) + 1 if p.exists() else 1\n  p.write_text(str(n))\n"
            f"  print('# Plan pass ' + str(n) + '\\n```json\\n' + {draft_json!r} + '\\n```')\n---\nArchitect\n",
            encoding="utf-8",
        )
        # Critic mock agent (requests revision so Architect refines once)
        (agents_dir / "planner-critic.md").write_text(
            "---\nname: planner-critic\nharness: mock\nmodel: critic-m3\n"
            "script: \"print('Add edge case handling. STATUS: NEEDS_REVISION')\"\n---\nCritic\n",
            encoding="utf-8",
        )

        cfg = dict(self.config)
        cfg["planning"] = {
            "rounds": 2,
            "agents": {
                "explorer": "planner-explorer",
                "architect": "planner-architect",
                "critic": "planner-critic",
            },
        }
        runner = HarnessRunner(config=cfg, repo_root=self.tmp_dir)
        planner = DAGPlanner(bus=self.bus, runner=runner, repo_root=self.tmp_dir, config=cfg)

        plan_md, tasks = planner.generate_plan_with_harness(goal="Build Auth")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].interfaces, ["verify_token(t: str) -> dict"])
        self.assertEqual(int(draft_flag.read_text()), 2)  # Draft + 1 Refiner pass

        steps = [row["step"] for row in planner.last_planning_trace]
        self.assertEqual(steps, ["1. Explore", "2. Architect Draft", "3.1 Critic", "3.1 Refine"])

        epic_num = planner.create_epic_for_approval("Build Auth", plan_md, tasks)
        epic_issue = self.bus.get_issue(epic_num)
        self.assertIn("Agent Planning Trace", epic_issue["body"])

    def test_08_epic_qualified_task_ids_prevent_collisions(self):
        """Verifies two Epics with identical local task IDs (T1, T2) spawn distinct [E1-T1] and [E4-T1] issues."""
        self.assertEqual(qualify_task_id(5, "T1"), "E5-T1")
        self.assertEqual(qualify_task_id(5, "E5-T1"), "E5-T1")

        tasks_1 = self.planner.parse_tasks_from_markdown(SAMPLE_PLAN_MARKDOWN)
        epic_1 = self.planner.create_epic_for_approval("Auth Feature", SAMPLE_PLAN_MARKDOWN, tasks_1)
        spawned_1 = self.planner.approve_and_spawn_dag(epic_1)

        tasks_2 = self.planner.parse_tasks_from_markdown(SAMPLE_PLAN_MARKDOWN)
        epic_2 = self.planner.create_epic_for_approval("Billing Feature", SAMPLE_PLAN_MARKDOWN, tasks_2)
        spawned_2 = self.planner.approve_and_spawn_dag(epic_2)

        iss_1_t1 = self.bus.get_issue(spawned_1["T1"])
        iss_2_t1 = self.bus.get_issue(spawned_2["T1"])
        iss_2_t2 = self.bus.get_issue(spawned_2["T2"])

        self.assertTrue(iss_1_t1["title"].startswith(f"[E{epic_1}-T1] "))
        self.assertTrue(iss_2_t1["title"].startswith(f"[E{epic_2}-T1] "))
        self.assertNotEqual(iss_1_t1["title"], iss_2_t1["title"])
        self.assertIn(f"epic:{epic_1}", [l["name"] for l in iss_1_t1["labels"]])
        self.assertIn(f"epic:{epic_2}", [l["name"] for l in iss_2_t1["labels"]])
        self.assertIn("Epic #", iss_2_t1["body"])
        self.assertIn("Billing Feature", iss_2_t1["body"])

        # Closing Epic 1's T1 unblocks Epic 1's T2, NOT Epic 2's T2
        self.bus.close_issue(spawned_1["T1"])
        reconciled = self.planner.reconcile_dag_and_unblock()
        self.assertIn(spawned_1["T2"], reconciled["unblocked_tasks"])
        self.assertNotIn(spawned_2["T2"], reconciled["unblocked_tasks"])
        self.assertIn("status:blocked", [l["name"] for l in self.bus.get_issue(spawned_2["T2"])["labels"]])

    def test_09_input_documents_loaded_into_planning(self):
        """Verifies docs in `docs/inputs/` and explicit `doc_paths` are injected into Architect prompt."""
        inputs_dir = self.tmp_dir / "docs" / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / "spec.md").write_text("# API Spec\nMust expose /healthz endpoint.", encoding="utf-8")

        docs_ctx = self.planner.load_input_documents()
        self.assertIn("docs/inputs/spec.md", docs_ctx)
        self.assertIn("/healthz", docs_ctx)

        prompt = self.planner._build_architect_prompt("Build API", "Context", docs_context=docs_ctx)
        self.assertIn("Project Input Documents", prompt)
        self.assertIn("/healthz", prompt)

    def test_10_github_projects_v2_sync_and_transitions(self):
        """Verifies GitHub Projects v2 creation, bulk sync, and automatic status column updates."""
        tasks = self.planner.parse_tasks_from_markdown(SAMPLE_PLAN_MARKDOWN)
        epic_num = self.planner.create_epic_for_approval("Projects Sync Epic", SAMPLE_PLAN_MARKDOWN, tasks)
        spawned = self.planner.approve_and_spawn_dag(epic_num)

        res = self.bus.sync_all_issues_to_project(title="Agent Swarm Kanban")
        self.assertEqual(res["project_number"], 1)
        self.assertEqual(res["synced_count"], 3)  # Epic + T1 + T2

        # Transition T1 to in-review -> Project status should map to "In Review"
        self.bus.transition_issue_state(spawned["T1"], "status:in-review")
        state = self.bus._load_mock()
        proj_items = state["projects"]["1"]["items"]
        self.assertEqual(proj_items[str(spawned["T1"])]["project_status"], "In Review")

        # Close T1 -> Project status should map to "Done"
        self.bus.close_issue(spawned["T1"])
        state = self.bus._load_mock()
        self.assertEqual(state["projects"]["1"]["items"][str(spawned["T1"])]["project_status"], "Done")


if __name__ == "__main__":
    unittest.main()
