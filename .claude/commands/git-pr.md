---
description: Create a pull request from staged changes — reads the staged diff, summarizes it, waits for review, then pushes and opens the PR.
---

Create a pull request from the current staged and committed changes.

Follow these steps exactly:

1. **Gather context** — run these in parallel:
   - `git diff --cached` to get only staged changes
   - `git status` to see what's staged, unstaged, and untracked
   - `git log origin/master..HEAD --oneline` to see commits ahead of remote
   - `git log --oneline -5` to understand recent commit style

2. **Summarize the changes** — read the staged diff carefully and produce:
   - A short PR title (under 70 characters)
   - A bullet-point description covering what changed and why, grouped by service/area if multiple things changed
   - Note any services that will be redeployed or affected

3. **Present for review** — show the user:
   - The proposed PR title
   - The full description you wrote
   - A summary of which files are staged
   - Ask: "Does this look correct? Any changes to the title or description before I push?"

4. **Wait for confirmation** before proceeding.

5. **Push and create the PR** — once confirmed:
   - `git push origin HEAD` to push the branch
   - `gh pr create --title "<title>" --body "<description>"` using a heredoc
   - Return the PR URL

If there are no staged commits ahead of origin, tell the user and stop.
