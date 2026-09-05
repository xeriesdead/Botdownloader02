---
name: GitHub push path
description: How to publish repository changes when the local Git remote cannot authenticate.
---

When the local HTTPS Git remote rejects authentication, use the attached GitHub integration's authenticated API to update the repository file through the Contents API instead of asking for a token in chat. Before updating a dependent module, inspect the related files on GitHub because the deployed branch can be ahead of the local checkout.

**Why:** The workspace Git remote can lose or reject its stored credentials even though the Replit GitHub connection is healthy.

**How to apply:** Read the local file, fetch the current GitHub versions and blob SHAs for related modules, preserve the deployed branch's newer interfaces, then update the intended paths with the authenticated connector and a commit message.