# Octo Multi-Agent GitHub Kanban & Swarm Controller

Native **Paseo** plugin integrating the **Distributed GitHub Orchestrator (`octo`)** directly into Paseo's UI:

- **Kanban Board**: Live columns (`status:awaiting-approval`, `status:ready`, `status:blocked`, `status:in-progress`, `status:in-review`, `status:rework`, `status:done`).
- **Phase 1 -> Phase 2**: Trigger multi-agent Planning (`octo plan`) and approve Epics with one click to spawn Epic-tagged tasks (`[E1-T1]`, `epic:1`).
- **Agent / Model Selector**: Choose any configured agent or harness profile from `agents/*.md` and `orchestrator.yaml`.
- **Cluster SSH Control**: Probe and manage configured worker nodes.
