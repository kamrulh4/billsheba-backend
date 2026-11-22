"""The gateway says "accepted" inside the body (response_code 202), not with the HTTP status."""

from unittest import mock

from django.test import SimpleTestCase

from common.helpers import sms_helper
from common.helpers.sms_helper import SMS


def reply(status=200, body=None, text=""):
    res = mock.Mock(status_code=status, text=text)
    res.raise_for_status.return_value = None
    if body is None:
        res.json.side_effect = ValueError("not json")
    else:
        res.json.return_value = body
    return res


class GatewayReplyTests(SimpleTestCase):
    def send(self, res, bulk=False):
        with mock.patch.object(sms_helper, "SMS_API_KEY", "key"), mock.patch(
            "common.helpers.sms_helper.requests.post", return_value=res
        ):
            return SMS.send_bulk_sms([{"to": "8801700000000", "message": "x"}]) if bulk else SMS.send_single_sms("8801700000000", "x")

    def test_http_200_with_response_code_202_is_a_success(self):
        ok = reply(200, {"response_code": 202, "message_id": 1, "success_message": "SMS Submitted Successfully 1"})
        self.assertTrue(self.send(ok))
        self.assertTrue(self.send(ok, bulk=True))

    def test_a_refusal_inside_a_200_reply_is_a_failure(self):
        for code in (1005, 1007, 1011):  # invalid number / no balance / user not found
            refused = reply(200, {"response_code": code, "error_message": "no"})
            self.assertFalse(self.send(refused), code)
            self.assertFalse(self.send(refused, bulk=True), code)

    def test_a_reply_without_response_code_falls_back_to_the_http_status(self):
        self.assertTrue(self.send(reply(202)))  # the old single-SMS rule still works
        self.assertFalse(self.send(reply(200)))
        self.assertTrue(self.send(reply(200), bulk=True))  # bulk accepted plain 200 or 202 before
        self.assertTrue(self.send(reply(202, {"status": "ok"}), bulk=True))
        self.assertFalse(self.send(reply(500), bulk=True))

    def test_without_an_api_key_nothing_is_sent(self):
        with mock.patch("common.helpers.sms_helper.requests.post") as post:
            self.assertFalse(SMS.send_single_sms("8801700000000", "x"))
        post.assert_not_called()
