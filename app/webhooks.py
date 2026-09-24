"""
Clerk webhook receiver: keeps app/users.py's local mirror in sync.

Clerk delivers webhooks via Svix, which signs every request - verifying that
signature (svix.Webhook) is what stops this endpoint from accepting a forged
user.created/updated/deleted from anyone who finds the URL. There is no
official Clerk Python SDK helper for this (see clerk-webhooks skill's JS-only
verifyWebhook()), so it's done directly with the svix package Clerk's own
delivery is built on.
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from svix.webhooks import Webhook, WebhookVerificationError

from app import users
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/clerk")
async def clerk_webhook(request: Request):
    if not settings.CLERK_WEBHOOK_SIGNING_SECRET:
        # Not configured yet - fail loudly rather than silently accepting
        # unverified events, which would let anyone who finds this URL
        # write arbitrary "Clerk" data into the users mirror.
        raise HTTPException(status_code=503, detail="Webhook signing secret not configured")

    body = await request.body()
    try:
        # Webhook.verify() only validates the signature (raises on failure)
        # and returns None - it does not parse or hand back the payload, so
        # the body is parsed separately below once verification succeeds.
        Webhook(settings.CLERK_WEBHOOK_SIGNING_SECRET).verify(body, dict(request.headers))
    except WebhookVerificationError as exc:
        logger.warning("Clerk webhook verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc

    event = json.loads(body)

    event_type = event.get("type")
    data = event.get("data") or {}

    if event_type in ("user.created", "user.updated"):
        await users.upsert_user(data)
        logger.info("Synced user %s (%s)", data.get("id"), event_type)
    elif event_type == "user.deleted":
        user_id = data.get("id")
        if user_id:
            await users.delete_user(user_id)
            logger.info("Removed user %s from local mirror (user.deleted)", user_id)
    else:
        logger.debug("Ignoring unhandled Clerk webhook event: %s", event_type)

    return {"received": True}
