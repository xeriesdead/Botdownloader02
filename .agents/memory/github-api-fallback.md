---
name: GitHub API fallback
description: Safe repository synchronization when the local HTTPS Git remote cannot authenticate
---

When the configured HTTPS Git remote rejects authentication, the installed Replit GitHub connection can publish the already-validated local file contents through GitHub's Git database API without exposing a token in chat.

**Why:** The workspace may have a valid GitHub integration while the shell's cached HTTPS credential is stale; asking for a token would bypass the safer managed connection.

**How to apply:** Resolve the active GitHub connection, verify the remote branch parent, create blobs/tree/commit, update the branch ref without force, then verify the resulting remote commit and keep the local working tree clean.