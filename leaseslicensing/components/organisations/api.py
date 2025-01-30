import logging

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, CharField, IntegerField, Value, When
from django.db.models.functions import Concat
from ledger_api_client.ledger_models import EmailUserRO as EmailUser
from ledger_api_client.managed_models import SystemGroupPermission
from ledger_api_client.utils import (
    create_organisation,
    get_organisation,
    get_search_organisation,
    update_organisation_obj,
)
from rest_framework import serializers, status, views, viewsets
from rest_framework.decorators import action, renderer_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework_datatables.pagination import DatatablesPageNumberPagination

from leaseslicensing.components.main.api import (
    KeyValueListMixin,
    LicensingViewSet,
    NoPaginationListMixin,
    UserActionLoggingViewset,
)
from leaseslicensing.components.main.decorators import (
    basic_exception_handler,
    logging_action,
)
from leaseslicensing.components.main.filters import LedgerDatatablesFilterBackend
from leaseslicensing.components.organisations.models import (  # ledger_organisation,
    Organisation,
    OrganisationAction,
    OrganisationContact,
    OrganisationRequest,
    OrganisationRequestUserAction,
    UserDelegation,
)
from leaseslicensing.components.organisations.serializers import (
    InternalOrganisationCreateSerializer,
    MyOrganisationsSerializer,
    OrganisationActionSerializer,
    OrganisationCheckExistSerializer,
    OrganisationCheckSerializer,
    OrganisationCommsSerializer,
    OrganisationContactAdminCountSerializer,
    OrganisationContactSerializer,
    OrganisationCreateSerializer,
    OrganisationDetailsSerializer,
    OrganisationKeyValueSerializer,
    OrganisationLogEntrySerializer,
    OrganisationPinCheckSerializer,
    OrganisationRequestActionSerializer,
    OrganisationRequestCommsSerializer,
    OrganisationRequestDTSerializer,
    OrganisationRequestLogEntrySerializer,
    OrganisationRequestSerializer,
    OrganisationSerializer,
    OrgUserAcceptSerializer,
)
from leaseslicensing.components.organisations.utils import (
    can_admin_org,
    get_organisation_ids_for_user,
)
from leaseslicensing.components.proposals.api import ProposalRenderer
from leaseslicensing.helpers import is_customer, is_internal

logger = logging.getLogger(__name__)


