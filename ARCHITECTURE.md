# Architecture

The Town is a Python engine plus a website-business pack, backed by SQLite.
Its unit of control is an action with exact content, an approval state and an
audit trail. An agent is a role with instructions, model routing and a limited
set of tools. It is not necessarily a separate model process.

## The approval spine

`engine/mayor.py` queues proposals and records decisions. `engine/executor.py`
refuses execution without approval and rechecks the Guard and kill switch.
`engine/decisions.py` serves the same decision path to terminal and HQ callers.
Rejections require reasons; edits preserve both versions. The event log records
proposal, review, decision, execution and cost. Feedback and the dashboard derive
from that record instead of maintaining a second version of events.

```mermaid
sequenceDiagram
    participant P as Website pack
    participant M as Mayor / Guard
    participant C as Charlie
    participant E as Executor / Outbox
    P->>M: Site hash, Critic result, exact email, recipient
    M->>M: Hard checks, optional soft flags
    M->>C: One outreach package
    C->>M: Approve, edit email, or reject with reason
    M->>E: Approved content only
    E->>E: Kill switch, Guard, hash and recipient rechecks
    E->>E: Publish approved dist; verify live page
    E->>E: Draft, or gated send when go-live is on
    E->>M: Recorded result; no automatic email retry
```

The package stops at the first failure. A successful publish followed by a failed
email remains visible as a partial outcome. Following up requires another
approved action. Model text cannot confirm an action: confirmation comes from
the user's own terminal input or an explicit HQ request.

## Roles and implementation

| Role | Responsibility | Location or status |
| --- | --- | --- |
| Mayor | Queue, decisions, routing | `engine/mayor.py` |
| Guard | Deterministic blocks; model may add flags | `engine/guard.py`, `packs/websites/guard.py` |
| Jarvis / Reaper | Chat, briefing, confirmation proposals | `engine/jarvis.py`, `engine/reaper.py` |
| Librarian | Evidence, lessons and approved playbook changes | `packs/websites/librarian.py`, `playbook_gate.py` |
| Treasurer | Logged API cost and budget pauses | `engine/budget.py`; fuller business reporting planned |
| Scout | Licensed candidates and deterministic saving rules | `packs/websites/scout.py` |
| Inspector | Measured need and a bounded factual note | `packs/websites/inspector.py` |
| Builder | A private site workspace and build | `packs/websites/builder.py` |
| Critic | Plain-code checks plus model review and send-backs | `packs/websites/critic.py`, `critic_measure.py` |
| Messenger | First-email packages and follow-up proposals | `packs/websites/messenger.py`, `package.py` |
| Clerk | Read Town replies, opt-outs, bounces, human queue | `packs/websites/clerk.py`; fuller scheduling planned |
| Caretaker | Ongoing client site maintenance | Planned |
| Herald | Business portfolio and marketing drafts | Planned |

## The Guard and switches

Hard rules are code: sending account, verified recipient, mailing address,
opt-out text, do-not-contact rechecks, daily caps, budgets and no prospect AI
calls. Soft model judgments may add concerns but cannot remove a hard failure.
The pack owns its limits in one YAML file. The engine stays business-neutral and
never imports the pack.

A stopped Town runs no agent or execution. The go-live switch is separate from
this stop state and from individual approvals. Only the human can enable real
sending after logged shadow days and launch checks. Every send still needs all
gates. Gmail permits exactly the required draft/read operations and one
`drafts.send` path, called only through the Outbox and never retried. Clerk reads
only Town threads and related bounce notices. It cannot send a reply itself.
Phone integration is planned and may call only Charlie.

Builder runs inside a private workspace with restricted file tools. Plain code
checks source before building and facts afterward. Critic gets screenshots and
measurements from code; it has no browser tool. Only a Critic-passed, unchanged
build can be proposed for publication. Public fixture sites are examples of
these checks, not real prospects.

## Autopilot

`packs/websites/autopilot.py` cycles through upkeep and inbox, inspections,
builds and Critic reviews, outreach packages, allowed follow-ups, and scouting
when the waiting pool is low. It never approves, rejects or edits. Limits and
each order's checks still apply. One loop runs at a time, including reactive
cycles after human decisions. Usage limits pause relevant model work; caps and
holds appear in the log and HQ. Reaper's Go command proposes starting that loop;
the user's confirmation starts it. Stop and stand down remain available.

## Memory and model routes

SQLite stores the record, feedback and proposed lessons. Approved playbooks
supply concise instructions on the next job. The Librarian must cite evidence;
before/after test-set evaluation must not regress. A model cannot write an
unapproved playbook change. Public playbooks are rewritten baselines.

The pack assigns models by tier. Haiku uses the API for small classification,
routing and wording jobs. Sonnet and Opus use the official `claude -p` CLI for
writing, building, reviewing and deeper analysis. The API key is removed from
that subprocess. API costs and subscription job measurements are logged
separately; API-equivalent build cost is not a cash charge.

## HQ

`engine/hq.py` serves a local FastAPI application on loopback, with a per-launch
token. `hq/web/` contains plain JavaScript modules; three.js renders the village
using the bundled Kenney CC0 models. The business pack supplies the village
layout and plain-English views. The stream comes from real events, and visible
handoffs travel through Town Hall. Needs you, Sites, Reaper chat and catch-up all
read the same queue and log. Sites are served read-only with a strict script
policy. HQ adds no new approval authority.

## Reproducing the showcase

The private checkout's `tools/make_showcase.py` reads an explicit file manifest,
rewrites public examples, scrubs known private details and checks every output
byte before writing to a new folder. Real lead identities and environment values
are used only as local scan inputs. `--stats` exports numerical aggregates, not
database rows. It never copies `.git`, private records, state or credentials.

`tools/showcase_media.py` creates a temporary fictional test-set Town with
scripted models. It captures the actual HQ UI, a frozen fictional site and a
short Town Hall handoff GIF. It approves and fake-publishes only the fictional
homepage; the prospect package stays proposed with the real Guard clear. No
external operation occurs.
The media provenance manifest binds those files to the reviewed capture.

The public snapshot has no live instance configuration. To run tests, install
the dev extras and run `pytest -m "not hq_browser"`. Operational deployment,
credentials and real records belong in a separate private environment.
