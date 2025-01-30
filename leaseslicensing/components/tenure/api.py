import logging

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from leaseslicensing.components.main.api import (
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
)
from leaseslicensing.components.tenure.models import (
    LGA,
    Act,
    Category,
    District,
    Group,
    Identifier,
    Name,
    Region,
    Tenure,
    Vesting,
)
from leaseslicensing.components.tenure.serializers import (
    ActSerializer,
    CategorySerializer,
    DistrictKeyValueSerializer,
    DistrictSerializer,
    GroupSerializer,
    IdentifierSerializer,
    LGASerializer,
    NameSerializer,
    RegionSerializer,
    TenureSerializer,
    VestingSerializer,
)
from leaseslicensing.permissions import IsAssessor

logger = logging.getLogger(__name__)


class IdentifierViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Identifier
    serializer_class = IdentifierSerializer
    key_value_display_field = "name"
    key_value_serializer_class = IdentifierSerializer
    queryset = Identifier.objects.all()
    permission_classes = [IsAssessor]


class VestingViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Vesting
    serializer_class = VestingSerializer
    key_value_display_field = "name"
    key_value_serializer_class = VestingSerializer
    queryset = Vesting.objects.all()
    permission_classes = [IsAssessor]


class NameViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Name
    serializer_class = NameSerializer
    key_value_display_field = "name"
    key_value_serializer_class = NameSerializer
    queryset = Name.objects.all()
    permission_classes = [IsAssessor]


class ActViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Act
    serializer_class = ActSerializer
    key_value_display_field = "name"
    key_value_serializer_class = ActSerializer
    queryset = Act.objects.all()
    permission_classes = [IsAssessor]


class TenureViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Tenure
    serializer_class = TenureSerializer
    key_value_display_field = "name"
    key_value_serializer_class = TenureSerializer
    queryset = Tenure.objects.all()
    permission_classes = [IsAssessor]


class CategoryViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Category
    serializer_class = CategorySerializer
    key_value_serializer_class = CategorySerializer
    key_value_display_field = "name"
    select2_search_case_sensitive = True
    queryset = Category.objects.all()
    permission_classes = [IsAssessor]


class RegionViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Region
    serializer_class = RegionSerializer
    key_value_display_field = "name"
    key_value_serializer_class = RegionSerializer
    queryset = Region.objects.all()
    permission_classes = [IsAuthenticated]


class DistrictViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = District
    serializer_class = DistrictSerializer
    key_value_serializer_class = DistrictKeyValueSerializer
    key_value_display_field = "name"
    queryset = District.objects.all()
    permission_classes = [IsAuthenticated]


class LGAViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = LGA
    serializer_class = LGASerializer
    key_value_display_field = "name"
    key_value_serializer_class = LGASerializer
    queryset = LGA.objects.all()
    permission_classes = [IsAssessor]


class GroupViewSet(
    viewsets.ReadOnlyModelViewSet,
    KeyValueListMixin,
    NoPaginationListMixin,
    Select2ListMixin,
):
    model = Group
    serializer_class = GroupSerializer
    key_value_display_field = "name"
    key_value_serializer_class = GroupSerializer
    queryset = Group.objects.all()
    permission_classes = [IsAuthenticated]