class OrganisationViewSet(UserActionLoggingViewset, KeyValueListMixin):
    queryset = Organisation.objects.none()
    serializer_class = OrganisationSerializer
    key_value_display_field = "ledger_organisation_name"
    key_value_serializer_class = OrganisationKeyValueSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["head", "get", "post", "put", "patch"]

    def get_serializer_class(self):
        if self.action == "create":
            return InternalOrganisationCreateSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request):
            return Organisation.objects.all()
        elif is_customer(self.request):
            if "organisation_lookup" == self.action:
                # Allow customers access to organisation lookup
                return Organisation.objects.only(
                    "id", "ledger_organisation_name", "ledger_organisation_abn"
                )
            if "validate_pins" == self.action:
                return Organisation.objects.all()
            logger.info(list(Organisation.objects.filter(delegates__user=user.id)))
            return Organisation.objects.filter(
                delegates__user=user.id,
                contacts__user=user.id,
                contacts__user_status=OrganisationContact.USER_STATUS_CHOICE_ACTIVE,
            )

        return Organisation.objects.none()

    def perform_create(self, serializer):
        """Create an organisation in ledger and in leases licensing"""
        name = serializer.validated_data["ledger_organisation_name"]
        trading_name = serializer.validated_data["ledger_organisation_trading_name"]
        abn = serializer.validated_data["ledger_organisation_abn"]
        email = serializer.validated_data["ledger_organisation_email"]

        # Check if this organisation already exists in ledger
        ledger_org = None
        search_organisation_name_response = get_search_organisation(name, None)
        search_organisation_abn_response = get_search_organisation(None, abn)

        if 200 == search_organisation_name_response["status"]:
            data = search_organisation_name_response["data"]
            ledger_org = data[0]

            # Check if this organisation already exists in leases licensing
            org = Organisation.objects.filter(
                ledger_organisation_id=ledger_org["organisation_id"]
            ).first()
            if org:
                msg = (
                    f"An organisation with that name already exists: {org.ledger_organisation_name} "
                    f"(ABN/ACN: {org.ledger_organisation_abn})"
                )
                raise serializers.ValidationError(msg)

        if 200 == search_organisation_abn_response["status"]:
            data = search_organisation_abn_response["data"]
            ledger_org = data[0]

            # Check if this organisation already exists in leases licensing
            org = Organisation.objects.filter(
                ledger_organisation_id=ledger_org["organisation_id"]
            ).first()
            if org:
                msg = (
                    f"An organisation with that abn already exists: {org.ledger_organisation_name} "
                    f"(ABN/ACN: {org.ledger_organisation_abn})"
                )
                raise serializers.ValidationError(msg)

        if not ledger_org:
            # Create this organisation in ledger
            create_organisation(name, abn)
            search_organisation_response = get_search_organisation(name, abn)
            if 200 == search_organisation_response["status"]:
                data = search_organisation_response["data"]
                ledger_org = data[0]

        organisation_dict = dict()
        logger.debug("ledger_org: %s", ledger_org)
        organisation_dict["organisation_id"] = ledger_org["organisation_id"]
        organisation_dict["organisation_email"] = email

        if trading_name:
            organisation_dict["organisation_trading_name"] = trading_name

        # Update the organisation email address (and trading name if provided)
        ledger_org_response = update_organisation_obj(organisation_dict)

        if 200 != ledger_org_response["status"]:
            msg = f"Error updating ledger organisation: {ledger_org['organisation_id']}"
            raise ValidationError(msg)

        # Create the organisation in leases licensing
        org, created = Organisation.objects.get_or_create(
            ledger_organisation_id=ledger_org["organisation_id"]
        )
        logger.debug("type(self.request) ", type(self.request))
        if created:
            logger.info("Created organisation: %s", org)
            org.log_user_action(
                OrganisationAction.ACTION_ADD_NEW_ORGANISATION.format(org), self.request
            )

        serializer.instance = org

    @action(
        methods=[
            "GET",
        ],
        detail=False,
        permission_classes=[IsAuthenticated],
    )
    def organisation_lookup(self, request, *args, **kwargs):
        search_term = request.GET.get("term", "")
        organisations = self.get_queryset().annotate(
            search_term=Concat(
                "ledger_organisation_name",
                Value(" "),
                "ledger_organisation_abn",
                output_field=CharField(),
            )
        )
        organisations = organisations.filter(search_term__icontains=search_term).only(
            "id", "ledger_organisation_name", "ledger_organisation_abn"
        )[:10]
        data_transform = [
            {
                "id": organisation.id,
                "text": f"{organisation.ledger_organisation_name} (ABN: {organisation.ledger_organisation_abn})",
                "first_five": organisation.first_five,
            }
            for organisation in organisations
        ]
        return Response({"results": data_transform})

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def contacts(self, request, *args, **kwargs):
        instance = self.get_object()
        admin_user_count = instance.admin_user_count
        queryset = instance.contacts.exclude(
            user_status=OrganisationContact.USER_STATUS_CHOICE_PENDING
        )
        queryset = queryset.annotate(
            admin_user_count=Value(admin_user_count, output_field=IntegerField())
        )
        serializer = OrganisationContactAdminCountSerializer(queryset, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def contacts_linked(self, request, *args, **kwargs):
        qs = self.get_queryset()
        serializer = OrganisationContactSerializer(qs, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def contacts_exclude(self, request, *args, **kwargs):
        instance = self.get_object()
        admin_user_count = instance.admin_user_count
        qs = instance.contacts.exclude(
            user_status=OrganisationContact.USER_STATUS_CHOICE_DRAFT
        )
        qs = qs.annotate(
            admin_user_count=Value(admin_user_count, output_field=IntegerField())
        )
        serializer = OrganisationContactSerializer(qs, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def validate_pins(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrganisationPinCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ret = instance.validate_pins(
            serializer.validated_data["pin1"],
            serializer.validated_data["pin2"],
            request,
        )

        if ret is None:
            # user has already been to this organisation - don't add again
            return Response({"valid": "User already exists"})

        data = {"valid": ret}
        if data["valid"]:
            # Notify each Admin member of request.
            instance.send_organisation_request_link_notification(request)
        return Response(data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def accept_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.accept_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def accept_declined_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.accept_declined_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def decline_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.decline_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def unlink_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.unlink_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    # No logging action decorator for this one as once the user is unlinked the
    # logging action will throw an exception trying to call get object since the user
    # no longer has permission to do so. The unlinking of the user is logged in the
    # unlink_user method on the organisation instance anyway.
    @action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def unlink_request_user(self, request, *args, **kwargs):
        instance = self.get_object()
        user_obj = EmailUser.objects.get(email=request.user.email.lower())
        instance.unlink_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def make_admin_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.make_admin_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def make_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.make_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def make_consultant(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.make_consultant(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def suspend_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.suspend_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def reinstate_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.reinstate_user(user_obj, request)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def relink_user(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = OrgUserAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_obj = EmailUser.objects.get(
            email=serializer.validated_data["email"].lower()
        )
        instance.relink_user(user_obj, request)
        serializer = self.get_serializer(instance)
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
        serializer = OrganisationActionSerializer(qs, many=True)
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
        serializer = OrganisationCommsSerializer(qs, many=True)
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
            request.data["organisation"] = f"{instance.id}"
            request.data["staff"] = f"{request.user.id}"
            request.data._mutable = mutable
            serializer = OrganisationLogEntrySerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            comms = serializer.save()

            # Save the files
            for f in request.FILES.getlist("files"):
                document = comms.documents.create()
                document.name = str(f)
                document._file = f
                document.save()

            return Response(serializer.data)

    @action(
        methods=[
            "POST",
        ],
        detail=False,
    )
    @basic_exception_handler
    def existence(self, request, *args, **kwargs):
        serializer = OrganisationCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = Organisation.existence(serializer.validated_data["abn"])
        # Check request user cannot be relinked to org.
        data.update([("user", request.user.id)])
        data.update([("abn", request.data["abn"])])
        serializer = OrganisationCheckExistSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PUT",
        ],
        detail=True,
    )
    @basic_exception_handler
    def update_details(self, request, *args, **kwargs):
        instance = self.get_object()
        if not can_admin_org(instance, request.user.id):
            return Response(
                status=status.HTTP_403_FORBIDDEN,
                data={
                    "message": "You do not have permission to update this organisation."
                },
            )

        response_ledger = update_organisation_obj(request.data)
        cache.delete(
            settings.CACHE_KEY_LEDGER_ORGANISATION.format(
                instance.ledger_organisation_id
            )
        )
        serializer = OrganisationDetailsSerializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        return Response(response_ledger)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def update_address(self, request, *args, **kwargs):
        return self.update_details(request, *args, **kwargs)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def get_org_address(self, request, *args, **kwargs):
        instance = self.get_object()
        if not instance.ledger_organisation_id:
            msg = f"Organisation: {instance.id} has no ledger organisation id"
            logger.error(msg)
            raise ValidationError(msg)

        response_ledger = get_organisation(instance.ledger_organisation_id)
        return Response(response_ledger["data"])

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def upload_id(self, request, *args, **kwargs):
        pass


class CreateOrganisationView(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    @transaction.atomic
    def post(self, request, format=None):
        """Create an organisation in ledger and in leases licensing"""
        serializer = OrganisationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["ledger_organisation_name"]
        abn = serializer.validated_data["ledger_organisation_abn"]
        email = serializer.validated_data["ledger_organisation_email"]
        trading_name = serializer.validated_data["ledger_organisation_trading_name"]
        admin_user_id = serializer.validated_data["admin_user_id"]

        # Get the admin user
        admin_user = EmailUser.objects.get(id=admin_user_id)

        # Check if this organisation already exists in ledger
        ledger_org = None
        search_organisation_response = get_search_organisation(name, abn)
        if 200 == search_organisation_response["status"]:
            data = search_organisation_response["data"]
            ledger_org = data[0]

            # Check if this organisation already exists in leases licensing
            org = Organisation.objects.filter(
                ledger_organisation_id=ledger_org["organisation_id"]
            ).first()
            if org:
                msg = (
                    f"An organisation with that name or abn already exists: {org.ledger_organisation_name} "
                    f"(ABN/ACN: {org.ledger_organisation_abn})"
                )
                logger.error(msg)
                raise serializers.ValidationError(msg)

        if not ledger_org:
            # Create this organisation in ledger
            create_organisation(name, abn)
            search_organisation_response = get_search_organisation(name, abn)
            if 200 == search_organisation_response["status"]:
                data = search_organisation_response["data"]
                ledger_org = data[0]

        organisation_dict = dict()
        organisation_dict["organisation_id"] = ledger_org["organisation_id"]
        organisation_dict["organisation_email"] = email

        if trading_name:
            organisation_dict["organisation_trading_name"] = trading_name

        # Update the organisation email address (and trading name if provided)
        ledger_org_response = update_organisation_obj(organisation_dict)

        if 200 != ledger_org_response["status"]:
            msg = f"Error updating ledger organisation: {ledger_org['organisation_id']}"
            logger.error(msg)
            raise ValidationError(msg)

        # Create the organisation in leases licensing
        org, created = Organisation.objects.get_or_create(
            ledger_organisation_id=ledger_org["organisation_id"]
        )
        if created:
            logger.info("Created organisation: %s", org)

        # Add the admin user to the organisation
        UserDelegation.objects.get_or_create(organisation=org, user=admin_user.id)

        # Make sure they are an organisation contact
        organisation_contact, created = OrganisationContact.objects.get_or_create(
            organisation=org,
            user=admin_user.id,
        )
        if created:
            # Make them an active admin user
            organisation_contact.user_status = (
                OrganisationContact.USER_STATUS_CHOICE_ACTIVE
            )
            organisation_contact.user_role = OrganisationContact.USER_ROLE_CHOICE_ADMIN

            # Update their contact details from the ledger email user
            if admin_user.email:
                organisation_contact.email = admin_user.email

            if admin_user.first_name:
                organisation_contact.first_name = admin_user.first_name

            if admin_user.last_name:
                organisation_contact.last_name = admin_user.last_name

            if admin_user.mobile_number:
                organisation_contact.mobile_number = admin_user.mobile_number

            if admin_user.phone_number:
                organisation_contact.phone_number = admin_user.phone_number

            if admin_user.fax_number:
                organisation_contact.fax_number = admin_user.fax_number

            organisation_contact.save()

        serializer = OrganisationSerializer(org)
        return Response(serializer.data)


class OrganisationRequestFilterBackend(LedgerDatatablesFilterBackend):
    """
    Custom filters
    """

    def filter_queryset(self, request, queryset, view):
        total_count = queryset.count()

        filter_organisation = request.GET.get("filter_organisation", None)
        filter_role = request.GET.get("filter_role", None)
        filter_status = request.GET.get("filter_status", None)

        if filter_organisation:
            filter_organisation = int(filter_organisation)
            queryset = queryset.filter(organisation_id=filter_organisation)

        if filter_role:
            queryset = queryset.filter(role=filter_role)

        if filter_status:
            queryset = queryset.filter(status=filter_status)

        queryset = self.apply_request(
            request, queryset, view, ledger_lookup_fields=["requester"]
        )

        setattr(view, "_datatables_filtered_count", queryset.count())
        setattr(view, "_datatables_total_count", total_count)

        return queryset


class OrganisationRequestPaginatedViewSet(viewsets.ReadOnlyModelViewSet):
    filter_backends = (OrganisationRequestFilterBackend,)
    pagination_class = DatatablesPageNumberPagination
    renderer_classes = (ProposalRenderer,)
    page_size = 10
    queryset = OrganisationRequest.objects.all()
    serializer_class = OrganisationRequestSerializer


class OrganisationRequestsViewSet(UserActionLoggingViewset, NoPaginationListMixin):
    queryset = OrganisationRequest.objects.all().order_by(
        Case(
            When(status=OrganisationRequest.STATUS_CHOICE_APPROVED, then=Value(0)),
            When(status=OrganisationRequest.STATUS_CHOICE_WITH_ASSESSOR, then=Value(1)),
            When(status=OrganisationRequest.STATUS_CHOICE_DECLINED, then=Value(2)),
        )
    )
    serializer_class = OrganisationRequestSerializer

    def get_serializer_class(self):
        if "retrieve" == self.action:
            return OrganisationRequestDTSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        if is_internal(self.request):
            return super().get_queryset()
        elif is_customer(self.request):
            return self.queryset.filter(requester=self.request.user.id).filter(
                status__in=[
                    OrganisationRequest.STATUS_CHOICE_APPROVED,
                    OrganisationRequest.STATUS_CHOICE_WITH_ASSESSOR,
                ]
            )
        return OrganisationRequest.objects.none()

    @action(
        methods=[
            "GET",
        ],
        detail=False,
    )
    @basic_exception_handler
    def datatable_list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        serializer = OrganisationRequestDTSerializer(qs, many=True)
        return Response(serializer.data)

    @action(
        methods=[
            "GET",
        ],
        detail=False,
    )
    @basic_exception_handler
    def get_pending_requests(self, request, *args, **kwargs):
        qs = self.get_queryset().filter(requester=request.user, status="with_assessor")
        serializer = OrganisationRequestDTSerializer(qs, many=True)
        return Response(serializer.data)

    @action(
        methods=[
            "GET",
        ],
        detail=False,
    )
    def get_amendment_requested_requests(self, request, *args, **kwargs):
        qs = self.get_queryset().filter(
            requester=request.user, status="amendment_requested"
        )
        serializer = OrganisationRequestDTSerializer(qs, many=True)
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
        serializer = OrganisationRequestDTSerializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    @basic_exception_handler
    def assign_user(self, request, *args, **kwargs):
        instance = self.get_object()
        user_id = request.data.get("user_id")
        logger.info("user_id: %s", user_id)
        instance.assign_to(user_id, request)
        serializer = OrganisationRequestDTSerializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    @basic_exception_handler
    def unassign(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.unassign(request)
        serializer = OrganisationRequestDTSerializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    @basic_exception_handler
    def accept(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.accept(request)
        serializer = OrganisationRequestDTSerializer(instance)
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
        instance.amendment_request(request)
        serializer = OrganisationRequestSerializer(instance)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PUT",
        ],
        detail=True,
    )
    @basic_exception_handler
    def reupload_identification_amendment_request(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.reupload_identification_amendment_request(request)
        serializer = OrganisationRequestSerializer(instance, partial=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "PATCH",
        ],
        detail=True,
    )
    @basic_exception_handler
    def decline(self, request, *args, **kwargs):
        instance = self.get_object()
        reason = ""
        instance.decline(reason, request)
        serializer = OrganisationRequestDTSerializer(instance)
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
        user = None
        if not user_id:
            raise serializers.ValiationError("A user id is required")
        try:
            user = EmailUser.objects.get(id=user_id)
        except EmailUser.DoesNotExist:
            raise serializers.ValidationError(
                "A user with the id passed in does not exist"
            )
        instance.assign_to(user, request)
        serializer = OrganisationRequestSerializer(instance)
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
        serializer = OrganisationRequestActionSerializer(qs, many=True)
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
        serializer = OrganisationRequestCommsSerializer(qs, many=True)
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
            request.data["organisation"] = f"{instance.id}"
            request.data["request"] = f"{instance.id}"
            request.data["staff"] = f"{request.user.id}"
            request.data._mutable = mutable
            serializer = OrganisationRequestLogEntrySerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            comms = serializer.save()

            # Save the files
            for f in request.FILES.getlist("files"):
                document = comms.documents.create()
                document.name = str(f)
                document._file = f
                document.save()

            return Response(serializer.data)

    @basic_exception_handler
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.validated_data["requester"] = request.user.id
        if request.data["role"] == "consultant":
            # Check if consultant can be relinked to org.
            data = Organisation.existence(request.data["abn"])
            data.update([("user", request.user.id)])
            data.update([("abn", request.data["abn"])])
            existing_org = OrganisationCheckExistSerializer(data=data)
            existing_org.is_valid(raise_exception=True)
        with transaction.atomic():
            instance = serializer.save()
            instance.log_user_action(
                OrganisationRequestUserAction.ACTION_LODGE_REQUEST.format(instance.id),
                request,
            )
            instance.send_organisation_request_email_notification(request)
        return Response(serializer.data)


class OrganisationAccessGroupMembers(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    def get(self, request, format=None):
        if not is_internal(request):
            raise PermissionDenied()

        members = []
        permissions = SystemGroupPermission.objects.filter(
            system_group__name=settings.GROUP_NAME_ORGANISATION_ACCESS
        )
        for permission in permissions:
            members.append(
                {
                    "name": permission.emailuser.get_full_name(),
                    "id": permission.emailuser.id,
                }
            )

        return Response(members)


class OrganisationContactFilterBackend(LedgerDatatablesFilterBackend):
    """
    Filters organisation contacts, allowing for full name and email search
    """

    def filter_queryset(self, request, queryset, view):
        total_count = queryset.count()
        admin_user_count = queryset.filter(
            user_role=OrganisationContact.USER_ROLE_CHOICE_ADMIN,
            user_status=OrganisationContact.USER_STATUS_CHOICE_ACTIVE,
        ).count()

        filter_role = request.GET.get("filter_role", None)

        if filter_role:
            queryset = queryset.filter(user_role=filter_role)

        # Apply regular request filters and union the result with the queryset
        queryset = self.apply_request(
            request, queryset, view, ledger_lookup_fields=[]
        ).annotate(
            admin_user_count=Value(admin_user_count, output_field=IntegerField())
        )

        setattr(view, "_datatables_filtered_count", queryset.count())
        setattr(view, "_datatables_total_count", total_count)

        return queryset


class OrganisationContactPaginatedViewSet(viewsets.ReadOnlyModelViewSet):
    filter_backends = (OrganisationContactFilterBackend,)
    pagination_class = DatatablesPageNumberPagination
    renderer_classes = (ProposalRenderer,)
    page_size = 10
    queryset = OrganisationContact.objects.all()
    serializer_class = OrganisationContactAdminCountSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organisation_id = self.request.query_params.get("organisation_id", None)
        if organisation_id:
            queryset = queryset.filter(organisation__id=organisation_id)
        return queryset


class OrganisationContactViewSet(LicensingViewSet):
    http_method_names = ["head", "get", "post", "put", "patch", "delete"]
    serializer_class = OrganisationContactSerializer
    queryset = OrganisationContact.objects.all()
    permiission_classes = []

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request):
            return OrganisationContact.objects.all()
        elif is_customer(self.request):
            user_orgs = get_organisation_ids_for_user(user.id)
            return OrganisationContact.objects.filter(organisation_id__in=user_orgs)
        return OrganisationContact.objects.none()

    def destroy(self, request, *args, **kwargs):
        """delete an Organisation contact"""
        instance = self.get_object()
        admin_user_count = instance.organisation.contacts.filter(
            user_role=OrganisationContact.USER_ROLE_CHOICE_ADMIN,
            user_status=OrganisationContact.USER_STATUS_CHOICE_ACTIVE,
        ).count()
        org_contact = instance.organisation.contacts.get(id=kwargs["pk"])
        if (
            admin_user_count == 1
            and org_contact.user_role == OrganisationContact.USER_ROLE_CHOICE_ADMIN
        ):
            raise serializers.ValidationError(
                "Cannot delete the last Organisation Admin"
            )

        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if "contact_form" in request.data.get("user_status"):
            serializer.save(user_status="contact_form")
        else:
            serializer.save()

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MyOrganisationsViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Organisation.objects.all()
    serializer_class = MyOrganisationsSerializer

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request):
            return Organisation.objects.all()
        elif is_customer(self.request):
            return Organisation.objects.filter(
                contacts__user=user.id,
                contacts__user_status=OrganisationContact.USER_STATUS_CHOICE_ACTIVE,
            )
        return Organisation.objects.none()

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def contacts(self, request, *args, **kwargs):
        instance = self.get_object()
        admin_user_count = instance.admin_user_count
        queryset = instance.contacts.exclude(
            user_status=OrganisationContact.USER_STATUS_CHOICE_PENDING
        )
        queryset = queryset.annotate(
            admin_user_count=Value(admin_user_count, output_field=IntegerField())
        )
        serializer = OrganisationContactAdminCountSerializer(queryset, many=True)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def contacts_exclude(self, request, *args, **kwargs):
        instance = self.get_object()
        admin_user_count = instance.admin_user_count
        qs = instance.contacts.exclude(
            user_status=OrganisationContact.USER_STATUS_CHOICE_DRAFT
        )
        qs = qs.annotate(
            admin_user_count=Value(admin_user_count, output_field=IntegerField())
        )
        serializer = OrganisationContactSerializer(qs, many=True)
        return Response(serializer.data)
