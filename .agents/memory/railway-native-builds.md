---
name: Railway native Python builds
description: Deployment constraint for Python dependencies that compile native extensions on Railway.
---

Railway's automatic Python/Nixpacks builder may not include a C compiler, so packages such as `tgcrypto` can fail during dependency installation. Prefer an explicit Dockerfile build that installs the required compiler and pins a compatible Python base image. This service uses Telegram polling, not webhook mode, in production.

**Why:** The bot's dependency build failed with `gcc: No such file or directory` under Railway's automatic Python 3.13 builder, while the project's Dockerfile built successfully after installing `build-essential` and `gcc`. Polling is the selected production mode because Railway runs the bot as an always-on process.

**How to apply:** Keep Railway configuration pointed at `Dockerfile` whenever the dependency set includes native extensions; do not rely on automatic Python detection for this service. Run `main.py` and only require `BOT_TOKEN`, `API_ID`, `API_HASH`, and `DATABASE_URL`; do not add webhook-only secrets for the polling deployment.