from django.db import models


class AIConversation(models.Model):
    """One ongoing agent session on a channel.

    Ownership binds to a verified email after OTP proof; until then the
    agent must not touch escrow records. ``channel_identity`` is a session
    marker, never proof of identity.
    """

    CHANNEL_CHOICES = [
        ('web', 'Website chat'),
        ('whatsapp', 'WhatsApp'),
    ]

    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES)
    channel_identity = models.CharField(max_length=255, db_index=True)
    owner_email = models.EmailField(null=True, blank=True, db_index=True)
    # Email awaiting OTP proof for in-chat verification (website assistant).
    # A bare 6-digit reply while this is set is consumed as the code -
    # never forwarded to the model.
    pending_email = models.EmailField(null=True, blank=True)
    # Flow context that must survive an OTP round-trip, e.g.
    # {'join_escrow_reference': 'LKTR...'} while verification is pending.
    pending_context = models.JSONField(default=dict, blank=True)
    # Awaiting user confirmation for one consequential tool call:
    # {'tool': name, 'arguments': {...}, 'proposed_at': iso-timestamp}.
    # Set when the agent presents a proposal, cleared on execution or
    # expiry. Never set from model input, only by the loop itself.
    pending_action = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f"{self.channel}:{self.channel_identity}"


class ConversationMessage(models.Model):
    """One persisted turn; tool traffic stored separately for replay."""

    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('tool', 'Tool result'),
    ]

    conversation = models.ForeignKey(
        AIConversation, on_delete=models.CASCADE, related_name='messages'
    )
    role = models.CharField(max_length=15, choices=ROLE_CHOICES)
    content = models.TextField(blank=True)
    tool_name = models.CharField(max_length=100, null=True, blank=True)
    tool_arguments = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.role} message {self.id}"
