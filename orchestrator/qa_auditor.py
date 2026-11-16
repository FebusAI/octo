"""
Integrated Quality Assurance (QA) & Code Review Gate (`qa_auditor.py`).
Reviews issues in `status:in-review` (and their corresponding Pull Requests):
1. Runs the mandatory verification and test command (`verification_cmd`) inside the worktree.
2. Performs basic static checks (forbidden patterns & credential scan).
3. Invokes an independent QA / Code Review Agent (`qa` or custom profile) in read-only mode
   to audit the git diff against the Issue Acceptance Criteria.
4. Posts a structured `[QA_REPORT]` on the Issue + GitHub PR Review:
   - If PASS: Approves PR, auto-merges (if configured), closes Issue (`status:done`), and triggers DAG unblocking.
   - If FAIL: Requests changes on PR and transitions Issue to `status:rework` with actionable remediation steps.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser, get_node_identity
from .github_bus import GitHubBus
from .harness_runner import HarnessRunner
from .swarm_board import SwarmBoard


class QAAuditor:
    verdict_re = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.IGNORECASE)

    def __init__(
        self,
        bus: GitHubBus,
        runner: HarnessRunner,
        repo_root: Path,
        config: Dict[str, Any],
        node_id: Optional[str] = None,
        qa_profile: str = "qa",
        model_override: Optional[str] = None,
    ):
        self.bus = bus
        self.runner = runner
        self.repo_root = repo_root
        self.config = config
        self.node_id = get_node_identity(node_id)
        self.qa_profile = qa_profile
        self.model_override = model_override
        self.qa_policy = config.get("qa_policy", {})
        self.board = SwarmBoard(self.repo_root)

    def scan_code_safety(self, diff_text: str) -> List[str]:
        """Static check for obvious forbidden patterns or hardcoded credentials."""
        violations = []
        forbidden = self.qa_policy.get("forbidden_patterns", [])
        for pat in forbidden:
            if pat in diff_text:
                violations.append(f"Forbidden pattern detected in diff: `{pat}`")

        added_lines = [
            line[1:]
            for line in diff_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        added_blob = "\n".join(added_lines)
        if "-----BEGIN OPENSSH PRIVATE KEY-----" in added_blob or "-----BEGIN RSA PRIVATE KEY-----" in added_blob:
            violations.append("Hardcoded SSH/RSA Private Key detected in added lines.")
        if re.search(r"ghp_[A-Za-z0-9]{36}", added_blob):
            violations.append("Hardcoded GitHub Personal Access Token detected in diff.")

        return violations

    # Backward compatibility alias
    scan_non_destructive_safety = scan_code_safety

    def audit_issue_in_review(self, issue_number: int, run_llm_audit: bool = True) -> Tuple[bool, str]:
        """
        Executes the QA & Code Review Audit for an issue in `status:in-review`.
        Returns `(passed, report_markdown)`.
        """
        issue = self.bus.get_issue(issue_number)
        spec = ProtocolParser.parse_task_issue_body(issue.get("body", ""))
        comments = issue.get("comments", [])
        handovers = ProtocolParser.extract_comments_meta(comments, msg_type="HANDOVER")
        latest_handover = handovers[-1] if handovers else {}

        branch_name = latest_handover.get("branch", f"feat/issue-{issue_number}")
        pr_number = latest_handover.get("pr_number")
        wt_root = self.repo_root / self.config.get("project", {}).get("worktree_dir", ".worktrees")
        wt_dir = wt_root / f"issue-{issue_number}"
        if not wt_dir.exists():
            wt_dir = self.repo_root

        # 1. Inspect Git Diff
        diff_proc = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            cwd=str(wt_dir),
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout if diff_proc.returncode == 0 else ""

        safety_violations = self.scan_code_safety(diff_text)

        # 2. Execute Mandatory Verification Command
        v_cmd = spec.get("verification_cmd", "").strip()
        v_passed = True
        v_output = "No verification command configured."
        if v_cmd:
            try:
                v_proc = subprocess.run(
                    v_cmd,
                    shell=True,
                    cwd=str(wt_dir),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                v_passed = v_proc.returncode == 0
                v_output = (v_proc.stdout + "\n" + v_proc.stderr).strip()[-2000:]
            except subprocess.TimeoutExpired:
                v_passed = False
                v_output = f"Verification command `{v_cmd}` timed out after 300s."

        # 3. Run Independent QA Review Harness
        llm_passed = True
        llm_review_text = "Automated LLM QA review skipped or mock-approved."
        resolved_profile, profile_spec, _, _ = self.runner.resolve_target(
            self.qa_profile, model_override=self.model_override
        )

        if run_llm_audit and profile_spec.get("adapter") != "mock":
            qa_prompt = f"""You are an Independent QA & Code Reviewer on Node `{self.node_id}`.
