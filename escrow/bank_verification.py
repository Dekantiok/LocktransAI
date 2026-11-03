"""Payout destination verification for PayPal email vaults.

Payouts go to a PayPal email; no bank lookup is needed. This module keeps
the historic helper names as thin email validators so older imports keep
working, but all verification is local email checks.
"""
import logging

from escrow.utils import normalize_email

logger = logging.getLogger(__name__)


def verify_paypal_email(email):
    email = normalize_email(email or '')
    if not email or '@' not in email or '.' not in email or ' ' in email:
        return {'status': False, 'error': 'Enter a valid PayPal email address.'}
    return {'status': True, 'account_name': email}


# Historic names kept for compatibility; all route to email checks.
def verify_bank_account(account_number, bank_code=None):
    del bank_code
    candidate = (account_number or '').strip()
    if '@' in candidate:
        return verify_paypal_email(candidate)
    return {'status': False, 'error': 'Enter a valid PayPal email address.'}


def list_banks():
    return {}


def get_bank_name(bank_code):
    del bank_code
    return 'PayPal'
