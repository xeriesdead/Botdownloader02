---
name: GitHub API fallback
description: Safe repository synchronization when the local HTTPS Git remote cannot authenticate
---

When the configured HTTPS Git remote rejects authentication, an added standard GitHub connector can publish the already-validated local file contents through GitHub's Git database API without exposing a token in chat.

**Why:** The workspace may have a valid GitHub integration while the shell's cached HTTPS credential is stale; asking for a token would bypass the safer managed connection.

**How to apply:** Resolve the added `github` connector, verify the remote branch parent, create blobs/tree/commit, update the branch ref without force, then verify the resulting remote commit and keep the local working tree clean. A separate GitHub App row may remain `not_added` and is not sufficient for this API path.

**Environment note:** Use the connector's `proxyFetch` inside the integration sandbox; do not install an SDK into the project or request a personal token.

**Why:** A Git blob POST with UTF-8 content returned a Cloudflare 403 through the connector proxy, while Base64 encoding of the same content succeeded.

**How to apply:** Send Git blob contents as Base64 with `encoding: "base64"` and verify each returned blob SHA against the local object before creating the tree.