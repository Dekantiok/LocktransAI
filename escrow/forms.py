from django import forms
from django.core.exceptions import ValidationError

from .models import EscrowContract, SettlementVault
from .utils import normalize_email

MIN_ESCROW_AMOUNT = 5
MAX_ESCROW_AMOUNT = 9999999999.99
MIN_INSPECTION_DAYS = 1
MAX_INSPECTION_DAYS = 30


class ContractCreationForm(forms.ModelForm):
    class Meta:
        model = EscrowContract
        fields = [
            'title', 'description', 'amount', 'fee_payer',
            'creator_email', 'creator_role', 'counterparty_email', 'inspection_days',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['amount'].min_value = MIN_ESCROW_AMOUNT
        self.fields['amount'].max_value = MAX_ESCROW_AMOUNT
        self.fields['inspection_days'].min_value = MIN_INSPECTION_DAYS
        self.fields['inspection_days'].max_value = MAX_INSPECTION_DAYS
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault('class', 'form-select')
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault('class', 'form-check-input')
            else:
                widget.attrs.setdefault('class', 'form-control')
        for name in ('creator_email', 'counterparty_email'):
            self.fields[name].widget.attrs.setdefault('autocapitalize', 'none')
            self.fields[name].widget.attrs.setdefault('autocomplete', 'email')
            self.fields[name].widget.attrs.setdefault('spellcheck', 'false')
        placeholders = {
            'title': 'e.g. MacBook Pro M3 or Web Design Milestone',
            'description': 'Specify the item condition, delivery terms, and acceptance criteria...',
            'amount': '500',
            'creator_email': 'you@example.com',
            'counterparty_email': 'counterparty@example.com',
            'inspection_days': '3',
        }
        for name, ph in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault('placeholder', ph)

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < MIN_ESCROW_AMOUNT:
            raise ValidationError(
                f"The escrow amount must be at least {MIN_ESCROW_AMOUNT:,.2f}."
            )
        return amount

    def clean_inspection_days(self):
        days = self.cleaned_data['inspection_days']
        if days < MIN_INSPECTION_DAYS:
            raise ValidationError("The inspection window must be at least 1 day.")
        if days > MAX_INSPECTION_DAYS:
            raise ValidationError(f"The inspection window cannot exceed {MAX_INSPECTION_DAYS} days.")
        return days

    def clean(self):
        cleaned_data = super().clean()
        creator_email = normalize_email(cleaned_data.get('creator_email'))
        counterparty_email = normalize_email(cleaned_data.get('counterparty_email'))
        if creator_email:
            cleaned_data['creator_email'] = creator_email
        if counterparty_email:
            cleaned_data['counterparty_email'] = counterparty_email
        if creator_email and creator_email == counterparty_email:
            raise ValidationError(
                "The creator and counterparty cannot use the same email address."
            )
        amount = cleaned_data.get('amount')
        if amount is not None and cleaned_data.get('fee_payer') == 'seller':
            from .services import calculate_service_fee
            if amount <= calculate_service_fee(amount):
                raise ValidationError(
                    "The amount is too small for the seller to pay the "
                    "service fee. Increase the amount or let the buyer "
                    "pay the fee."
                )
        return cleaned_data


class SettlementVaultForm(forms.ModelForm):
    # PayPal Payouts destination. No bank resolve; the email itself is the
    # payout target and is verified as deliverable at payout time.
    paypal_email = forms.EmailField(
        label="PayPal email",
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'seller-paypal@example.com',
            'autocomplete': 'email',
        }),
    )

    class Meta:
        model = SettlementVault
        fields = ['paypal_email']

    def clean_paypal_email(self):
        from .utils import normalize_email
        email = normalize_email(self.cleaned_data.get('paypal_email') or '')
        if not email or '@' not in email:
            raise ValidationError("Enter a valid PayPal email address.")
        return email
