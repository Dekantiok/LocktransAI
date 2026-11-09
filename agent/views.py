"""Website AI assistant over the shared agent loop.
Bare 6-digit messages are OTPs, never model input; confirming a draft emails a code that resumes creation here.
"""
import logging

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from agent.loop import (
    OTP_MESSAGE_RE,
    _proposal_fresh,
    clear_proposal,
    find_pending_join_code,
    get_or_create_conversation,
    restart_thread,
    run_turn,
)
from escrow.forms import ContractCreationForm
from escrow.invitations import send_counterparty_invitation
from escrow.models import EscrowDraft, TransitionError
from escrow.services import (
    audit,
    calculate_service_fee,
    consume_otp,
    create_escrow_from_validated,
    escrow_next_step,
    issue_otp,
    resume_escrow_after_verification,
)
from escrow.utils import normalize_email
from voice.transcriber import (
    VoiceConfigError,
    VoiceTranscriptionError,
    transcribe_audio,
)

logger = logging.getLogger(__name__)


def _conversation(request):
    if not request.session.session_key:
        request.session.save()
    conversation = get_or_create_conversation(
        'web', f"session:{request.session.session_key}"
    )
    verified = normalize_email(request.session.get('chat_verified_email', ''))
    if verified and not conversation.owner_email:
        conversation.owner_email = verified
        conversation.save(update_fields=['owner_email'])
    elif conversation.owner_email and conversation.owner_email != verified:
        # Follow the session: server-side proof of who sits at this browser.
        conversation.owner_email = verified or None
        conversation.save(update_fields=['owner_email'])
    return conversation


def _pending_terms(pending):
    """Server-computed fee breakdown for a pending creation proposal."""
    pending = pending or {}
    if pending.get('tool') != 'request_escrow_creation':
        return None
    draft_id = (pending.get('arguments') or {}).get('draft_id') or pending.get('draft_id')
    try:
        draft = EscrowDraft.objects.filter(id=draft_id).first()
        if draft is None:
            return None
        form = ContractCreationForm({
            'title': draft.title,
            'description': draft.description,
            'amount': draft.amount,
            'fee_payer': draft.fee_payer,
            'creator_email': draft.owner_email or 'you@example.com',
            'creator_role': draft.creator_role,
            'counterparty_email': draft.counterparty_email,
            'inspection_days': draft.inspection_days,
        })
        if not form.is_valid():
            return None
        cleaned = form.cleaned_data
        fee = calculate_service_fee(cleaned['amount'])
        amount = cleaned['amount']
        if cleaned['fee_payer'] == 'buyer':
            buyer_pays, seller_gets = amount + fee, amount
        else:
            buyer_pays, seller_gets = amount, amount - fee
        money = lambda v: f"${v:,.2f}"  # noqa: E731
        return {
            'amount': money(amount),
            'service_fee': money(fee),
            'fee_payer': cleaned['fee_payer'],
            'buyer_pays': money(buyer_pays),
            'seller_receives': money(seller_gets),
        }
    except Exception:
        logger.exception("Could not build pending terms")
        return None


def _thread_context(conversation, request):
    pending = conversation.pending_action or {}
    # Drop stale proposals so refresh starts clean.
    if pending.get('tool') and not _proposal_fresh(pending):
        clear_proposal(conversation)
        conversation.refresh_from_db()
        pending = {}
    messages = list(conversation.messages.order_by('id'))
    return {
        'messages': messages,
        'pending': pending if pending.get('tool') else None,
        'pending_terms': _pending_terms(pending) if pending.get('tool') else None,
        'owner_email': conversation.owner_email,
        'pending_email': request.session.get('chat_pending_email', ''),
        'last_message_id': messages[-1].id if messages else 0,
    }


def _complete_pending_join(conversation, email):
    """Finish a join waiting on verification; always consumes stored context."""
    code = find_pending_join_code(conversation)
    if conversation.pending_context:
        conversation.pending_context = {}
        conversation.save(update_fields=['pending_context'])
    if not code:
        return None
    contract, kind, error = resume_escrow_after_verification(code, email)
    if contract is None:
        return f"You're verified, but I can't continue that escrow: {error}"
    role = 'buyer' if email == contract.buyer_email else 'seller'
    audit(f'counterparty.{kind}_via_chat', actor=email, escrow=contract)
    if kind == 'joined':
        return (f"Verified and joined, escrow {contract.code} "
                f"({contract.title}, {contract.amount} {contract.currency}). "
                + escrow_next_step(contract, email))
    return (f"Verified, welcome back. You're the {role} on escrow "
            f"{contract.code} ({contract.title}, {contract.amount} "
            f"{contract.currency}), currently {contract.status}. "
            + escrow_next_step(contract, email))


