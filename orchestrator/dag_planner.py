"""
DAG Planner, Multi-Agent Planning Loop, Human Approval Gate, and Dependency Unblocker (`dag_planner.py`).
- Phase 1: Octo orchestrates Explorer -> Architect -> Critic <-> Architect agents to produce
  a thorough RFC + JSON Task DAG -> Master Epic Issue (`status:awaiting-approval`).
- Phase 2: On `/approve` or `octo approve <N>`, spawns Epic-qualified sub-issues (`[E<N>-T1]`,
  label `epic:<N>`) and unlocks dependencies dynamically as upstream tasks close.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser, TaskSpec, qualify_task_id
from .github_bus import GitHubBus
from .harness_runner import HarnessRunner
from .swarm_board import SwarmBoard
from .overlap_detector import OverlapDetector
from .c2c_coordinator import C2CCoordinator


class DAGPlanner:
    """
    Orchestrates planning agents and manages the lifecycle of an Epic from high-level
    Goal to approved GitHub Issues DAG.
    """

    PLAN_JSON_RE = re.compile(r"```json\s*(\{.*?\"tasks\"\s*:\s*\[.*?\].*?\})\s*```", re.DOTALL)
    CRITIC_STATUS_RE = re.compile(r"STATUS:\s*(APPROVED|NEEDS_REVISION)", re.IGNORECASE)

    def __init__(self, bus: GitHubBus, runner: HarnessRunner, repo_root: Path, config: Dict[str, Any]):
        self.bus = bus
        self.runner = runner
        self.repo_root = repo_root
        self.config = config
        self.board = SwarmBoard(self.repo_root)
        self.c2c = C2CCoordinator(self.repo_root, self.bus, self.board)
        self.detector = OverlapDetector(self.repo_root)
        self.last_planning_trace: List[Dict[str, str]] = []

    def _build_architect_prompt(self, goal: str, context_brief: str) -> str:
        ctx_section = f"## Codebase Context Brief (from Explorer Agent)\n{context_brief.strip()}\n\n" if context_brief.strip() else ""
        return f"""Design a thorough, modular, production-grade Architecture RFC and Task DAG for the following Goal.

GOAL:
{goal}

{ctx_section}Your output MUST include ALL of the following sections:
1. **Executive Summary & Problem Statement**
2. **Explicit Goals (`G1`, `G2`, ...) & Non-Goals (`NG1`, `NG2`, ...)**
3. **Architectural Design & Module Boundaries** (components, data flow, error handling, safety/security considerations)
4. **Testing & Verification Strategy**
5. **Task Dependency Graph** as a Mermaid flowchart (`flowchart TD`)
6. **Machine-Readable Task DAG** inside a fenced ```json ... ``` block matching this exact schema:
```json
{{
  "epic_title": "Short descriptive title for the Epic",
  "summary": "Executive summary of the architecture",
  "goals": ["G1: ...", "G2: ..."],
  "non_goals": ["NG1: ..."],
  "tasks": [
    {{
      "id": "T1",
      "title": "Action-oriented task title",
      "role": "worker",
      "goal_refs": ["G1"],
      "depends_on": [],
      "target_files": ["path/to/file.py", "tests/test_file.py"],
      "interfaces": ["Exact function signatures, classes, schemas, or CLI flags"],
      "edge_cases": ["Invalid input handling, boundary conditions, failure recovery"],
      "verification_cmd": "python3 -m unittest discover -s tests -v",
      "description": "Comprehensive zero-context-loss specification so an isolated worker on another machine can implement this without guessing.",
      "acceptance_criteria": [
        "Concrete testable criterion 1",
        "Concrete testable criterion 2"
      ]
    }}
  ]
}}
```
Ensure parallel tasks never edit the same `target_files` unless ordered with `depends_on`.
"""

    def _build_critic_prompt(self, goal: str, context_brief: str, draft_plan: str, round_num: int) -> str:
        ctx_section = f"## Codebase Context Brief\n{context_brief.strip()}\n\n" if context_brief.strip() else ""
        return f"""Review Draft Plan (Critic Round {round_num}) for the following Goal.

GOAL:
{goal}

{ctx_section}## Draft Architecture RFC & Task DAG
{draft_plan.strip()}

