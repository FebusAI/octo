"""
Ephemeral Multi-Node Worker Daemon & Isolated Git Worktree Manager (`worker_daemon.py`).
Allows any workstation or worker node to join or leave the cluster at any time:
- Claims `status:ready` or `status:rework` issues atomically via GitHub comments
- Recovers any `wip/issue-<N>` checkpoint branch if a previous node disconnected mid-task
- Executes the assigned coding agent/harness (`claude`, `pi`, `aider`, `agy`) inside `.worktrees/issue-<N>`
- Runs the mandatory verification command (`verification_cmd`)
- Pushes the branch, opens/updates the Pull Request, and transitions the issue to `status:in-review`
"""

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser, get_node_identity
from .github_bus import GitHubBus
from .harness_runner import HarnessRunner
from .overlap_detector import OverlapDetector
from .swarm_board import SwarmBoard


class WorkerDaemon:
    def __init__(
        self,
        bus: GitHubBus,
        runner: HarnessRunner,
        repo_root: Path,
        config: Dict[str, Any],
        node_id: Optional[str] = None,
        profile_name: str = "worker",
        model_override: Optional[str] = None,
        assignee: Optional[str] = None,
    ):
        self.bus = bus
        self.runner = runner
        self.repo_root = repo_root
        self.config = config
        self.node_id = get_node_identity(node_id)
        self.profile_name = profile_name
        self.model_override = model_override
        self.assignee = assignee or config.get("execution", {}).get("worker_assignee_filter") or None
        self.lease_cfg = config.get("lease", {})
        self.ttl_seconds = int(self.lease_cfg.get("ttl_seconds", 900))
        self.hb_interval = int(self.lease_cfg.get("heartbeat_interval", 240))
        self.optimistic_wait = float(self.lease_cfg.get("claim_optimistic_wait", 2.0))
        self.board = SwarmBoard(self.repo_root)
        self.overlap_detector = OverlapDetector(self.repo_root)

    # -------------------------------------------------------------------------
    # Git Worktree Isolation & Crash-Recovery Branch Management
    # -------------------------------------------------------------------------
    def prepare_worktree(self, issue_number: int) -> Tuple[Path, str]:
        """
        Creates an isolated Git worktree at `.worktrees/issue-<N>` on branch `feat/issue-<N>`.
        If a previous node pushed a `wip/issue-<N>` or `feat/issue-<N>` branch before disconnecting,
        automatically checks out that remote state so zero work is lost!
        """
        wt_root = self.repo_root / self.config.get("project", {}).get("worktree_dir", ".worktrees")
        wt_root.mkdir(parents=True, exist_ok=True)
        wt_dir = wt_root / f"issue-{issue_number}"
        branch_name = f"{self.lease_cfg.get('feat_branch_prefix', 'feat/issue-')}{issue_number}"
        base_branch = self.config.get("project", {}).get("default_branch", "main")

        # In mock mode or if not a git repo with commits yet, fallback to working directory or local copy
        git_check = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        if git_check.returncode != 0:
            wt_dir.mkdir(parents=True, exist_ok=True)
            return wt_dir, branch_name

        # Fetch latest remote branches if remote origin exists
        subprocess.run(["git", "fetch", "--all", "--prune"], cwd=str(self.repo_root), capture_output=True)

        if wt_dir.exists():
            return wt_dir, branch_name

        # Check if branch already exists locally or on origin (from a previous worker/rework)
        branch_exists = (
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
                cwd=str(self.repo_root),
                capture_output=True,
            ).returncode
            == 0
        )
        remote_exists = (
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
                cwd=str(self.repo_root),
                capture_output=True,
            ).returncode
            == 0
        )

        if branch_exists:
            subprocess.run(
                ["git", "worktree", "add", str(wt_dir), branch_name],
                cwd=str(self.repo_root),
                check=True,
                capture_output=True,
            )
        elif remote_exists:
            subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, str(wt_dir), f"origin/{branch_name}"],
                cwd=str(self.repo_root),
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, str(wt_dir), "HEAD"],
                cwd=str(self.repo_root),
                check=True,
                capture_output=True,
            )

        return wt_dir, branch_name

    def commit_and_push_checkpoint(self, wt_dir: Path, branch_name: str, commit_msg: str) -> bool:
        """Commits any modified files in the worktree and pushes to origin if configured."""
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt_dir),
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return False

        subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(wt_dir),
            capture_output=True,
        )
        # Push if remote origin exists
        has_origin = (
            subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(wt_dir), capture_output=True).returncode
            == 0
        )
        if has_origin and not self.bus.mock_dir:
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=str(wt_dir),
                capture_output=True,
            )
        return True

    # -------------------------------------------------------------------------
    # Context Builder (Zero Context Loss across PCs & Rework Cycles)
    # -------------------------------------------------------------------------
    def build_worker_prompt(self, issue: Dict[str, Any], spec: Dict[str, Any]) -> str:
        """
        Assembles the full context for the coding harness:
        - Issue Specification & Acceptance Criteria
        - Previous Agent Handovers, Checkpoints, Orchestrator Decisions, and QA Reports
        - Protocol instructions for asking questions (`[AGENT-QUESTION]`) if blocked
        """
        comments = issue.get("comments", [])
        history_blocks = []
        for c in comments[-10:]:
            author = (c.get("author") or {}).get("login", "agent")
            cbody = c.get("body", "").strip()
            history_blocks.append(f"--- Comment by @{author} ---\n{cbody}")

        history_str = "\n\n".join(history_blocks) if history_blocks else "No prior comments."
        verification_cmd = spec.get("verification_cmd") or "pytest"

        return f"""# GITHUB ISSUE #{issue['number']}: {issue['title']}

## 1. Issue Specification
{issue.get('body', '')}

## 2. Chronological Thread & Handover Context (Zero-Context-Loss Log)
{history_str}

## 3. Your Mission on Node `{self.node_id}`
1. You are working in an isolated workspace for Issue #{issue['number']}.
2. Implement the required changes cleanly, adhering to project conventions and non-destructive security practices.
3. Run the mandatory verification command to verify your work:
   `{verification_cmd}`
4. If the issue is in `status:rework`, carefully address every item in the latest `🛡️ [QA & SECURITY REPORT]` above.
5. **Need Clarification?** If a critical requirement is ambiguous or impossible due to an architectural conflict, output a block starting with `OCTO_QUESTION:` explaining the exact question for `@orchestrator`, and stop.
6. Otherwise, complete the implementation, ensure all tests pass, and output a concise summary of:
   - Files created/modified
   - Verification test results
   - Key technical decisions for the QA & Security Reviewer
"""

    # -------------------------------------------------------------------------
    # Reaping Stale Leases (When a PC goes offline mid-task)
    # -------------------------------------------------------------------------
    def reap_stale_in_progress_issues(self) -> List[int]:
        """
        Checks all `status:in-progress` issues. If the active lease has expired (because a PC
        was shut down or lost network), automatically transitions the issue back to `status:ready`
        with a handover alert so another PC can immediately resume from the WIP branch!
        """
        reaped = []
        in_progress = self.bus.list_issues(label="status:in-progress", state="open")
        for iss in in_progress:
            active_claim = self.bus.get_active_claim(iss)
            if active_claim is None:
                inum = iss["number"]
                self.bus.transition_issue_state(inum, "status:ready")
                self.bus.post_protocol_message(
                    inum,
                    "HANDOVER",
                    f"⚠️ **Stale Lease Recovered by `{self.node_id}`**: Previous worker lease expired without heartbeat. "
                    f"Issue returned to `status:ready` so any available node can resume from branch `feat/issue-{inum}`.",
                    {"reaped_by": self.node_id, "status": "released"},
                )
                reaped.append(inum)
        return reaped

    # -------------------------------------------------------------------------
    # Single Task Claim & Execution Cycle
    # -------------------------------------------------------------------------
    def process_next_available_issue(self) -> Optional[int]:
        """
        Looks for `status:rework` (highest priority) or `status:ready` issues,
        attempts an atomic claim, executes the harness, runs verification, and submits PR.
        """
        # First reap any stale leases from dead nodes
        self.reap_stale_in_progress_issues()

        candidates = self.bus.list_issues(label="status:rework", state="open", assignee=self.assignee) + self.bus.list_issues(
            label="status:ready", state="open", assignee=self.assignee
        )
        # Filter out Epics
        candidates = [
            c
            for c in candidates
            if "type:epic" not in [l["name"] for l in c.get("labels", [])]
        ]

        if not candidates:
            return None

        resolved_profile, profile_spec, _, _ = self.runner.resolve_target(
            self.profile_name, model_override=self.model_override
        )
        model_name = f"{profile_spec.get('provider', '')}/{profile_spec.get('model', 'default')}".strip("/")

        for candidate in candidates:
            inum = candidate["number"]

            # Overlap & Conflict Pre-Check across active tasks
            if self.config.get("multiplayer", {}).get("overlap_detection", {}).get("auto_check_on_claim", True):
                active_tasks = self.bus.list_issues(label="status:in-progress", state="open")
                overlaps = self.overlap_detector.check_overlaps([candidate] + active_tasks)
                critical = [r for r in overlaps if r.level == "CRITICAL_COLLISION"]
                if critical and self.config.get("multiplayer", {}).get("overlap_detection", {}).get("block_on_critical_collision", True):
                    self.board.post(
                        sender=self.node_id,
                        recipient="@coordinators",
                        text=f"@coordinators 🚨 [OVERLAP] {critical[0].message} Skipping task #{inum} temporarily to prevent collision.",
                        channel="c2c",
                    )
                    continue

            won, claim_uuid_or_reason = self.bus.try_claim_issue(
                issue_number=inum,
                node_id=self.node_id,
                harness_profile=resolved_profile,
                model_name=model_name,
                ttl_seconds=self.ttl_seconds,
                optimistic_wait=self.optimistic_wait,
            )
            if not won:
                continue

            claim_uuid = claim_uuid_or_reason
            self._execute_claimed_issue(candidate, claim_uuid, resolved_profile, model_name)
            return inum

        return None

    def _execute_claimed_issue(
        self,
        issue: Dict[str, Any],
        claim_uuid: str,
        resolved_profile: str,
        model_name: str,
    ) -> None:
        inum = issue["number"]
        spec = ProtocolParser.parse_task_issue_body(issue.get("body", ""))
        wt_dir, branch_name = self.prepare_worktree(inum)

        # Notify Swarm Board
        self.board.post(
            sender=self.node_id,
            recipient="@all",
            text=f"@all Worker `{self.node_id}` (`{resolved_profile}`) claimed Issue #{inum} ({issue.get('title')}) on branch `{branch_name}`.",
            channel="main",
            metadata={"issue": inum, "branch": branch_name, "node": self.node_id},
        )

        def _hb() -> None:
            self.commit_and_push_checkpoint(
                wt_dir, branch_name, f"wip(issue-{inum}): automatic checkpoint from {self.node_id}"
            )
            self.bus.send_heartbeat(
                issue_number=inum,
                node_id=self.node_id,
                claim_uuid=claim_uuid,
                wip_branch=branch_name,
                progress_note=f"Coding harness `{resolved_profile}` actively working in `{wt_dir.name}`.",
                ttl_seconds=self.ttl_seconds,
            )

        prompt = self.build_worker_prompt(issue, spec)
        legacy_prompt = self.repo_root / "prompts" / "worker-implementer.md"

        res = self.runner.run(
            profile_name=resolved_profile,
            prompt=prompt,
            workdir=wt_dir,
            system_prompt_file=legacy_prompt if legacy_prompt.exists() else None,
            session_id=f"issue-{inum}",
            read_only=False,
            heartbeat_callback=_hb,
            heartbeat_interval=self.hb_interval,
            model_override=self.model_override,
        )

        if not res.ok:
            self.commit_and_push_checkpoint(
                wt_dir, branch_name, f"wip(issue-{inum}): partial state before harness error on {self.node_id}"
            )
            self.bus.release_claim(
                issue_number=inum,
                node_id=self.node_id,
                claim_uuid=claim_uuid,
                reason=f"Harness `{resolved_profile}` exited with code {res.returncode}: `{res.stderr[:300]}`",
                next_status="status:ready",
            )
            return

        # Check if the worker raised an architectural question for the Orchestrator
        if "OCTO_QUESTION:" in res.stdout:
            q_text = res.stdout.split("OCTO_QUESTION:", 1)[1].strip()
            self.commit_and_push_checkpoint(
                wt_dir, branch_name, f"wip(issue-{inum}): paused awaiting clarification"
            )
            self.board.post(
                sender=self.node_id,
                recipient="@coordinator",
                text=f"@coordinator [question] Worker `{self.node_id}` on Issue #{inum} needs clarification:\n{q_text}",
                channel="main",
                metadata={"issue": inum},
            )
            self.bus.post_protocol_message(
                inum,
                "QUESTION",
                f"@orchestrator Worker `{self.node_id}` (`{resolved_profile}`) requires clarification before proceeding:\n\n{q_text}",
                {"node_id": self.node_id, "claim_uuid": claim_uuid, "status": "released"},
            )
            self.bus.transition_issue_state(inum, "status:needs-clarification")
            return

        # Run mandatory local verification command before submitting PR
        v_cmd = spec.get("verification_cmd", "").strip()
        v_passed = True
        v_output = "No verification command specified."
        if v_cmd:
            v_proc = subprocess.run(
                v_cmd,
                shell=True,
                cwd=str(wt_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            v_passed = v_proc.returncode == 0
            v_output = (v_proc.stdout + "\n" + v_proc.stderr).strip()[-1500:]

        # Commit final changes
        self.commit_and_push_checkpoint(
            wt_dir, branch_name, f"feat(issue-{inum}): implement {issue['title']} [node:{self.node_id}]"
        )

        base_branch = self.config.get("project", {}).get("default_branch", "main")
        pr_body = f"""Closes #{inum}

## 🤝 Agent Handover Summary
- **Worker Node**: `{self.node_id}`
- **Harness / Model**: `{resolved_profile}` (`{model_name}`)
- **Branch**: `{branch_name}`
- **Local Verification (`{v_cmd}`)**: {"✅ PASSED" if v_passed else "⚠️ FAILED (Needs QA inspection)"}

### Implementation Details
{res.stdout.strip()[-3000:]}

### Verification Output
```text
{v_output}
```
"""
        pr_num = self.bus.create_or_update_pr(
            issue_number=inum,
            branch=branch_name,
            base_branch=base_branch,
            title=f"feat(issue-{inum}): {issue['title']}",
            body=pr_body,
        )

        handover_meta = {
            "claim_uuid": claim_uuid,
            "node_id": self.node_id,
            "harness": resolved_profile,
            "model": model_name,
            "branch": branch_name,
            "pr_number": pr_num,
            "verification_passed": v_passed,
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.bus.post_protocol_message(
            inum,
            "HANDOVER",
            f"✅ **Implementation Complete on `{self.node_id}`** — Opened PR #{pr_num} from branch `{branch_name}`.\n"
            f"- **Harness**: `{resolved_profile}` (`{model_name}`)\n"
            f"- **Verification (`{v_cmd}`)**: {'✅ PASSED' if v_passed else '⚠️ FAILED'}\n\n"
            f"Transitioning to `status:in-review` for Independent QA & Non-Destructive Security Review.",
            handover_meta,
        )
        # Notify Swarm Board for verifiers and peers
        self.board.post(
            sender=self.node_id,
            recipient="@verifier",
            text=f"@verifier [done] Issue #{inum} implementation ready for review. PR #{pr_num} opened from `{branch_name}`.",
            channel="qa",
            metadata={"issue": inum, "pr": pr_num, "v_passed": v_passed},
        )
        self.bus.transition_issue_state(inum, "status:in-review")
