---
name: GitHub push path
description: How to publish repository changes when the local Git remote cannot authenticate.
---

When the local HTTPS Git remote rejects authentication, use the attached GitHub integration's authenticated API to update the repository file through the Contents API instead of asking for a token in chat.

**Why:** The workspace Git remote can lose or reject its stored credentials even though the Replit GitHub connection is healthy.

**How to apply:** Read the local file, fetch its current GitHub blob SHA, then update that same path on the intended branch with the authenticated connector and a commit message.