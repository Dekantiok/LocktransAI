"""Lifecycle notifications: tell each party what just happened.

Hooks sit beside the state transitions in views, services, and agent
tools. Best-effort WhatsApp push to the verified sender; anything
unconfigured or failing degrades silently and never breaks the money move.
"""
import logging

logger = logging.getLogger(__name__)


def _recipients(event, contract):
    buyer, seller = contract.buyer_email, contract.seller_email
    return {
        'counterparty_joined': [contract.creator_email],
        'funding_confirmed': [seller],
        'dispatch_confirmed': [buyer],
        'receipt_confirmed': [buyer, seller],
        'bank_details_needed': [seller],
        'payout_completed': [seller, buyer],
        'payout_failed': [seller, buyer],
        'disputed': [buyer, seller],
    }.get(event, [])


def _copy(event, contract):
    code = contract.code
    if event == 'counterparty_joined':
        return (f"Update on escrow {code}: the counterparty has joined "
                "and verified. It is now awaiting funding, the buyer "
                "can pay next.")
    if event == 'bank_details_needed':
        return (f"Action needed on escrow {code}: the buyer is ready to "
                "release funds, but your PayPal payout email is missing. "
                "Reply here with your PayPal email and I'll save it.")
    if event == 'funding_confirmed':
        return (f"Escrow {code} is funded ({contract.total_charge} "
                f"{contract.currency}). Please dispatch, then tell me "
                "'dispatched' here.")
    if event == 'dispatch_confirmed':
        return (f"Your order in escrow {code} is on its way. When it "
                "arrives, tell me 'received' to start inspection.")
    if event == 'receipt_confirmed':
        ends = (contract.inspection_ends.strftime('%d %b, %H:%M')
                if contract.inspection_ends else 'the deadline')
        return (f"Receipt confirmed for {code}, inspection ends {ends}. "
                "Say 'release' to pay the seller or 'dispute' if "
                "something is wrong.")
    if event == 'payout_completed':
        return f"Escrow {code} is complete, the seller has been paid."
    if event == 'payout_failed':
        return (f"Update on escrow {code}: the payout transfer did not "
                "complete. Funds remain held while our team reconciles "
                "it. No action is needed from you right now.")
    if event == 'disputed':
        return (f"A dispute was raised on escrow {code}, funds are "
                "frozen until it is resolved.")
    return ''


def whatsapp_numbers_for(email):
    """Sender ids verified to this email. Empty when none or unconfigured."""
    from django.conf import settings
    if not getattr(settings, 'WHATSAPP_ACCESS_TOKEN', ''):
        return []
    from whatsapp.models import WhatsAppLink
    return list(WhatsAppLink.objects.filter(
        verified_email=email).values_list('wa_id', flat=True))


def _append_web_message(email, text):
    """Drop the update into the recipient's latest webchat, if any.

    Open chat pages poll and swap it in live; it also greets them if
    they return before any refresh. The actor's own chat is skipped by
    the caller, they already saw the result of their action.
    """
    from agent.models import AIConversation
    conversation = (AIConversation.objects
                    .filter(channel='web', owner_email=email)
                    .order_by('-updated_at', '-id').first())
    if conversation is None:
        return False
    recent = conversation.messages.filter(
        role='assistant').order_by('-id')[:5]
    if any(row.content == text for row in recent):
        return False
    conversation.messages.create(role='assistant', content=text)
    return True


def notify_event(event, contract, actor=None):
    """Notify recipients via WhatsApp push + their webchat. Never raises."""
    text = _copy(event, contract)
    if not text:
        return 0
    from whatsapp.adapter import send_text_message
    sent = 0
    for email in _recipients(event, contract):
        try:
            for wa_id in whatsapp_numbers_for(email):
                if send_text_message(wa_id, text):
                    sent += 1
        except Exception:
            logger.exception("Lifecycle notify failed for %s", email)
        if email != actor:
            try:
                if _append_web_message(email, text):
                    sent += 1
            except Exception:
                logger.exception("Webchat notify failed for %s", email)
    if sent:
        logger.info("Lifecycle %s for %s pushed to %d chat(s)",
                    event, contract.code, sent)
    return sent
