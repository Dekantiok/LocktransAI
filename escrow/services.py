import datetime
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils.timezone import now

from .gateways import get_payment_gateway
from .lifecycle import notify_event
from .models import (
    AuditEvent,
    AuthOTP,
    EscrowContract,
    PaymentTransaction,
    PayoutRecord,
    SettlementVault,
    TransitionError,
)
from .notifications import send_otp_via_termii
from .utils import generate_escrow_code, generate_otp, generate_secure_code, normalize_email

logger = logging.getLogger(__name__)

OTP_TTL = datetime.timedelta(minutes=15)


def audit(action, actor='system', escrow=None, metadata=None):
    """Append a consequential event to the audit trail (best-effort)."""
    try:
        return AuditEvent.objects.create(
            action=action,
            actor=actor or 'system',
            escrow=escrow,
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("Audit write failed for action %s", action)
        return None


def calculate_service_fee(amount):
    """Flat-tier service fee in USD."""
    from decimal import Decimal

    amount = Decimal(amount)

    if amount <= Decimal('50'):
        return Decimal('1.50')
    elif amount <= Decimal('100'):
        return Decimal('2.00')
    elif amount <= Decimal('500'):
        return Decimal('2.50')
    elif amount <= Decimal('1000'):
        return Decimal('5.00')
    else:
        return Decimal('10.00')


def chargeable_total(contract):
    return contract.total_charge


def chargeable_kobo(contract):
    return contract.total_charge_kobo


def get_unique_contract_code(attempts=5):
    """Reserve a LKTR-prefixed code that is actually free."""
    for _ in range(attempts):
        code = generate_escrow_code()
        if not EscrowContract.objects.filter(code=code).exists():
            return code
    raise RuntimeError("Unable to allocate a unique escrow code.")


def create_escrow_from_validated(cleaned_data):
    """Create an escrow from validated data (shared manual/agent path)."""
    contract = EscrowContract(
        title=cleaned_data['title'],
        description=cleaned_data['description'],
        amount=cleaned_data['amount'],
        fee_payer=cleaned_data['fee_payer'],
        creator_email=normalize_email(cleaned_data['creator_email']),
        creator_role=cleaned_data['creator_role'],
        counterparty_email=normalize_email(cleaned_data['counterparty_email']),
        inspection_days=cleaned_data.get('inspection_days') or 3,
    )
    contract.code = get_unique_contract_code()
    contract.service_fee = calculate_service_fee(contract.amount)
    contract.status = 'pending_verification'
    contract.gateway_reference = f"{contract.code}_{int(now().timestamp())}"
    contract.save()
    audit('escrow.created', actor=contract.creator_email, escrow=contract)
    return contract


def issue_otp(email, intent):
    """
    Create and deliver a one-time code; exactly one live per (email, intent).
    """
    email = normalize_email(email)
    cooldown = getattr(settings, 'OTP_ISSUE_COOLDOWN_SECONDS', 60)
    recent = AuthOTP.objects.filter(
        email=email, intent=intent, created_at__gt=now() - datetime.timedelta(seconds=cooldown)
    ).exists()
    if recent:
        logger.info("OTP issuance cooldown active for %s (%s)", email, intent)
        return None

    AuthOTP.objects.filter(email=email, intent=intent, is_used=False).update(is_used=True)

    otp_code = generate_otp()
    record = AuthOTP(
        email=email,
        intent=intent,
        expires_at=now() + OTP_TTL,
    )
    record.set_code(otp_code)
    record.save()
    delivered = send_otp_via_termii(email, otp_code)
    if not delivered:
        logger.warning("OTP delivery failed for %s (%s)", email, intent)
        record.is_used = True
        record.save(update_fields=['is_used'])
        return None
    return record


class OtpResult:
    SUCCESS = 'success'
    INVALID = 'invalid'
    EXPIRED = 'expired'
    EXHAUSTED = 'exhausted'

    MESSAGES = {
        INVALID: "That code is not correct. Please check and try again.",
        EXPIRED: "That code has expired. Please request a new one.",
        EXHAUSTED: "Too many incorrect attempts. Please request a new code.",
    }

    def __init__(self, outcome, record=None):
        self.outcome = outcome
        self.record = record

    @property
    def ok(self):
        return self.outcome == self.SUCCESS

    @property
    def message(self):
        return self.MESSAGES.get(self.outcome, "Invalid or expired code.")


def consume_otp(email, submitted_code, intent):
    """Validate a code; attempts burn on mismatch via conditional UPDATEs (concurrent guesses can't overspend)."""
    email = normalize_email(email)
    submitted_code = (submitted_code or '').strip()
    if not submitted_code:
        return OtpResult(OtpResult.INVALID)

    if (getattr(settings, 'DEMO_MODE', False)
            and submitted_code == getattr(settings, 'DEMO_OTP_CODE', '123456')):
        record = (
            AuthOTP.objects.filter(email=email, intent=intent, is_used=False)
            .order_by('-id')
            .first()
        )
        if record is not None:
            record.is_used = True
            record.save(update_fields=['is_used'])
        logger.warning("DEMO MODE: universal code accepted for %s (%s)", email, intent)
        return OtpResult(OtpResult.SUCCESS, record)

    record = (
        AuthOTP.objects.filter(email=email, intent=intent, is_used=False)
        .order_by('-id')
        .first()
    )
    if record is None:
        return OtpResult(OtpResult.INVALID)

    if not record.check_code(submitted_code):
        burned = AuthOTP.objects.filter(
            pk=record.pk, attempts_left__gt=0,
        ).update(attempts_left=max(0, record.attempts_left - 1))
        left = record.attempts_left - burned
        if left <= 0:
            return OtpResult(OtpResult.EXHAUSTED, record)
        return OtpResult(OtpResult.INVALID, record)

    # Correct code only: expired or replaced codes reject without burning tries.
    if record.is_expired:
        return OtpResult(OtpResult.EXPIRED, record)

    if record.attempts_left <= 0:
        return OtpResult(OtpResult.EXHAUSTED, record)

    claimed = AuthOTP.objects.filter(
        pk=record.pk, is_used=False, attempts_left__gt=0,
    ).update(is_used=True)
    if not claimed:
        record.refresh_from_db()
        if record.attempts_left <= 0:
            return OtpResult(OtpResult.EXHAUSTED, record)
        return OtpResult(OtpResult.INVALID, record)
    record.is_used = True
    return OtpResult(OtpResult.SUCCESS, record)


@dataclass
class PayoutResult:
    success: bool
    reason: str = ''
    detail: dict = None

    @property
    def message(self):
        return {
            'missing_vault': "The seller has not added their PayPal payout email yet.",
            'invalid_amount': "The payout amount is not valid for this escrow.",
            'payout_in_progress': "A payout is already in progress for this escrow.",
            'not_in_inspection': "This escrow is not awaiting a payout.",
            'not_awaiting_payout': "This escrow is not awaiting a payout.",
            'transfer_failed': "The PayPal payout did not go through. Our team has been notified.",
        }.get(self.reason, "The payout could not be completed.")


def _transfer_succeeded(result):
    if not isinstance(result, dict):
        return False
    if 'status' in result:
        if isinstance(result.get('data'), dict):
            batch_status = str(result['data'].get('batch_status') or '').upper()
            if batch_status in ('PENDING', 'PROCESSING', 'SUCCESS'):
                return True
        return bool(result['status'])
    return result.get('message') == 'Success'


def confirm_funding_payment(contract, amount_kobo, currency=None):
    """Mark a funded contract awaiting_dispatch. Shared by webhook and callback; conditional UPDATE advances once."""
    if (
        EscrowContract.objects.filter(pk=contract.pk, status='awaiting_funding').count()
        == 0
    ):
        return False
    if currency and str(currency).upper() != contract.currency.upper():
        logger.error(
            "Currency mismatch on %s: got %s expected %s",
            contract.code, currency, contract.currency,
        )
        return False
    if (
        EscrowContract.objects.filter(pk=contract.pk, status='awaiting_funding').count()
        == 0
    ):
        return False
    try:
        paid_kobo = int(amount_kobo)
    except (TypeError, ValueError):
        logger.error(
            "Unusable paid amount %r for contract %s", amount_kobo, contract.code
        )
        return False
    if paid_kobo != contract.total_charge_kobo:
        logger.error(
            "Amount mismatch on %s: got %s expected %s",
            contract.code, paid_kobo, contract.total_charge_kobo,
        )
        return False
    moved = EscrowContract.objects.filter(
        pk=contract.pk, status='awaiting_funding'
    ).update(status='awaiting_dispatch')
    if moved:
        logger.info("Payment confirmed for contract %s", contract.code)
        contract.refresh_from_db()
        PaymentTransaction.objects.update_or_create(
            provider_reference=contract.gateway_reference,
            defaults={
                'escrow': contract,
                'provider': 'paypal',
                'amount_kobo': paid_kobo,
                'currency': contract.currency,
                'status': 'success',
            },
        )
        audit('funding.confirmed', escrow=contract, metadata={
            'amount_kobo': paid_kobo,
            'reference': contract.gateway_reference,
        })
        notify_event('funding_confirmed', contract)
    return bool(moved)


def settle_pending_payout(contract):
    """Transfer for pending_payout (conditional, idempotent)."""
    vault = SettlementVault.objects.filter(contract=contract).first()
    if not vault:
        return PayoutResult(success=False, reason='missing_vault')

    if EscrowContract.objects.filter(pk=contract.pk, status='pending_payout').count() == 0:
        return PayoutResult(success=False, reason='not_awaiting_payout')

    amount = contract.seller_receives
    if amount <= 0:
        return PayoutResult(success=False, reason='invalid_amount')

    # Claim before any external call: an initiated/success/uncertain row means a transfer is in flight or needs review.
    blocking = PayoutRecord.objects.filter(
        escrow=contract, status__in=('initiated', 'success', 'uncertain'),
    ).order_by('-id').first()
    if blocking is not None:
        return PayoutResult(success=False, reason='payout_in_progress')

    claim = PayoutRecord.objects.create(
        escrow=contract, amount=amount, currency=contract.currency,
        status='initiated')
    try:
        result = get_payment_gateway().execute_seller_payout(vault, amount)
    except Exception:
        logger.exception("Payout raised for contract %s", contract.code)
        result = None

    raw = result if isinstance(result, dict) else {}
    nested = raw.get('data')
    nested = nested if isinstance(nested, dict) else {}

    def pick(*keys):
        for key in keys:
            for source in (raw, nested):
                value = source.get(key)
                if value not in (None, ''):
                    return value
        return None

    def finish_payout(status):
        claim.status = status
        claim.recipient_code = pick('recipient_code', 'recipient')
        claim.transfer_reference = pick('reference')
        claim.provider_transfer_code = pick('transfer_code')
        claim.provider_response = raw if raw else {'error': 'gateway raised'}
        claim.save(update_fields=[
            'status', 'recipient_code', 'transfer_reference',
            'provider_transfer_code', 'provider_response'])
        return claim

    if not _transfer_succeeded(result):
        logger.error("Payout failed for contract %s: %r", contract.code, result)
        contract.refresh_from_db()
        record = finish_payout(
            'uncertain' if result is None else 'failed'
        )
        audit('payout.failed', escrow=contract, metadata={
            'payout_id': record.id,
            'reason': 'uncertain' if result is None else 'transfer_failed',
        })
        return PayoutResult(success=False, reason='transfer_failed', detail=result)

    nested_status = str(nested.get('status') or '').lower()
    if nested_status and nested_status not in ('success', 'successful'):
        # Provider accepted but has not settled (e.g. PayPal pending).
        # Stay in pending_payout; the transfer webhook completes it.
        record = finish_payout('initiated')
        audit('payout.initiated', escrow=contract,
              metadata={'payout_id': record.id})
        return PayoutResult(success=True, reason='initiated', detail=result)

    EscrowContract.objects.filter(pk=contract.pk, status='pending_payout').update(status='completed')
    contract.refresh_from_db()
    logger.info("Payout complete for contract %s", contract.code)
    record = finish_payout('success')
    audit('payout.completed', escrow=contract, metadata={'payout_id': record.id})
    notify_event('payout_completed', contract)
    return PayoutResult(success=True)


def release_escrow_funds(contract):
    """Move in_inspection to payout via conditional claim; single transfer."""
    # Read via the manager, so a stale in-memory copy can't claim for a deleted vault.
    if not SettlementVault.objects.filter(contract=contract).exists():
        return PayoutResult(success=False, reason='missing_vault')

    claimed = EscrowContract.objects.filter(pk=contract.pk, status='in_inspection').update(
        status='pending_payout', buyer_confirmed=True
    )
    if not claimed:
        logger.info("Payout already claimed for contract %s", contract.code)
        return PayoutResult(success=False, reason='not_in_inspection')

    return settle_pending_payout(contract)


def initiate_refund(contract):
    """Start a provider refund for a funded escrow; idempotent per cancellation, pending until the provider confirms."""
    from .models import RefundRecord
    existing = RefundRecord.objects.filter(
        escrow=contract, status__in=('requested', 'initiated', 'success'),
    ).order_by('-id').first()
    if existing is not None:
        return existing
    payment = PaymentTransaction.objects.filter(
        escrow=contract, status='success').order_by('-id').first()
    record = RefundRecord.objects.create(
        escrow=contract, payment=payment,
        amount_kobo=contract.total_charge_kobo,
        currency=contract.currency, status='requested')
    try:
        result = get_payment_gateway().refund_payment(
            contract.gateway_reference, contract.total_charge_kobo)
    except Exception:
        logger.exception("Refund raised for contract %s", contract.code)
        result = None
    raw = result if isinstance(result, dict) else {}
    nested = raw.get('data') if isinstance(raw.get('data'), dict) else {}
    if raw.get('status'):
        record.status = 'initiated'
        record.provider_reference = (
            nested.get('reference') or nested.get('id')
            or raw.get('reference') or contract.gateway_reference)
        record.provider_response = raw
        record.save(update_fields=['status', 'provider_reference', 'provider_response'])
        audit('refund.initiated', escrow=contract,
              metadata={'refund_id': record.id})
    else:
        record.status = 'failed'
        record.provider_response = raw if raw else {'error': 'gateway raised'}
        record.save(update_fields=['status', 'provider_response'])
        audit('refund.failed', escrow=contract,
              metadata={'refund_id': record.id})
    return record


def auto_release_expired_inspections(dry_run=False):
    """Release escrows whose inspection window elapsed without buyer action."""
    expired = EscrowContract.objects.filter(
        status='in_inspection', inspection_ends__lt=now()
    )
    if dry_run:
        return list(expired)

    released, skipped = 0, 0
    for contract in expired:
        result = release_escrow_funds(contract)
        if result.success:
            released += 1
        else:
            skipped += 1
            logger.warning(
                "Auto-release skipped contract %s: %s", contract.code, result.reason
            )
    return released, skipped


def transition_or_log(contract, target, **fields):
    """Convenience wrapper used by views that already guard on role."""
    try:
        return contract.transition_to(target, **fields)
    except TransitionError:
        logger.warning(
            "Rejected illegal transition %s -> %s on contract %s",
            contract.status, target, contract.code,
        )
        return None


def complete_counterparty_join(code, actor_email):
    """Join as counterparty after email proof; moves to awaiting_funding, sets the join flag, notifies and audits."""
    actor = normalize_email(actor_email)
    contract = EscrowContract.objects.filter(code=(code or '').strip()).first()
    if contract is None:
        return None, "No escrow found with that code."
    if actor != contract.counterparty_email:
        return None, "That email is not the counterparty for this escrow."
    if contract.status == 'awaiting_funding':
        return contract, ''
    if contract.status != 'awaiting_counterparty':
        return None, (
            f"This escrow is not waiting for a counterparty to join "
            f"(status: {contract.status})."
        )
    fields = {}
    if actor == contract.buyer_email:
        fields['buyer_confirmed'] = True
    elif actor == contract.seller_email:
        fields['seller_confirmed'] = True
    try:
        contract.transition_to('awaiting_funding', **fields)
    except TransitionError:
        return None, "This escrow could not be joined right now."
    audit('counterparty.joined', actor=actor, escrow=contract)
    notify_event('counterparty_joined', contract, actor=actor)
    return contract, ''


def escrow_next_step(contract, actor_email):
    """Short 'what happens next for you' line for a party on an escrow."""
    actor = normalize_email(actor_email)
    is_buyer = actor == contract.buyer_email
    has_vault = SettlementVault.objects.filter(contract=contract).exists()
    status = contract.status
    if status == 'awaiting_counterparty':
        return ("Waiting for the counterparty to join, share the code "
                "with them." if actor == contract.creator_email else
                "Say \"join\" to accept and continue.")
    if status == 'awaiting_funding':
        return ('Say "fund" and I\'ll get your payment link.'
                if is_buyer else
                "Waiting for the buyer to fund this escrow.")
    if status == 'awaiting_dispatch':
        return ("Waiting for the seller to ship." if is_buyer else
                'Tell me "shipped" once you have dispatched it.')
    if status == 'in_transit':
        return ('Tell me "received" when it arrives.' if is_buyer else
                "Waiting for the buyer to confirm receipt.")
    if status == 'in_inspection':
        if is_buyer:
            return ('Say "release" to pay the seller, or "dispute" if '
                    "something is wrong."
                    + ("" if has_vault else
                       " The seller still needs to add their PayPal payout email."))
        return ("Waiting for the buyer to release or dispute."
                if has_vault else
                "I need your PayPal payout email: tell me the email address "
                "where PayPal should send your payout and I'll save it.")
    if status == 'pending_payout':
        return "Payout is being processed."
    if status == 'completed':
        return "This escrow is complete, the seller has been paid."
    if status == 'disputed':
        return "This escrow is disputed; funds are frozen pending review."
    if status == 'cancelled':
        return "This escrow was cancelled."
    return f"Current status: {status}."


def resume_escrow_after_verification(code, actor_email):
    """Resume for either party after email proof; kind is 'joined', 'resumed', or '' with an error."""
    actor = normalize_email(actor_email)
    contract = EscrowContract.objects.filter(code=(code or '').strip()).first()
    if contract is None:
        return None, '', "No escrow found with that code."
    if actor not in (contract.buyer_email, contract.seller_email):
        return None, '', (
            "That email is not linked to this escrow. Use the email the "
            "escrow was created with.")
    if (actor == contract.counterparty_email
            and contract.status == 'awaiting_counterparty'):
        joined, error = complete_counterparty_join(code, actor)
        if joined is None:
            return None, '', error
        return joined, 'joined', ''
    return contract, 'resumed', ''


def contracts_for_email(email, include_closed=False):
    email = normalize_email(email)
    queryset = EscrowContract.objects.filter(
        Q(creator_email=email) | Q(counterparty_email=email)
    )
    if not include_closed:
        queryset = queryset.exclude(status__in=EscrowContract.TERMINAL_STATUSES)
    return queryset
