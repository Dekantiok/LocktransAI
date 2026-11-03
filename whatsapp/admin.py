from django.contrib import admin

from .models import WhatsAppLink


@admin.register(WhatsAppLink)
class WhatsAppLinkAdmin(admin.ModelAdmin):
    list_display = ('wa_id', 'verified_email', 'pending_email', 'updated_at')
    search_fields = ('wa_id', 'verified_email', 'pending_email')
    readonly_fields = ('created_at', 'updated_at')