Audit every section and task carefully:
1. Are Goals (`G1..`) and Non-Goals (`NG1..`) concrete, complete, and traceable to tasks?
2. Is every task self-contained (zero context loss): exact `target_files`, exact `interfaces`, `edge_cases`, and detailed `description`?
3. Are `depends_on` edges accurate with zero parallel file collisions?
4. Is every `verification_cmd` real, deterministic, and matching this repo's test tooling?
5. Is the fenced ```json ... ``` block valid and complete?

Conclude with either `STATUS: APPROVED` or `STATUS: NEEDS_REVISION` plus concrete numbered fixes.
"""

    def _build_refiner_prompt(self, goal: str, context_brief: str, previous_plan: str, critique: str) -> str:
        return f"""Refine and strengthen your Architecture RFC and Task DAG by addressing ALL findings from the Critic Agent.

GOAL:
{goal}

## Codebase Context Brief
{context_brief.strip() or "(None)"}

## Previous Draft Plan
{previous_plan.strip()}

## Critic Agent Findings
{critique.strip()}

Produce the complete, updated RFC (Summary, Goals/Non-Goals, Architecture, Verification Strategy, Mermaid `flowchart TD`, and the full updated fenced ```json ... ``` task list). Do not omit unchanged tasks.
"""

    def generate_plan_with_harness(
        self,
        goal: str,
        profile: str = "architect",
        explorer_profile: str = "explorer",
        critic_profile: str = "critic",
        model_override: Optional[str] = None,
        rounds: Optional[int] = None,
        skip_explore: bool = False,
    ) -> Tuple[str, List[TaskSpec]]:
        """
        Orchestrates the multi-agent planning loop:
          1. Explorer Agent inspects repo -> Codebase Context Brief
          2. Architect Agent sets Goals/Non-Goals + Architecture + Task DAG
          3. Critic <-> Architect refinement loop (`rounds - 1` max critique passes)
        """
        planning_cfg = self.config.get("planning", {})
        total_rounds = int(rounds if rounds is not None else planning_cfg.get("rounds", 2))
        total_rounds = max(1, total_rounds)

        trace: List[Dict[str, str]] = []
        context_brief = ""

        # Step 1: Codebase Exploration
        if not skip_explore and explorer_profile:
            explore_prompt = (
                f"Inspect the current repository in read-only mode to gather grounding context for this Goal:\n\n"
                f"GOAL: {goal}\n\n"
                f"Report repository structure, relevant modules/symbols, test framework & exact test command, "
                f"conventions, and potential touchpoints/risks."
            )
            exp_res = self.runner.run(
                profile_name=explorer_profile,
                prompt=explore_prompt,
                workdir=self.repo_root,
                read_only=True,
                model_override=model_override,
            )
            if exp_res.ok and exp_res.stdout.strip():
                context_brief = exp_res.stdout.strip()
                trace.append({
                    "step": "1. Explore",
                    "agent": exp_res.profile_name,
                    "model": exp_res.model_used,
                    "outcome": "Context brief generated",
                })
            else:
                trace.append({
                    "step": "1. Explore",
                    "agent": explorer_profile,
                    "model": "n/a",
                    "outcome": f"Skipped/fallback (exit {exp_res.returncode})",
                })

        # Step 2: Initial Goal-Setting & Architecture Draft
        arch_prompt = self._build_architect_prompt(goal, context_brief)
        arch_res = self.runner.run(
            profile_name=profile,
            prompt=arch_prompt,
            workdir=self.repo_root,
            read_only=True,
            model_override=model_override,
        )
        if not arch_res.ok:
            raise RuntimeError(f"Architect agent failed ({arch_res.profile_name}): {arch_res.stderr}")

        plan_markdown = arch_res.stdout.strip()
        trace.append({
            "step": "2. Architect Draft",
            "agent": arch_res.profile_name,
            "model": arch_res.model_used,
            "outcome": "Initial RFC & DAG drafted",
        })

        # Step 3: Critic <-> Architect Refinement Loop
        for r in range(1, total_rounds):
            critic_prompt = self._build_critic_prompt(goal, context_brief, plan_markdown, round_num=r)
            crit_res = self.runner.run(
                profile_name=critic_profile,
                prompt=critic_prompt,
                workdir=self.repo_root,
                read_only=True,
                model_override=model_override,
            )
            if not crit_res.ok or not crit_res.stdout.strip():
                trace.append({
                    "step": f"3.{r} Critic",
                    "agent": critic_profile,
                    "model": crit_res.model_used,
                    "outcome": f"Skipped (exit {crit_res.returncode})",
                })
                break

            critique_text = crit_res.stdout.strip()
            status_match = self.CRITIC_STATUS_RE.search(critique_text)
            verdict = status_match.group(1).upper() if status_match else "NEEDS_REVISION"
            trace.append({
                "step": f"3.{r} Critic",
                "agent": crit_res.profile_name,
                "model": crit_res.model_used,
                "outcome": verdict,
            })

            if verdict == "APPROVED":
                break

            refiner_prompt = self._build_refiner_prompt(goal, context_brief, plan_markdown, critique_text)
            ref_res = self.runner.run(
                profile_name=profile,
                prompt=refiner_prompt,
                workdir=self.repo_root,
                read_only=True,
                model_override=model_override,
            )
            if ref_res.ok and ref_res.stdout.strip():
                plan_markdown = ref_res.stdout.strip()
                trace.append({
                    "step": f"3.{r} Refine",
                    "agent": ref_res.profile_name,
                    "model": ref_res.model_used,
                    "outcome": "Plan refined from critic feedback",
                })

        # Step 4: Parse & Validate DAG (with 1 automatic JSON repair pass if malformed)
        try:
            tasks = self.parse_tasks_from_markdown(plan_markdown)
        except ValueError as err:
            repair_prompt = (
                f"Your previous plan output could not be parsed into a valid JSON task DAG ({err}).\n"
                f"Preserve the RFC and output a valid fenced ```json ... ``` block with a `tasks` array.\n\n"
                f"Previous output:\n{plan_markdown}"
            )
            rep_res = self.runner.run(
                profile_name=profile,
                prompt=repair_prompt,
                workdir=self.repo_root,
                read_only=True,
                model_override=model_override,
            )
            if not rep_res.ok:
                raise ValueError(f"Could not parse valid JSON task DAG and repair failed: {err}") from err
            plan_markdown = rep_res.stdout.strip()
            trace.append({
                "step": "4. JSON Repair",
                "agent": rep_res.profile_name,
                "model": rep_res.model_used,
                "outcome": "Repaired JSON DAG block",
            })
            tasks = self.parse_tasks_from_markdown(plan_markdown)

        self.last_planning_trace = trace
        return plan_markdown, tasks

    @classmethod
    def parse_tasks_from_markdown(cls, markdown_text: str) -> List[TaskSpec]:
        """Extracts the JSON task DAG from the planner output or plan file."""
        match = cls.PLAN_JSON_RE.search(markdown_text)
        if not match:
            try:
                data = json.loads(markdown_text)
            except Exception as e:
                raise ValueError(f"Could not find valid JSON task DAG block in plan: {e}")
        else:
            data = json.loads(match.group(1))

        tasks: List[TaskSpec] = []
        for item in data.get("tasks", []):
            tasks.append(
                TaskSpec(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    role=str(item.get("role", "worker")),
                    depends_on=[str(d) for d in item.get("depends_on", [])],
                    target_files=[str(f) for f in item.get("target_files", [])],
                    verification_cmd=str(item.get("verification_cmd", "pytest")),
                    description=str(item.get("description", "")),
                    acceptance_criteria=[str(c) for c in item.get("acceptance_criteria", [])],
                    goal_refs=[str(g) for g in item.get("goal_refs", [])],
                    interfaces=[str(i) for i in item.get("interfaces", [])],
                    edge_cases=[str(e) for e in item.get("edge_cases", [])],
                )
            )
        cls.validate_dag(tasks)
        return tasks

    @staticmethod
    def validate_dag(tasks: List[TaskSpec]) -> None:
        """Verifies that task IDs are unique and there are no circular dependencies."""
        by_id = {t.id: t for t in tasks}
        if len(by_id) != len(tasks):
            raise ValueError("Duplicate task IDs detected in plan.")

        visited: Dict[str, int] = {}

        def dfs(tid: str) -> None:
            state = visited.get(tid, 0)
            if state == 1:
                raise ValueError(f"Cycle detected in task DAG involving task '{tid}'")
            if state == 2:
                return
            visited[tid] = 1
            for dep in by_id[tid].depends_on:
                if dep in by_id:
                    dfs(dep)
            visited[tid] = 2

        for t in tasks:
            dfs(t.id)

    def create_epic_for_approval(
        self,
        goal_title: str,
        plan_markdown: str,
        tasks: List[TaskSpec],
        planning_trace: Optional[List[Dict[str, str]]] = None,
    ) -> int:
        """
        Phase 1 Completion:
        Saves the plan under `docs/plans/`, creates the Master Epic Issue on GitHub
        with `type:epic` and `status:awaiting-approval`, and embeds the serialized DAG.
        """
        self.bus.bootstrap_labels()
        trace = planning_trace if planning_trace is not None else self.last_planning_trace

        dag_meta = {
            "goal": goal_title,
            "planning_trace": trace,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "role": t.role,
                    "goal_refs": t.goal_refs,
                    "depends_on": t.depends_on,
                    "target_files": t.target_files,
                    "interfaces": t.interfaces,
                    "edge_cases": t.edge_cases,
                    "verification_cmd": t.verification_cmd,
                    "description": t.description,
                    "acceptance_criteria": t.acceptance_criteria,
                }
                for t in tasks
            ],
            "approved": False,
            "spawned_issues": {},
        }

        tasks_table = "\n".join(
            f"| `{t.id}` | **{t.title}** | `{t.role}` | {', '.join(f'`{g}`' for g in t.goal_refs) or '—'} | "
            f"{', '.join(f'`{d}`' for d in t.depends_on) or 'None (Ready)'} | `{t.verification_cmd}` |"
            for t in tasks
        )

        trace_section = ""
        if trace:
            trace_rows = "\n".join(
                f"| {row['step']} | `{row['agent']}` | `{row['model']}` | {row['outcome']} |"
                for row in trace
            )
            trace_section = f"""
