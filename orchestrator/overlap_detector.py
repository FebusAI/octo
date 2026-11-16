"""
Real-Time Code Overlap & Conflict Detection Engine (`overlap_detector.py`).
Implements proactive collision avoidance across distributed workers:
- Analyzes `target_files` across active tasks, Git worktrees, and Pull Requests
- Inspects Python imports and symbols to detect cross-worker interface dependencies
- Identifies:
  1. DIRECT_COLLISION (two workers editing the exact same file concurrently)
  2. INTERFACE_DEPENDENCY (one worker editing a module/schema while another imports it)
  3. PACKAGE_CONGESTION (multiple workers in the same subfolder)
- Generates proactive alerts for Swarm Coordinators to negotiate boundaries automatically
"""

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import ProtocolParser


@dataclass
class OverlapReport:
    level: str  # "CRITICAL_COLLISION" | "INTERFACE_DEPENDENCY" | "PACKAGE_CONGESTION" | "SAFE"
    message: str
    conflicting_tasks: List[int]
    conflicting_files: List[str]
    suggested_action: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class OverlapDetector:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def extract_python_imports(self, file_path: Path) -> Set[str]:
        """Extracts top-level imported module and symbol names from a Python source file."""
        if not file_path.exists() or file_path.suffix != ".py":
            return set()
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            return imports
        except Exception:
            return set()

    def get_worktree_modified_files(self, worktree_dir: Path) -> List[str]:
        """Returns files currently modified or staged in an isolated worktree."""
        if not worktree_dir.exists():
            return []
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree_dir),
                capture_output=True,
                text=True,
            )
            files = []
            for line in res.stdout.splitlines():
                if len(line) > 3:
                    files.append(line[3:].strip())
            return files
        except Exception:
            return []

    def check_overlaps(
        self,
        active_tasks: List[Dict[str, Any]],
        worktrees_root: Optional[Path] = None,
    ) -> List[OverlapReport]:
        """
        Cross-analyzes all active tasks (`status:in-progress`, `status:ready`, `status:in-review`).
        Each task dict contains: {"number": int, "title": str, "body": str, "labels": [...], "claim": dict}
        """
        reports: List[OverlapReport] = []
        wt_root = worktrees_root or (self.repo_root / ".worktrees")

        # Map task -> set of files (declared in body + detected in worktree)
        task_files: Dict[int, Set[str]] = {}
        task_info: Dict[int, Dict[str, Any]] = {}

        for t in active_tasks:
            inum = int(t.get("number", 0))
            if not inum:
                continue
            spec = ProtocolParser.parse_task_issue_body(t.get("body", ""))
            files = set(spec.get("target_files", []))

            # Also inspect live worktree for real disk changes
            wt_dir = wt_root / f"issue-{inum}"
            live_files = self.get_worktree_modified_files(wt_dir)
            files.update(live_files)

            clean_files = {f.strip().lstrip("./") for f in files if f.strip()}
            task_files[inum] = clean_files

            # Support both [{"name": "status:..."}] and ["status:..."]
            raw_labels = t.get("labels", [])
            label_names = [l.get("name", "") if isinstance(l, dict) else str(l) for l in raw_labels]
            status_lbl = next((l for l in label_names if l.startswith("status:")), "unknown")

            task_info[inum] = {
                "title": t.get("title", ""),
                "claim": t.get("claim"),
                "status": status_lbl,
            }

        task_nums = sorted(list(task_files.keys()))

        # Pairwise comparison across active tasks
        for i in range(len(task_nums)):
            for j in range(i + 1, len(task_nums)):
                t1 = task_nums[i]
                t2 = task_nums[j]
                f1 = task_files[t1]
                f2 = task_files[t2]

                # 1. Direct File Collision Check
                common_files = sorted(list(f1.intersection(f2)))
                if common_files:
                    c1 = task_info[t1]["claim"] or {}
                    c2 = task_info[t2]["claim"] or {}
                    n1 = c1.get("node_id", "unassigned")
                    n2 = c2.get("node_id", "unassigned")

                    reports.append(
                        OverlapReport(
                            level="CRITICAL_COLLISION",
                            message=(
                                f"🚨 Direct File Collision: Issue #{t1} ({n1}) and Issue #{t2} ({n2}) "
                                f"are both targeting the exact same file(s): {', '.join(common_files)}!"
                            ),
                            conflicting_tasks=[t1, t2],
                            conflicting_files=common_files,
                            suggested_action="Sequence tasks (add Depends-On) or have Coordinators split file ownership.",
                            metadata={"task_1": task_info[t1], "task_2": task_info[t2]},
                        )
                    )
                    continue

                # 2. Interface / Schema Dependency Check
                # If t1 modifies module M, and t2 imports M (or vice versa)
                for file_a in f1:
                    mod_a = Path(file_a).stem
                    for file_b in f2:
                        path_b = self.repo_root / file_b
                        imports_b = self.extract_python_imports(path_b)
                        if mod_a in imports_b:
                            reports.append(
                                OverlapReport(
                                    level="INTERFACE_DEPENDENCY",
                                    message=(
                                        f"⚠️ Interface Dependency: Issue #{t1} modifies `{file_a}` "
                                        f"while Issue #{t2} imports `{mod_a}` in `{file_b}`."
                                    ),
                                    conflicting_tasks=[t1, t2],
                                    conflicting_files=[file_a, file_b],
                                    suggested_action="Coordinators should exchange interface contracts (C2C) before finalizing changes.",
                                    metadata={"producer_task": t1, "consumer_task": t2, "module": mod_a},
                                )
                            )

                # 3. Same-Directory / Package Congestion
                dirs_1 = {os.path.dirname(f) for f in f1 if os.path.dirname(f)}
                dirs_2 = {os.path.dirname(f) for f in f2 if os.path.dirname(f)}
                common_dirs = dirs_1.intersection(dirs_2)
                if common_dirs:
                    reports.append(
                        OverlapReport(
                            level="PACKAGE_CONGESTION",
                            message=f"ℹ️ Package Proximity: Issues #{t1} and #{t2} are both active in directory `{list(common_dirs)[0]}`.",
                            conflicting_tasks=[t1, t2],
                            conflicting_files=sorted(list(f1.union(f2))),
                            suggested_action="Monitor for implicit namespace or import conflicts during PR review.",
                            metadata={"common_dirs": list(common_dirs)},
                        )
                    )

        return reports
