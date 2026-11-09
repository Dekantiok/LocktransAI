
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone

from .utils import normalize_email


class TransitionError(Exception):
    """Raised when a status change is not permitted by the state machine."""


class EscrowContract(models.Model):
    STATUS_CHOICES = [
        ('pending_verification', 'Pending Verification'),
        ('awaiting_counterparty', 'Awaiting Counterparty'),
        ('awaiting_funding', 'Awaiting Funding'),
        ('awaiting_dispatch', 'Awaiting Dispatch'),
        ('in_transit', 'In Transit'),
        ('in_inspection', 'In Inspection'),
        ('pending_payout', 'Pending Payout'),
        ('completed', 'Completed'),
        ('disputed', 'Disputed'),
        ('cancelled', 'Cancelled'),
    ]

    ROLE_CHOICES = [
        ('buyer', 'Buyer'),
        ('seller', 'Seller'),
    ]

    # Every status change must be listed here. Funded cancel needs an ops refund check.
    TRANSITIONS = {
        'pending_verification': {'awaiting_counterparty', 'cancelled'},
        'awaiting_counterparty': {'awaiting_funding', 'cancelled'},
        'awaiting_funding': {'awaiting_dispatch', 'disputed', 'cancelled'},
        'awaiting_dispatch': {'in_transit', 'disputed', 'cancelled'},
        'in_transit': {'in_inspection', 'disputed', 'cancelled'},
        'in_inspection': {'pending_payout', 'disputed', 'cancelled'},
        'pending_payout': {'completed', 'disputed', 'cancelled'},
        'disputed': {'pending_payout', 'cancelled'},
        'completed': set(),
        'cancelled': set(),
    }

    # States where no money has moved yet, so a party can cancel outright
    # with no refund required.
    PRE_FUNDING_STATUSES = {
        'pending_verification', 'awaiting_counterparty', 'awaiting_funding',
    }

    TERMINAL_STATUSES = {'completed', 'cancelled'}

    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=50, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='USD')
    service_fee = models.DecimalField(max_digits=10, decimal_places=2)
    fee_payer = models.CharField(max_length=10, choices=ROLE_CHOICES)
    creator_email = models.EmailField(db_index=True)
    creator_role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    counterparty_email = models.EmailField(db_index=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending_verification')
    inspection_days = models.IntegerField(default=3)
    inspection_ends = models.DateTimeField(null=True, blank=True)
    buyer_confirmed = models.BooleanField(default=False)
    seller_confirmed = models.BooleanField(default=False)
    gateway_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['status', 'inspection_ends'], name='escrow_status_inspection_idx'),
        ]

    def __str__(self):
        return f"{self.title} ({self.code})"

    def save(self, *args, **kwargs):
        self.creator_email = normalize_email(self.creator_email)
        self.counterparty_email = normalize_email(self.counterparty_email)
        super().save(*args, **kwargs)

    @property
    def buyer_email(self):
        if self.creator_role == 'buyer':
            return self.creator_email
        return self.counterparty_email

    @property
    def seller_email(self):
        if self.creator_role == 'seller':
            return self.creator_email
        return self.counterparty_email

    @property
    def parties(self):
        return (self.buyer_email, self.seller_email)

    @property
    def seller_receives(self):
        if self.fee_payer == 'seller':
            return self.amount - self.service_fee
        return self.amount

    @property
    def total_charge(self):
        """What the buyer actually pays at checkout (amount + fee if buyer pays)."""
        if self.fee_payer == 'buyer':
            return self.amount + self.service_fee
        return self.amount

    @property
    def total_charge_kobo(self):
        return int(self.total_charge * 100)

    @property
    def is_inspection_expired(self):
        if not self.inspection_ends:
            return False
        return self.inspection_ends <= timezone.now()

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    def can_transition_to(self, target):
        return target in self.TRANSITIONS.get(self.status, set())

    def transition_to(self, target, **fields):
        if not self.can_transition_to(target):
            raise TransitionError(
                f"Cannot move contract {self.code} from {self.status} to {target}."
            )
        # Single conditional UPDATE: if a concurrent request already
        # moved this contract, this call loses cleanly instead of
        # overwriting their state with a stale in-memory copy.
        updated = type(self).objects.filter(
            pk=self.pk, status=self.status).update(status=target, **fields)
        if not updated:
            raise TransitionError(
                f"Cannot move contract {self.code} from {self.status} to {target}."
            )
        for field, value in fields.items():
            setattr(self, field, value)
        self.status = target
        return self


class AuthOTP(models.Model):
    INTENT_CHOICES = [
        ('create', 'Create'),
        ('join', 'Join'),
        ('reauth', 'Reauth'),
    ]

    MAX_ATTEMPTS = 5

    email = models.EmailField(db_index=True)
    # Only a hash of the code is ever persisted. The plaintext is sent to
    # the user once at issuance and never stored.
    code_hash = models.CharField(max_length=128)
    intent = models.CharField(max_length=15, choices=INTENT_CHOICES)
    attempts_left = models.IntegerField(default=MAX_ATTEMPTS)
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['email', 'intent', 'is_used'], name='otp_lookup_idx'),
        ]

    def __str__(self):
        return f"OTP for {self.email} ({self.intent})"

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    @property
    def is_usable(self):
        return not self.is_used and not self.is_expired and self.attempts_left > 0

    def set_code(self, plaintext_code):
        """Hash a freshly generated code for storage."""
        self.code_hash = make_password(plaintext_code)

    def check_code(self, plaintext_code):
        """Constant-time check of a submitted code against the stored hash."""
        if not plaintext_code:
            return False
        return check_password(plaintext_code, self.code_hash)


