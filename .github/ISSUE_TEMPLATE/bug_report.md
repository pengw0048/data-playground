---
name: Bug report
about: Something behaves incorrectly
title: ""
labels: bug
---

**What happened**
A clear description of the bug, and what you expected instead.

**To reproduce**
Steps from the owning surface: Workspace, Canvas, Jobs/Inbox, a dataset revision, or a write flow.
For an engine or run issue, the node kind and relevant configuration help.

**Relevant identifiers (when available)**
Include only the durable references that help someone find the same state: a Workspace item, Canvas,
dataset and revision, Task, Job, Attempt, write receipt, or backend/provider.

**Environment**

- OS + browser:
- Ran via `make run` / `dataplay` / dev (`make dev-web`)?
- Any relevant env vars (`DP_DATASET_ROOTS`, `DP_EXECUTION`, object store, `DP_AGENT_MODEL`)?

**Logs / errors**
The kernel output and any error shown in the owning surface. (Redact secrets.)
