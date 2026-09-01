---
name: Railway memory safety
description: Memory constraints for the Telegram downloader deployment
---

Treat large Telegram transfers as exclusive work on Railway: serialize download workers and release cached Pyrogram clients after jobs finish.

**Why:** Multiple Pyrogram transfer clients and large media operations can exceed the container memory limit, causing the process to be killed while Telegram status messages remain stuck.

**How to apply:** Keep the queue at one active transfer unless the deployment's memory capacity is explicitly increased, and avoid retaining per-user Telegram clients indefinitely.