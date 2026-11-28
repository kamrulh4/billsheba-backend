"""Helper class to communicate with Mikrotik Router."""

import requests
from django.conf import settings

# Load from Django settings
MIKROTIK_URL = settings.MIKROTIK_URL
MIKROTIK_USER = settings.MIKROTIK_USER
MIKROTIK_PASS = settings.MIKROTIK_PASS
# Routers commonly use self-signed certificates, so verification defaults to off;
# set MIKROTIK_VERIFY_SSL=True when the router has a real certificate.
MIKROTIK_VERIFY_SSL = getattr(settings, "MIKROTIK_VERIFY_SSL", False)
REQUEST_TIMEOUT = getattr(settings, "MIKROTIK_REQUEST_TIMEOUT", 15)  # seconds
USER_NOT_FOUND = "User not found in PPP secrets"


class Mikrotik:
    """
    Utility class to interact with MikroTik router via REST API.
    Supports: create, read, update, disable, delete PPP users and manage active sessions.
    """

    @staticmethod
    def get_user_by_username(username, organization=None):
        """
        Fetch a PPP secret by username.
        :param username: str
        :return: tuple(success: bool, data_or_error: dict/str)
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, "Router configuration is not added yet"

        if not username:
            return False, "Username is required"

        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            query_url = f"{router_ip}/rest/ppp/secret/print"
            response = requests.post(
                query_url,
                json={".query": [f"name={username}"]},
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                return False, f"HTTP {response.status_code}: {response.text}"

            data = response.json()
            if not data:
                return False, USER_NOT_FOUND

            return True, data[0]

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def get_users_from_server(organization=None):
        """
        Fetch all PPP users (secrets).
        :return: tuple(success: bool, users_list_or_error: list/str)
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, "Router configuration is not added yet"
        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            url = f"{router_ip}/rest/ppp/secret"
            response = requests.get(
                url,
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                return True, response.json()
            else:
                return False, f"HTTP {response.status_code}: {response.text}"

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def get_profiles(organization=None):
        """
        Fetch all PPP profiles (these become packages).
        :return: tuple(success: bool, profiles_list_or_error: list/str)
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            return False, "Router configuration is not added yet"
        try:
            response = requests.get(
                f"{organization.get_router_ip()}/rest/ppp/profile",
                auth=(organization.get_router_username(), organization.get_router_password()),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                return True, response.json()
            return False, f"HTTP {response.status_code}: {response.text}"
        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def get_active_names(organization, timeout=5):
        """
        Usernames of every connected PPPoE session, in one cheap call.
        ``.proplist`` makes the router return only the fields we use, so the response stays small
        even with thousands of sessions.
        :return: tuple(success: bool, {username: {"address", "uptime"}} or error str)
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            return False, "Router configuration is not added yet"
        try:
            response = requests.get(
                f"{organization.get_router_ip()}/rest/ppp/active",
                params={".proplist": "name,address,uptime"},
                auth=(organization.get_router_username(), organization.get_router_password()),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=timeout,
            )
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            return True, {
                row["name"]: {"address": row.get("address", ""), "uptime": row.get("uptime", "")}
                for row in response.json()
                if row.get("name")
            }
        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def get_user_sessions(organization):
        """
        Get all active PPP sessions.
        :return: tuple(success: bool, sessions_list_or_error: list/str)
        """
        if (
            not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, []
        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            url = f"{router_ip}/rest/ppp/active"
            response = requests.get(
                url,
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )
            # print("RRRRR: ", response)
            if response.status_code == 200:
                return True, response.json()

            return False, []

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def delete_user_session(session_id, organization=None):
        """
        Terminate an active PPP session by ID.
        :param session_id: str (.id from session)
        :param organization: Organization instance with router credentials
        :return: bool
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False
        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            url = f"{router_ip}/rest/ppp/active/{session_id}"
            response = requests.delete(
                url,
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in [200, 204]:
                return True
            else:
                print(
                    f"[Mikrotik] Failed to delete session {session_id}: {response.text}"
                )
                return False

        except Exception as e:
            print(f"[Mikrotik] Exception deleting session {session_id}: {str(e)}")
            return False

    @staticmethod
    def toggle_ppp_user(username, disable=True, organization=None, session_id=None):
        """
        Enable or disable a PPP user.
        If disabling, also terminate active session.
        :param username: str
        :param disable: bool (True to disable, False to enable)
        :return: tuple(success: bool, message: str)
        """
        if not username:
            return False, "Username is required"

        if (
            not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, "Router configuration is not added yet"

        try:
            # Step 1: Get user
            success, user = Mikrotik.get_user_by_username(username, organization)
            if not success:
                return False, f"User not found: {user}"

            secret_id = user[".id"]
            disabled_str = "true" if disable else "false"

            # Step 2: Update disabled status
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            patch_url = f"{router_ip}/rest/ppp/secret/{secret_id}"
            response = requests.patch(
                patch_url,
                json={"disabled": disabled_str},
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                try:
                    error_msg = response.json().get("message", response.text)
                except Exception as _:
                    error_msg = response.text
                return False, f"Update failed: {error_msg}"

            # Step 3: If disabling, terminate active session
            if disable and session_id:
                if Mikrotik.delete_user_session(session_id, organization):
                    print(f"[Mikrotik] Terminated session for '{username}'")
                else:
                    print(
                        f"[Mikrotik] Warning: Could not terminate session for '{username}'"
                    )
            elif disable:
                success, sessions = Mikrotik.get_user_sessions(organization)
                if success:
                    for session in sessions:
                        if session.get("name") == username:
                            session_id = session[".id"]
                            if Mikrotik.delete_user_session(session_id, organization):
                                print(f"[Mikrotik] Terminated session for '{username}'")
                            else:
                                print(
                                    f"[Mikrotik] Warning: Could not terminate session for '{username}'"
                                )
                    # print("sessions not available for the user")
                else:
                    print(
                        f"[Mikrotik] Warning: Could not fetch active sessions: {sessions}"
                    )

            action = "disabled" if disable else "enabled"
            return True, f"User '{username}' {action} successfully"

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def create_ppp_user(user, organization=None):
        """
        Docstring for create_ppp_user

        :param user: Description
        :param organization: Description
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, "Router configuration is not added yet"

        username = user.get("username", "")
        password = user.get("password", "")
        if not username or not password:
            return False, "Username and password are required"

        payload = {
            "name": username,
            "password": password,
            "service": user.get("service", "pppoe"),  # Default to pppoe
            "profile": user.get("profile", "default"),  # Default profile
            "disabled": "false",
            "comment": user.get("comment", "Created via API"),
        }
        # print("Payload to create PPP user: ", payload)
        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            url = f"{router_ip}/rest/ppp/secret"
            response = requests.put(  # MikroTik uses PUT to CREATE
                url,
                json=payload,
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )
            # print("RRRRR: ", response)
            if response.status_code == 201:
                return True, "PPP user created successfully"
            else:
                try:
                    # print("Response JSON: ", response.json())
                    response = response.json()
                    return (
                        False,
                        f"{response.get('detail', 'Failed to create user')}",
                    )

                except Exception as _:
                    # print("Response Text: ", response.text)
                    return False, f"Create failed: {response.text}"

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    @staticmethod
    def update_ppp_user(username, changes, organization=None):
        """
        Change fields (name / password / profile) of an existing PPP secret.
        :return: tuple(found: bool, error: str | None). ``(False, None)`` means the router answered
            and simply has no such user; ``error`` set means nothing was changed.
        """
        if not organization or not organization.router_configured:
            return False, "Router configuration is not added yet"

        ok, secret = Mikrotik.get_user_by_username(username, organization)
        if not ok:
            if secret == USER_NOT_FOUND:
                return False, None
            return False, str(secret)

        try:
            response = requests.patch(
                f"{organization.get_router_ip()}/rest/ppp/secret/{secret['.id']}",
                json=changes,
                auth=(organization.get_router_username(), organization.get_router_password()),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                try:
                    body = response.json()
                    error = body.get("detail") or body.get("message") or response.text
                except Exception:
                    error = response.text
                return True, str(error)
            return True, None

        except requests.exceptions.Timeout:
            return True, "Request timed out"
        except requests.exceptions.RequestException as e:
            return True, f"Network error: {str(e)}"
        except Exception as e:
            return True, f"Unexpected error: {str(e)}"

    @staticmethod
    def disconnect_ppp_user(username, organization):
        """
        Drop the user's live session so the router applies the new settings when it reconnects.
        :return: "done", "offline" (nothing to drop) or "failed"
        """
        ok, sessions = Mikrotik.get_user_sessions(organization)
        if not ok:
            return "failed"
        mine = [s for s in sessions if s.get("name") == username]
        if not mine:
            return "offline"
        dropped = [Mikrotik.delete_user_session(s[".id"], organization) for s in mine]
        return "done" if all(dropped) else "failed"

    @staticmethod
    def delete_ppp_user(username, organization=None):
        """
        Delete a PPP user (ppp secret) by username.
        :param username: str
        :return: tuple(success: bool, message: str)
        """
        if (
            not organization
            or not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            print("Router configuration is not added yet")
            return False, "Router configuration is not added yet"

        success, user = Mikrotik.get_user_by_username(username, organization)
        if not success:
            return False, f"User not found: {user}"

        try:
            # Get decrypted credentials
            router_ip = organization.get_router_ip()
            router_user = organization.get_router_username()
            router_pass = organization.get_router_password()
            
            url = f"{router_ip}/rest/ppp/secret/{user['.id']}"
            response = requests.delete(
                url,
                auth=(router_user, router_pass),
                verify=MIKROTIK_VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 204:
                return False, f"Failed to delete user: {response.text}"
            return True, "User deleted successfully"

        except requests.exceptions.Timeout:
            return False, "Request timed out"
        except requests.exceptions.RequestException as e:
            return False, f"Network error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"
