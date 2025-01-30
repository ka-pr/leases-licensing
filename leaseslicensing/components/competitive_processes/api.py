import logging
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, F, Q, Value
from rest_framework import views
from rest_framework.decorators import action as detail_route
from rest_framework.decorators import renderer_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework_datatables.pagination import DatatablesPageNumberPagination
from rest_framework_datatables.renderers import DatatablesRenderer

from leaseslicensing.components.competitive_processes.email import (
    send_competitive_process_create_notification,
)
from leaseslicensing.components.competitive_processes.models import CompetitiveProcess
from leaseslicensing.components.competitive_processes.serializers import (
    CompetitiveProcessLogEntrySerializer,
    CompetitiveProcessSerializer,
    CompetitiveProcessUserActionSerializer,
    ListCompetitiveProcessSerializer,
)
from leaseslicensing.components.main.api import (
    Select2ListMixin,
    UserActionLoggingViewset,
)
from leaseslicensing.components.main.decorators import (
    basic_exception_handler,
    logging_action,
)
from leaseslicensing.components.main.filters import LedgerDatatablesFilterBackend
from leaseslicensing.components.main.process_document import process_generic_document
from leaseslicensing.components.main.serializers import RelatedItemSerializer
from leaseslicensing.components.main.utils import (
    populate_gis_data,
    save_geometry,
    save_groups_data,
    save_site_name,
    validate_map_files,
)
from leaseslicensing.components.proposals.models import Proposal
from leaseslicensing.helpers import is_internal
from leaseslicensing.permissions import IsCompetitiveProcessEditor

logger = logging.getLogger("leaseslicensing")


class CompetitiveProcessFilterBackend(LedgerDatatablesFilterBackend):
    def filter_queryset(self, request, queryset, view):
        total_count = queryset.count()
        filter_status = (
            request.GET.get("filter_status")
            if request.GET.get("filter_status") != "all"
            else ""
        )
        filter_competitive_process_created_from = request.GET.get(
            "filter_competitive_process_created_from"
        )
        filter_competitive_process_created_to = request.GET.get(
            "filter_competitive_process_created_to"
        )

        if filter_status:
            queryset = queryset.filter(status=filter_status)
        if filter_competitive_process_created_from:
            filter_competitive_process_created_from = datetime.strptime(
                filter_competitive_process_created_from, "%Y-%m-%d"
            )
            queryset = queryset.filter(
                created_at__gte=filter_competitive_process_created_from
            )
        if filter_competitive_process_created_to:
            filter_competitive_process_created_to = datetime.strptime(
                filter_competitive_process_created_to, "%Y-%m-%d"
            )
            queryset = queryset.filter(
                created_at__lte=filter_competitive_process_created_to
            )

        queryset = self.apply_request(
            request, queryset, view, ledger_lookup_fields=["assigned_officer_id"]
        )

        setattr(view, "_datatables_filtered_count", queryset.count())
        setattr(view, "_datatables_total_count", total_count)

        return queryset


