"""WhatsApp Cloud API adapter.

Provider-shaped code only: signatures, payload parsing, delivery. No escrow logic here -
the views hand normalised messages to ``flow.py``, which drives the shared
agent loop and backend tools.
"""
import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def verify_signature(raw_body, signature_header):
    """Validate ``X-Hub-Signature-256`` against the app secret.

    Returns True when valid. When no app secret is configured (local dev),
    validation is skipped with a warning so flows can be exercised without
    Meta credentials, production must always set WHATSAPP_APP_SECRET.
    """
    secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
    if not secret:
        if getattr(settings, 'DEBUG', False):
            logger.warning("WHATSAPP_APP_SECRET unset, skipping check in DEBUG")
            return True
        logger.error("WHATSAPP_APP_SECRET unset, rejecting webhook")
        return False
    if not signature_header or not signature_header.startswith('sha256='):
        return False
    expected = hmac.new(secret.encode(), raw_body or b'', hashlib.sha256).hexdigest()
    return hmac.compare_digest('sha256=' + expected, signature_header)


def parse_webhook_payload(payload):
    """Normalise a Cloud API webhook payload.

    Returns ``(messages, statuses)`` where each message is
    ``{'wamid': str, 'from': str, 'type': str, 'text': str}`` and each
    status is ``{'wamid': str, 'status': str, 'recipient': str}``.
    Malformed payloads yield empty lists, never an exception.
    """
    messages, statuses = [], []
    if not isinstance(payload, dict):
        return messages, statuses
    for entry in payload.get('entry') or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get('changes') or []:
            value = (change or {}).get('value') if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            for raw in value.get('messages') or []:
                if not isinstance(raw, dict):
                    continue
                wamid = raw.get('id') or ''
                sender = raw.get('from') or ''
                msg_type = raw.get('type') or ''
                text = ''
                media_id = ''
                if msg_type == 'text':
                    text_obj = raw.get('text') or {}
                    text = (text_obj.get('body') or '') if isinstance(text_obj, dict) else ''
                elif msg_type in ('audio', 'voice'):
                    media_obj = raw.get(msg_type) or {}
                    media_id = (media_obj.get('id') or '') if isinstance(media_obj, dict) else ''
                if wamid and sender:
                    messages.append({
                        'wamid': wamid, 'from': sender,
                        'type': msg_type, 'text': text or '',
                        'media_id': media_id,
                    })
            for raw in value.get('statuses') or []:
                if not isinstance(raw, dict):
                    continue
                statuses.append({
                    'wamid': raw.get('id') or '',
                    'status': raw.get('status') or '',
                    'recipient': raw.get('recipient_id') or '',
                })
    return messages, statuses


def send_text_message(to_wa_id, body):
    """Deliver one text reply via the Cloud API. Never raises.

    Returns True on accepted delivery, False otherwise (unknown numbers,
    expired windows, bad credentials). Callers log the failure; the
    webhook itself still acks 200 so Meta stops retrying.
    """
    token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
    phone_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    api_version = getattr(settings, 'WHATSAPP_API_VERSION', 'v21.0')
    if not token or not phone_id:
        logger.error("WhatsApp outbound not configured, dropping reply to %s", to_wa_id)
        return False
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"
    try:
        response = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={
                'messaging_product': 'whatsapp',
                'to': to_wa_id,
                'type': 'text',
                'text': {'body': (body or '')[:4000]},
            },
            timeout=getattr(settings, 'GATEWAY_HTTP_TIMEOUT', 15),
        )
    except Exception:
        logger.exception("WhatsApp send raised for %s", to_wa_id)
        return False
    if response.status_code not in (200, 201):
        logger.warning(
            "WhatsApp send failed for %s: %s %s",
            to_wa_id, response.status_code, response.text[:500],
        )
        return False
    return True


def _media_host_ok(url):
    """Only fetch media from Meta's graph/CDN hosts."""
    from urllib.parse import urlparse
    host = (urlparse(url or '').hostname or '').lower()
    return host == 'graph.facebook.com' or host.endswith('.fbcdn.net')


def download_media(media_id):
    """Download a voice note / audio attachment. Returns bytes or None.

    Streams with a byte cap so a hostile media id cannot fill memory, and
    only fetches from Meta hosts. Never raises: failures return None and
    the caller guides the user back to text or a shorter recording.
    """
    token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
    api_version = getattr(settings, 'WHATSAPP_API_VERSION', 'v21.0')
    if not token or not media_id:
        return None
    timeout = getattr(settings, 'GATEWAY_HTTP_TIMEOUT', 15)
    max_bytes = getattr(settings, 'WHATSAPP_MEDIA_MAX_BYTES', 8 * 1024 * 1024)
    try:
        meta = requests.get(
            f"https://graph.facebook.com/{api_version}/{media_id}",
            headers={'Authorization': f'Bearer {token}'},
            timeout=timeout,
        )
        if meta.status_code != 200:
            logger.warning("WhatsApp media lookup failed: %s", meta.status_code)
            return None
        url = (meta.json() or {}).get('url') or ''
        if not url or not _media_host_ok(url):
            logger.warning("WhatsApp media URL rejected for %s", media_id)
            return None
        blob = requests.get(
            url, headers={'Authorization': f'Bearer {token}'},
            timeout=timeout + 15, stream=True,
        )
        if blob.status_code != 200:
            logger.warning("WhatsApp media download failed: %s", blob.status_code)
            return None
        chunks = []
        total = 0
        for piece in blob.iter_content(chunk_size=64 * 1024):
            if not piece:
                continue
            total += len(piece)
            if total > max_bytes:
                logger.warning("WhatsApp media over cap (%s)", media_id)
                return None
            chunks.append(piece)
        content = b''.join(chunks)
        if not content:
            logger.warning("WhatsApp media download empty for %s", media_id)
            return None
    except Exception:
        logger.exception("WhatsApp media download raised for %s", media_id)
        return None
    return content
