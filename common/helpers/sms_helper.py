"""
Docstring for common.helpers.sms_helper
"""

import json

import requests
from django.conf import settings

SMS_URL: str = settings.SMS_URL
SMS_API_KEY: str = settings.SMS_API_KEY
SMS_SENDER_ID: str = settings.SMS_SENDER_ID


def _accepted(response, plain_ok=(202,)) -> bool:
    """Did the gateway accept the message(s)?

    BulkSMSBD answers HTTP 200 with ``{"response_code": 202, "success_message": "SMS Submitted Successfully"}``;
    any other ``response_code`` (invalid number, no balance, bad key...) is a refusal even though the HTTP
    status is 200. A reply with no ``response_code`` counts when its HTTP status is in ``plain_ok``.
    """
    try:
        code = response.json().get("response_code")
    except (ValueError, AttributeError):
        code = None
    if code is not None:
        try:
            return int(code) == 202
        except (TypeError, ValueError):
            return False
    return response.status_code in plain_ok


class SMS:
    """SMS helper class"""

    @staticmethod
    def send_single_sms(to: str, message: str) -> bool:
        """Send SMS using BulkSMSBD API."""
        if not SMS_API_KEY:  # not configured (dev / tests): never touch the network
            print("SMS skipped: SMS_API_KEY is not set")
            return False
        body = {
            "api_key": SMS_API_KEY,
            "senderid": SMS_SENDER_ID,
            "number": to,
            "message": message,
        }
        url: str = SMS_URL + "/smsapi"
        try:
            response = requests.post(url, data=body, timeout=60)
            response.raise_for_status()
            if not _accepted(response):
                print(f"Failed to send SMS, {response.text}")
                return False
            return True

        except requests.RequestException as e:
            # Log the error
            print(f"Error sending SMS: {e}")
            return False

    @staticmethod
    def send_bulk_sms(messages) -> bool:
        """
        Docstring for send_bulk_sms

        :param messages: Description
        :return: Description
        :rtype: bool
        """
        if not SMS_API_KEY:
            print("SMS skipped: SMS_API_KEY is not set")
            return False
        body = {
            "api_key": SMS_API_KEY,
            "senderid": SMS_SENDER_ID,
            "messages": json.dumps(messages),
        }

        url: str = SMS_URL + "/smsapimany"
        try:
            response = requests.post(url, data=body, timeout=60)
            response.raise_for_status()
            if not _accepted(response, plain_ok=(200, 202)):
                print(f"Failed to send SMS, {response.text}")
                return False
            return True

        except requests.RequestException as e:
            # Log the error
            print(f"Error sending SMS: {e}")
            return False


# {
#   type : "post",
#   url : "http://bulksmsbd.net/api/smsapimany",
#   data : {
#     "api_key" : "your api key",
#     "senderid" : "sender id",
#     "messages" :
#             [
#                   {
#                     "to" : "88016xxxxxxxx",
#                     "message" : "SMS text 1"
#                   },
#                   {
#                     "to" : "88019xxxxxxxx",
#                     "message" : "SMS Text 2"
#                   }
#             ]
#   }
# }
