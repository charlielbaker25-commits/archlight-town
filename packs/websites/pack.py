"""Everything the launcher needs from the website pack, in one place."""
from pathlib import Path

from engine.executor import Handler
from packs.websites.clerk import clerk_handlers
from packs.websites.leads import lead_handlers
from packs.websites.librarian import librarian_handlers
from packs.websites.playbook_gate import playbook_handlers
from packs.websites.outreach import OUTREACH_PACKAGE, Outbox, outreach_handlers
from packs.websites.previews import LIVE_TYPES, Publisher
from packs.websites.schema import PACK_SCHEMAS, upgrade

PACK_DIR = Path(__file__).resolve().parent
LIMITS_FILE = PACK_DIR / "limits.yaml"
MODELS_FILE = PACK_DIR / "models.yaml"
PLAYBOOKS_DIR = PACK_DIR / "playbooks"

__all__ = ["PACK_SCHEMAS", "LIMITS_FILE", "MODELS_FILE", "PLAYBOOKS_DIR", "LIVE_EXTERNAL_TYPES", "handlers",
           "publish_handlers", "upgrade"]

# External action types that really run once Charlie approves them (Charlie, 2026-10-21): publishing a
# preview or the homepage. Each is gated by the Critic's pass and the dist hash, and the dev instance's
# Cloudflare client is a fake. Every other external type stays dry-run: send_email is NEVER in this set
# (its dry run saves a Gmail draft). It is live only through the executor's `live_now` check, which asks
# golive.live_types at each execution: Charlie's switch, the mailing address and the shadow days.
# The outreach package (Charlie, 2026-11-05) is here because its first step is a real publish. Its email step is
# NOT made live by this: it goes through the same Outbox, which saves a Gmail draft while go-live is off and sends
# (Outbox.send, every go-live gate checked there) only while it's on (packs/websites/package.py).
LIVE_EXTERNAL_TYPES = LIVE_TYPES | {OUTREACH_PACKAGE}


def handlers(outbox: Outbox | None = None) -> dict[str, Handler]:
    """The action types this pack knows how to carry out. The launcher passes an Outbox (with the Gmail
    client its real-vs-fake rule picked), so an approved email is saved as a Gmail draft. The publish
    handlers need a Cloudflare client, so the launcher adds them from its Publisher (publish_handlers)."""
    return {**lead_handlers(), **outreach_handlers(outbox), **librarian_handlers(), **playbook_handlers(),
            **clerk_handlers()}


def publish_handlers(publisher: Publisher, outbox: Outbox | None = None) -> dict[str, Handler]:
    """Publishing, and the outreach package (it publishes, then hands its email to the Outbox)."""
    from packs.websites.package import package_handlers   # here: package imports the Messenger and previews
    return {**publisher.handlers(), **package_handlers(publisher, outbox)}
