import logging
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from ledger_api_client.utils import generate_payment_session
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView
from rest_framework_datatables.filters import DatatablesFilterBackend

from leaseslicensing.components.approvals.models import ApprovalUserAction
from leaseslicensing.components.approvals.serializers import ApprovalSerializer
from leaseslicensing.components.invoicing.email import (
    send_invoice_paid_external_notification,
    send_invoice_paid_internal_notification,
)
from leaseslicensing.components.invoicing.models import (
    CPICalculationMethod,
    Invoice,
    InvoiceTransaction,
    InvoicingDetails,
    OracleCode,
)
from leaseslicensing.components.invoicing.serializers import (
    CPICalculationMethodSerializer,
    InvoiceEditOracleInvoiceNumberSerializer,
    InvoiceSerializer,
    InvoiceTransactionSerializer,
    InvoicingDetailsSerializer,
    OracleCodeKeyValueSerializer,
    OracleCodeSerializer,
)
from leaseslicensing.components.invoicing.utils import generate_ledger_invoice
from leaseslicensing.components.main.api import (
    KeyValueListMixin,
    LicensingViewSet,
    NoPaginationListMixin,
)
from leaseslicensing.components.organisations.utils import get_organisation_ids_for_user
from leaseslicensing.helpers import is_customer, is_finance_officer
from leaseslicensing.permissions import IsAssessor, IsFinanceOfficer

logger = logging.getLogger(__name__)


class InvoiceFilterBackend(DatatablesFilterBackend):
    def filter_queryset(self, request, queryset, view):
        approval_id = request.GET.get("approval_id", None)
        if approval_id:
            queryset = queryset.filter(approval_id=approval_id)

        total_count = queryset.count()

        filter_invoice_organisation = (
            request.GET.get("filter_invoice_organisation")
            if request.GET.get("filter_invoice_organisation") != "all"
            else ""
        )
        filter_invoice_status = (
            request.GET.get("filter_invoice_status")
            if request.GET.get("filter_invoice_status") != "all"
            else ""
        )
        filter_invoice_due_date_from = request.GET.get("filter_invoice_due_date_from")
        filter_invoice_due_date_to = request.GET.get("filter_invoice_due_date_to")

        if filter_invoice_organisation:
            queryset = queryset.filter(
                approval__current_proposal__org_applicant=filter_invoice_organisation
            )

        if filter_invoice_status:
            if "overdue" == filter_invoice_status:
                queryset = queryset.filter(
                    status=Invoice.INVOICE_STATUS_UNPAID,
                    date_due__lte=datetime.now().date(),
                )
            else:
                queryset = queryset.filter(status=filter_invoice_status)

        if filter_invoice_due_date_from:
            filter_invoice_due_date_from = datetime.strptime(
                filter_invoice_due_date_from, "%Y-%m-%d"
            )
            queryset = queryset.filter(date_due__gte=filter_invoice_due_date_from)

        if filter_invoice_due_date_to:
            filter_invoice_due_date_to = datetime.strptime(
                filter_invoice_due_date_to, "%Y-%m-%d"
            )
            queryset = queryset.filter(date_due__lte=filter_invoice_due_date_to)

        fields = self.get_fields(request)
        ordering = self.get_ordering(request, view, fields)
        queryset = queryset.order_by(*ordering)
        if len(ordering):
            queryset = queryset.order_by(*ordering)

        queryset = super().filter_queryset(request, queryset, view)

        setattr(view, "_datatables_filtered_count", queryset.count())
        setattr(view, "_datatables_total_count", total_count)

        return queryset


