"""Send one email from a service and report success, never raising.

``deliver`` sends once and returns whether it worked (the email module already logged a masked
failure). ``deliver_quietly`` is the background task wrapper. ``deliver_within_timeout`` bounds a
synchronous send (sign in codes) by ``SMTP_TIMEOUT_SECONDS``, the email timeout used for every
provider here, so a slow provider cannot hold a sign in request open.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.integrations import email as email_integration

logger = logging.getLogger(__name__)


async def deliver(message: email_integration.EmailMessage) -> bool:
    """Send once; report success. Failures are logged by the email module without details."""
    try:
        await email_integration.send_email(message)
    except email_integration.EmailDeliveryError:
        return False
    except Exception:  # an unexpected failure must not undo committed work
        logger.warning("email send failed unexpectedly")
        return False
    return True


async def deliver_quietly(message: email_integration.EmailMessage) -> None:
    """Background-task wrapper: the response has already been sent, so nothing is raised."""
    await deliver(message)


async def deliver_within_timeout(message: email_integration.EmailMessage) -> bool:
    """Send synchronously, giving up after ``SMTP_TIMEOUT_SECONDS``; report success."""
    try:
        return await asyncio.wait_for(deliver(message), timeout=get_settings().smtp_timeout_seconds)
    except TimeoutError:
        logger.warning("email send timed out")
        return False