class SettlementVault(models.Model):
    contract = models.OneToOneField(
        EscrowContract, on_delete=models.CASCADE, related_name='settlement_vault'
    )
    # PayPal Payouts destination (primary). Legacy NUBAN fields kept nullable
    # for backward-compat reads of pre-PayPal vaults.
    paypal_email = models.EmailField(max_length=255, blank=True, default='')
    bank_name = models.CharField(max_length=100, blank=True, default='')
    bank_code = models.CharField(max_length=10, blank=True, default='')
    account_number = models.CharField(max_length=10, blank=True, default='')
    account_name = models.CharField(max_length=150, blank=True, default='')

    def __str__(self):
        return f"Vault for {self.contract.code}"

    @property
    def masked_account_number(self):
        """Only ever expose the last four digits outside the seller's own view."""
        if self.paypal_email and not self.account_number:
            local, _, domain = self.paypal_email.partition('@')
            if len(local) <= 2:
                return '**@' + domain
            return local[:2] + '***@' + domain
        if len(self.account_number) <= 4:
            return '*' * len(self.account_number)
        return '*' * (len(self.account_number) - 4) + self.account_number[-4:]


class EscrowDraft(models.Model):
    """Proposed transaction, not a live escrow; creation only after explicit confirmation."""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('proposed', 'Proposed'),
        ('converted', 'Converted'),
        ('abandoned', 'Abandoned'),
    ]

    owner_email = models.EmailField(db_index=True, blank=True, default='')
    # Owner email unproven until the creation OTP is consumed and binds this session.
    owner_session = models.CharField(max_length=64, db_index=True, blank=True, default='')
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default='USD')
    fee_payer = models.CharField(max_length=10, blank=True)
    creator_role = models.CharField(max_length=10, blank=True)
    counterparty_email = models.EmailField(blank=True)
    inspection_days = models.IntegerField(default=3)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"Draft {self.id} for {self.owner_email} ({self.status})"

    def save(self, *args, **kwargs):
        self.owner_email = normalize_email(self.owner_email)
        self.counterparty_email = normalize_email(self.counterparty_email)
        super().save(*args, **kwargs)


class Identity(models.Model):
    """Verified user identity; proof via OTP challenges."""

    email = models.EmailField(unique=True, db_index=True)
    first_verified_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)


class VerifiedContact(models.Model):
    """A contact address proven to belong to an Identity."""

    CHANNEL_CHOICES = [
        ('email', 'Email'),
    ]

    identity = models.ForeignKey(
        Identity, on_delete=models.CASCADE, related_name='contacts'
    )
    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES, default='email')
    value = models.CharField(max_length=255, db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['identity', 'channel', 'value'],
                name='verified_contact_unique',
            ),
        ]

    def __str__(self):
        return f"{self.channel}:{self.value}"

    def save(self, *args, **kwargs):
        self.value = normalize_email(self.value)
        super().save(*args, **kwargs)


class PaymentTransaction(models.Model):
    """One provider-side payment attempt; exactly one row per reference."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    escrow = models.ForeignKey(
        EscrowContract, on_delete=models.CASCADE, related_name='payments'
    )
    provider = models.CharField(max_length=20, default='paypal')
    provider_reference = models.CharField(max_length=100, unique=True, db_index=True)
    amount_kobo = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    provider_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.provider_reference} ({self.status})"


class PayoutRecord(models.Model):
    """One seller-payout attempt; latest row is current state."""

    STATUS_CHOICES = [
        ('initiated', 'Initiated'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('uncertain', 'Uncertain'),
    ]

    escrow = models.ForeignKey(
        EscrowContract, on_delete=models.CASCADE, related_name='payouts'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default='USD')
    recipient_code = models.CharField(max_length=100, null=True, blank=True)
    transfer_reference = models.CharField(max_length=100, null=True, blank=True)
    provider_transfer_code = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='initiated')
    provider_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"Payout {self.id} for {self.escrow.code} ({self.status})"


class RefundRecord(models.Model):
    """One buyer-refund operation; not a refund until the provider confirms."""

    STATUS_CHOICES = [
        ('requested', 'Requested'),
        ('initiated', 'Initiated'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    escrow = models.ForeignKey(
        EscrowContract, on_delete=models.CASCADE, related_name='refunds'
    )
    payment = models.ForeignKey(
        PaymentTransaction, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='refunds',
    )
    amount_kobo = models.BigIntegerField()
    currency = models.CharField(max_length=3, default='USD')
    provider_reference = models.CharField(max_length=100, null=True, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='requested')
    provider_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Refund {self.id} for {self.escrow.code} ({self.status})"


class WebhookEvent(models.Model):
    """Inbound provider events, recorded before processing (dedup)."""

    provider = models.CharField(max_length=20, db_index=True)
    event = models.CharField(max_length=100)
    event_id = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    signature_valid = models.BooleanField(default=False)
    processed = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'event', 'event_id'],
                name='webhook_event_unique',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.event} ({self.event_id})"


class AuditEvent(models.Model):
    """Append-only trail of auth and financial events."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=255, default='system')
    action = models.CharField(max_length=100, db_index=True)
    escrow = models.ForeignKey(
        EscrowContract, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='audit_events',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"{self.action} by {self.actor}"
