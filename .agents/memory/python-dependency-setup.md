---
name: Python dependency setup
description: Dependency setup pitfalls for this Telegram bot in the Replit workspace.
---

Do not include the standalone PyPI package `telegram` with `python-telegram-bot`; it can shadow the `telegram` module provided by PTB and break imports. Replit package installation may also append requested packages to `requirements.txt`, so check for duplicates afterward.

**Why:** Test setup exposed the namespace conflict, and package installation added duplicate requirement lines before the dependency list was cleaned.

**How to apply:** Inspect `requirements.txt` before installing Python packages, keep `python-telegram-bot` as the source of the `telegram` imports, and review the dependency diff after using package-management tools.