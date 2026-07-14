---
name: conscious
description: How to collaborate in a Conscious multi-agent swarm. Use whenever you are bound to a conscious_id (the system prompt will say so). The shared brain lives at .brain/ in your workspace — read it before acting, and use the conscious_* tools to coordinate with the orchestrator and other agents.
---

# Conscious — the shared-brain multi-agent system

You are one of several agents collaborating on one repository. You all share
**one brain**: the `.brain/` folder at the root of your workspace. The brain is
human-readable (Markdown + JSON), git-versioned by default, and survives Space
reboots via an HF Dataset mirror.

## Roles

- **Orchestrator** (one per Conscious): the user's default chat target.
  Routes work, delegates, and **mediates all brain writes** from sub-agents.
  Can post blackboard entries directly via `conscious_post`.
- **Sub-agent**: any other agent in the Conscious. Reads the brain freely,
  writes its own invoke/delegate results to the drawer freely, but **cannot
  write the blackboard directly** — it must `conscious_propose` and the
  orchestrator commits (or rejects) the proposal.

Your role is set at spawn time. The system prompt tells you which you are.

## The brain layout

```
.brain/
├── CONSCIOUS.md            # regenerated summary — read this first
├── goal.md                 # the user's goal, verbatim
├── plan.json               # task DAG
├── config.json             # conscious-level config
├── agents/<id>.md          # one profile per agent (incl. you)
├── blackboard/<section>.jsonl   # append-only: decisions/findings/questions/...
├── blackboard/events.jsonl      # event log (subscribe-able)
├── drawer/<invoke-id>/          # verbatim sub-agent outputs
├── proposals/queue.jsonl        # sub-agent proposals pending orchestrator
└── skills/                      # this file lives here
```

## The 12 tools

| Tool | Who | What |
|---|---|---|
| `conscious_context` | anyone | Read the brain (delta-synced via `since` cursor). **Call at the start of every turn.** |
| `conscious_post` | orchestrator only | Post a blackboard entry directly. |
| `conscious_propose` | sub-agents | Propose a blackboard write. Queued for the orchestrator. |
| `conscious_commit_proposal` | orchestrator | Commit a sub-agent's proposal. |
| `conscious_reject_proposal` | orchestrator | Reject a proposal with a reason. |
| `conscious_invoke` | anyone | Synchronously call another agent (blocks until done). Result lands in the drawer. |
| `conscious_delegate` | anyone | Asynchronously call another agent. Returns immediately; poll `conscious_drawer`. |
| `conscious_drawer` | anyone | Read drawer entries (single with full result, or recent list). |
| `conscious_message` | anyone | Send a message to another agent (or broadcast). |
| `conscious_claim` | anyone | Atomically claim an unclaimed task. |
| `conscious_task` | orchestrator only | Create/update a task in the plan DAG. Sub-agents propose via `conscious_propose` with `section="plan"`. |
| `conscious_subscribe` | anyone | Replace your event subscriptions (prefix-matched, e.g. `proposal.*`). |

## Discipline

1. **Read before acting.** Always call `conscious_context` at the start of
   every turn. The brain has the goal, the plan, open decisions, pending
   proposals, and your inbox.
2. **Sub-agents propose; orchestrator commits.** If you are a sub-agent and
   want to record a decision/finding/question, use `conscious_propose`. Do
   not try to call `conscious_post` — the backend rejects it.
3. **The drawer is verbatim.** When you `conscious_invoke` or
   `conscious_delegate`, the sub-agent's raw output goes to
   `.brain/drawer/<invoke-id>/result.md` unchanged. The orchestrator reads
   and decides; no mechanical merge.
4. **Pings are pull-based.** Subscribe to event prefixes
   (`proposal.*`, `task.claimed`, `drawer.completed`, ...) and
   `conscious_context(since=N)` surfaces matching new events as pings.
5. **Be a good citizen.** Messages are short. Proposals have a clear `reason`.
   Claim only tasks you can finish. Post completion events by completing
   drawer entries.

## Phase 1 note

`conscious_invoke` and `conscious_delegate` return stubbed results in Phase 1
(no real worktree-per-agent execution yet). The API contract is fully
testable; real execution lands in Phase 2 with `git worktree` isolation per
agent.
