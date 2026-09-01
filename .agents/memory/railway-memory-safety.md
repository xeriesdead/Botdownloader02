---
name: Railway memory safety
description: Memory constraints for the Telegram downloader deployment
---

Treat large Telegram transfers as exclusive work on Railway: serialize download workers, release cached Pyrogram clients after jobs finish, and isolate each temporary download in a removable directory.

**Why:** Multiple Pyrogram transfer clients and large media operations can exceed the container memory limit, causing the process to be killed while Telegram status messages remain stuck; failed downloads can otherwise leave partial files behind.

**How to apply:** Keep the queue at one active transfer unless the deployment's memory capacity is explicitly increased, avoid retaining per-user Telegram clients indefinitely, and remove the whole temporary directory in `finally` after each transfer.