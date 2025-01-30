import logging
from copy import deepcopy
from datetime import datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
from django.db import transaction
from django.db.models import F, Q
from rest_framework import serializers, views, viewsets
from rest_framework.decorators import action as detail_route
from rest_framework.decorators import action as list_route
from rest_framework.decorators import renderer_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework_datatables.pagination import DatatablesPageNumberPagination

from leaseslicensing.components.compliances.models import (
    Compliance,
    ComplianceAmendmentReason,
    ComplianceAmendmentRequest,
    ComplianceAssessment,
    ComplianceReferral,
    update_proposal_compliance_filename,
)
from leaseslicensing.components.compliances.serializers import (
    CompAmendmentRequestDisplaySerializer,
    ComplianceActionSerializer,
    ComplianceAmendmentRequestSerializer,
    ComplianceAssessmentSerializer,
    ComplianceCommsSerializer,
    ComplianceReferralDatatableSerializer,
    ComplianceReferralSerializer,
    ComplianceSerializer,
    InternalComplianceSerializer,
    SaveComplianceSerializer,
    UpdateComplianceAssessmentSerializer,
    UpdateComplianceReferralSerializer,
)
from leaseslicensing.components.main.api import (
    LicensingViewSet,
    UserActionLoggingViewset,
)
from leaseslicensing.components.main.decorators import (
    basic_exception_handler,
    logging_action,
)
from leaseslicensing.components.main.filters import LedgerDatatablesFilterBackend
from leaseslicensing.components.main.models import (
    ApplicationType,
    upload_protected_files_storage,
)
from leaseslicensing.components.organisations.utils import get_organisation_ids_for_user
from leaseslicensing.components.proposals.api import ProposalRenderer
from leaseslicensing.components.proposals.serializers import SendReferralSerializer
from leaseslicensing.helpers import is_compliance_referee, is_customer, is_internal
from leaseslicensing.permissions import (
    HasObjectPermission,
    IsAsignedAssessor,
    IsAssessor,
    IsAssignedComplianceReferee,
    IsComplianceReferee,
    IsFinanceOfficer,
)

logger = logging.getLogger(__name__)


