import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function getEnvWithPath(): NodeJS.ProcessEnv {
  const home = homedir();
  const extraPaths = [
    "/opt/homebrew/bin",
    join(home, ".local/bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
  ];
  return {
    ...process.env,
    PATH: [...extraPaths, process.env.PATH || ""].join(":"),
  };
}

export async function resolveWorkspaceDir(paseo: any, workspaceId?: string): Promise<string> {
  if (workspaceId && paseo?.workspaces?.ref) {
    try {
      const ws = paseo.workspaces.ref(workspaceId);
      await ws.refresh();
      if (ws.directory && existsSync(join(ws.directory, "orchestrator.yaml"))) {
        return ws.directory;
      }
    } catch {
      // fall through
    }
  }
  return process.cwd();
}

export async function fetchKanbanBoard(paseo: any, workspaceId?: string, probeNodes = false) {
  const dir = await resolveWorkspaceDir(paseo, workspaceId);
  if (!existsSync(join(dir, "orchestrator.yaml"))) {
    return {
      eligible: false,
      workspaceDir: dir,
      repo: "",
      reason: "Nessun file orchestrator.yaml trovato in questo workspace.",
      profiles: [],
      issues: [],
      nodes: [],
      swarmMessages: [],
      overlapReports: [],
      coordinators: [],
    };
  }

  const pyScript = `
import json
from dataclasses import asdict
from pathlib import Path
from orchestrator.config import list_agent_specs, load_config
from orchestrator.github_bus import GitHubBus
from orchestrator.cluster_ssh import ClusterSSHManager
from orchestrator.swarm_board import SwarmBoard
from orchestrator.overlap_detector import OverlapDetector
from orchestrator.c2c_coordinator import C2CCoordinator

root = Path(".")
cfg = load_config(root)
bus = GitHubBus(config=cfg)
mgr = ClusterSSHManager(cfg)
board = SwarmBoard(repo_root=root)
detector = OverlapDetector(repo_root=root)
c2c = C2CCoordinator(repo_root=root, bus=bus, board=board)

repo = ""
try:
    repo = bus._run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], check=False)
except Exception:
    pass

raw_issues = []
try:
    raw_issues = bus.list_issues(state="all", limit=100)
except Exception:
    pass

issues_out = []
for iss in raw_issues:
    labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in iss.get("labels", [])]
    status_lbl = next((l for l in labels if l.startswith("status:")), "status:unclassified")
    type_lbl = next((l for l in labels if l.startswith("type:")), "type:task")
    claim = bus.get_active_claim(iss)
    claim_str = f"{claim['node_id']} ({claim['harness']})" if claim else None
    issues_out.append({
        "number": int(iss["number"]),
        "title": str(iss.get("title", "")),
        "state": str(iss.get("state", "OPEN")),
        "statusLabel": status_lbl,
        "typeLabel": type_lbl,
        "url": str(iss.get("url", "")),
        "activeClaim": claim_str,
        "commentsCount": len(iss.get("comments", [])),
    })

nodes_out = []
probe = ${probeNodes ? "True" : "False"}
for name, spec in mgr.list_configured_nodes().items():
    entry = {
        "name": name,
        "host": str(spec.get("host", name)),
        "defaultProfile": str(spec.get("default_profile", "worker")),
        "roles": [str(r) for r in spec.get("roles", [])],
    }
    if probe:
        info = mgr.probe_node(name, timeout=2)
        entry["online"] = bool(info.get("online", False))
        entry["tools"] = [k for k, v in info.get("tools", {}).items() if v]
        entry["activeWorkers"] = len(info.get("workers", []))
    nodes_out.append(entry)

# Swarm Board Messages
swarm_msgs_raw = board.read(since_seq=0)
swarm_msgs_out = [
    {
        "seq": m.seq,
        "sender": m.sender,
        "recipient": m.recipient,
        "text": m.text,
        "channel": m.channel,
        "created_at": m.created_at,
    }
    for m in swarm_msgs_raw[-25:]
]

# Overlap Detector
active_tasks = [
    iss for iss in raw_issues
    if iss.get("state", "OPEN").upper() == "OPEN" and "type:epic" not in [l.get("name", "") if isinstance(l, dict) else str(l) for l in iss.get("labels", [])]
]
overlap_reports_raw = detector.check_overlaps(active_tasks)
overlap_reports_out = [
    {
        "level": r.level,
        "message": r.message,
        "conflicting_tasks": r.conflicting_tasks,
        "conflicting_files": r.conflicting_files,
        "suggested_action": r.suggested_action,
    }
    for r in overlap_reports_raw
]

# C2C Coordinators
coordinators_out = [asdict(c) for c in c2c.list_active_coordinators()]

agents_out = list(list_agent_specs(root, cfg).keys())
profiles = agents_out + [p for p in cfg.get("harnesses", {}).get("profiles", {}).keys() if p not in agents_out]

print(json.dumps({
    "eligible": True,
    "workspaceDir": str(root.resolve()),
    "repo": repo,
    "reason": "",
    "profiles": profiles,
    "issues": issues_out,
    "nodes": nodes_out,
    "swarmMessages": swarm_msgs_out,
    "overlapReports": overlap_reports_out,
    "coordinators": coordinators_out,
}))
`;

  try {
    const { stdout } = await execFileAsync("python3", ["-c", pyScript], {
      cwd: dir,
      env: getEnvWithPath(),
      timeout: 25000,
    });
    return JSON.parse(stdout.trim());
  } catch (err: any) {
    return {
      eligible: true,
      workspaceDir: dir,
      repo: "",
      reason: err?.message || String(err),
      profiles: ["planner-architect", "worker", "qa", "claude-cli", "pi-local"],
      issues: [],
      nodes: [],
      swarmMessages: [],
      overlapReports: [],
      coordinators: [],
    };
  }
}

export async function executeOctoAction(
  paseo: any,
  input: {
    workspaceId?: string;
    action: string;
    goal?: string;
    issueNumber?: number;
    profile?: string;
    nodeName?: string;
    messageText?: string;
    recipient?: string;
    channel?: string;
    subject?: string;
    kind?: string;
  },
) {
  const dir = await resolveWorkspaceDir(paseo, input.workspaceId);
  const octoBin = join(dir, "bin", "octo");
  const profile = input.profile || "worker";

  try {
    let args: string[] = [];
    let cmd = octoBin;

    switch (input.action) {
      case "init":
        args = ["init"];
        break;
      case "plan":
        if (!input.goal?.trim()) throw new Error("Specify a Goal for planning.");
        args = ["plan", input.goal.trim(), "--agent", input.profile || "planner-architect"];
        break;
      case "approve":
        if (!input.issueNumber) throw new Error("Specifica il numero della Epic da approvare.");
        args = ["approve", String(input.issueNumber)];
        break;
      case "worker_once":
        args = ["worker", "--once", "--profile", profile];
        break;
      case "qa_once":
        args = input.issueNumber
          ? ["qa", "--issue", String(input.issueNumber), "--profile", profile]
          : ["qa", "--profile", profile];
        break;
      case "orchestrate_once":
        args = ["orchestrate", "--once"];
        break;
      case "swarm_post":
        if (!input.messageText?.trim()) throw new Error("Specifica il messaggio da pubblicare sulla Swarm Board.");
        args = ["swarm", "post", input.recipient || "@all", input.messageText.trim(), "--channel", input.channel || "main"];
        break;
      case "c2c_send":
        if (!input.messageText?.trim()) throw new Error("Specifica il messaggio C2C da inviare.");
        args = [
          "c2c",
          "send",
          input.recipient || "@coordinators",
          input.messageText.trim(),
          "--kind",
          input.kind || "HEADS_UP",
          "--subject",
          input.subject || "Coordination",
        ];
        break;
      case "c2c_negotiate":
        args = ["c2c", "negotiate"];
        break;
      case "paseo_spawn_worker": {
        // Spawns a visible native Paseo Agent session in its own worktree!
        cmd = "/opt/homebrew/bin/paseo";
        const issueHint = input.issueNumber ? ` per la Issue #${input.issueNumber}` : "";
        const taskPrompt =
          `Esegui nel terminale \`./bin/octo worker --once --profile ${profile}\`${issueHint}, ` +
          `verifica i test e riporta l'esito del task sulla Kanban GitHub.`;
        args = [
          "run",
          "--no-wait",
          "--provider",
          "pi",
          "--title",
          `Octo Worker (${profile})${issueHint}`,
          "--cwd",
          dir,
          taskPrompt,
        ];
        break;
      }
      case "node_add":
        if (!input.nodeName) throw new Error("Specify the node name from orchestrator.yaml.");
        args = ["node", "add", input.nodeName, "--profile", profile];
        break;
      case "node_remove":
        if (!input.nodeName) throw new Error("Specifica il nome del nodo PC da rimuovere.");
        args = ["node", "remove", input.nodeName];
        break;
      default:
        throw new Error(`Azione non supportata: ${input.action}`);
    }

    const { stdout, stderr } = await execFileAsync(cmd, args, {
      cwd: dir,
      env: getEnvWithPath(),
      timeout: 120000,
    });
    const out = [stdout, stderr].filter(Boolean).join("\n").trim();
    return { ok: true, output: out || "Comando completato con successo." };
  } catch (err: any) {
    const msg = [err?.stdout, err?.stderr, err?.message].filter(Boolean).join("\n").trim();
    return { ok: false, output: msg || String(err) };
  }
}
