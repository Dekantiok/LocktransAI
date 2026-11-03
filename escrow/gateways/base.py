from abc import ABC, abstractmethod

class PaymentGatewayBase(ABC):
    @abstractmethod
    def initialize_vault_payment(self, escrow_record, return_url):
        pass

    @abstractmethod
    def verify_incoming_webhook(self, request_headers, request_body):
        pass

    @abstractmethod
    def execute_seller_payout(self, settlement_vault_record, total_disbursement):
        pass

    def verify_transaction(self, gateway_reference):
        raise NotImplementedError

    def refund_payment(self, gateway_reference, amount_kobo=None):
        raise NotImplementedError