def _is_discard_command(text):
    """A typed 'discard it' drops the awaiting proposal, no model needed."""
    import re
    return bool(re.match(
        r'^\s*(discard( (it|that|this))?|disregard( (it|that|this))?|'
        r'forget it|never mind)\s*[.!]*\s*$',
        (text or '').strip(), re.IGNORECASE))


def chat_page(request):
    conversation = _conversation(request)
    # Refresh always restarts: drop the verified identity too.
    request.session.pop('chat_verified_email', None)
    request.session.pop('chat_pending_email', None)
    restart_thread(conversation)
    context = _thread_context(conversation, request)
    return render(request, 'agent/chat.html', context)


@require_POST
def chat_verify(request):
    """Start or complete email verification for this chat session."""
    conversation = _conversation(request)
    email = normalize_email(request.POST.get('email', ''))
    code = (request.POST.get('code') or '').strip()

    if code:
        pending_email = request.session.get('chat_pending_email', '')
        if not pending_email:
            return render(
                request, 'agent/_thread.html',
                _thread_context(conversation, request),
            )
        result = consume_otp(pending_email, code, 'reauth')
        if result.ok:
            request.session['chat_verified_email'] = pending_email
            del request.session['chat_pending_email']
            conversation = _conversation(request)
            audit('chat.verified', actor=pending_email)
        else:
            conversation.messages.create(
                role='assistant',
                content=f"That code did not work: {result.message}",
            )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if not email:
        conversation.messages.create(
            role='assistant',
            content="Please share the email address you want to verify.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if issue_otp(email, 'reauth') is None:
        conversation.messages.create(
            role='assistant',
            content=("I could not send a code to that address right now. "
                     "Please try again in a minute."),
        )
    else:
        request.session['chat_pending_email'] = email
        conversation.messages.create(
            role='assistant',
            content=(f"I sent a 6-digit code to {email}. "
                     "Type it here or enter it below."),
        )
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


def _pending_creation(pending):
    """Return (email, draft_id) when the pending action awaits a creation OTP."""
    pending = pending or {}
    email = pending.get('verification_email') or (
        pending.get('email') if pending.get('verification_required') else ''
    )
    email = normalize_email(email or '')
    draft_id = pending.get('draft_id') or (pending.get('arguments') or {}).get('draft_id')
    if pending.get('tool') == 'request_escrow_creation' and email and draft_id:
        return email, draft_id
    return None, None


def _try_resume_creation_with_code(request, conversation, text):
    """Consume a pasted creation OTP; plaintext code is never stored or sent to the model."""
    pending = conversation.pending_action or {}
    email, draft_id = _pending_creation(pending)
    if not email:
        return None
    code = text.strip()
    result = consume_otp(email, code, 'create')
    conversation.messages.create(role='user', content='[one-time code withheld]')
    if not result.ok:
        conversation.messages.create(
            role='assistant', content=result.message,
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    try:
        draft = EscrowDraft.objects.get(id=draft_id)
    except (EscrowDraft.DoesNotExist, ValueError, TypeError):
        conversation.messages.create(
            role='assistant',
            content="That draft is no longer available. Tell me the deal terms and we'll set it up again.",
        )
        conversation.pending_action = {}
        conversation.save(update_fields=['pending_action'])
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    session_key = request.session.session_key or ''
    if not (
        (draft.owner_email and draft.owner_email == email)
        or (draft.owner_session and draft.owner_session == session_key)
    ):
        conversation.messages.create(
            role='assistant',
            content="That code does not match this chat session. Please confirm the draft again.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    form = ContractCreationForm({
        'title': draft.title,
        'description': draft.description,
        'amount': draft.amount,
        'fee_payer': draft.fee_payer,
        'creator_email': email,
        'creator_role': draft.creator_role,
        'counterparty_email': draft.counterparty_email,
        'inspection_days': draft.inspection_days,
    })
    if not form.is_valid():
        conversation.messages.create(
            role='assistant',
            content=("The draft is missing something, "
                     "tell me the full deal terms and we'll fix it together."),
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    contract = create_escrow_from_validated(form.cleaned_data)
    draft.status = 'converted'
    draft.save(update_fields=['status'])
    # Code proof matches the manual /verify/ page: advance past pending_verification so the counterparty can join.
    confirmed_fields = {}
    if email == contract.buyer_email:
        confirmed_fields['buyer_confirmed'] = True
    elif email == contract.seller_email:
        confirmed_fields['seller_confirmed'] = True
    try:
        contract.transition_to('awaiting_counterparty', **confirmed_fields)
    except TransitionError:
        logger.warning("Created %s but could not advance it", contract.code)
    else:
        send_counterparty_invitation(
            contract, base_url=request.build_absolute_uri('/')[:-1])
    conversation.owner_email = email
    conversation.pending_action = {}
    conversation.save(update_fields=['owner_email', 'pending_action'])
    request.session['chat_verified_email'] = email
    audit('escrow.created_via_agent', actor=email, escrow=contract)
    conversation.messages.create(
        role='assistant',
        content=(
            f"Verified, your escrow {contract.code} is created. "
            "Share the code with the other party so they can join and verify; "
            "I'll help track funding and delivery from here."
        ),
    )
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_POST
def chat_send(request):
    conversation = _conversation(request)
    text = (request.POST.get('message') or '').strip()
    if not text:
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if _is_discard_command(text) and (conversation.pending_action or {}).get('tool'):
        clear_proposal(conversation)
        conversation.messages.create(role='user', content=text)
        conversation.messages.create(
            role='assistant',
            content="Discarded, nothing was done. Tell me what you would like instead.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    if OTP_MESSAGE_RE.match(text):
        resumed = _try_resume_creation_with_code(
            request, conversation, text)
        if resumed is not None:
            return resumed

    agent_pending = normalize_email(conversation.pending_email or '')
    if agent_pending and OTP_MESSAGE_RE.match(text):
        # Code the agent requested via request_verification_code: consume here so joining finishes in chat.
        result = consume_otp(agent_pending, text, 'reauth')
        conversation.messages.create(role='user', content='[one-time code withheld]')
        if result.ok:
            request.session['chat_verified_email'] = agent_pending
            request.session.pop('chat_pending_email', None)
            conversation.owner_email = agent_pending
            conversation.pending_email = None
            conversation.save(update_fields=['owner_email', 'pending_email'])
            join_reply = _complete_pending_join(conversation, agent_pending)
            audit('chat.verified', actor=agent_pending)
            if join_reply:
                conversation.messages.create(
                    role='assistant', content=join_reply)
                return render(
                    request, 'agent/_thread.html',
                    _thread_context(conversation, request),
                )
            conversation.messages.create(
                role='assistant',
                content=(f"Verified, I can now help with escrows for "
                         f"{agent_pending}. What would you like to do?"),
            )
        else:
            conversation.messages.create(
                role='assistant', content=result.message,
            )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    pending_email = request.session.get('chat_pending_email', '')
    if pending_email and OTP_MESSAGE_RE.match(text):
        # Legacy upfront verification: keep consuming it for old sessions.
        result = consume_otp(pending_email, text, 'reauth')
        conversation.messages.create(role='user', content='[one-time code withheld]')
        if result.ok:
            request.session['chat_verified_email'] = pending_email
            del request.session['chat_pending_email']
            conversation = _conversation(request)
            conversation.messages.create(
                role='assistant',
                content=(f"Verified, I can now help with escrows for "
                         f"{pending_email}. What would you like to do?"),
            )
            audit('chat.verified', actor=pending_email)
        else:
            conversation.messages.create(
                role='assistant', content=result.message,
            )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )

    base_url = request.build_absolute_uri('/')[:-1]
    session_key = request.session.session_key or ''
    run_turn(conversation, text, base_url=base_url, session_key=session_key)
    conversation.refresh_from_db()
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_POST
def chat_dismiss(request):
    conversation = _conversation(request)
    clear_proposal(conversation)
    conversation.messages.create(
        role='assistant',
        content="Discarded, nothing was done. Tell me what you would like instead.",
    )
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


VOICE_THROTTLE_PER_HOUR = 20


def _voice_throttled(request):
    """Cheap per-session cap so the metered API can't be burned at will."""
    if not request.session.session_key:
        request.session.save()
    key = f"voice:{request.session.session_key}"
    count = cache.get(key, 0)
    if count >= VOICE_THROTTLE_PER_HOUR:
        return True
    cache.set(key, count + 1, 3600)
    return False


def _transcribe_upload(uploaded):
    audio_bytes = uploaded.read()
    return transcribe_audio(audio_bytes)


@require_POST
def chat_voice(request):
    """Voice note into the chat: transcribe, then run a normal agent turn."""
    conversation = _conversation(request)
    uploaded = request.FILES.get('audio')
    if not uploaded:
        conversation.messages.create(
            role='assistant',
            content="I didn't receive any audio. Please try recording again.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    if _voice_throttled(request):
        conversation.messages.create(
            role='assistant',
            content="You've used a lot of voice notes this hour, please type for a bit.",
        )
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    try:
        transcript = _transcribe_upload(uploaded)
    except VoiceConfigError as exc:
        conversation.messages.create(role='assistant', content=str(exc))
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    except VoiceTranscriptionError as exc:
        conversation.messages.create(role='assistant', content=str(exc))
        return render(
            request, 'agent/_thread.html',
            _thread_context(conversation, request),
        )
    audit('chat.voice_transcribed', actor=conversation.owner_email or 'anonymous',
          metadata={'chars': len(transcript)})
    if OTP_MESSAGE_RE.match(transcript.strip()):
        resumed = _try_resume_creation_with_code(
            request, conversation, transcript.strip())
        if resumed is not None:
            return resumed
        agent_pending = normalize_email(conversation.pending_email or '')
        if agent_pending:
            result = consume_otp(agent_pending, transcript.strip(), 'reauth')
            conversation.messages.create(
                role='user', content='[one-time code withheld]')
            if result.ok:
                request.session['chat_verified_email'] = agent_pending
                conversation.owner_email = agent_pending
                conversation.pending_email = None
                conversation.save(update_fields=['owner_email', 'pending_email'])
                join_reply = _complete_pending_join(conversation, agent_pending)
                audit('chat.verified', actor=agent_pending)
                conversation.messages.create(
                    role='assistant',
                    content=join_reply or (
                        f"Verified, I can now help with escrows for "
                        f"{agent_pending}. What would you like to do?"))
            else:
                conversation.messages.create(
                    role='assistant', content=result.message)
            return render(
                request, 'agent/_thread.html',
                _thread_context(conversation, request),
            )
    base_url = request.build_absolute_uri('/')[:-1]
    session_key = request.session.session_key or ''
    run_turn(conversation, transcript, base_url=base_url, session_key=session_key)
    conversation.refresh_from_db()
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_POST
def transcribe_field(request):
    """Transcribe a short field-level recording; returns JSON for forms."""
    uploaded = request.FILES.get('audio')
    if not uploaded:
        return JsonResponse({'ok': False, 'error': 'No audio received.'})
    if _voice_throttled(request):
        return JsonResponse(
            {'ok': False, 'error': 'Voice limit reached for this hour.'},
            status=429)
    try:
        transcript = _transcribe_upload(uploaded)
    except VoiceConfigError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=503)
    except VoiceTranscriptionError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=422)
    return JsonResponse({'ok': True, 'transcript': transcript})


@require_POST
def chat_new(request):
    """Start over: wipe this browser session's thread and any proposal."""
    conversation = _conversation(request)
    actor = conversation.owner_email or 'anonymous'
    request.session.pop('chat_verified_email', None)
    request.session.pop('chat_pending_email', None)
    restart_thread(conversation)
    audit('chat.restarted', actor=actor)
    return render(
        request, 'agent/_thread.html',
        _thread_context(conversation, request),
    )


@require_GET
def chat_poll(request):
    """Live updates for an open chat page; 204 when nothing new. Never restarts (page load does)."""
    conversation = _conversation(request)
    context = _thread_context(conversation, request)
    try:
        since = int(request.GET.get('since') or 0)
    except (TypeError, ValueError):
        since = 0
    if since and context['last_message_id'] <= since:
        return HttpResponse(status=204)
    return render(request, 'agent/_thread.html', context)
