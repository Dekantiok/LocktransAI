import secrets
import string


def generate_secure_code(length=9):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def generate_escrow_code(suffix_length=5):
    """Escrow codes look like LKTRA1b2C: a LKTR prefix the AI and users
    can spot inside chat, plus random alphanumerics."""
    alphabet = string.ascii_letters + string.digits
    return 'LKTR' + ''.join(secrets.choice(alphabet) for _ in range(suffix_length))


def generate_otp(length=6):
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def normalize_email(email):
    """Canonicalise an email so lookups, session checks and comparisons agree."""
    if not email:
        return ''
    return email.strip().lower()
