from django.db import models


class WhatsAppLink(models.Model):
    """Maps a WhatsApp sender to a verified email identity.

    The sender number (wa_id) is a communication endpoint, never proof of
    identity. Proof comes only from a backend-verified email OTP. Until
    ``verified_email`` is set, the agent must not touch escrows owned by
    any address.
    """

    wa_id = models.CharField(max_length=32, unique=True, db_index=True)
    # Email address an OTP was most recently sent to for this sender.
    pending_email = models.EmailField(blank=True, default='')
    # Email address proven via OTP by this sender.
    verified_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.wa_id} -> {self.verified_email or 'unverified'}"
