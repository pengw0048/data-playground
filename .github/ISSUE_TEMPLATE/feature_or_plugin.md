---
name: Feature or plugin idea
about: Propose a new capability, node, or plugin seam
title: ""
labels: enhancement
---

**What you want to do**
The data task or workflow you're trying to accomplish (not just the mechanism).

**Proposed shape**
A new built-in node? A new operator on an existing one? Or a plugin via an existing seam
(`add_node` / `add_adapter` / `add_destination` / `add_runner` / `add_capability` / …)? If a plugin
seam is missing something you need, describe the gap.

**Keeping it generic**
Data Playground's core stays provider-agnostic and offline-first. If this is specific to one
backend / store / vendor, note how it could live behind a plugin rather than in the core.
