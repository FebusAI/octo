"""
Coordinator-to-Coordinator (C2C) Multiplayer Protocol (`c2c_coordinator.py`).
Implements peer coordinator negotiation across multiple developers,
workspaces, and worker nodes:
- Discovers active Coordinators working on parallel Epics/Assignments
- Exchanges architectural plans, exported interfaces, and database/API schemas
- Sends proactive `[C2C-HEADS-UP]` and `[C2C-NEGOTIATE]` messages without human mediation
- Logs all peer communications to the `#c2c` Swarm Channel and mirrors to GitHub Issue threads
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser, get_node_identity
from .github_bus import GitHubBus
from .overlap_detector import OverlapDetector, OverlapReport
from .swarm_board import SwarmBoard


@dataclass
class CoordinatorIdentity:
    coordinator_id: str
    node_id: str
    active_epic: Optional[int]
    goal_title: str
    owned_files: List[str] = field(default_factory=list)
    heartbeat_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class C2CMessage:
    msg_id: str
    sender_coordinator: str
    target_coordinator: str  # specific coordinator ID or "@coordinators" broadcast
    kind: str  # "PLAN_SHARE" | "HEADS_UP" | "LOCK_REQUEST" | "CONTRACT_SYNC" | "RESOLVE"
    subject: str
    body: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


class C2CCoordinator:
    def __init__(
        self,
        repo_root: Path,
        bus: GitHubBus,
        board: SwarmBoard,
        coordinator_id: Optional[str] = None,
        node_id: Optional[str] = None,
    ):
        self.repo_root = repo_root
        self.bus = bus
        self.board = board
        self.node_id = get_node_identity(node_id)
        self.coordinator_id = coordinator_id or f"coordinator-{self.node_id}"
        self.c2c_dir = repo_root / ".swarm" / "c2c"
        self.c2c_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.c2c_dir / "coordinators.json"

        # Ensure #c2c channel exists on Swarm Board
        channels = self.board.list_channels()
        if "c2c" not in channels:
            self.board.create_channel("c2c", "Coordinator-to-Coordinator Cross-Assignment Channel", self.coordinator_id)

    def register(self, active_epic: Optional[int], goal_title: str, owned_files: List[str]) -> CoordinatorIdentity:
        """Announces this Coordinator's presence in the multiplayer cluster."""
        registry = self._load_registry()
        ident = CoordinatorIdentity(
            coordinator_id=self.coordinator_id,
            node_id=self.node_id,
            active_epic=active_epic,
            goal_title=goal_title,
            owned_files=owned_files,
            heartbeat_at=datetime.now(timezone.utc).isoformat(),
        )
        registry[self.coordinator_id] = asdict(ident)
        self._save_registry(registry)

        # Broadcast on Swarm Board channel #c2c
        epic_hint = f"on Epic #{active_epic} ({goal_title})" if active_epic else "idle"
        self.board.post(
            sender=self.coordinator_id,
            text=f"@#c2c Coordinator `{self.coordinator_id}` online {epic_hint}.",
            channel="c2c",
            metadata={"type": "COORDINATOR_ONLINE", "coordinator": asdict(ident)},
        )
        return ident

    def _load_registry(self) -> Dict[str, Any]:
        if not self.registry_file.exists():
            return {}
        try:
            return json.loads(self.registry_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_registry(self, data: Dict[str, Any]) -> None:
        self.registry_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_active_coordinators(self) -> List[CoordinatorIdentity]:
        reg = self._load_registry()
        return [CoordinatorIdentity(**v) for v in reg.values()]

    def send_message(
        self,
        target_coordinator: str,
        kind: str,
        subject: str,
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> C2CMessage:
        """Sends a direct peer-to-peer message to another Coordinator or broadcast to @coordinators."""
        msg_id = f"c2c-{int(datetime.now(timezone.utc).timestamp())}"
        msg = C2CMessage(
            msg_id=msg_id,
            sender_coordinator=self.coordinator_id,
            target_coordinator=target_coordinator,
            kind=kind,
            subject=subject,
            body=body,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )

        # Post to Swarm Board channel #c2c
        target_tag = f"@{target_coordinator}" if not target_coordinator.startswith("@") else target_coordinator
        board_text = f"{target_tag} [{kind}] **{subject}**\n\n{body}"
        self.board.post(
            sender=self.coordinator_id,
            recipient=target_tag,
            text=board_text,
            channel="c2c",
            metadata=asdict(msg),
        )

        # If related to a specific Epic issue, mirror as an agent comment on GitHub
        epic_num = (metadata or {}).get("epic_number")
        if epic_num and hasattr(self.bus, "post_protocol_message"):
            try:
                self.bus.post_protocol_message(
                    issue_number=int(epic_num),
                    msg_type="DECISION",
                    summary_markdown=f"🌐 **[C2C MULTIPLAYER]** `{self.coordinator_id}` -> `{target_coordinator}`: {subject}\n\n{body}",
                    meta=asdict(msg),
                )
            except Exception:
                pass

        return msg

    def auto_negotiate_overlaps(
        self,
        detector: OverlapDetector,
        active_tasks: List[Dict[str, Any]],
    ) -> List[C2CMessage]:
        """
        Runs the Overlap Detector and automatically drafts/sends C2C negotiation messages
        between Coordinators if cross-task or cross-epic file overlaps are found.
        """
        overlaps = detector.check_overlaps(active_tasks)
        sent_messages: List[C2CMessage] = []

        for rep in overlaps:
            if rep.level in ("CRITICAL_COLLISION", "INTERFACE_DEPENDENCY"):
                t1, t2 = rep.conflicting_tasks[0], rep.conflicting_tasks[1]
                subj = f"Overlap Risk between Task #{t1} and Task #{t2}"
                body = (
                    f"⚠️ {rep.message}\n\n"
                    f"- **Conflicting Files**: {', '.join(f'`{f}`' for f in rep.conflicting_files)}\n"
                    f"- **Suggested Action**: {rep.suggested_action}\n"
                    f"- **Automatic Negotiation**: Coordinator `{self.coordinator_id}` requests alignment on file ownership."
                )
                msg = self.send_message(
                    target_coordinator="@coordinators",
                    kind="HEADS_UP",
                    subject=subj,
                    body=body,
                    metadata={"overlap_level": rep.level, "tasks": [t1, t2], "files": rep.conflicting_files},
                )
                sent_messages.append(msg)

        return sent_messages

    def get_c2c_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieves recent C2C negotiation messages from the Swarm Board."""
        msgs = self.board.read(since_seq=0, channel="c2c")
        return [asdict(m) for m in msgs[-limit:]]
