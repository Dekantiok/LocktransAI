import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


def send_otp_via_termii(email, otp_code):
    if getattr(settings, 'DEMO_MODE', False):
        logger.warning("DEMO MODE: OTP for %s not emailed (demo code applies)", email)
        return True

    if settings.DEBUG and not getattr(settings, 'TERMII_SEND_IN_DEBUG', False):
        # Local mock: pretend delivery succeeded so flows can be exercised
        # without spending Termii credit. Nothing is emailed here, set
        # TERMII_SEND_IN_DEBUG=True (with LOG_OTP_TO_CONSOLE as needed) for
        # live local testing.
        if getattr(settings, 'LOG_OTP_TO_CONSOLE', False):
            logger.warning("DEVELOPMENT OTP for %s: %s", email, otp_code)
        return True

    api_key = getattr(settings, 'TERMII_API_KEY', 'dummy_key')
    email_config_id = getattr(settings, 'TERMII_EMAIL_CONFIG_ID', 'default_config')

    url = "https://api.ng.termii.com/api/email/otp/send"

    # Email Token API: `code` is REQUIRED, it is the exact OTP the user
    # receives. The pin_* / message_text fields belong to the SMS Token API
    # and are ignored here; omitting `code` was why production emails never
    # matched the code stored in AuthOTP.
    payload = {
        "api_key": api_key,
        "email_address": email,
        "code": otp_code,
        "email_configuration_id": email_config_id,
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    if api_key in ('', 'dummy_key') or email_config_id in ('', 'default_config'):
        logger.error(
            "Termii OTP not attempted for %s: TERMII_API_KEY/TERMII_EMAIL_CONFIG_ID "
            "look unset in this environment.", email,
        )
        return False

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=getattr(settings, 'GATEWAY_HTTP_TIMEOUT', DEFAULT_TIMEOUT),
        )
        response_data = response.json()
        if response_data.get("code") != "ok":
            logger.error(
                "Termii rejected OTP for %s: %s (response: %r)",
                email, response_data.get("message"), response_data,
            )
        return response_data.get("code") == "ok"
    except Exception:
        logger.exception("Termii OTP delivery failed for %s", email)
        return False