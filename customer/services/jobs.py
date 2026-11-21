"""Run a per-ISP billing job on Celery, like the original design; run it right away only if no worker is reachable."""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def run_in_background(task, org, inline) -> dict:
    """Queue ``task(org.id)`` on Celery and return at once (``{"queued": True}``).

    Without a broker (a laptop with no Redis) the job runs inside the request instead, so the button
    never silently does nothing; the result then carries ``"queued": False`` plus the job's numbers.
    """
    if settings.BILLING_JOBS_IN_BACKGROUND:
        try:
            task.apply_async(args=[org.id], retry=False)  # retry=False: fail fast when Redis is down
            return {"queued": True}
        except Exception:
            logger.warning("Celery is not reachable, running %s now", task.name, exc_info=True)
    return {"queued": False, **inline()}