## 🤖 Agent Planning Trace
| Step | Agent | Model | Outcome |
| :--- | :--- | :--- | :--- |
{trace_rows}

---
"""

        body = f"""<!-- OCTO-META:EPIC_DAG {json.dumps(dag_meta, ensure_ascii=False)} -->
# 🏛️ Architecture & Execution Plan: {goal_title}

> [!IMPORTANT]
> **Human-in-the-Loop Gate (`status:awaiting-approval`)**
> This Epic is paused in the **Planning Phase**. No workers will write code until approved:
> - **GitHub UI**: Comment `/approve` on this Issue.
> - **CLI**: Run `./bin/octo approve <issue-number>`.

---
{trace_section}
## 📊 Proposed Task DAG ({len(tasks)} Tasks)

| ID | Task Title | Role | Goals | Depends On | Verification Command |
| :--- | :--- | :--- | :--- | :--- | :--- |
{tasks_table}

---

## 📐 Detailed Architecture RFC & Specification

{plan_markdown.strip()}
"""
        epic_num = self.bus.create_issue(
            title=f"[EPIC] {goal_title}",
            body=body,
            labels=["type:epic", "status:awaiting-approval"],
        )

        plans_dir = self.repo_root / self.config.get("project", {}).get("plans_dir", "docs/plans")
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plans_dir / f"EPIC-{epic_num}.md"
        plan_file.write_text(body, encoding="utf-8")

        all_files: List[str] = []
        for t in tasks:
            all_files.extend(t.target_files)
        self.c2c.register(epic_num, goal_title, all_files)
        self.board.post(
            sender=self.c2c.coordinator_id,
            recipient="@all",
            text=f"@all Swarm initialized for Epic #{epic_num}: {goal_title}. Paused at status:awaiting-approval.",
            channel="main",
            metadata={"epic": epic_num, "tasks_count": len(tasks)},
        )

        return epic_num

    def approve_and_spawn_dag(self, epic_number: int) -> Dict[str, int]:
        """
        Phase 2 Transition (Planning -> Implementation):
        Reads the Master Epic Issue, spawns Epic-qualified GitHub Issues (`[E<epic>-T<n>]`,
        label `epic:<epic>`), maps symbolic `depends_on` (`T1`) to real Issue numbers (`#102`),
        and sets root tasks to `status:ready` and dependent tasks to `status:blocked`.
        """
        epic = self.bus.get_issue(epic_number)
        body = epic.get("body", "")

        dag_meta = None
        for match in ProtocolParser.METADATA_RE.finditer(body):
            if match.group(1).upper() == "EPIC_DAG":
                dag_meta = json.loads(match.group(2))
                break

        if not dag_meta:
            raise ValueError(f"Issue #{epic_number} does not contain an OCTO-META:EPIC_DAG block.")

        if dag_meta.get("approved") and dag_meta.get("spawned_issues"):
            return {k: int(v) for k, v in dag_meta["spawned_issues"].items()}

        epic_title = str(
            dag_meta.get("goal")
            or epic.get("title", "").removeprefix("[EPIC]").strip()
        )
        epic_label = f"epic:{epic_number}"
        self.bus.ensure_label(
            epic_label,
            color="BFDADC",
            description=f"Tasks belonging to Epic #{epic_number}",
        )

        raw_tasks = dag_meta.get("tasks", [])
        tasks = [
            TaskSpec(
                id=t["id"],
                title=t["title"],
                role=t.get("role", "worker"),
                depends_on=t.get("depends_on", []),
                target_files=t.get("target_files", []),
                verification_cmd=t.get("verification_cmd", "pytest"),
                description=t.get("description", ""),
                acceptance_criteria=t.get("acceptance_criteria", []),
                goal_refs=t.get("goal_refs", []),
                interfaces=t.get("interfaces", []),
                edge_cases=t.get("edge_cases", []),
            )
            for t in raw_tasks
        ]

        id_to_issue: Dict[str, int] = {}

        def _resolve_deps(raw_deps: List[str]) -> Tuple[List[str], List[str]]:
            meta_deps: List[str] = []
            display_deps: List[str] = []
            for d in raw_deps:
                if d in id_to_issue:
                    inum = id_to_issue[d]
                    meta_deps.append(f"#{inum}")
                    display_deps.append(f"#{inum} (`{qualify_task_id(epic_number, d)}`)")
                else:
                    meta_deps.append(d)
                    display_deps.append(d)
            return meta_deps, display_deps

        # Pass 1: Create Epic-qualified issues
        for t in tasks:
            qualified_id = qualify_task_id(epic_number, t.id)
            resolved_deps, display_deps = _resolve_deps(t.depends_on)
            t_resolved = TaskSpec(
                id=t.id,
                title=t.title,
                role=t.role,
                depends_on=resolved_deps,
                target_files=t.target_files,
                verification_cmd=t.verification_cmd,
                description=t.description,
                acceptance_criteria=t.acceptance_criteria,
                goal_refs=t.goal_refs,
                interfaces=t.interfaces,
                edge_cases=t.edge_cases,
            )

            is_blocked = len(t_resolved.depends_on) > 0
            initial_status = "status:blocked" if is_blocked else "status:ready"
            role_label = "type:qa" if t.role in ("qa", "test") else "type:task"

            issue_body = ProtocolParser.format_task_issue_body(
                epic_number,
                t_resolved,
                epic_title=epic_title,
                depends_on_display=display_deps,
            )
            issue_num = self.bus.create_issue(
                title=f"[{qualified_id}] {t.title}",
                body=issue_body,
                labels=[role_label, initial_status, epic_label],
            )
            id_to_issue[t.id] = issue_num

        # Pass 2: Resolve any forward dependencies
        for t in tasks:
            inum = id_to_issue[t.id]
            resolved_deps, display_deps = _resolve_deps(t.depends_on)
            if resolved_deps != t.depends_on:
                t.depends_on = resolved_deps
                self.bus.update_issue_body(
                    inum,
                    ProtocolParser.format_task_issue_body(
                        epic_number,
                        t,
                        epic_title=epic_title,
                        depends_on_display=display_deps,
                    ),
                )

        # Update Epic Issue with native GitHub Sub-Issue checklist and mark status:in-progress
        dag_meta["approved"] = True
        dag_meta["spawned_issues"] = id_to_issue
        checklist_lines = [
            f"- [ ] #{id_to_issue[t.id]} — **{qualify_task_id(epic_number, t.id)}: {t.title}** (`{t.role}`)"
            for t in tasks
        ]
        checklist_block = "\n## 🚀 Spawned Sub-Issues (Live Kanban Tracker)\n" + "\n".join(checklist_lines) + "\n"

        updated_body = ProtocolParser.METADATA_RE.sub(
            f"<!-- OCTO-META:EPIC_DAG {json.dumps(dag_meta, ensure_ascii=False)} -->",
            body,
            count=1,
        )
        updated_body += "\n" + checklist_block
        self.bus.update_issue_body(epic_number, updated_body)
        self.bus.transition_issue_state(epic_number, "status:in-progress")
        self.bus.post_protocol_message(
            epic_number,
            "DECISION",
            f"✅ **Plan Approved!** Spawned {len(id_to_issue)} sub-issues (`{epic_label}`) into the Kanban board.\n"
            + "\n".join(checklist_lines),
            {"approved": True, "spawned_issues": id_to_issue},
        )

        # Notify Swarm Board
        self.board.post(
            sender=self.c2c.coordinator_id,
            recipient="@all",
            text=f"@all Plan Approved for Epic #{epic_number}! Spawned {len(id_to_issue)} tasks:\n" + "\n".join(checklist_lines),
            channel="main",
            metadata={"epic": epic_number, "spawned": id_to_issue},
        )
        for t in tasks:
            inum = id_to_issue[t.id]
            qid = qualify_task_id(epic_number, t.id)
            is_ready = not t.depends_on
            self.board.post(
                sender=self.c2c.coordinator_id,
                recipient=f"@{t.role}",
                text=f"@{t.role} Task [{qid}] #{inum} (Epic #{epic_number}): {t.title} ({'status:ready' if is_ready else 'status:blocked'}). Target files: {', '.join(t.target_files) if t.target_files else 'TBD'}.",
                channel="main",
                metadata={"task_id": qid, "epic": epic_number, "issue": inum, "ready": is_ready},
            )

        return id_to_issue

    def reconcile_dag_and_unblock(self) -> Dict[str, Any]:
        """
        Scans all open issues in the repository:
        1. Approves any `status:awaiting-approval` Epic that received `/approve`.
        2. Unblocks `status:blocked` tasks once all `Depends-On` issues are closed/done.
        3. Closes open Epics once all spawned sub-issues are closed.
        """
        summary: Dict[str, Any] = {
            "auto_approved_epics": [],
            "unblocked_tasks": [],
            "completed_epics": [],
        }

        all_issues = self.bus.list_issues(state="all", limit=200)
        by_num = {iss["number"]: iss for iss in all_issues}

        for iss in all_issues:
            if iss["state"].upper() != "OPEN":
                continue
            labels = [l["name"] for l in iss.get("labels", [])]
            if "type:epic" in labels and "status:awaiting-approval" in labels:
                for c in iss.get("comments", []):
                    cbody = (c.get("body") or "").strip()
                    if re.search(r"(?:^|\s)/approve\b", cbody, re.IGNORECASE):
                        self.approve_and_spawn_dag(iss["number"])
                        summary["auto_approved_epics"].append(iss["number"])
                        break

        for iss in all_issues:
            if iss["state"].upper() != "OPEN":
                continue
            labels = [l["name"] for l in iss.get("labels", [])]
            if "status:blocked" in labels:
                spec = ProtocolParser.parse_task_issue_body(iss.get("body", ""))
                deps = spec.get("depends_on", [])
                all_deps_done = True
                for d in deps:
                    dep_num_str = str(d).lstrip("#")
                    if dep_num_str.isdigit():
                        dep_iss = by_num.get(int(dep_num_str))
                        if not dep_iss:
                            all_deps_done = False
                            break
                        dep_labels = [l["name"] for l in dep_iss.get("labels", [])]
                        if dep_iss["state"].upper() != "CLOSED" and "status:done" not in dep_labels:
                            all_deps_done = False
                            break
                if all_deps_done:
                    self.bus.transition_issue_state(iss["number"], "status:ready")
                    self.bus.post_protocol_message(
                        iss["number"],
                        "DECISION",
                        f"🟢 **Dependencies Satisfied ({', '.join(deps)})** — Task automatically unlocked and moved to `status:ready`.",
                        {"unblocked_dependencies": deps},
                    )
                    summary["unblocked_tasks"].append(iss["number"])

        for iss in all_issues:
            if iss["state"].upper() != "OPEN":
                continue
            labels = [l["name"] for l in iss.get("labels", [])]
            if "type:epic" in labels and "status:in-progress" in labels:
                for match in ProtocolParser.METADATA_RE.finditer(iss.get("body", "")):
                    if match.group(1).upper() == "EPIC_DAG":
                        dag_meta = json.loads(match.group(2))
                        spawned = dag_meta.get("spawned_issues", {})
                        if spawned and all(
                            by_num.get(int(num), {}).get("state", "").upper() == "CLOSED"
                            for num in spawned.values()
                        ):
                            self.bus.close_issue(
                                iss["number"],
                                comment="🎉 **[EPIC COMPLETED]** All sub-issues have passed QA review and are merged!",
                            )
                            summary["completed_epics"].append(iss["number"])

        return summary
