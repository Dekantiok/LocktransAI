from django.conf import settings


def demo_mode(request):
    """Expose demo flags to templates (banner + judge instructions)."""
    del request
    return {
        'DEMO_MODE': getattr(settings, 'DEMO_MODE', False),
        'DEMO_OTP_CODE': getattr(settings, 'DEMO_OTP_CODE', '123456'),
    }
