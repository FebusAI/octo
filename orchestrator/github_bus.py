"""
GitHub Native Control Plane & Message Bus (`github_bus.py`).
Provides atomic operations over GitHub Issues, Labels, Milestones, Pull Requests,
and GitHub Projects v2 via `gh` CLI (with offline/local mock mode for dry-run testing).
"""

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser


class GitHubBusError(Exception):
    pass


class GitHubBus:
    """
    Wraps GitHub CLI (`gh`) so any PC with `gh auth status` can participate in the swarm
    without managing custom databases, message queues, or webhooks.
    Also supports a local JSON-backed simulation mode (`mock_dir`) for offline testing & CI.
    """

    def __init__(
        self,
        repo: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        mock_dir: Optional[Path] = None,
    ):
        self.repo = repo or os.environ.get("OCTO_REPO")
        if not self.repo:
            try:
                proc = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], capture_output=True, text=True)
                if proc.returncode == 0 and proc.stdout.strip():
                    self.repo = proc.stdout.strip()
            except Exception:
                pass
        self.config = config or {}
        self.mock_dir = mock_dir or (
            Path(os.environ["OCTO_MOCK_DIR"]) if os.environ.get("OCTO_MOCK_DIR") else None
        )
        if self.mock_dir:
            self.mock_dir.mkdir(parents=True, exist_ok=True)
            self._init_mock_store()

    # -------------------------------------------------------------------------
    # Low-level GH CLI / Mock Transport
    # -------------------------------------------------------------------------
    def _init_mock_store(self) -> None:
        assert self.mock_dir is not None
        store_file = self.mock_dir / "github_state.json"
        if not store_file.exists():
            store_file.write_text(
                json.dumps({"issues": {}, "prs": {}, "labels": {}, "next_id": 1}, indent=2),
                encoding="utf-8",
            )

    def _load_mock(self) -> Dict[str, Any]:
        assert self.mock_dir is not None
        return json.loads((self.mock_dir / "github_state.json").read_text(encoding="utf-8"))

    def _save_mock(self, data: Dict[str, Any]) -> None:
        assert self.mock_dir is not None
        (self.mock_dir / "github_state.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _run_gh(self, args: List[str], input_text: Optional[str] = None, check: bool = True) -> str:
        cmd = ["gh"]
        if self.repo and args and args[0] in ("issue", "pr", "label", "release", "project"):
            # `gh project` uses --owner instead of -R
            if args[0] != "project":
                cmd.extend(["-R", self.repo])
        cmd.extend(args)

        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
        )
        if check and proc.returncode != 0:
            raise GitHubBusError(f"gh command failed ({' '.join(cmd)}): {proc.stderr.strip()}")
        return proc.stdout.strip()

    # -------------------------------------------------------------------------
    # Kanban Labels Bootstrap
    # -------------------------------------------------------------------------
    def bootstrap_labels(self) -> List[str]:
        """Creates all required `status:*` and `type:*` Kanban labels on GitHub."""
        kanban_cfg = self.config.get("kanban", {})
        states = kanban_cfg.get("states", {})
        roles = kanban_cfg.get("roles", {})

        created = []
        all_specs = list(states.values()) + list(roles.values())

        if self.mock_dir:
            state = self._load_mock()
            for spec in all_specs:
                lbl = spec["label"]
                state["labels"][lbl] = spec
                created.append(lbl)
            self._save_mock(state)
            return created

        for spec in all_specs:
            lbl = spec["label"]
            color = spec.get("color", "EDEDED")
            desc = spec.get("description", "")
            self._run_gh(
                ["label", "create", lbl, "--color", color, "--description", desc, "--force"],
                check=False,
            )
            created.append(lbl)
        return created

    def ensure_label(self, name: str, color: str = "BFDADC", description: str = "") -> str:
        """Ensures an arbitrary label (e.g. `epic:1`) exists on GitHub or in mock store."""
        if self.mock_dir:
            state = self._load_mock()
            if name not in state["labels"]:
                state["labels"][name] = {"label": name, "color": color, "description": description}
                self._save_mock(state)
            return name

        self._run_gh(
            ["label", "create", name, "--color", color, "--description", description, "--force"],
            check=False,
        )
        return name

    # -------------------------------------------------------------------------
    # Issues & Kanban State Management
    # -------------------------------------------------------------------------
    def create_issue(
        self,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        milestone: Optional[str] = None,
    ) -> int:
        labels = labels or []
        if self.mock_dir:
            state = self._load_mock()
            issue_num = state["next_id"]
            state["next_id"] += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            state["issues"][str(issue_num)] = {
                "number": issue_num,
                "title": title,
                "body": body,
                "state": "OPEN",
                "labels": [{"name": l} for l in labels],
                "comments": [],
                "createdAt": now_iso,
                "updatedAt": now_iso,
            }
            self._save_mock(state)
            return issue_num

        args = ["issue", "create", "--title", title, "--body-file", "-"]
        for l in labels:
            args.extend(["--label", l])
        if milestone:
            args.extend(["--milestone", milestone])

        out = self._run_gh(args, input_text=body)
        # Out ends with .../issues/123
        num_str = out.rstrip("/").split("/")[-1]
        return int(num_str)

    def get_issue(self, issue_number: int) -> Dict[str, Any]:
        if self.mock_dir:
            state = self._load_mock()
            issue = state["issues"].get(str(issue_number))
            if not issue:
                raise GitHubBusError(f"Issue #{issue_number} not found in mock store")
            return issue

        out = self._run_gh(
            [
                "issue",
                "view",
                str(issue_number),
                "--json",
                "number,title,body,state,labels,comments,assignees,createdAt,updatedAt,url",
            ]
        )
        return json.loads(out)

    def list_issues(
        self,
        label: Optional[str] = None,
        state: str = "open",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if self.mock_dir:
            store = self._load_mock()
            results = []
            for issue in store["issues"].values():
                if state != "all" and issue["state"].lower() != state.lower():
                    continue
                lbl_names = [l["name"] for l in issue.get("labels", [])]
                if label and label not in lbl_names:
                    continue
                results.append(issue)
            return sorted(results, key=lambda x: x["number"])

        args = [
            "issue",
            "list",
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,body,state,labels,comments,createdAt,updatedAt,url",
        ]
        if label:
            args.extend(["--label", label])
        out = self._run_gh(args)
        return json.loads(out) if out else []

    def transition_issue_state(self, issue_number: int, new_status_label: str) -> None:
        """
        Atomically moves an issue from its current `status:*` label to `new_status_label`.
        """
        issue = self.get_issue(issue_number)
        current_labels = [l["name"] for l in issue.get("labels", [])]
        old_status_labels = [
            l for l in current_labels if l.startswith("status:") and l != new_status_label
        ]

        if self.mock_dir:
            store = self._load_mock()
            iss = store["issues"][str(issue_number)]
            kept = [l for l in iss["labels"] if not l["name"].startswith("status:")]
            kept.append({"name": new_status_label})
            iss["labels"] = kept
            iss["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._save_mock(store)
            return

        args = ["issue", "edit", str(issue_number), "--add-label", new_status_label]
        for old_lbl in old_status_labels:
            args.extend(["--remove-label", old_lbl])
        self._run_gh(args)

    def update_issue_body(self, issue_number: int, new_body: str) -> None:
        if self.mock_dir:
            store = self._load_mock()
            store["issues"][str(issue_number)]["body"] = new_body
            store["issues"][str(issue_number)]["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._save_mock(store)
            return
        self._run_gh(["issue", "edit", str(issue_number), "--body-file", "-"], input_text=new_body)

    def close_issue(self, issue_number: int, comment: Optional[str] = None) -> None:
        if comment:
            self.post_comment(issue_number, comment)
        self.transition_issue_state(issue_number, "status:done")
        if self.mock_dir:
            store = self._load_mock()
            store["issues"][str(issue_number)]["state"] = "CLOSED"
            self._save_mock(store)
            return
        self._run_gh(["issue", "close", str(issue_number)])

    # -------------------------------------------------------------------------
    # Agent-to-Agent Communication Bus (Structured Comments)
    # -------------------------------------------------------------------------
    def post_comment(self, issue_number: int, body: str) -> None:
        if self.mock_dir:
            store = self._load_mock()
            iss = store["issues"][str(issue_number)]
            cid = f"c-{len(iss['comments']) + 1}-{uuid.uuid4().hex[:6]}"
            iss["comments"].append(
                {
                    "id": cid,
                    "body": body,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "author": {"login": os.environ.get("USER", "agent")},
                }
            )
            iss["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._save_mock(store)
            return

        self._run_gh(["issue", "comment", str(issue_number), "--body-file", "-"], input_text=body)

    def post_protocol_message(
        self,
        issue_number: int,
        msg_type: str,
        summary_markdown: str,
        meta: Dict[str, Any],
    ) -> None:
        formatted = ProtocolParser.format_comment(msg_type, summary_markdown, meta)
        self.post_comment(issue_number, formatted)

    # -------------------------------------------------------------------------
    # Optimistic Distributed Claiming & Lease Heartbeat (Multi-PC Safe)
    # -------------------------------------------------------------------------
    def try_claim_issue(
        self,
        issue_number: int,
        node_id: str,
        harness_profile: str,
        model_name: str,
        ttl_seconds: int = 900,
        optimistic_wait: float = 2.0,
    ) -> Tuple[bool, str]:
        """
        Attempts to atomically claim an issue across distributed PCs using GitHub comments
        as a total-order log.
        1. Checks if issue already has an active, non-expired claim.
        2. Posts a `[AGENT-CLAIM]` comment with a unique `claim_uuid` and `lease_expires_at`.
        3. Waits `optimistic_wait` seconds (0 in mock mode) and re-reads comments.
        4. The first non-expired claim in chronological order wins.
        """
        issue = self.get_issue(issue_number)
        active_claim = self.get_active_claim(issue)
        if active_claim and active_claim.get("node_id") != node_id:
            return False, f"Already claimed by {active_claim.get('node_id')} until {active_claim.get('lease_expires_at')}"

        claim_uuid = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        wip_branch = f"wip/issue-{issue_number}"

        meta = {
            "claim_uuid": claim_uuid,
            "node_id": node_id,
            "pid": os.getpid(),
            "harness": harness_profile,
            "model": model_name,
            "claimed_at": now.isoformat(),
            "lease_expires_at": expires.isoformat(),
            "wip_branch": wip_branch,
            "status": "active",
        }
        summary = (
            f"- **Worker Node**: `{node_id}` (PID `{os.getpid()}`)\n"
            f"- **Harness / Model**: `{harness_profile}` (`{model_name}`)\n"
            f"- **WIP Branch**: `{wip_branch}`\n"
            f"- **Lease Expires**: `{expires.strftime('%Y-%m-%d %H:%M:%S UTC')}`\n"
            f"- **Claim Token**: `{claim_uuid}`"
        )
        self.post_protocol_message(issue_number, "CLAIM", summary, meta)

        if not self.mock_dir and optimistic_wait > 0:
            time.sleep(optimistic_wait)

        # Verify we won the race
        refreshed = self.get_issue(issue_number)
        winner = self.get_active_claim(refreshed)
        if winner and winner.get("claim_uuid") == claim_uuid:
            self.transition_issue_state(issue_number, "status:in-progress")
            return True, claim_uuid
        return False, f"Lost optimistic claim race to {winner.get('node_id') if winner else 'unknown'}"

    def send_heartbeat(
        self,
        issue_number: int,
        node_id: str,
        claim_uuid: str,
        wip_branch: str,
        progress_note: str = "Worker active and progressing.",
        ttl_seconds: int = 900,
    ) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        meta = {
            "claim_uuid": claim_uuid,
            "node_id": node_id,
            "heartbeat_at": now.isoformat(),
            "lease_expires_at": expires.isoformat(),
            "wip_branch": wip_branch,
            "status": "active",
        }
        summary = (
            f"- **Node**: `{node_id}` (`{claim_uuid}`)\n"
            f"- **Lease Extended Until**: `{expires.strftime('%Y-%m-%d %H:%M:%S UTC')}`\n"
            f"- **Status**: {progress_note}"
        )
        self.post_protocol_message(issue_number, "HEARTBEAT", summary, meta)

    def release_claim(
        self,
        issue_number: int,
        node_id: str,
        claim_uuid: str,
        reason: str,
        next_status: str = "status:ready",
    ) -> None:
        meta = {
            "claim_uuid": claim_uuid,
            "node_id": node_id,
            "released_at": datetime.now(timezone.utc).isoformat(),
            "status": "released",
            "reason": reason,
        }
        self.post_protocol_message(
            issue_number,
            "HANDOVER",
            f"🔓 **Lease Released by `{node_id}`**: {reason}\nTransitioning issue to `{next_status}`.",
            meta,
        )
        self.transition_issue_state(issue_number, next_status)

    @classmethod
    def get_active_claim(cls, issue: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Reconstructs the current lease state from the chronological comment stream.
        """
        comments = issue.get("comments", [])
        metas = ProtocolParser.extract_comments_meta(comments)
        now = datetime.now(timezone.utc)

        current_claim: Optional[Dict[str, Any]] = None
        for m in metas:
            mtype = m.get("_type")
            if mtype == "CLAIM" and m.get("status") == "active":
                exp_str = m.get("lease_expires_at", "")
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                except ValueError:
                    continue
                if current_claim is None:
                    if exp_dt > now:
                        current_claim = dict(m)
                else:
                    # Only replace if current_claim is expired
                    cur_exp = datetime.fromisoformat(current_claim["lease_expires_at"])
                    if cur_exp <= now and exp_dt > now:
                        current_claim = dict(m)
            elif mtype == "HEARTBEAT" and current_claim:
                if m.get("claim_uuid") == current_claim.get("claim_uuid"):
                    current_claim["lease_expires_at"] = m.get(
                        "lease_expires_at", current_claim["lease_expires_at"]
                    )
            elif mtype == "HANDOVER" and current_claim:
                if m.get("claim_uuid") == current_claim.get("claim_uuid") and m.get("status") in (
                    "released",
                    "completed",
                ):
                    current_claim = None

        if current_claim:
            try:
                if datetime.fromisoformat(current_claim["lease_expires_at"]) <= now:
                    return None
            except ValueError:
                return None
        return current_claim

    # -------------------------------------------------------------------------
    # Pull Request & QA Review Operations
    # -------------------------------------------------------------------------
    def create_or_update_pr(
        self,
        issue_number: int,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> int:
        if self.mock_dir:
            store = self._load_mock()
            for pr_id, pr in store["prs"].items():
                if pr["headRefName"] == branch and pr["state"] == "OPEN":
                    pr["title"] = title
                    pr["body"] = body
                    self._save_mock(store)
                    return int(pr_id)
            pr_num = store["next_id"]
            store["next_id"] += 1
            store["prs"][str(pr_num)] = {
                "number": pr_num,
                "issue_number": issue_number,
                "headRefName": branch,
                "baseRefName": base_branch,
                "title": title,
                "body": body,
                "state": "OPEN",
                "reviews": [],
            }
            self._save_mock(store)
            return pr_num

        # Check if a PR already exists for this branch
        existing = self._run_gh(
            ["pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            check=False,
        )
        if existing:
            prs = json.loads(existing)
            if prs:
                pr_num = prs[0]["number"]
                self._run_gh(
                    ["pr", "edit", str(pr_num), "--title", title, "--body-file", "-"],
                    input_text=body,
                )
                return int(pr_num)

        out = self._run_gh(
            [
                "pr",
                "create",
                "--head",
                branch,
                "--base",
                base_branch,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input_text=body,
        )
        return int(out.rstrip("/").split("/")[-1])

    def submit_pr_review(
        self,
        pr_number: int,
        approved: bool,
        review_body: str,
    ) -> None:
        if self.mock_dir:
            store = self._load_mock()
            pr = store["prs"].get(str(pr_number))
            if pr:
                pr["reviews"].append(
                    {
                        "state": "APPROVED" if approved else "CHANGES_REQUESTED",
                        "body": review_body,
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._save_mock(store)
            return

        flag = "--approve" if approved else "--request-changes"
        # Note: GitHub prevents self-approving PRs from the same GitHub user account.
        # If the worker and QA agent share the same GitHub token, fall back to `--comment` on error.
        try:
            self._run_gh(["pr", "review", str(pr_number), flag, "--body-file", "-"], input_text=review_body)
        except GitHubBusError:
            self._run_gh(["pr", "review", str(pr_number), "--comment", "--body-file", "-"], input_text=review_body)

    def merge_pr(self, pr_number: int, squash: bool = True) -> None:
        if self.mock_dir:
            store = self._load_mock()
            if str(pr_number) in store["prs"]:
                store["prs"][str(pr_number)]["state"] = "MERGED"
                self._save_mock(store)
            return

        args = ["pr", "merge", str(pr_number), "--delete-branch"]
        args.append("--squash" if squash else "--merge")
        self._run_gh(args)
