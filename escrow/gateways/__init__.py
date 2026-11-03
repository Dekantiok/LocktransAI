from django.conf import settings

from .demo import DemoGateway
from .paypal import PayPalGateway


def get_payment_gateway():
    if getattr(settings, 'DEMO_MODE', False):
        return DemoGateway()
    return PayPalGateway()
