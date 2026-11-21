"""Create a consistent database backup and prune old ones.

    python manage.py backup_db            # -> BACKUP_DIR/billsheba-YYYYmmdd-HHMMSS.{sqlite3,sql.gz}

SQLite uses the online-backup API (safe while the app is running). PostgreSQL uses ``pg_dump``.
"""

import gzip
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


def run_backup(directory: Path, keep: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db = settings.DATABASES["default"]
    engine = db["ENGINE"]

    if engine.endswith("sqlite3"):
        target = directory / f"billsheba-{stamp}.sqlite3"
        source = sqlite3.connect(str(db["NAME"]))
        try:
            dest = sqlite3.connect(str(target))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
    elif "postgres" in engine:
        if not shutil.which("pg_dump"):
            raise CommandError("pg_dump is not installed in this environment.")
        target = directory / f"billsheba-{stamp}.sql.gz"
        params = connection.get_connection_params()
        cmd = ["pg_dump", "--no-owner", "--dbname", db["NAME"]]
        for flag, key in (("--host", "host"), ("--port", "port"), ("--username", "user")):
            if params.get(key):
                cmd += [flag, str(params[key])]
        env = {"PGPASSWORD": str(params.get("password", ""))}
        result = subprocess.run(cmd, capture_output=True, env=env, check=False)
        if result.returncode != 0:
            raise CommandError(result.stderr.decode()[:500])
        with gzip.open(target, "wb") as fh:
            fh.write(result.stdout)
    else:
        raise CommandError(f"Unsupported database engine: {engine}")

    # keep the newest `keep` backups
    backups = sorted(directory.glob("billsheba-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
    return target


class Command(BaseCommand):
    help = "Back up the database to BACKUP_DIR and keep only the newest BACKUP_KEEP files"

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=None)
        parser.add_argument("--keep", type=int, default=None)

    def handle(self, *args, **options):
        path = run_backup(Path(options["dir"] or settings.BACKUP_DIR), options["keep"] or settings.BACKUP_KEEP)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {path} ({path.stat().st_size} bytes)"))
