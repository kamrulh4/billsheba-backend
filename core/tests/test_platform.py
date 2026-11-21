"""Operability: health probes, request ids, structured logs, backups."""

import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from app.logging_utils import JsonFormatter, RequestIdFilter, request_id_var
from core.management.commands.backup_db import run_backup


class HealthTests(TestCase):
    def test_liveness_and_readiness(self):
        self.assertEqual(self.client.get("/health/").json()["status"], "healthy")
        res = self.client.get("/health/ready")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ready", "database": "ok", "cache": "ok"})

    def test_readiness_reports_503_when_database_is_down(self):
        with mock.patch("django.db.backends.utils.CursorWrapper.execute", side_effect=Exception("db down")):
            res = self.client.get("/health/ready")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["status"], "degraded")
        self.assertEqual(res.json()["database"], "error")


class RequestIdTests(TestCase):
    def test_id_is_generated_and_echoed(self):
        res = self.client.get("/health/")
        self.assertRegex(res["X-Request-ID"], r"^[0-9a-f]{16}$")

    def test_incoming_id_is_propagated(self):
        res = self.client.get("/health/", HTTP_X_REQUEST_ID="trace-abc-123")
        self.assertEqual(res["X-Request-ID"], "trace-abc-123")


class JsonLoggingTests(SimpleTestCase):
    def test_json_line_has_request_id_and_exception(self):
        logger = logging.getLogger("test.jsonlog")
        record = logger.makeRecord("test.jsonlog", logging.ERROR, __file__, 1, "boom %s", ("now",), None)
        token = request_id_var.set("req-42")
        try:
            RequestIdFilter().filter(record)
            line = json.loads(JsonFormatter().format(record))
        finally:
            request_id_var.reset(token)
        self.assertEqual((line["level"], line["msg"], line["request_id"]), ("ERROR", "boom now", "req-42"))
        try:
            raise ValueError("x")
        except ValueError:
            import sys

            rec2 = logger.makeRecord("t", logging.ERROR, __file__, 1, "bad", (), sys.exc_info())
        self.assertIn("ValueError", json.loads(JsonFormatter().format(rec2))["exc"])


class BackupTests(SimpleTestCase):
    def sqlite_source(self, directory):
        path = Path(directory) / "src.sqlite3"
        con = sqlite3.connect(path)
        con.execute("create table t (id integer primary key, v text)")
        con.execute("insert into t (v) values ('hello')")
        con.commit()
        con.close()
        return path

    def test_sqlite_backup_is_restorable_and_old_ones_are_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.sqlite_source(tmp)
            out = Path(tmp) / "out"
            db = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(src)}
            with override_settings(DATABASES={"default": db}):
                for i in range(4):
                    path = run_backup(out, keep=2)
                    os.utime(path, (time.time() + i, time.time() + i))
                    time.sleep(1.05)  # filenames have 1-second resolution
            files = sorted(out.glob("billsheba-*"))
            self.assertEqual(len(files), 2)
            restored = sqlite3.connect(files[-1])
            self.assertEqual(restored.execute("select v from t").fetchone(), ("hello",))
            restored.close()

    def test_management_command_writes_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.sqlite_source(tmp)
            db = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(src)}
            with override_settings(DATABASES={"default": db}):
                call_command("backup_db", dir=str(Path(tmp) / "b"), keep=3, stdout=open(os.devnull, "w"))
            self.assertEqual(len(list((Path(tmp) / "b").glob("billsheba-*.sqlite3"))), 1)

    def test_nightly_task_is_registered_and_scheduled(self):
        from app.celery import cel_app

        cel_app.loader.import_default_modules()  # what a real worker does at startup
        self.assertIn("core.tasks.backup_database", cel_app.tasks)
        self.assertEqual(cel_app.conf.beat_schedule["nightly-backup"]["task"], "core.tasks.backup_database")
