# The test set

Eleven FICTIONAL St. Louis-area businesses with known good answers. `town testset` reruns
the agents on them after every prompt, playbook, model or engine change. A change only
ships if the score holds or improves (BLUEPRINT 7.1).

Never add a real business: real sites change (breaking the answers), and a test must
never be able to contact anyone. Every case uses a 555-01xx phone, an `@example.com`
email or none, and a reserved `*.example.com` website. The two free-builder/social URLs
use the real host names (that's what Scout recognizes) with slugs marked "fictional".

**Offline rule:** in a test-set run, every case URL (including the facebook.com and
wixsite.com ones) resolves to that case's local `site/` folder, and nothing is ever fetched
from the internet. Agents that look at a site go through `resolve_case_url()` in
`packs/websites/testset_runner.py`, which refuses any URL that isn't the case's own.
A pytest fails if a test-set run makes any outside network request. The fixtures have no
outside links or assets.

## Each case folder
- `case.yaml`: the business, shaped like a row in the `leads` table.
- `answer.yaml`: Charlie's answer key. It's his judgment, so changes need his OK.
- `site/`: the frozen website, when the business has one.

## Answer key fields
- `worth_pursuing`: yes or no (the final call, after inspection).
- `scout`: what Scout should do with only the lead row to go on.
  `pick`, `reject`, or `inspector_decides` (Scout can't see the site, so it isn't scored on these).
- `reject_reason`: why, for a bad lead.
- `contact_route`: `email`, `held` (no public email: saved as "held (no email)" and never contacted; email-first, 2026-11-03), or `none` (a lead we'd never contact). `call_script_when_asked: yes` on a held case: the Messenger test set still drafts its call script, as if the owner had replied asking for a call.
- `site_problems`: the Inspector's categories (below). Empty means nothing wrong.
  `not_inspected` means the lead is rejected before the Inspector, so it isn't scored there.
- `site_notes`: what a careful person would notice on the site.
- `good_draft`: what a good outreach email or call script would say.

## Inspector categories
- `no_site`: no website at all.
- `social_or_builder_only`: only a social profile or a free-builder page.
- `not_mobile_friendly`: no mobile layout (no viewport tag, fixed width, tiny text).
- `no_https`: served over plain http, or sends forms over http.
- `slow`: a heavy page (oversized images or files).
- `outdated`: an old design or stale content (old copyright, old dates, dated tech).
- `broken_links`: links or images that point at nothing.

The Inspector is scored on cases 01-09 (not `not_inspected` ones): one point per case when the
problems it finds match `site_problems` exactly. It judges "outdated" against 2026, the year the
fixtures were frozen.

## The Critic's fixtures (`critic/`)
Five frozen sample sites, all for fictional businesses from the cases above. `town testset --agents critic`
(on demand: each review uses Charlie's Claude plan) reviews each one and scores a point when the verdict
matches `answer.yaml` and, for a flawed site, the Critic's reasons name the flaw (`reason_mentions`).
Every fixture passes every plain-code check on purpose: a flaw the code already catches would only test the code.
- `c1-good-dentist`: case 07 as a Builder v2 preview (rebuilt 2026-11-02 from the old c1: same design and
  facts, plus a real header and footer and the tagged sample sections). Must pass.
- `c2-invented-claim`: c1 plus "Same-day emergency visits available." in the hero.
- `c3-generic-template`: hand-written for case 02 in the stock "AI template" look; honest content.
- `c4-phone-bug`: c1 plus one CSS rule below 480px that puts the call button on top of the headline.
- `c5-thin-preview` (added 2026-11-02): the OLD c1 (dev build #8 of case 07): honest and clean but thin. Must
  fail on "would the owner want this?" (Charlie's review of real build #8).
- 2026-10-26: c1, c2 and c4 got one CSS rule (a smaller sun between 640 and 1023px, in both `src/` and
  `dist/`), because at tablet width the sun sat behind the headline, and a graphic overlapping text is now a
  must-fix and a plain-code hard check. The answer keys didn't change; each flawed fixture still has exactly
  its one flaw.
- 2026-11-02 (Builder v2): c1, c2 and c4 rebuilt from the new c1 (`src/` and `dist/`); c5 added; the old c1 and
  c3 now fail the new owner_wants score too (c3's key already says fail). Every fixture still passes every
  plain-code check (checked with the Critic's own measuring: Lighthouse 100/100 on c1, c2 and c4).
Each folder has `brief.json` (what the Builder was given), `dist/` (the built site), `src/` when there is one,
and `answer.yaml`. The answer keys are Charlie's judgment (drafted 2026-10-21, pending his approval).
