"""
Shared Swarm Message Board (`swarm_board.py`).
Implements real-time intra-task communication & peer Coordinator-to-Coordinator (C2C) messaging.
100% HOSTED ON GITHUB:
- Authoritative event log is hosted directly on a dedicated GitHub Issue (`type:swarm-board`)
  with structured machine-readable comments (`<!-- OCTO-META:SWARM {...} -->`)
- Local append-only cache in `.swarm/board_<id>.jsonl` for high performance in Paseo and CLI
- Seamless multi-node synchronization: any worker across connected machines sees the exact
  same GitHub total-order message stream without custom server infrastructure.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ProtocolParser


@dataclass
class SwarmMessage:
    seq: int
    sender: str
    recipient: str  # e.g. "@parser", "@verifier", "@all", "@coordinator", "#channel"
    text: str
    channel: str = "main"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return "[done]" in self.text.lower()

    @property
    def is_stall(self) -> bool:
        return "[stall]" in self.text.lower()

    @property
    def is_overlap_alert(self) -> bool:
        return "[overlap]" in self.text.lower() or "[collision]" in self.text.lower()


class SwarmBoard:
    """
    Authoritative Swarm Message Board hosted directly on GitHub Issues.
    Every post is recorded as a structured GitHub comment, providing a 100% cloud-hosted,
    auditable message bus across all distributed PC nodes.
    """

    MENTION_RE = re.compile(r"@([a-zA-Z0-9_\-]+)")
    CHANNEL_RE = re.compile(r"@#([a-zA-Z0-9_\-]+)")

    def __init__(self, repo_root: Path, bus: Optional[Any] = None, swarm_id: Optional[str] = None):
        self.repo_root = repo_root
        self.bus = bus
        self.swarm_id = swarm_id or "default"
        self.swarm_dir = repo_root / ".swarm"
        self.swarm_dir.mkdir(parents=True, exist_ok=True)
        self.board_file = self.swarm_dir / f"board_{self.swarm_id}.jsonl"
        self.channels_file = self.swarm_dir / f"channels_{self.swarm_id}.json"
        self.issue_id_file = self.swarm_dir / f"github_issue_{self.swarm_id}.txt"
        self._github_issue_number: Optional[int] = None

        if not self.channels_file.exists():
            self._save_channels({"main": {"topic": "Default Swarm Broadcast Channel", "created_by": "coordinator"}})

    def _load_channels(self) -> Dict[str, Any]:
        if not self.channels_file.exists():
            return {"main": {"topic": "Default", "created_by": "coordinator"}}
        try:
            return json.loads(self.channels_file.read_text(encoding="utf-8"))
        except Exception:
            return {"main": {"topic": "Default", "created_by": "coordinator"}}

    def _save_channels(self, channels: Dict[str, Any]) -> None:
        self.channels_file.write_text(json.dumps(channels, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_channels(self) -> Dict[str, Any]:
        return self._load_channels()

    def create_channel(self, channel_name: str, topic: str, creator: str = "coordinator") -> None:
        channels = self._load_channels()
        channels[channel_name] = {
            "topic": topic,
            "created_by": creator,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_channels(channels)

    # -------------------------------------------------------------------------
    # Authoritative GitHub Issue Hosting
    # -------------------------------------------------------------------------
    def get_or_create_github_issue(self) -> Optional[int]:
        if self._github_issue_number is not None:
            return self._github_issue_number

        if not self.bus:
            return None

        # Check cached issue id on disk
        if self.issue_id_file.exists():
            try:
                cached_id = int(self.issue_id_file.read_text(encoding="utf-8").strip())
                self._github_issue_number = cached_id
                return cached_id
            except Exception:
                pass

        # Query GitHub for existing Swarm Board issue
        try:
            issues = self.bus.list_issues(label="type:swarm-board", state="all")
            if issues:
                inum = int(issues[0]["number"])
                self._github_issue_number = inum
                self.issue_id_file.write_text(str(inum), encoding="utf-8")
                return inum
        except Exception:
            pass

        # Create new dedicated Swarm Board issue on GitHub
        try:
            body = (
                "# 🐝 Distributed Swarm & Multiplayer Message Board (SSOT)\n\n"
                "> [!NOTE]\n"
                "> This GitHub Issue serves as the **authoritative, real-time message bus** for all "
                "Coordinators, Workers, and Verifiers across distributed worker nodes.\n"
                "> Messages are recorded chronologically in structured `[SWARM-MESSAGE]` and `[C2C-MESSAGE]` blocks."
            )
            inum = self.bus.create_issue(
                title=f"[SWARM] Distributed Multi-Agent Message Board ({self.swarm_id})",
                body=body,
                labels=["type:swarm-board", "status:in-progress"],
            )
            self._github_issue_number = inum
            self.issue_id_file.write_text(str(inum), encoding="utf-8")
            return inum
        except Exception:
            return None

    def sync_from_github(self) -> List[SwarmMessage]:
        """Fetches the latest messages from the GitHub Issue and refreshes the local cache."""
        issue_num = self.get_or_create_github_issue()
        if not issue_num or not self.bus:
            return self._read_local_cache()

        try:
            issue = self.bus.get_issue(issue_num)
            comments = issue.get("comments", [])
            messages: List[SwarmMessage] = []
            seen_seqs = set()

            for c in comments:
                body = c.get("body", "")
                author = (c.get("author") or {}).get("login") or (c.get("user") or {}).get("login") or "agent"
                created_at = c.get("createdAt") or c.get("created_at") or datetime.now(timezone.utc).isoformat()

                for match in ProtocolParser.METADATA_RE.finditer(body):
                    msg_type = match.group(1).upper()
                    if msg_type in ("SWARM", "C2C", "HANDOVER"):
                        try:
                            meta = json.loads(match.group(2))
                            seq = int(meta.get("seq", len(messages) + 1))
                            if seq in seen_seqs:
                                continue
                            seen_seqs.add(seq)
                            sender = meta.get("sender") or meta.get("sender_coordinator") or author
                            recipient = meta.get("recipient") or meta.get("target_coordinator") or "@all"
                            text = meta.get("text") or meta.get("body") or ""
                            channel = meta.get("channel", "main")
                            messages.append(
                                SwarmMessage(
                                    seq=seq,
                                    sender=sender,
                                    recipient=recipient,
                                    text=text,
                                    channel=channel,
                                    created_at=meta.get("created_at", created_at),
                                    metadata=meta,
                                )
                            )
                        except Exception:
                            continue

            # Sort by sequence number and update local cache file
            messages.sort(key=lambda m: m.seq)
            with open(self.board_file, "w", encoding="utf-8") as f:
                for m in messages:
                    f.write(json.dumps(asdict(m), ensure_ascii=False) + "\n")

            return messages
        except Exception:
            return self._read_local_cache()

    def _read_local_cache(self) -> List[SwarmMessage]:
        if not self.board_file.exists():
            return []
        messages: List[SwarmMessage] = []
        with open(self.board_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    messages.append(SwarmMessage(**json.loads(line)))
                except Exception:
                    continue
        return messages

    def get_next_seq(self) -> int:
        messages = self._read_local_cache()
        return len(messages) + 1

    def post(
        self,
        sender: str,
        text: str,
        recipient: Optional[str] = None,
        channel: str = "main",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SwarmMessage:
        """
        Posts a new message to the Swarm Board:
        1. Posts directly as a comment on the authoritative GitHub Issue
        2. Appends to the local disk cache
        """
        if not recipient:
            channel_match = self.CHANNEL_RE.search(text)
            mention_match = self.MENTION_RE.search(text)
            if channel_match:
                recipient = f"@#{channel_match.group(1)}"
            elif mention_match:
                recipient = f"@{mention_match.group(1)}"
            else:
                recipient = "@all"

        seq = self.get_next_seq()
        created_at = datetime.now(timezone.utc).isoformat()
        msg_meta = metadata or {}
        msg_meta.update({
            "seq": seq,
            "sender": sender,
            "recipient": recipient,
            "channel": channel,
            "text": text.strip(),
            "created_at": created_at,
        })

        msg = SwarmMessage(
            seq=seq,
            sender=sender,
            recipient=recipient,
            text=text.strip(),
            channel=channel,
            created_at=created_at,
            metadata=msg_meta,
        )

        # Mirror to GitHub Issue as single source of truth
        issue_num = self.get_or_create_github_issue()
        if issue_num and self.bus:
            summary = f"[{channel}] `{sender}` ➔ `{recipient}`\n\n{text.strip()}"
            try:
                self.bus.post_protocol_message(
                    issue_number=issue_num,
                    msg_type="SWARM",
                    summary_markdown=summary,
                    meta=msg_meta,
                )
            except Exception:
                pass

        # Write to local cache
        with open(self.board_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

        return msg

    def read(
        self,
        since_seq: int = 0,
        recipient: Optional[str] = None,
        channel: Optional[str] = None,
        mentions_only: bool = False,
        sync_github: bool = False,
    ) -> List[SwarmMessage]:
        """
        Reads messages from the board starting after `since_seq`.
        Optionally syncs from GitHub if requested.
        """
        messages = self.sync_from_github() if sync_github else self._read_local_cache()
        clean_recipient = recipient.lstrip("@").lower() if recipient else None
        filtered: List[SwarmMessage] = []

        for msg in messages:
            if msg.seq <= since_seq:
                continue

            # Channel filtering: unless channel == "*", match channel exactly
            if channel and channel != "*":
                if msg.channel != channel:
                    continue

            # Recipient filtering
            if mentions_only and clean_recipient:
                msg_target = msg.recipient.lstrip("@").lower()
                if (
                    msg_target != clean_recipient
                    and not clean_recipient.startswith(msg_target)
                    and msg_target != "all"
                    and not msg.recipient.startswith("@#")
                ):
                    continue

            filtered.append(msg)

        return filtered

    def wait_for_message(
        self,
        since_seq: int,
        recipient: Optional[str] = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 1.0,
    ) -> List[SwarmMessage]:
        """Sleeps until a matching message is posted or timeout expires."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            msgs = self.read(since_seq=since_seq, recipient=recipient, mentions_only=True, sync_github=True)
            if msgs:
                return msgs
            time.sleep(poll_interval)
        return []

    def get_summary(self) -> Dict[str, Any]:
        """Provides an executive overview of the swarm board for Paseo and CLI."""
        all_msgs = self.read(since_seq=0)
        issue_num = self._github_issue_number or (
            int(self.issue_id_file.read_text(encoding="utf-8").strip()) if self.issue_id_file.exists() else None
        )
        participants = set()
        channels = set()
        completed_tasks = []
        overlap_alerts = []

        for m in all_msgs:
            participants.add(m.sender)
            channels.add(m.channel)
            if m.is_done:
                completed_tasks.append(m)
            if m.is_overlap_alert:
                overlap_alerts.append(m)

        return {
            "swarm_id": self.swarm_id,
            "github_issue": issue_num,
            "total_messages": len(all_msgs),
            "latest_seq": all_msgs[-1].seq if all_msgs else 0,
            "participants": sorted(list(participants)),
            "channels": sorted(list(channels)),
            "completed_count": len(completed_tasks),
            "overlap_alerts_count": len(overlap_alerts),
            "recent_messages": [asdict(m) for m in all_msgs[-10:]],
        }