class GetComplianceStatusesDict(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    def get(self, request, format=None):
        data = [
            {"code": i[0], "description": i[1]}
            for i in Compliance.PROCESSING_STATUS_CHOICES
        ]
        return Response(data)


class GetComplianceCustomerStatusesDict(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    def get(self, request, format=None):
        data = [
            {"code": i[0], "description": i[1]}
            for i in Compliance.CUSTOMER_STATUS_CHOICES
        ]
        return Response(data)


class ComplianceFilterBackend(LedgerDatatablesFilterBackend):
    """
    Custom filters
    """

    def filter_queryset(self, request, queryset, view):
        total_count = queryset.count()

        filter_due_date_from = request.GET.get("filter_due_date_from")
        filter_due_date_to = request.GET.get("filter_due_date_to")
        filter_compliance_status = (
            request.GET.get("filter_compliance_status")
            if request.GET.get("filter_compliance_status") != "all"
            else ""
        )
        filter_approval_type = (
            request.GET.get("filter_approval_type")
            if request.GET.get("filter_approval_type") != "all"
            else ""
        )

        if filter_due_date_from:
            filter_due_date_from = datetime.strptime(filter_due_date_from, "%Y-%m-%d")
            queryset = queryset.filter(due_date__gte=filter_due_date_from)
        if filter_due_date_to:
            filter_due_date_to = datetime.strptime(filter_due_date_to, "%Y-%m-%d")
            queryset = queryset.filter(due_date__lte=filter_due_date_to)
        if filter_compliance_status:
            queryset = queryset.filter(processing_status=filter_compliance_status)
        if filter_approval_type:
            filter_approval_type = int(filter_approval_type)
            queryset = queryset.filter(approval__approval_type__id=filter_approval_type)

        queryset = self.apply_request(
            request,
            queryset,
            view,
            ledger_lookup_fields=[
                "approval__current_proposal__ind_applicant",
                "assigned_to",
            ],
        )

        setattr(view, "_datatables_filtered_count", queryset.count())
        setattr(view, "_datatables_total_count", total_count)

        return queryset


class CompliancePaginatedViewSet(viewsets.ReadOnlyModelViewSet):
    filter_backends = (ComplianceFilterBackend,)
    pagination_class = DatatablesPageNumberPagination
    renderer_classes = (ProposalRenderer,)
    page_size = 10
    queryset = Compliance.objects.all()
    serializer_class = ComplianceSerializer
    permission_classes = [IsAssessor | IsComplianceReferee | HasObjectPermission]

    def get_queryset(self):
        queryset = super().get_queryset()
        if not is_internal(self.request) and not is_customer(self.request):
            return queryset.none()

        if is_compliance_referee(self.request):
            queryset = queryset.exclude(
                processing_status=Compliance.PROCESSING_STATUS_DISCARDED,
            ).filter(referrals__referral=self.request.user.id)

        if is_customer(self.request):
            organisation_ids = get_organisation_ids_for_user(self.request.user.id)
            queryset = queryset.exclude(
                processing_status=Compliance.PROCESSING_STATUS_DISCARDED
            ).filter(
                Q(proposal__submitter=self.request.user.id)
                | Q(proposal__org_applicant_id__in=organisation_ids)
            )

        target_organisation_id = self.request.query_params.get(
            "target_organisation_id", None
        )
        if (
            target_organisation_id
            and target_organisation_id.isnumeric()
            and int(target_organisation_id) > 0
        ):
            target_organisation_id = int(target_organisation_id)
            queryset = queryset.exclude(
                approval__current_proposal__org_applicant__isnull=True
            ).filter(
                approval__current_proposal__org_applicant__id=target_organisation_id
            )

        return queryset

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()

        compliances_referred_to_me = self.request.query_params.get(
            "compliances_referred_to_me", False
        )
        if compliances_referred_to_me:
            ids = (
                ComplianceReferral.objects.exclude(
                    processing_status=ComplianceReferral.PROCESSING_STATUS_RECALLED
                )
                .filter(referral=self.request.user.id)
                .values_list("compliance_id", flat=True)
            )
            qs = (
                Compliance.objects.filter(
                    id__in=ids, referrals__referral=self.request.user.id
                )
                .order_by()
                .annotate(referral_processing_status=F("referrals__processing_status"))
            )

        qs = self.filter_queryset(qs)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ComplianceSerializer(
            result_page, context={"request": request}, many=True
        )
        result = self.paginator.get_paginated_response(serializer.data)

        return result

    @list_route(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def list_external(self, request, *args, **kwargs):
        """
        User is accessing /external/ page
        """
        qs = self.get_queryset()
        qs = self.filter_queryset(qs)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ComplianceSerializer(
            result_page, context={"request": request}, many=True
        )
        result = self.paginator.get_paginated_response(serializer.data)
        return result

    @list_route(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def compliances_external(self, request, *args, **kwargs):
        """
        Paginated serializer for datatables - used by the external dashboard

        To test:
            http://localhost:8000/api/compliance_paginated/compliances_external/?format=datatables&draw=1&length=2
        """

        qs = self.get_queryset().exclude(
            processing_status=Compliance.PROCESSING_STATUS_FUTURE
        )
        qs = self.filter_queryset(qs)

        # on the internal organisations dashboard, filter the Proposal/Approval/Compliance datatables
        # by applicant/organisation
        applicant_id = request.GET.get("org_id")
        if applicant_id:
            qs = qs.filter(proposal__org_applicant_id=applicant_id)
        submitter_id = request.GET.get("submitter_id", None)
        if submitter_id:
            qs = qs.filter(proposal__submitter_id=submitter_id)
        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ComplianceSerializer(
            result_page, context={"request": request}, many=True
        )
        return self.paginator.get_paginated_response(serializer.data)


class ComplianceViewSet(UserActionLoggingViewset):
    serializer_class = ComplianceSerializer
    queryset = Compliance.objects.all()
    permission_classes = [
        IsAssessor | IsFinanceOfficer | IsComplianceReferee | HasObjectPermission
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        if is_compliance_referee(self.request):
            logger.debug(f"User {self.request.user} is a compliance referee")
            queryset = queryset.filter(referrals__referral=self.request.user.id)
        elif is_customer(self.request):
            logger.debug(f"User {self.request.user} is a customer")
            queryset = queryset.exclude(
                processing_status=Compliance.PROCESSING_STATUS_DISCARDED
            ).filter(Q(proposal__submitter=self.request.user.id))

        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        # Filter by org
        org_id = request.GET.get("org_id", None)
        if org_id:
            queryset = queryset.filter(proposal__org_applicant_id=org_id)
        submitter_id = request.GET.get("submitter_id", None)
        if submitter_id:
            queryset = queryset.filter(proposal__submitter_id=submitter_id)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def filter_list(self, request, *args, **kwargs):
        """Used by the external dashboard filters"""
        region_qs = (
            self.get_queryset()
            .filter(proposal__region__isnull=False)
            .values_list("proposal__region__name", flat=True)
            .distinct()
        )
        activity_qs = (
            self.get_queryset()
            .filter(proposal__activity__isnull=False)
            .values_list("proposal__activity", flat=True)
            .distinct()
        )
        application_types = ApplicationType.objects.all().values_list("name", flat=True)
        data = dict(
            regions=region_qs,
            activities=activity_qs,
            application_types=application_types,
        )
        return Response(data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    def internal_compliance(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def submit(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            data = {
                "text": request.data.get("detail"),
            }
            if instance.gross_turnover_required:
                data["gross_turnover"] = request.data.get("gross_turnover")

            serializer = SaveComplianceSerializer(instance, data=data)
            serializer.is_valid(raise_exception=True)
            instance = serializer.save()
            instance.submit(request)

            serializer = self.get_serializer(instance)

            num_files = request.data.get("num_files")
            for i in range(int(num_files)):
                filename = request.data.get("name" + str(i))
                _file = request.data.get("file" + str(i))

                if not isinstance(_file, InMemoryUploadedFile) and not isinstance(
                    _file, TemporaryUploadedFile
                ):
                    raise serializers.ValidationError("No files attached")

                document = instance.documents.get_or_create(name=filename)[0]
                path = upload_protected_files_storage.save(
                    update_proposal_compliance_filename(document, filename),
                    ContentFile(_file.read()),
                )
                document._file = path
                document.save()

            return Response(serializer.data)

    @logging_action(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    def save(self, request, *args, **kwargs):
        instance = self.get_object()
        data = {
            "text": request.data.get("detail"),
        }
        serializer = SaveComplianceSerializer(instance, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        num_files = request.data.get("num_files")
        for i in range(int(num_files)):
            filename = request.data.get("name" + str(i))
            _file = request.data.get("file" + str(i))

            if not isinstance(_file, InMemoryUploadedFile):
                raise serializers.ValidationError("No files attached")

            document = instance.documents.get_or_create(name=filename)[0]
            path = upload_protected_files_storage.save(
                update_proposal_compliance_filename(document, filename),
                ContentFile(_file.read()),
            )
            document._file = path
            document.save()

        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def assign_request_user(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.assign_to(request.user.id, request)
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def delete_document(self, request, *args, **kwargs):
        instance = self.get_object()
        doc = request.data.get("document")
        instance.delete_document(request, doc)
        serializer = ComplianceSerializer(instance, context={"request": request})
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def assign_to(self, request, *args, **kwargs):
        instance = self.get_object()
        user_id = request.data.get("user_id", None)
        if not user_id:
            raise serializers.ValiationError("A user id is required")
        instance.assign_to(user_id, request)
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def unassign(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.unassign(request)
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def accept(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.accept(request)
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def amendment_request(self, request, *args, **kwargs):
        instance = self.get_object()
        qs = instance.amendment_requests
        qs = qs.filter(status="requested")
        serializer = CompAmendmentRequestDisplaySerializer(qs, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def action_log(self, request, *args, **kwargs):
        instance = self.get_object()
        qs = instance.action_logs.all()
        serializer = ComplianceActionSerializer(qs, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def comms_log(self, request, *args, **kwargs):
        instance = self.get_object()
        qs = instance.comms_logs.all()
        serializer = ComplianceCommsSerializer(qs, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def add_comms_log(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            mutable = request.data._mutable
            request.data._mutable = True
            request.data["compliance"] = f"{instance.id}"
            request.data["staff"] = f"{request.user.id}"
            request.data._mutable = mutable
            serializer = ComplianceCommsSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            comms = serializer.save()

            # Save the files
            for f in request.FILES.getlist("files"):
                document = comms.documents.create()
                document.name = str(f)
                document._file = f
                document.save()

            return Response(serializer.data)

    @logging_action(methods=["post"], detail=True)
    @basic_exception_handler
    def assessor_send_referral(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = SendReferralSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        instance.send_referral(
            request,
            serializer.validated_data["email"],
            serializer.validated_data["text"],
        )
        serializer = InternalComplianceSerializer(
            instance, context={"request": request}
        )
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def switch_status(self, request, *args, **kwargs):
        instance = self.get_object()
        status = request.data.get("status")
        if not status:
            raise serializers.ValidationError("Status is required")
        else:
            if status not in [
                Compliance.PROCESSING_STATUS_WITH_ASSESSOR,
                Compliance.PROCESSING_STATUS_WITH_REFERRAL,
            ]:
                raise serializers.ValidationError("The status provided is not allowed")
        instance.switch_status(request.user.id, status)
        if is_internal(request):
            serializer = InternalComplianceSerializer(
                instance, context={"request": request}
            )
        else:
            serializer = ComplianceSerializer(instance, context={"request": request})
        return Response(serializer.data)

    @list_route(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def compliance_reminder_days_prior(self, request, *args, **kwargs):
        return Response(settings.COMPLIANCES_DAYS_PRIOR_TO_SEND_REMINDER)


class ComplianceAmendmentRequestViewSet(viewsets.ModelViewSet):
    queryset = ComplianceAmendmentRequest.objects.all()
    serializer_class = ComplianceAmendmentRequestSerializer
    permission_classes = [IsAssessor | HasObjectPermission]

    @basic_exception_handler
    def create(self, request, *args, **kwargs):
        request_data = deepcopy(request.data)
        request_data.update({"officer": request.user.id})
        serializer = self.get_serializer(data=request_data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        instance.generate_amendment(request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class ComplianceAmendmentReasonChoicesView(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    def get(self, request, format=None):
        choices_list = []
        choices = ComplianceAmendmentReason.objects.all()
        if choices:
            for c in choices:
                choices_list.append({"key": c.id, "value": c.reason})
        return Response(choices_list)


class ComplianceReferralViewSet(LicensingViewSet):
    queryset = ComplianceReferral.objects.all()
    serializer_class = ComplianceReferralSerializer
    permission_classes = [IsAssignedComplianceReferee]

    def get_queryset(self):
        if is_internal(self.request):
            return ComplianceReferral.objects.all()
        return super().get_queryset()

    def get_serializer_class(self):
        if self.action == "update" or self.action == "partial_update":
            return UpdateComplianceReferralSerializer
        return super().get_serializer_class()

    @list_route(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def datatable_list(self, request, *args, **kwargs):
        compliance_id = request.GET.get("compliance_id", None)
        qs = self.get_queryset()
        if compliance_id:
            qs = qs.filter(compliance_id=int(compliance_id))
        serializer = ComplianceReferralDatatableSerializer(
            qs, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @detail_route(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def remind(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.remind(request)
        serializer = InternalComplianceSerializer(
            instance.compliance, context={"request": request}
        )
        return Response(serializer.data)

    @detail_route(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def recall(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.recall(request)
        serializer = InternalComplianceSerializer(
            instance.compliance, context={"request": request}
        )
        return Response(serializer.data)

    @detail_route(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def resend(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.resend(request)
        serializer = InternalComplianceSerializer(
            instance.compliance, context={"request": request}
        )
        return Response(serializer.data)

    @detail_route(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    @basic_exception_handler
    def complete(self, request, *args, **kwargs):
        instance = self.get_object()
        update_serializer = UpdateComplianceReferralSerializer(
            instance, data=request.data
        )
        if not update_serializer.is_valid():
            raise serializers.ValidationError(update_serializer.errors)
        instance = update_serializer.save()
        serializer = self.partial_update(request, *args, **kwargs)
        instance.complete(request)
        serializer = InternalComplianceSerializer(
            instance.compliance, context={"request": request}
        )
        return Response(serializer.data)


class ComplianceAssessmentViewSet(LicensingViewSet):
    queryset = ComplianceAssessment.objects.all()
    serializer_class = ComplianceAssessmentSerializer
    permission_classes = [IsAsignedAssessor]
    http_method_names = ["head", "get", "patch"]

    def get_serializer_class(self):
        if self.action == "update" or self.action == "partial_update":
            return UpdateComplianceAssessmentSerializer
        return super().get_serializer_class()
