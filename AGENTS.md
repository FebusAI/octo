# Distributed Multi-Agent GitHub Kanban & Orchestrator Protocol (`AGENTS.md`)

This repository is managed by **Octo (`./bin/octo`)**, a GitHub-native orchestrator.
GitHub Issues, Labels, and Pull Requests are the **Single Source of Truth (SSOT)**.

> **Core Principle**: Octo only orchestrates. All analysis, goal-setting, planning, critique, implementation, and review are performed by the declarative **Agents** configured in [`agents/`](agents/).

---

## 1. Safe Workspace Rules
- Never run destructive commands (`rm -rf /`, `DROP DATABASE`, `mkfs`) or commit hardcoded secrets/private keys.
- Keep private hosts, LAN IPs, and custom overrides in `orchestrator.local.yaml` or `agents/*.local.md` (both gitignored).
- Worker code changes happen inside isolated Git worktrees (`.worktrees/issue-<N>`) on feature branches (`feat/issue-<N>`).

---

## 2. Two-Phase Workflow

### Phase 1: Multi-Agent Planning (`status:awaiting-approval`)
1. Run `./bin/octo plan "<goal>"` (or inspect agents first via `./bin/octo agents`).
2. Octo orchestrates the configured planning pipeline (`explorer` -> `architect` -> `critic` <-> `architect` refinement rounds) to produce an Architecture RFC (`docs/plans/EPIC-<N>.md`) and Master Epic Issue (`[EPIC] ...`) in `status:awaiting-approval`.
3. **STOP GATE**: Do NOT write implementation code while the Epic is in `status:awaiting-approval`. Approve via `./bin/octo approve <epic-num>` or by commenting `/approve` on the GitHub Issue.

### Phase 2: Epic-Scoped Implementation & QA
1. `octo approve <epic-num>` creates Epic-qualified GitHub sub-issues (`[E<epic>-T1] ...`, `[E<epic>-T2] ...`) tagged with label `epic:<epic>` and linked back to the parent Epic.
   - Root tasks start as `status:ready`.
   - Dependent tasks start as `status:blocked` and unlock automatically as upstream dependencies close.
2. Any worker node runs `./bin/octo worker --once` (or `--loop`), claims a `status:ready` / `status:rework` issue, implements in `.worktrees/issue-<N>`, verifies with `verification_cmd`, opens a PR, and moves the issue to `status:in-review`.
3. If blocked by ambiguity, the worker outputs `OCTO_QUESTION: ...` and moves the issue to `status:needs-clarification`.
4. `./bin/octo qa` (or `./bin/octo orchestrate --once`) runs deterministic checks + the `qa` agent:
   - **PASS** -> Approves & merges PR, closes issue (`status:done`), unblocks dependent tasks.
   - **FAIL** -> Requests changes and moves issue to `status:rework`.

---

## 3. Quick CLI Reference
```bash
./bin/octo init                         # Bootstrap Kanban labels & Swarm Board issue
./bin/octo agents                       # Inspect configured agents, models & planning pipeline
./bin/octo status                       # View live Kanban board
./bin/octo plan "Feature X"             # Run multi-agent planning loop -> Epic awaiting approval
./bin/octo plan "Feature X" --model opus --rounds 3
./bin/octo approve 1                    # Spawn [E1-T1], [E1-T2]... with label epic:1
./bin/octo worker --once                # Claim & implement 1 ready task
./bin/octo qa                           # Audit in-review PRs
./bin/octo orchestrate --once           # Reap stale leases + QA + unblock DAG
./bin/octo overlap                      # Check active tasks for file collisions
```