Review the implementation for GitHub Issue #{issue_number}: {issue['title']}.

## Issue Specification
{issue.get('body', '')}

## Automated Verification Command Result (`{v_cmd}`)
Status: {"PASSED" if v_passed else "FAILED"}
Output:
```text
{v_output}
```

## Git Diff to Audit
```diff
{diff_text[:12000]}
```

## Review Instructions
1. Verify whether all Acceptance Criteria from the Issue Specification are met.
2. Check for edge cases, error handling, clean architecture, and regressions.
3. Conclude your response with an explicit line:
   `VERDICT: PASS` (if ready to merge) or `VERDICT: FAIL` (if rework is required), followed by bulleted findings.
"""
            legacy_prompt = self.repo_root / "prompts" / "qa-reviewer.md"
            res = self.runner.run(
                profile_name=resolved_profile,
                prompt=qa_prompt,
                workdir=wt_dir,
                system_prompt_file=legacy_prompt if legacy_prompt.exists() else None,
                read_only=True,
                model_override=self.model_override,
            )
            if res.ok and res.stdout.strip():
                llm_review_text = res.stdout.strip()
                m = self.verdict_re.search(llm_review_text)
                if m and m.group(1).upper() == "FAIL":
                    llm_passed = False
            else:
                llm_review_text = f"Note: LLM QA harness `{resolved_profile}` returned code {res.returncode}. Relying on deterministic verification."

        overall_passed = (len(safety_violations) == 0) and v_passed and llm_passed

        # Check rework count to prevent infinite loops
        prior_qa_reports = ProtocolParser.extract_comments_meta(comments, msg_type="QA_REPORT")
        rework_count = sum(1 for r in prior_qa_reports if not r.get("passed", False))

        report_md = f"""### {"✅ QA & Code Review: PASSED" if overall_passed else "❌ QA & Code Review: CHANGES REQUESTED"}
- **Auditor Node**: `{self.node_id}`
- **QA Profile**: `{resolved_profile}`
- **Verification Test (`{v_cmd}`)**: {"✅ PASS" if v_passed else "❌ FAIL"}
- **Rework Cycle**: `{rework_count + (0 if overall_passed else 1)}`

#### Verification Command Output
```text
{v_output}
```

#### Auditor Analysis
{llm_review_text}
"""
        qa_meta = {
            "auditor_node": self.node_id,
            "profile": resolved_profile,
            "passed": overall_passed,
            "verification_passed": v_passed,
            "safety_violations": safety_violations,
            "pr_number": pr_number,
            "audited_at": datetime.now(timezone.utc).isoformat(),
        }
        self.bus.post_protocol_message(issue_number, "QA_REPORT", report_md, qa_meta)

        if pr_number:
            self.bus.submit_pr_review(int(pr_number), approved=overall_passed, review_body=report_md)

        if overall_passed:
            self.board.post(
                sender=f"verifier-{self.node_id}",
                recipient="@coordinator",
                text=f"@coordinator [done] QA Review PASSED for Issue #{issue_number} (PR #{pr_number}). Ready for auto-merge.",
                channel="qa",
                metadata={"issue": issue_number, "passed": True},
            )
            if pr_number and self.config.get("project", {}).get("auto_merge_on_qa_pass", True):
                squash = bool(self.config.get("project", {}).get("squash_merge", True))
                self.bus.merge_pr(int(pr_number), squash=squash)
            self.bus.close_issue(issue_number)
        else:
            self.board.post(
                sender=f"verifier-{self.node_id}",
                recipient="@all",
                text=f"@all [rework] QA Review requested changes for Issue #{issue_number}. Fix required.",
                channel="qa",
                metadata={"issue": issue_number, "passed": False},
            )
            max_rework = int(self.qa_policy.get("max_rework_cycles", 3))
            if rework_count + 1 >= max_rework:
                self.bus.transition_issue_state(issue_number, "status:needs-clarification")
                self.bus.post_protocol_message(
                    issue_number,
                    "QUESTION",
                    f"@orchestrator Task #{issue_number} failed QA {rework_count + 1} times (max={max_rework}). Escalating for human/architect review.",
                    {"escalated": True, "rework_count": rework_count + 1},
                )
            else:
                self.bus.transition_issue_state(issue_number, "status:rework")

        return overall_passed, report_md

    def process_all_in_review(self, run_llm_audit: bool = True) -> List[Tuple[int, bool]]:
        results = []
        in_review = self.bus.list_issues(label="status:in-review", state="open")
        for iss in in_review:
            inum = iss["number"]
            passed, _ = self.audit_issue_in_review(inum, run_llm_audit=run_llm_audit)
            results.append((inum, passed))
        return results
