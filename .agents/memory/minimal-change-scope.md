---
name: Minimal-change scope
description: The user's repeated instruction to avoid changing unrelated bot structure
---

For changes to this Telegram bot, keep edits narrowly scoped and preserve the existing architecture unless the user explicitly requests a restructure.

**Why:** The user repeatedly asked that fixes and new integrations not change other parts of the bot.

**How to apply:** Prefer additive provider-specific code and reuse the existing `/get` routing, queue, and quota paths. Avoid broad refactors or changes to unrelated platform behavior.