class CompetitiveProcessViewSet(UserActionLoggingViewset, Select2ListMixin):
    queryset = CompetitiveProcess.objects.none()
    filter_backends = (CompetitiveProcessFilterBackend,)
    lookup_field = "id"
    key_value_display_field = "lodgement_number"
    permission_classes = [IsCompetitiveProcessEditor]

    def perform_create(self, serializer):
        """
        Send notification emails on Competitive Process creation
        """

        instance = serializer.save()
        send_competitive_process_create_notification(self.request, instance)

    def get_serializer_class(self):
        """Configure serializers to use"""
        if self.action == "list":
            return ListCompetitiveProcessSerializer
        return CompetitiveProcessSerializer

    @basic_exception_handler
    def get_queryset(self):
        if is_internal(self.request):
            queryset = CompetitiveProcess.objects.all()
            if self.action == "select2_list":
                # Make sure only competitive processes that are in progress are returned
                # for the select 2 list
                queryset = queryset.filter(status=CompetitiveProcess.STATUS_IN_PROGRESS)
        else:
            queryset = CompetitiveProcess.objects.none()
        return queryset

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def complete(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        instance.complete(request)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def discard(self, request, *args, **kwargs):
        instance = self.get_object()
        if len(request.data):
            # If there is data in the request, update the instance before discarding
            # this is done when the user is discarding a competitive process from the details
            # page, if the user is discarding from the datatables page, there is no need to update
            # the instance before discarding
            serializer = self.get_serializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
        else:
            serializer = self.get_serializer(instance)

        instance.discard(request)
        return Response(serializer.data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def unlock(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.unlock(request)
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)

    @detail_route(methods=["POST"], detail=True)
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def process_shapefile_document(self, request, *args, **kwargs):
        instance = self.get_object()
        returned_data = process_generic_document(
            request, instance, document_type="shapefile_document"
        )
        if returned_data:
            return Response(returned_data)
        else:
            return Response()

    @detail_route(methods=["post"], detail=True)
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def validate_map_files(self, request, *args, **kwargs):
        instance = self.get_object()
        valid_geometry_saved = validate_map_files(
            request, instance, "competitive_process"
        )
        instance.save()
        if valid_geometry_saved:
            populate_gis_data(
                instance, "competitive_process_geometries", "competitive_process"
            )
        serializer = self.get_serializer(instance)
        logger.debug(f"validate_map_files response: {serializer.data}")
        return Response(serializer.data)

    @basic_exception_handler
    def perform_update(self, serializer):
        instance = serializer.save()
        request = self.request
        competitive_process_data = request.data
        # Pop "geometry" data to handle it independently of the "competitive process"
        competitive_process_geometry_data = request.data.get(
            "competitive_process_geometries", None
        )

        winner_id = competitive_process_data.get("winner_id", None)
        if winner_id != instance.winner_id:
            # Set the winner to the new winner_id
            logger.info(
                f"Setting winner_id to {winner_id} for Competitive Process: {instance.lodgement_number}"
            )
            instance.winner_id = winner_id
            instance.save()

        # Deal with nested data
        save_site_name(instance, competitive_process_data["site_name"])
        save_groups_data(
            instance,
            competitive_process_data["groups"],
            foreign_key_field="competitive_process",
        )

        # Handle "geometry" data
        if competitive_process_geometry_data:
            save_geometry(
                request,
                instance,
                "competitive_processes",
                competitive_process_geometry_data,
                foreign_key_field="competitive_process",
                source_type=settings.SOURCE_CHOICE_COMPETITIVE_PROCESS_EDITOR,
            )

            populate_gis_data(
                instance,
                "competitive_process_geometries",
                foreign_key_field="competitive_process",
            )

    @detail_route(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def action_log(self, request, *args, **kwargs):
        instance = self.get_object()
        qs = instance.action_logs.all()
        serializer = CompetitiveProcessUserActionSerializer(qs, many=True)
        return Response(serializer.data)

    @detail_route(
        methods=[
            "GET",
        ],
        detail=True,
    )
    @basic_exception_handler
    def comms_log(self, request, *args, **kwargs):
        instance = self.get_object()
        qs = instance.comms_logs.all()
        serializer = CompetitiveProcessLogEntrySerializer(qs, many=True)
        return Response(serializer.data)

    @detail_route(
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
            request.data["proposal"] = f"{instance.id}"
            request.data["staff"] = f"{request.user.id}"
            request.data._mutable = mutable
            serializer = CompetitiveProcessLogEntrySerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            comms = serializer.save()

            # Save the files
            for f in request.FILES.getlist("files"):
                document = comms.documents.create()
                document.name = str(f)
                document._file = f
                document.save()

            return Response(serializer.data)

    @detail_route(methods=["POST"], detail=True)
    @renderer_classes((JSONRenderer,))
    @basic_exception_handler
    def process_competitive_process_document(self, request, *args, **kwargs):
        instance = self.get_object()
        returned_data = process_generic_document(
            request, instance, document_type="competitive_process_document"
        )
        if returned_data:
            return Response(returned_data)
        else:
            return Response()

    @detail_route(
        methods=["GET"],
        detail=True,
        renderer_classes=[DatatablesRenderer],
        pagination_class=DatatablesPageNumberPagination,
    )
    def related_items(self, request, *args, **kwargs):
        """Uses union to combine a queryset of multiple different model types
        and uses a generic related item serializer to return the data"""
        instance = self.get_object()
        if hasattr(instance, "originating_proposal"):
            queryset = (
                Proposal.objects.annotate(
                    description=F("processing_status"),
                    type=Value("competitiveprocess", output_field=CharField()),
                )
                .filter(
                    Q(id=instance.originating_proposal.id)
                    | Q(id__in=instance.generated_proposal.values_list("id", flat=True))
                    | Q(
                        processing_status=Proposal.PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS,
                        competitive_process_to_copy_to_id=instance.id,
                    )
                )
                .values("id", "lodgement_number", "description", "type")
            )
        else:
            queryset = (
                Proposal.objects.annotate(
                    description=F("processing_status"),
                    type=Value("competitiveprocess", output_field=CharField()),
                )
                .filter(
                    Q(id__in=instance.generated_proposal.values_list("id", flat=True))
                    | Q(
                        processing_status=Proposal.PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS,
                        competitive_process_to_copy_to_id=instance.id,
                    )
                )
                .values("id", "lodgement_number", "description", "type")
            )
        serializer = RelatedItemSerializer(queryset, many=True)
        data = {}
        # Add the fields that the datatables renderer expects
        data["data"] = serializer.data
        data["recordsFiltered"] = queryset.count()
        data["recordsTotal"] = queryset.count()
        return Response(data=data)

    @logging_action(
        methods=[
            "POST",
        ],
        detail=True,
    )
    @basic_exception_handler
    def assign_user(self, request, *args, **kwargs):
        instance = self.get_object()
        assigned_officer = request.data.get("assigned_officer", None)
        assigned_officer_id = (
            assigned_officer.get("id", None) if assigned_officer else None
        )
        instance.assign_to(assigned_officer_id, request)
        serializer = self.get_serializer(instance)
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
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


class GetCompetitiveProcessStatusesDict(views.APIView):
    renderer_classes = [
        JSONRenderer,
    ]

    def get(self, request, format=None):
        data = [{"id": i[0], "text": i[1]} for i in CompetitiveProcess.STATUS_CHOICES]
        return Response(data)
