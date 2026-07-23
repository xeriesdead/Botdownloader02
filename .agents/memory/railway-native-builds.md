---
name: Railway native Python builds
description: Deployment constraint for Python dependencies that compile native extensions on Railway.
---

Railway's automatic Python/Nixpacks builder may not include a C compiler, so packages such as `tgcrypto` can fail during dependency installation. Prefer an explicit Dockerfile build that installs the required compiler and pins a compatible Python base image.

**Why:** The bot's dependency build failed with `gcc: No such file or directory` under Railway's automatic Python 3.13 builder, while the project's Dockerfile built successfully after installing `build-essential` and `gcc`.

**How to apply:** Keep Railway configuration pointed at `Dockerfile` whenever the dependency set includes native extensions; do not rely on automatic Python detection for this service.