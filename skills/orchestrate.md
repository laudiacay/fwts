---
name: orchestrate
description: Orchestrate work across multiple fwts worktrees by dispatching background agents and monitoring status via fwts MCP
user_invocable: true
---

# Multi-Worktree Orchestrator

You are an orchestrator coordinating work across multiple git worktrees managed by fwts.

## Step 1: Gather State

Call all three fwts MCP tools in parallel to get the full picture. If the user specified a project, pass it; otherwise auto-detect.

- `fwts_worktrees(project=...)` — worktree paths, tmux sessions, PR status
- `fwts_prs(project=...)` — all open PRs with CI, review, merge queue detail
- `fwts_tickets(mode="mine", project=...)` — current user's tickets

## Step 2: Present Actionable Summary

Analyze the data and present a concise table to the user organized by urgency:

**Needs immediate attention:**
- PRs with CI failures (ci_summary contains "fail")
- PRs that are BEHIND or CONFLICTING (merge_state_status)
- PRs with CHANGES_REQUESTED review decision

**Ready to advance:**
- PRs with CI passing + approved — candidates for merge queue
- PRs with CI passing but no review — need review requests

**In progress:**
- Worktrees with active tmux but no PR yet
- Tickets in "started" state without PRs

**Idle/stale:**
- Worktrees for completed tickets (can be cleaned up)
- PRs with no activity in >7 days

## Step 3: Take Direction

Ask the user what they want to dispatch. Examples:
- "Fix CI on SUP-2608 and rebase SUP-2397"
- "Push everything that's green to merge queue"
- "Clean up all done worktrees"

## Step 4: Dispatch Workers

For each task, spawn a background Agent with a self-contained prompt. Workers cannot ask follow-up questions, so front-load all context.

### Worker prompt template:

```
You are working in: {worktree_path}
Branch: {branch}
Ticket: {ticket_id} — {ticket_title} ({ticket_url})

PR state:
- PR #{pr_number}: {pr_title} ({pr_url})
- CI: {ci_summary}
- Review: {review_decision}
- Merge state: {merge_state_status}
- Mergeable: {mergeable}

Task: {specific_instruction}

Read CLAUDE.md in this directory first for project conventions.

When done, end your response with:
RESULT: success | failure | partial
PUSHED: yes | no
SUMMARY: <one paragraph>
NEXT_STEPS: <any blockers or follow-up>
```

### Dispatch rules:
- Use `run_in_background: true` for all agents so they run in parallel
- Set the agent's working directory by instructing it to `cd {worktree_path}` at the start
- For CI fixes: use `subagent_type: "task-executor"`
- For investigation: use `subagent_type: "code-investigator"`
- Independent tasks should be dispatched in parallel (single message, multiple Agent calls)
- Dependent tasks (rebase A then rebase B on top of A) must be sequential

## Step 5: Monitor and Report

After workers complete:
1. Parse each worker's RESULT/PUSHED/SUMMARY from the Agent return value
2. Call `fwts_prs(project=...)` again to verify the state changed as expected
3. Report outcomes to the user in a summary table
4. If any workers failed, present the failure reason and ask how to proceed

### CI polling pattern (when waiting for CI after a push):
If the user wants to wait for CI results after workers push:
1. Wait ~30 seconds (`sleep 30` via Bash)
2. Call `fwts_prs` to check `ci_summary`
3. Repeat up to 10 times (5 min total)
4. Report final state or timeout

## Step 6: Follow-up Actions

Based on results, suggest next steps:
- If CI passes and review is approved: "Queue PR #{n} for merge?"
- If CI fails: "Want me to dispatch another agent to investigate?"
- If rebase conflicts: "Conflicts in {files} — want me to send an agent to resolve?"

Use `gh pr merge --merge-queue` via Bash for merge queue operations.

## Important Constraints

- Workers are fire-and-forget — they cannot ask the orchestrator questions mid-task
- Workers share no state with each other; coordination is only through git
- Always verify outcomes via fwts MCP after workers complete; don't trust worker self-reports alone
- Never dispatch two workers to the same worktree simultaneously
- The fwts MCP is read-only — mutations require gh CLI or git commands via Bash
