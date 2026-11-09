"""WhatsApp Cloud API webhook. Always acks 200 after persisting; duplicates ignored via WebhookEvent."""
import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from escrow.models import WebhookEvent
from escrow.services import audit

from .adapter import parse_webhook_payload, verify_signature
from .flow import EMAIL_HINT, handle_text_message

logger = logging.getLogger(__name__)


def _verify_token():
    from django.conf import settings
    return getattr(settings, 'WHATSAPP_WEBHOOK_VERIFY_TOKEN', '')


def webhook_verify(request):
    mode = request.GET.get('hub.mode')
    token = request.GET.get('hub.verify_token')
    challenge = request.GET.get('hub.challenge')
    expected = _verify_token()
    if mode == 'subscribe' and token and expected and token == expected:
        return HttpResponse(challenge or '', content_type='text/plain')
    return HttpResponseForbidden('Verification failed')


@csrf_exempt
def webhook(request):
    if request.method == 'GET':
        return webhook_verify(request)
    if request.method != 'POST':
        return HttpResponseBadRequest('Method not allowed')

    if not verify_signature(request.body, request.headers.get('X-Hub-Signature-256')):
        logger.warning("WhatsApp webhook rejected: bad signature")
        return HttpResponseForbidden('Invalid signature')

    try:
        payload = json.loads(request.body.decode('utf-8') if request.body else '{}')
    except (ValueError, UnicodeDecodeError):
        logger.warning("WhatsApp webhook: malformed JSON payload")
        return HttpResponse('Ignored', status=200)

    messages, statuses = parse_webhook_payload(payload)

    # Delivery/read receipts: record for observability, no reply.
    for status in statuses:
        event_id = status.get('wamid') or f"status-{timezone.now().timestamp()}"
        try:
            record, created = WebhookEvent.objects.get_or_create(
                provider='whatsapp', event=f"status:{status.get('status') or 'unknown'}",
                event_id=event_id[:100],
                defaults={'signature_valid': True, 'payload': status},
            )
            if not record.processed:
                record.processed = True
                record.processed_at = timezone.now()
                record.save(update_fields=['processed', 'processed_at'])
        except Exception:
            logger.exception("WhatsApp status-event record failed")

    base_url = request.build_absolute_uri('/')[:-1]
    for message in messages:
        wamid = message['wamid']
        sender = message['from']
        try:
            record, created = WebhookEvent.objects.get_or_create(
                provider='whatsapp', event=f"message:{message.get('type') or 'unknown'}",
                event_id=wamid[:100],
                defaults={'signature_valid': True, 'payload': message},
            )
        except Exception:
            logger.exception("WhatsApp event record failed for %s", wamid)
            continue
        if not created and record.processed:
            logger.info("Ignoring duplicate WhatsApp event %s", wamid)
            continue

        def mark_processed():
            record.processed = True
            record.processed_at = timezone.now()
            record.save(update_fields=['processed', 'processed_at'])

        try:
            if message.get('type') in ('audio', 'voice'):
                from . import adapter
                from .flow import handle_voice_message
                media_id = message.get('media_id') or ''
                audio_bytes = adapter.download_media(media_id) if media_id else None
                handle_voice_message(sender, audio_bytes, base_url=base_url)
            elif message.get('type') != 'text' or not (message.get('text') or '').strip():
                # Non-text: acknowledge and guide back to text.
                from . import adapter
                from agent.loop import get_or_create_conversation
                conversation = get_or_create_conversation('whatsapp', sender)
                conversation.messages.create(
                    role='user', content=f"[{message.get('type') or 'unsupported'} message]",
                )
                reply = ("I can only read text messages here. "
                         "Please describe the deal in words, or reply with your email to verify.")
                conversation.messages.create(role='assistant', content=reply)
                adapter.send_text_message(sender, reply)
            elif sender and (message.get('text') or '').strip().lower() in ('hi', 'hello', 'hey', 'start'):
                from . import adapter
                from agent.loop import get_or_create_conversation
                from .models import WhatsAppLink
                conversation = get_or_create_conversation('whatsapp', sender)
                link, _ = WhatsAppLink.objects.get_or_create(wa_id=sender)
                if link.verified_email:
                    conversation.owner_email = link.verified_email
                    conversation.save(update_fields=['owner_email'])
                    reply = (f"Welcome back. You're verified as {link.verified_email}. "
                             "What would you like to do?")
                else:
                    reply = EMAIL_HINT
                conversation.messages.create(role='user', content=message['text'])
                conversation.messages.create(role='assistant', content=reply)
                adapter.send_text_message(sender, reply)
            else:
                handle_text_message(sender, message.get('text') or '', base_url=base_url)
        except Exception:
            logger.exception("WhatsApp message processing failed for %s", wamid)
            audit('whatsapp.processing_failed', metadata={'wamid': wamid})
            # Mark processed anyway: retrying the same payload won't help.
        mark_processed()

    return HttpResponse('Received', status=200)
