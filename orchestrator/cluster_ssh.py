"""
Multi-Node SSH Cluster Manager & Hot-Plug Node Controller (`cluster_ssh.py`).
Allows the user or Orchestrator to:
- Check connectivity & Harness readiness across remote nodes (`gpu-worker-1`, `mac-worker`, or ad-hoc SSH hosts)
- Dynamically add/spawn a background worker on a remote node (`octo node add gpu-worker-1`)
- Gracefully stop/remove a worker from a remote node (`octo node remove gpu-worker-1`) so it releases any active GitHub lease
"""

import subprocess
from typing import Any, Dict, List, Optional, Tuple


class ClusterSSHManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.nodes_cfg: Dict[str, Any] = config.get("nodes", {})

    def list_configured_nodes(self) -> Dict[str, Any]:
        return self.nodes_cfg

    def probe_node(self, node_name: str, timeout: int = 5) -> Dict[str, Any]:
        """
        Non-destructively probes a local or remote SSH node to check:
        - Reachability
        - Available coding harnesses (`pi`, `claude`, `aider`, `gh`)
        - Active `octo worker` processes
        """
        spec = self.nodes_cfg.get(node_name, {"host": node_name})
        host = spec.get("host", node_name)

        probe_script = (
            "echo \"HOST=$(hostname)\"; "
            "for bin in gh git python3 pi claude aider; do "
            "  if command -v $bin >/dev/null 2>&1; then echo \"BIN_$bin=yes\"; else echo \"BIN_$bin=no\"; fi; "
            "done; "
            "pgrep -fl 'octo.*worker' || true"
        )

        if host in ("localhost", "127.0.0.1"):
            cmd = ["sh", "-c", probe_script]
        else:
            cmd = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                host,
                probe_script,
            ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            if proc.returncode != 0:
                return {
                    "node": node_name,
                    "host": host,
                    "online": False,
                    "error": proc.stderr.strip() or f"Exit code {proc.returncode}",
                }
            lines = proc.stdout.strip().splitlines()
            info: Dict[str, Any] = {"node": node_name, "host": host, "online": True, "tools": {}, "workers": []}
            for line in lines:
                if line.startswith("HOST="):
                    info["hostname"] = line.split("=", 1)[1]
                elif line.startswith("BIN_"):
                    k, _, v = line.partition("=")
                    info["tools"][k.replace("BIN_", "")] = v == "yes"
                elif line.strip():
                    info["workers"].append(line.strip())
            return info
        except Exception as e:
            return {"node": node_name, "host": host, "online": False, "error": str(e)}

    def start_remote_worker(
        self,
        node_name: str,
        repo_url: str,
        profile: Optional[str] = None,
        workdir: Optional[str] = None,
        dry_run: bool = False,
    ) -> Tuple[bool, str]:
        """
        Hot-plugs a remote PC into the GitHub Kanban Swarm:
        1. Connects via SSH
        2. Clones or updates the GitHub repository in `workdir`
        3. Starts `./bin/octo worker --loop --node-id <node_name> --profile <profile>` in background (`nohup`)
        """
        spec = self.nodes_cfg.get(node_name, {"host": node_name})
        host = spec.get("host", node_name)
        chosen_profile = profile or spec.get("default_profile", "worker")
        target_dir = workdir or spec.get("workdir", "~/Projects/Orchestrator-Workspace")

        remote_cmd = (
            f"mkdir -p {target_dir} && cd {target_dir} && "
            f"(if [ -d .git ]; then git pull --ff-only; else git clone {repo_url} .; fi) && "
            f"nohup ./bin/octo worker --loop --node-id {node_name} --profile {chosen_profile} "
            f"> .octo-worker-{node_name}.log 2>&1 < /dev/null & "
            f"echo STARTED_PID=$!"
        )

        if dry_run:
            return True, f"[DRY-RUN] ssh {host} \"{remote_cmd}\""

        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, remote_cmd],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        return False, proc.stderr.strip()

    def stop_remote_worker(self, node_name: str, dry_run: bool = False) -> Tuple[bool, str]:
        """
        Gracefully sends SIGTERM to the worker daemon on `node_name` so it can release its GitHub lease
        before exiting.
        """
        spec = self.nodes_cfg.get(node_name, {"host": node_name})
        host = spec.get("host", node_name)
        remote_cmd = f"pkill -TERM -f 'octo worker.*--node-id {node_name}' || true"

        if dry_run:
            return True, f"[DRY-RUN] ssh {host} \"{remote_cmd}\""

        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, remote_cmd],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip() or "Stop signal sent."
