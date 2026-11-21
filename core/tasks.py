import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task
def backup_database():
    """Nightly database backup (scheduled in app/celery.py)."""
    from pathlib import Path

    from core.management.commands.backup_db import run_backup

    path = run_backup(Path(settings.BACKUP_DIR), settings.BACKUP_KEEP)
    logger.info("database backup written: %s", path)
    return str(path)
