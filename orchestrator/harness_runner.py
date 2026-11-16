"""
Multi-Harness & Agent Adapter Engine (`harness_runner.py`).
Executes declarative Agents (`agents/*.md`) or Harness Profiles (`orchestrator.yaml`)
across coding CLIs:
- `claude` (Claude Code CLI)
- `pi` (Pi CLI with local OpenAI-compatible endpoints or cloud providers)
- `aider` (Aider CLI)
- `agy` (Antigravity CLI)
- `mock` / `custom` (for deterministic CI tests or custom binary wrappers)
"""

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import AgentSpec, load_agent_spec


@dataclass
class HarnessResult:
    returncode: int
    stdout: str
    stderr: str
    command: List[str]
    profile_name: str
    model_used: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class HarnessRunner:
    """
    Resolves an Agent (`agents/<name>.md`) or Harness Profile (`orchestrator.yaml`)
    and executes a coding, planning, or review prompt in the target workspace.
    """

    def __init__(self, config: Dict[str, Any], repo_root: Path):
        self.config = config
        self.repo_root = repo_root
        self.harnesses_cfg = config.get("harnesses", {})
        self.profiles = self.harnesses_cfg.get("profiles", {})
        self.planning_cfg = config.get("planning", {})
        self.execution_cfg = config.get("execution", {})

    def _resolve_role_alias(self, target: str) -> str:
        planning_agents = self.planning_cfg.get("agents", {})
        role_map = {
            "explorer": planning_agents.get("explorer", "planner-explorer"),
            "architect": planning_agents.get("architect", "planner-architect"),
            "critic": planning_agents.get("critic", "planner-critic"),
            "planner": planning_agents.get(
                "architect",
                self.harnesses_cfg.get("default_planner", "planner-architect"),
            ),
            "worker": self.execution_cfg.get(
                "worker_agent",
                self.harnesses_cfg.get("default_worker", "worker"),
            ),
            "qa": self.execution_cfg.get(
                "qa_agent",
                self.harnesses_cfg.get("default_qa", "qa"),
            ),
        }
        return str(role_map.get(target, target))

    def resolve_target(
        self,
        agent_or_profile: str,
        model_override: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any], Optional[AgentSpec], str]:
        """
        Resolves an agent name, role alias, harness profile, or inline `adapter:model` spec.
        Returns `(resolved_name, spec_dict, agent_spec_or_none, source_label)`.
        """
        resolved_name = self._resolve_role_alias(agent_or_profile)
        agent_spec = load_agent_spec(resolved_name, self.repo_root, self.config)

        if agent_spec is not None:
            base_spec: Dict[str, Any] = {}
            source = str(agent_spec.source_path.relative_to(self.repo_root)) if (
                agent_spec.source_path and str(agent_spec.source_path).startswith(str(self.repo_root))
            ) else "orchestrator.yaml:agents"

            if agent_spec.profile and agent_spec.profile in self.profiles:
                base_spec = dict(self.profiles[agent_spec.profile])

            if agent_spec.harness:
                base_spec["adapter"] = agent_spec.harness
            if agent_spec.provider:
                base_spec["provider"] = agent_spec.provider
            if agent_spec.model:
                base_spec["model"] = agent_spec.model
            if agent_spec.thinking:
                base_spec["thinking"] = agent_spec.thinking
            if agent_spec.endpoint:
                base_spec["endpoint"] = agent_spec.endpoint
            if agent_spec.api_base:
                base_spec["api_base"] = agent_spec.api_base
            if agent_spec.api_key:
                base_spec["api_key"] = agent_spec.api_key
            if agent_spec.extra_args:
                base_spec["extra_args"] = list(agent_spec.extra_args)
            if agent_spec.tools:
                base_spec["tools"] = agent_spec.tools
            if agent_spec.script:
                base_spec["script"] = agent_spec.script
            if agent_spec.command:
                base_spec["command"] = list(agent_spec.command)
            if agent_spec.read_only:
                base_spec["read_only"] = True

            if "adapter" not in base_spec:
                base_spec["adapter"] = "claude"
            if model_override:
                base_spec["model"] = model_override
            return resolved_name, base_spec, agent_spec, source

        # Fallback: direct harness profile in orchestrator.yaml
        spec = self.profiles.get(resolved_name)
        if spec:
            merged = dict(spec)
            if model_override:
                merged["model"] = model_override
            return resolved_name, merged, None, f"orchestrator.yaml:profiles.{resolved_name}"

        # Dynamic inline syntax: "pi:provider/model" or "claude:opus"
        if ":" in agent_or_profile:
            adapter, _, model_spec = agent_or_profile.partition(":")
            if "/" in model_spec:
                provider, _, model = model_spec.partition("/")
            else:
                provider, model = "", model_spec
            inline_spec: Dict[str, Any] = {
                "adapter": adapter,
                "provider": provider,
                "model": model_override or model,
                "thinking": "medium",
            }
            return agent_or_profile, inline_spec, None, "cli:inline"

        raise ValueError(f"Unknown agent or harness profile: {agent_or_profile}")

    def resolve_profile(self, profile_or_role: str) -> Tuple[str, Dict[str, Any]]:
        """Backward-compatible wrapper returning `(resolved_name, spec_dict)`."""
        name, spec, _, _ = self.resolve_target(profile_or_role)
        return name, spec

    def describe_target(
        self,
        agent_or_profile: str,
        model_override: Optional[str] = None,
    ) -> Dict[str, str]:
        """Returns human-readable diagnostic info about which agent/harness/model will execute."""
        resolved_name, spec, _, source = self.resolve_target(agent_or_profile, model_override=model_override)
        adapter = str(spec.get("adapter", "claude"))
        provider = str(spec.get("provider", ""))
        model = str(spec.get("model", "default"))
        model_id = f"{provider}/{model}" if provider else model
        return {
            "name": resolved_name,
            "adapter": adapter,
            "provider": provider,
            "model": model,
            "model_id": model_id,
            "thinking": str(spec.get("thinking", "")),
            "endpoint": str(spec.get("endpoint") or spec.get("api_base") or ""),
            "source": source,
        }

    def _materialize_system_prompt(
        self,
        resolved_name: str,
        agent_spec: Optional[AgentSpec],
        system_prompt_file: Optional[Path],
    ) -> Tuple[Optional[Path], str]:
        """Returns `(prompt_file_path, prompt_text)` with frontmatter stripped."""
        if system_prompt_file and system_prompt_file.exists():
            return system_prompt_file, system_prompt_file.read_text(encoding="utf-8").strip()
        if agent_spec and agent_spec.system_prompt.strip():
            prompt_text = agent_spec.system_prompt.strip()
            digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:10]
            prompts_cache = self.repo_root / ".octo-sessions" / "prompts"
            prompts_cache.mkdir(parents=True, exist_ok=True)
            cached_file = prompts_cache / f"{resolved_name}-{digest}.md"
            if not cached_file.exists():
                cached_file.write_text(prompt_text + "\n", encoding="utf-8")
            return cached_file, prompt_text
        return None, ""

    def build_command(
        self,
        profile_name: str,
        prompt: str,
        workdir: Path,
        system_prompt_file: Optional[Path] = None,
        session_id: Optional[str] = None,
        read_only: bool = False,
        model_override: Optional[str] = None,
    ) -> Tuple[List[str], str]:
        """
        Builds the exact CLI invocation for the chosen agent or harness profile.
        Returns `(command_list, model_identifier)`.
        """
        resolved_name, spec, agent_spec, _ = self.resolve_target(
            profile_name, model_override=model_override
        )
        adapter = spec.get("adapter", "claude").lower()
        provider = spec.get("provider", "")
        model = spec.get("model", "default")
        thinking = spec.get("thinking", "")
        extra_args = list(spec.get("extra_args", []))
        effective_read_only = read_only or bool(spec.get("read_only", False))

        prompt_file, prompt_text = self._materialize_system_prompt(
            resolved_name, agent_spec, system_prompt_file
        )

        model_id = f"{provider}/{model}" if provider else model

        if adapter == "pi":
            pi_bin = shutil.which("pi") or os.path.expanduser("~/.local/bin/pi")
            cmd = [pi_bin, "--print", "--approve"]
            if provider:
                cmd.extend(["--provider", provider])
            if model:
                cmd.extend(["--model", model])
            if thinking:
                cmd.extend(["--thinking", thinking])
            if prompt_file and prompt_file.exists():
                cmd.extend(["--append-system-prompt", str(prompt_file)])
            if session_id:
                session_dir = workdir / ".octo-sessions"
                session_dir.mkdir(parents=True, exist_ok=True)
                cmd.extend(["--session-dir", str(session_dir), "--session-id", session_id])
            if effective_read_only:
                tools = spec.get("tools") or "read,bash"
                cmd.extend(["--tools", tools])
            cmd.extend(extra_args)
            cmd.append(prompt)
            return cmd, model_id

        elif adapter == "claude":
            claude_bin = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
            cmd = [claude_bin, "-p"]
            if model:
                cmd.extend(["--model", model])
            if prompt_text:
                cmd.extend(["--append-system-prompt", prompt_text])
            if effective_read_only and "--allowedTools" not in extra_args and "--tools" not in extra_args:
                tools = spec.get("tools") or "Read,Glob,Grep,Bash"
                cmd.extend(["--tools", tools])
            cmd.extend(extra_args)
            cmd.append(prompt)
            return cmd, model_id

        elif adapter == "aider":
            aider_bin = shutil.which("aider") or os.path.expanduser("~/.local/bin/aider")
            full_prompt = f"{prompt_text}\n\n---\n\n{prompt}" if prompt_text else prompt
            cmd = [aider_bin, "--message", full_prompt]
            if model:
                cmd.extend(["--model", model])
            if spec.get("api_base") or spec.get("endpoint"):
                cmd.extend(["--openai-api-base", spec.get("api_base") or spec["endpoint"]])
            if spec.get("api_key"):
                cmd.extend(["--openai-api-key", spec["api_key"]])
            cmd.extend(extra_args)
            return cmd, model_id

        elif adapter == "agy":
            agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
            full_prompt = f"{prompt_text}\n\n---\n\n{prompt}" if prompt_text else prompt
            cmd = [agy_bin, "chat", "-m", full_prompt]
            cmd.extend(extra_args)
            return cmd, model_id

        elif adapter == "mock":
            cmd = ["python3", "-c", spec.get("script", "print('MOCK HARNESS OK')")]
            return cmd, "mock/deterministic"

        elif adapter == "custom":
            cmd_template = spec.get("command", [])
            cmd = [
                arg.replace("{prompt}", prompt)
                .replace("{system_prompt}", prompt_text)
                .replace("{workdir}", str(workdir))
                .replace("{model}", model)
                for arg in cmd_template
            ]
            return cmd, model_id

        raise ValueError(f"Unsupported harness adapter: {adapter}")

    def run(
        self,
        profile_name: str,
        prompt: str,
        workdir: Path,
        system_prompt_file: Optional[Path] = None,
        session_id: Optional[str] = None,
        read_only: bool = False,
        timeout_seconds: int = 1800,
        heartbeat_callback: Optional[Callable[[], None]] = None,
        heartbeat_interval: int = 180,
        model_override: Optional[str] = None,
    ) -> HarnessResult:
        """
        Executes the agent/harness in `workdir` and periodically triggers `heartbeat_callback`
        so the GitHub Issue lease never expires while an LLM is thinking or coding.
        """
        resolved_name, spec, _, _ = self.resolve_target(profile_name, model_override=model_override)
        cmd, model_id = self.build_command(
            profile_name=resolved_name,
            prompt=prompt,
            workdir=workdir,
            system_prompt_file=system_prompt_file,
            session_id=session_id,
            read_only=read_only,
            model_override=model_override,
        )

        env = os.environ.copy()
        endpoint = spec.get("endpoint") or spec.get("api_base")
        if endpoint:
            env["OPENAI_API_BASE"] = str(endpoint)
        if spec.get("api_key"):
            env["OPENAI_API_KEY"] = str(spec["api_key"])

        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        elapsed = 0
        step = min(10, heartbeat_interval)
        last_hb = 0

        try:
            while proc.poll() is None:
                try:
                    proc.wait(timeout=step)
                    break
                except subprocess.TimeoutExpired:
                    elapsed += step
                    if elapsed >= timeout_seconds:
                        proc.kill()
                        out, err = proc.communicate()
                        return HarnessResult(
                            returncode=124,
                            stdout=out,
                            stderr=f"{err}\n[OCTO] Timed out after {timeout_seconds}s",
                            command=cmd,
                            profile_name=resolved_name,
                            model_used=model_id,
                        )
                    if heartbeat_callback and (elapsed - last_hb) >= heartbeat_interval:
                        try:
                            heartbeat_callback()
                        except Exception:
                            pass
                        last_hb = elapsed

            out, err = proc.communicate()
            return HarnessResult(
                returncode=proc.returncode or 0,
                stdout=out,
                stderr=err,
                command=cmd,
                profile_name=resolved_name,
                model_used=model_id,
            )
        finally:
            if proc.poll() is None:
                proc.kill()
