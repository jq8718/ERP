from django import forms
from django.utils import timezone

from masterdata.models import Customer, Supplier
from system.display import set_form_labels

from .models import ExpenseRecord, OpeningPayable, OpeningReceivable


class OpeningReceivableForm(forms.ModelForm):
    class Meta:
        model = OpeningReceivable
        fields = ["customer", "source_doc_no", "opening_date", "due_date", "opening_amount", "remark"]
        widgets = {
            "opening_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["customer"].queryset = Customer.objects.filter(status=Customer.CustomerStatus.ACTIVE).order_by("customer_no")
        self.fields["opening_date"].initial = self.fields["opening_date"].initial or timezone.localdate()

    def clean_opening_amount(self):
        amount = self.cleaned_data["opening_amount"]
        if amount <= 0:
            raise forms.ValidationError("期初金额必须大于 0")
        return amount


class OpeningPayableForm(forms.ModelForm):
    class Meta:
        model = OpeningPayable
        fields = ["supplier", "source_doc_no", "opening_date", "due_date", "opening_amount", "remark"]
        widgets = {
            "opening_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["supplier"].queryset = Supplier.objects.filter(status=Supplier.SupplierStatus.ACTIVE).order_by("supplier_no")
        self.fields["opening_date"].initial = self.fields["opening_date"].initial or timezone.localdate()

    def clean_opening_amount(self):
        amount = self.cleaned_data["opening_amount"]
        if amount <= 0:
            raise forms.ValidationError("期初金额必须大于 0")
        return amount


class ExpenseRecordForm(forms.ModelForm):
    class Meta:
        model = ExpenseRecord
        fields = ["expense_date", "category", "amount", "payment_method", "payee", "invoice_no", "remark"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_form_labels(self)
        self.fields["expense_date"].initial = self.fields["expense_date"].initial or timezone.localdate()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("费用金额必须大于 0")
        return amount
