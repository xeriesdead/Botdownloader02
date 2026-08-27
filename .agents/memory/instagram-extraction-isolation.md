---
name: Instagram extraction isolation
description: Runtime constraint for preventing Instagram extractor hangs from blocking bot workers.
---

Blocking third-party Instagram extractors must run in a killable child process when the bot needs a hard timeout.

**Why:** Cancelling an asyncio task or waiting thread does not reliably interrupt a blocking network call, so the Telegram status can remain at analysis indefinitely and the queue worker can stay occupied.

**How to apply:** Keep the hard timeout around the process boundary, terminate the child on expiry, and clean up its temporary directory before reporting the failure.

Instagram/Instaloader APIs also vary across releases; prefer constructor arguments supported by the installed version and catch the stable base exception instead of assuming optional exception names exist.