from django.db import models


class AIConversation(models.Model):
    """One agent session per channel. Ownership binds only after OTP proof; channel_identity is never proof."""

    CHANNEL_CHOICES = [
        ('web', 'Website chat'),
        ('whatsapp', 'WhatsApp'),
    ]

    channel = models.CharField(max_length=15, choices=CHANNEL_CHOICES)
    channel_identity = models.CharField(max_length=255, db_index=True)
    owner_email = models.EmailField(null=True, blank=True, db_index=True)
    # Bare 6-digit reply while set is consumed as the code, never sent to the model.
    pending_email = models.EmailField(null=True, blank=True)
    # Flow context surviving OTP round-trips (e.g. pending join reference).
    pending_context = models.JSONField(default=dict, blank=True)
    # Pending consequential tool call awaiting confirmation; set only by the loop.
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
