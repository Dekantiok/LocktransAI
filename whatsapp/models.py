from django.db import models


class WhatsAppLink(models.Model):
    """Maps a WhatsApp sender to a verified email. The number is never identity; proof is email OTP only."""

    wa_id = models.CharField(max_length=32, unique=True, db_index=True)
    pending_email = models.EmailField(blank=True, default='')
    verified_email = models.EmailField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.wa_id} -> {self.verified_email or 'unverified'}"
