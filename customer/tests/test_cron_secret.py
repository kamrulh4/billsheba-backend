"""Platform-wide billing jobs: open to the platform owner and to the scheduler's secret, nobody else."""

from unittest import mock

from django.test import override_settings

from customer.tests.test_reliability import API, Base

URLS = (
    "generate-organizations-bills",
    "deactive-organizations-due-customer",
    "deactive-organizations-expired-customer",
    "reconcile-payments",
    "daily-cycle",
)


@override_settings(BILLING_JOBS_IN_BACKGROUND=False)  # run the job inline in the test
class CronSecretTests(Base):
    def call(self, url, **headers):
        self.client.credentials(**headers)
        return self.client.get(f"{API}/tasks/{url}")

    @override_settings(CRON_SECRET="s3cret-value")
    def test_the_right_secret_runs_the_job_without_any_login(self):
        for url in URLS:
            res = self.call(url, HTTP_X_CRON_SECRET="s3cret-value")
            self.assertEqual(res.status_code, 200, (url, res.content))

    @override_settings(CRON_SECRET="s3cret-value")
    def test_wrong_or_missing_secret_is_refused(self):
        for url in URLS:
            self.assertIn(self.call(url, HTTP_X_CRON_SECRET="nope").status_code, (401, 403))
            self.assertIn(self.call(url).status_code, (401, 403))

    @override_settings(CRON_SECRET="")
    def test_without_a_configured_secret_an_empty_header_gets_nothing(self):
        self.assertIn(self.call("daily-cycle", HTTP_X_CRON_SECRET="").status_code, (401, 403))

    @override_settings(CRON_SECRET="s3cret-value")
    def test_an_isp_admin_still_cannot_run_the_platform_wide_jobs(self):
        self.as_admin()
        self.assertEqual(self.client.get(f"{API}/tasks/daily-cycle").status_code, 403)

    @override_settings(CRON_SECRET="s3cret-value", BILLING_JOBS_IN_BACKGROUND=True)
    def test_it_is_queued_on_celery_when_possible(self):
        with mock.patch("customer.tasks.daily_billing_cycle.apply_async") as queued:
            res = self.call("daily-cycle", HTTP_X_CRON_SECRET="s3cret-value")
        self.assertEqual((res.status_code, res.data["queued"]), (200, True))
        queued.assert_called_once()