class InvoiceViewSet(LicensingViewSet):
    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    filter_backends = [InvoiceFilterBackend]
    search_fields = ["lodgement_number", "oracle_invoice_number"]

    def get_queryset(self):
        if is_customer(self.request):
            org_ids = get_organisation_ids_for_user(self.request.user.id)
            return (
                super()
                .get_queryset()
                .exclude(status=Invoice.INVOICE_STATUS_PENDING_UPLOAD_ORACLE_INVOICE)
                .exclude(invoice_pdf="")
                .exclude(oracle_invoice_number__isnull=True)
                .filter(
                    Q(approval__current_proposal__ind_applicant=self.request.user.id)
                    | Q(approval__current_proposal__org_applicant__in=org_ids)
                )
            )
        return super().get_queryset()

    @action(detail=False, methods=["get"])
    def statuses(self, request, *args, **kwargs):
        status_choices = [
            {"id": status[0], "name": status[1]}
            for status in Invoice.INVOICE_STATUS_CHOICES
        ]
        if is_customer(request):
            status_choices = [
                status
                for status in status_choices
                if status["id"]
                not in [
                    Invoice.INVOICE_STATUS_PENDING_UPLOAD_ORACLE_INVOICE,
                    Invoice.INVOICE_STATUS_VOID,
                    Invoice.INVOICE_STATUS_DISCARDED,
                ]
            ]
        return Response(status_choices)

    @action(detail=True, methods=["get"])
    def transactions(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = InvoiceTransactionSerializer(
            instance.transactions.all(), many=True
        )
        return Response(serializer.data)

    @action(detail=True, methods=["POST"])
    def record_transaction(self, request, *args, **kwargs):
        if not is_finance_officer(request):
            return Response(
                {
                    "message": "You do not have permission to record an invoice transaction"
                }
            )

        instance = self.get_object()

        credit = request.data.get("credit", Decimal("0.00"))
        debit = request.data.get("debit", Decimal("0.00"))

        serializer = InvoiceTransactionSerializer(
            data={
                "invoice": instance.id,
                "credit": credit,
                "debit": debit,
            }
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        invoice_transaction = serializer.save()

        if Decimal("0.00") == invoice_transaction.invoice.balance:
            invoice_transaction.invoice.status = Invoice.INVOICE_STATUS_PAID
            invoice_transaction.invoice.save()

        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @action(detail=False, methods=["POST"])
    @transaction.atomic
    def generate_ad_hoc_invoice(self, request, *args, **kwargs):
        invoice_pdf = request.FILES.get("invoice_pdf", None)
        if not invoice_pdf:
            raise serializers.ValidationError(
                {"invoice_pdf": ["This field is required."]}
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()

        instance.invoice_pdf = invoice_pdf
        instance.ad_hoc = True
        instance.gst_free = instance.approval.approval_type.gst_free
        instance.status = Invoice.INVOICE_STATUS_UNPAID
        instance.save()

        # Generate ledger invoice
        generate_ledger_invoice(instance)

        # Log the creation of the invoice against the approval
        instance.approval.log_user_action(
            ApprovalUserAction.ACTION_AD_HOC_INVOICE_GENERATED_APPROVAL.format(
                instance.lodgement_number, instance.approval.lodgement_number
            ),
            request,
        )

        return Response(serializer.data)

    @action(detail=True, methods=["POST"])
    @transaction.atomic
    def upload_oracle_invoice(self, request, *args, **kwargs):
        if not is_finance_officer(request):
            return Response(
                {"message": "You do not have permission to upload an Oracle Invoice"}
            )

        instance = self.get_object()

        invoice_pdf = request.FILES.get("invoice_pdf", None)
        if not invoice_pdf:
            raise serializers.ValidationError(
                {"invoice_pdf": ["Please upload an Oracle Invoice .pdf file"]}
            )

        logger.info(f"Oracle Invoice uploaded for Invoice: {instance.lodgement_number}")

        # Once the finance user has uploaded an oracle invoice and entered the oracle invoice number
        # we can set the issue date and due date
        date_issued = timezone.now().date()
        date_due = date_issued + timezone.timedelta(
            days=settings.DEFAULT_DAYS_BEFORE_PAYMENT_DUE
        )

        data = request.data.copy()
        data.update(
            {
                "status": Invoice.INVOICE_STATUS_UNPAID,
                "date_issued": date_issued,
                "date_due": date_due,
            }
        )

        serializer = InvoiceEditOracleInvoiceNumberSerializer(instance, data=data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        instance = serializer.save()

        instance.invoice_pdf = invoice_pdf
        instance.save()

        # Generate ledger invoice
        generate_ledger_invoice(instance)

        return Response(serializer.data)

    @action(methods=["GET"], detail=True)
    def pay_invoice(self, request, *args, **kwargs):
        logger.info("Pay Invoice")
        invoice = self.get_object()
        return_url = reverse(
            "external-pay-invoice-success",
            kwargs={"id": invoice.id},
        )
        fallback_url = reverse(
            "external-pay-invoice-failure",
            kwargs={"id": invoice.id},
        )

        if settings.DEBUG and "localhost" in settings.SITE_URL:
            # For local development, these urls need to have a full domain to be accepted by
            # ledger as valid urls otherwise there will be a 'Payments Error'
            return_url = settings.SITE_URL + return_url
            fallback_url = settings.SITE_URL + fallback_url
        else:
            return_url = request.build_absolute_uri(return_url)
            fallback_url = request.build_absolute_uri(fallback_url)

        logger.debug(f"Return URL: {request.build_absolute_uri(return_url)}")
        logger.debug(f"Fallback URL: {request.build_absolute_uri(fallback_url)}")
        payment_session = generate_payment_session(
            request, invoice.invoice_reference, return_url, fallback_url
        )
        logger.info(f"Payment session: {payment_session}")

        if 200 == payment_session["status"]:
            return redirect(reverse("ledgergw-payment-details"))

        return redirect(fallback_url)

    @action(
        methods=["GET"],
        detail=True,
    )
    def retrieve_invoice_receipt(self, request, *args, **kwargs):
        instance = self.get_object()
        invoice_url = instance.ledger_invoice_url
        if invoice_url:
            response = requests.get(invoice_url)
            return FileResponse(response, content_type="application/pdf")

        raise Http404


class InvoiceTransactionViewSet(LicensingViewSet):
    queryset = InvoiceTransaction.objects.all()
    serializer_class = InvoiceTransactionSerializer
    permission_classes = [IsAssessor | IsFinanceOfficer]


class CPICalculationMethodViewSet(
    mixins.ListModelMixin, viewsets.GenericViewSet, NoPaginationListMixin
):
    queryset = CPICalculationMethod.objects.filter(archived=False)
    serializer_class = CPICalculationMethodSerializer
    permission_classes = [IsFinanceOfficer]


class PayInvoiceSuccessCallbackView(APIView):
    throttle_classes = [AnonRateThrottle, UserRateThrottle]
    permission_classes = [AllowAny]

    @transaction.atomic
    def get(self, request, uuid, format=None):
        logger.info("Leases Licensing Pay Invoice Success View get method called.")

        if (
            uuid
            and Invoice.objects.filter(
                uuid=uuid, status=Invoice.INVOICE_STATUS_UNPAID
            ).exists()
        ):
            logger.info(
                f"Looking for Invoice with uuid: {uuid}.",
            )
            invoice = Invoice.objects.get(uuid=uuid)

            logger.info(
                f"Found - Invoice: {invoice.id}",
            )

            if invoice.amount > Decimal("0.00"):
                it = InvoiceTransaction.objects.create(
                    invoice=invoice,
                    debit=invoice.amount,
                )
                logger.info(f"Created Invoice Transaction: {it.id} Debit: {it.debit}")
            elif invoice.amount < Decimal("0.00"):
                it = InvoiceTransaction.objects.create(
                    invoice=invoice,
                    credit=invoice.amount,
                )
                logger.info(f"Created Invoice Transaction: {it.id} Credit: {it.credit}")

            invoice.status = Invoice.INVOICE_STATUS_PAID
            invoice.date_paid = timezone.now()
            invoice.save()

            logger.info(
                f"Invoice: {invoice.id} - Marked as paid.",
            )

            logger.info(
                f"Sending notifications for Invoice: {invoice.id}.",
            )

            send_invoice_paid_external_notification(invoice)

            send_invoice_paid_internal_notification(invoice)

            logger.info(
                f"Notifications sent for Invoice: {invoice.id}.",
            )

            logger.info(
                "Returning status.HTTP_200_OK. Invoice marked as paid successfully.",
            )
            # this end-point is called by an unmonitored get request in ledger so there is no point having a
            # a response body however we will return a status in case this is used on the ledger end in future
            return Response(status=status.HTTP_200_OK)

        # If there is no uuid to identify the cart then send a bad request status back in case ledger can
        # do something with this in future
        logger.info(
            "Returning status.HTTP_400_BAD_REQUEST bad request as there "
            f"was not an unpaid invoice with uuid: {uuid}."
        )
        return Response(status=status.HTTP_400_BAD_REQUEST)


class InvoicingDetailsViewSet(LicensingViewSet):
    queryset = InvoicingDetails.objects.all()
    serializer_class = InvoicingDetailsSerializer
    permission_classes = [IsFinanceOfficer]

    @action(methods=["PUT"], detail=True)
    def update_and_preview_invoices(self, request, *args, **kwargs):
        # Save any changes made to the invoicing details
        self.update(request, *args, **kwargs)
        return self.preview_invoices(request, *args, **kwargs)

    @action(methods=["GET"], detail=True)
    def preview_invoices(self, request, *args, **kwargs):
        return Response(self.get_object().preview_invoices)

    @action(methods=["PUT"], detail=True)
    def complete_editing(self, request, *args, **kwargs):
        instance = self.get_object()

        self.update(request, *args, **kwargs)

        # Check for any changes made to the gross turnover amounts for financial quarters
        instance.process_gross_turnover_invoices()

        instance.approval.status = instance.approval.APPROVAL_STATUS_CURRENT
        instance.approval.save(version_comment="Completed Editing Invoicing Details")
        serializer = ApprovalSerializer(instance.approval, context={"request": request})
        return Response(serializer.data)


class OracleCodeViewSet(LicensingViewSet, KeyValueListMixin):
    queryset = OracleCode.objects.all()
    serializer_class = OracleCodeSerializer
    permission_classes = [IsFinanceOfficer]
    key_value_display_field = "code"
    key_value_serializer_class = OracleCodeKeyValueSerializer
