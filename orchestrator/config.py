"""
Configuration loader, Agent spec parser, and structured protocol definitions
for GitHub-native Multi-Agent Orchestration (`config.py`).
Zero external dependencies required (uses PyYAML if available, with built-in fallback parser).
"""

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_yaml_text(text: str) -> Dict[str, Any]:
    """Parse YAML text using PyYAML if installed, or fallback to lightweight parser."""
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        return _simple_yaml_parse(text)


def load_yaml_file(path: Path) -> Dict[str, Any]:
    """Load YAML configuration from disk."""
    if not path.exists():
        return {}
    return load_yaml_text(path.read_text(encoding="utf-8"))


def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merges `override` into a copy of `base`."""
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = deep_merge_dicts(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(repo_root: Path) -> Dict[str, Any]:
    """
    Loads `orchestrator.yaml` and optionally overlays `orchestrator.local.yaml`
    (gitignored) so local IPs, SSH users, and private models stay out of git.
    """
    base = load_yaml_file(repo_root / "orchestrator.yaml")
    local = load_yaml_file(repo_root / "orchestrator.local.yaml")
    if local:
        return deep_merge_dicts(base, local)
    return base


def _simple_yaml_parse(text: str) -> Dict[str, Any]:
    """Lightweight indentation-based YAML subset parser for zero-dependency nodes."""
    result: Dict[str, Any] = {}
    stack: List[tuple[int, Any]] = [(-1, result)]
    lines = text.splitlines()

    for raw_line in lines:
        line_no_comment = re.sub(r'\s+#.*$', '', raw_line) if not raw_line.lstrip().startswith('#') else ''
        if not line_no_comment.strip():
            continue
        indent = len(line_no_comment) - len(line_no_comment.lstrip(' '))
        stripped = line_no_comment.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if stripped.startswith("- "):
            val_str = stripped[2:].strip()
            val = _parse_scalar(val_str)
            if isinstance(parent, list):
                parent.append(val)
        elif ":" in stripped:
            key, _, val_str = stripped.partition(":")
            key = key.strip().strip('"').strip("'")
            val_str = val_str.strip()
            if not val_str:
                new_container: Any = {}
                if isinstance(parent, dict):
                    parent[key] = new_container
                    stack.append((indent, new_container))
            else:
                val = _parse_scalar(val_str)
                if isinstance(parent, dict):
                    parent[key] = val
                    if isinstance(val, list) and not val:
                        stack.append((indent, val))
    return result


def _parse_scalar(val_str: str) -> Any:
    if val_str.startswith("[") and val_str.endswith("]"):
        try:
            return json.loads(val_str)
        except Exception:
            return [x.strip().strip('"').strip("'") for x in val_str[1:-1].split(",") if x.strip()]
    if val_str.lower() == "true":
        return True
    if val_str.lower() == "false":
        return False
    if re.match(r'^-?\d+$', val_str):
        return int(val_str)
    if re.match(r'^-?\d+\.\d+$', val_str):
        return float(val_str)
    return val_str.strip('"').strip("'")


@dataclass
class AgentSpec:
    """
    Declarative Agent definition loaded from `agents/<name>.md` (and/or `orchestrator.yaml`).
    Octo orchestrates agents; each agent declares its own harness, model, and system prompt.
    """
    name: str
    description: str = ""
    harness: str = "claude"           # claude | pi | aider | agy | custom | mock
    profile: str = ""                 # Optional reference to harnesses.profiles.<name>
    provider: str = ""
    model: str = ""
    thinking: str = ""
    endpoint: str = ""
    api_base: str = ""
    api_key: str = ""
    read_only: bool = False
    tools: str = ""
    extra_args: List[str] = field(default_factory=list)
    script: str = ""                  # For mock harness
    command: List[str] = field(default_factory=list)  # For custom harness
    system_prompt: str = ""
    source_path: Optional[Path] = None


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_agent_markdown(text: str, default_name: str, source_path: Optional[Path] = None) -> AgentSpec:
    """Parses an `agents/<name>.md` file with YAML frontmatter + Markdown system prompt."""
    meta: Dict[str, Any] = {}
    body = text.strip()
    m = _FRONTMATTER_RE.match(text)
    if m:
        meta = load_yaml_text(m.group(1))
        body = m.group(2).strip()

    extra = meta.get("extra_args", [])
    if not isinstance(extra, list):
        extra = [str(extra)]

    cmd = meta.get("command", [])
    if not isinstance(cmd, list):
        cmd = [str(cmd)]

    return AgentSpec(
        name=str(meta.get("name") or default_name),
        description=str(meta.get("description", "")),
        harness=str(meta.get("harness") or meta.get("adapter") or ""),
        profile=str(meta.get("profile", "")),
        provider=str(meta.get("provider", "")),
        model=str(meta.get("model", "")),
        thinking=str(meta.get("thinking", "")),
        endpoint=str(meta.get("endpoint", "")),
        api_base=str(meta.get("api_base", "")),
        api_key=str(meta.get("api_key", "")),
        read_only=bool(meta.get("read_only", False)),
        tools=str(meta.get("tools", "")),
        extra_args=[str(x) for x in extra],
        script=str(meta.get("script", "")),
        command=[str(x) for x in cmd],
        system_prompt=body,
        source_path=source_path,
    )


def load_agent_spec(agent_name: str, repo_root: Path, config: Optional[Dict[str, Any]] = None) -> Optional[AgentSpec]:
    """
    Loads an AgentSpec by name.
    Precedence:
      1. `agents/<name>.local.md` (gitignored local override)
      2. `agents/<name>.md`
      3. `config["agents"][name]` in orchestrator.yaml / orchestrator.local.yaml
    If `config["agents"][name]` exists alongside a `.md` file, YAML keys override frontmatter.
    """
    agents_dir = repo_root / "agents"
    local_md = agents_dir / f"{agent_name}.local.md"
    base_md = agents_dir / f"{agent_name}.md"

    spec: Optional[AgentSpec] = None
    if local_md.exists():
        spec = parse_agent_markdown(local_md.read_text(encoding="utf-8"), agent_name, local_md)
    elif base_md.exists():
        spec = parse_agent_markdown(base_md.read_text(encoding="utf-8"), agent_name, base_md)

    yaml_agent = (config or {}).get("agents", {}).get(agent_name)
    if isinstance(yaml_agent, dict):
        if spec is None:
            spec = AgentSpec(name=agent_name)
        for field_name in (
            "description", "harness", "profile", "provider", "model",
            "thinking", "endpoint", "api_base", "api_key", "tools", "script",
        ):
            if field_name in yaml_agent and yaml_agent[field_name] not in (None, ""):
                setattr(spec, field_name, str(yaml_agent[field_name]))
        if "adapter" in yaml_agent and yaml_agent["adapter"]:
            spec.harness = str(yaml_agent["adapter"])
        if "read_only" in yaml_agent:
            spec.read_only = bool(yaml_agent["read_only"])
        if "extra_args" in yaml_agent and isinstance(yaml_agent["extra_args"], list):
            spec.extra_args = [str(x) for x in yaml_agent["extra_args"]]
        if "command" in yaml_agent and isinstance(yaml_agent["command"], list):
            spec.command = [str(x) for x in yaml_agent["command"]]
        if "prompt_file" in yaml_agent and yaml_agent["prompt_file"]:
            pfile = repo_root / str(yaml_agent["prompt_file"])
            if pfile.exists():
                spec.system_prompt = pfile.read_text(encoding="utf-8").strip()
                spec.source_path = pfile

    return spec


def list_agent_specs(repo_root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, AgentSpec]:
    """Discovers all defined agents from `agents/*.md` and `config['agents']`."""
    names: List[str] = []
    agents_dir = repo_root / "agents"
    if agents_dir.exists():
        for p in sorted(agents_dir.glob("*.md")):
            stem = p.name
            if stem.endswith(".local.md"):
                name = stem[:-len(".local.md")]
            else:
                name = stem[:-len(".md")]
            if name not in names:
                names.append(name)
    for name in (config or {}).get("agents", {}).keys():
        if name not in names:
            names.append(name)

    result: Dict[str, AgentSpec] = {}
    for name in names:
        spec = load_agent_spec(name, repo_root, config)
        if spec:
            result[name] = spec
    return result


def qualify_task_id(epic_number: Optional[int], task_id: str) -> str:
    """
    Qualifies a plan-local task ID (`T1`, `T2`) with its parent Epic number (`E5-T1`)
    so tasks from different Epics never collide on the Kanban board.
    """
    clean_id = task_id.strip()
    if not epic_number:
        return clean_id
    prefix = f"E{epic_number}-"
    if clean_id.upper().startswith(prefix.upper()):
        return f"E{epic_number}-{clean_id[len(prefix):]}"
    return f"{prefix}{clean_id}"


@dataclass
class TaskSpec:
    """Structured representation of a GitHub Issue Task in the DAG."""
    id: str                           # Local task ID inside the Epic plan (e.g. "T1", "T2")
    title: str
    role: str = "worker"              # worker | qa | architect
    depends_on: List[str] = field(default_factory=list)  # List of task IDs or Issue numbers (#12)
    target_files: List[str] = field(default_factory=list)
    verification_cmd: str = ""
    description: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    goal_refs: List[str] = field(default_factory=list)
    interfaces: List[str] = field(default_factory=list)
    edge_cases: List[str] = field(default_factory=list)
    issue_number: Optional[int] = None


@dataclass
class ClaimMetadata:
    """Metadata embedded in [AGENT-CLAIM] and [AGENT-HEARTBEAT] comments."""
    node_id: str
    hostname: str
    pid: int
    harness: str
    model: str
    claim_uuid: str
    claimed_at: str
    lease_expires_at: str
    wip_branch: str
    status: str = "active"            # active | released | completed | handoff


class ProtocolParser:
    """
    Parses and formats structured metadata inside GitHub Issue bodies and comments
    so agents on different PCs never lose the thread.
    """

    METADATA_RE = re.compile(
        r"<!--\s*OCTO-META:(\w+)\s*(\{.*?\})\s*-->",
        re.DOTALL,
    )

    @classmethod
    def format_comment(cls, msg_type: str, summary_markdown: str, meta: Dict[str, Any]) -> str:
        """
        Formats a GitHub Issue comment with both human-readable Markdown and a
        machine-parseable JSON block in an HTML comment.
        """
        meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        header_badge = {
            "CLAIM": "🔒 **[AGENT-CLAIM]**",
            "HEARTBEAT": "💓 **[AGENT-HEARTBEAT]**",
            "CHECKPOINT": "💾 **[AGENT-CHECKPOINT]**",
            "QUESTION": "❓ **[AGENT-QUESTION]**",
            "DECISION": "⚖️ **[ORCHESTRATOR-DECISION]**",
            "HANDOVER": "🤝 **[AGENT-HANDOVER]**",
            "QA_REPORT": "🛡️ **[QA & CODE REVIEW REPORT]**",
            "PLAN": "📐 **[ARCHITECTURE & EXECUTION PLAN]**",
            "SWARM": "🐝 **[SWARM-MESSAGE]**",
            "C2C": "🌐 **[C2C-MESSAGE]**",
        }.get(msg_type.upper(), f"🤖 **[{msg_type.upper()}]**")

        return (
            f"<!-- OCTO-META:{msg_type.upper()} {meta_json} -->\n"
            f"{header_badge}\n\n"
            f"{summary_markdown.strip()}\n"
        )

    @classmethod
    def extract_comments_meta(cls, comments: List[Dict[str, Any]], msg_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Extracts all structured OCTO-META blocks from a list of GitHub comments."""
        extracted = []
        for c in comments:
            body = c.get("body", "") or ""
            for match in cls.METADATA_RE.finditer(body):
                found_type = match.group(1).upper()
                if msg_type and found_type != msg_type.upper():
                    continue
                try:
                    data = json.loads(match.group(2))
                    data["_comment_id"] = c.get("id")
                    data["_created_at"] = c.get("createdAt") or c.get("created_at")
                    data["_author"] = (c.get("author") or {}).get("login") or (c.get("user") or {}).get("login")
                    data["_type"] = found_type
                    extracted.append(data)
                except json.JSONDecodeError:
                    continue
        return extracted

    @classmethod
    def format_task_issue_body(
        cls,
        epic_number: Optional[int],
        task: TaskSpec,
        epic_title: Optional[str] = None,
        depends_on_display: Optional[List[str]] = None,
    ) -> str:
        """Creates a zero-context-loss GitHub Issue body for a sub-agent worker."""
        qualified_id = qualify_task_id(epic_number, task.id)
        meta = {
            "task_id": qualified_id,
            "local_task_id": task.id,
            "epic_issue": epic_number,
            "epic_title": epic_title or "",
            "role": task.role,
            "goal_refs": task.goal_refs,
            "depends_on": task.depends_on,
            "target_files": task.target_files,
            "verification_cmd": task.verification_cmd,
        }
        meta_json = json.dumps(meta, ensure_ascii=False)
        dep_items = depends_on_display if depends_on_display is not None else [str(d) for d in task.depends_on]
        deps_str = ", ".join(dep_items) if dep_items else "None (Root Task)"
        files_str = "\n".join(f"- `{f}`" for f in task.target_files) if task.target_files else "- *(See specification)*"
        criteria_str = (
            "\n".join(f"- [ ] {c}" for c in task.acceptance_criteria)
            if task.acceptance_criteria
            else "- [ ] Implementation matches specification and passes verification command."
        )

        if epic_number:
            epic_ref = f"Epic #{epic_number}" + (f" — *{epic_title}*" if epic_title else "")
        else:
            epic_ref = "Standalone Task"

        goals_str = ", ".join(f"`{g}`" for g in task.goal_refs) if task.goal_refs else "*(See Epic RFC)*"

        extra_sections = ""
        if task.interfaces:
            iface_lines = "\n".join(f"- {item}" for item in task.interfaces)
            extra_sections += f"\n## 🔌 Interfaces & Contracts\n{iface_lines}\n"
        if task.edge_cases:
            edge_lines = "\n".join(f"- {item}" for item in task.edge_cases)
            extra_sections += f"\n## ⚠️ Edge Cases & Failure Modes\n{edge_lines}\n"

        return f"""<!-- OCTO-META:TASK_SPEC {meta_json} -->
> **Parent Epic**: {epic_ref}
> **Task ID**: `{qualified_id}` | **Role**: `{task.role}` | **Goal Refs**: {goals_str}

## 🎯 Objective & Specification
{task.description.strip()}

---

## 🔗 Dependencies (`Depends-On`)
- **Upstream Tasks**: {deps_str}

## 📂 Target Files & Scope
{files_str}
{extra_sections}
## ✅ Acceptance Criteria & Definition of Done
{criteria_str}

## 🧪 Mandatory Verification Command
```bash
{task.verification_cmd or "pytest"}
```

---
> 🤖 *Managed by `octo`. Claim via `octo worker`, work in isolated worktree `feat/issue-<N>`, and submit a PR for QA review.*
"""

    @classmethod
    def parse_task_issue_body(cls, body: str) -> Dict[str, Any]:
        """Extracts TASK_SPEC metadata from an issue body, falling back to regex parsing."""
        for match in cls.METADATA_RE.finditer(body or ""):
            if match.group(1).upper() == "TASK_SPEC":
                try:
                    return json.loads(match.group(2))
                except json.JSONDecodeError:
                    pass

        deps = re.findall(r"(?:Depends-On|Blocked-By|Upstream Tasks).*?#(\d+)", body or "", re.IGNORECASE)
        cmd_match = re.search(r"## 🧪 Mandatory Verification.*?\n```(?:bash|sh)?\n(.*?)\n```", body or "", re.DOTALL)
        return {
            "task_id": "MANUAL",
            "epic_issue": None,
            "epic_title": "",
            "role": "worker",
            "depends_on": [f"#{d}" for d in deps],
            "target_files": [],
            "verification_cmd": cmd_match.group(1).strip() if cmd_match else "",
        }


def get_node_identity(custom_node_id: Optional[str] = None) -> str:
    if custom_node_id:
        return custom_node_id
    env_id = os.environ.get("OCTO_NODE_ID")
    if env_id:
        return env_id
    return socket.gethostname().split(".")[0]


def find_repo_root(start_dir: Optional[Path] = None) -> Path:
    cwd = start_dir or Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / "orchestrator.yaml").exists() or (p / ".git").exists():
            return p
    return cwd
