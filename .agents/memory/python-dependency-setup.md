---
name: Python dependency setup
description: Dependency setup pitfalls for this Telegram bot in the Replit workspace.
---

Do not include the standalone PyPI package `telegram` with `python-telegram-bot`; it can shadow the `telegram` module provided by PTB and break imports. Replit package installation may also append requested packages to `requirements.txt`, so check for duplicates afterward.

**Why:** Replit's package installer added the standalone `telegram` package when asked to install `python-telegram-bot`. Removing the conflicting package left PTB's metadata installed but its import files missing, so tests then failed at import time.

**How to apply:** Inspect `requirements.txt` before installing Python packages, keep `python-telegram-bot` as the source of the `telegram` imports, and review the dependency diff after using package-management tools. If the installer injects `telegram`, do not repeatedly uninstall/reinstall it; validate with an isolated test shim or the clean Railway dependency environment instead.