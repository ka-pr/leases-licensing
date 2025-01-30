from django.conf import settings
from ledger_api_client.ledger_models import EmailUserRO as EmailUser
from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator

from leaseslicensing.components.main.serializers import CommunicationLogEntrySerializer
from leaseslicensing.components.main.utils import get_secure_document_url
from leaseslicensing.components.organisations.models import (
    Organisation,
    OrganisationAction,
    OrganisationContact,
    OrganisationLogEntry,
    OrganisationRequest,
    OrganisationRequestLogEntry,
    OrganisationRequestUserAction,
    UserDelegation,
)
from leaseslicensing.components.organisations.utils import (
    can_admin_org,
    can_approve,
    can_manage_org,
    can_relink,
    is_consultant,
)
from leaseslicensing.components.users.serializers import ContactSerializer
from leaseslicensing.ledger_api_utils import retrieve_email_user


class OrganisationCheckSerializer(serializers.Serializer):
    # Validation serializer for new Organisations
    abn = serializers.CharField()
    name = serializers.CharField()

    def validate(self, data):
        # Check no admin request pending approval.
        requests = OrganisationRequest.objects.filter(
            abn=data["abn"], role="employee"
        ).exclude(status__in=("declined", "approved"))
        if requests.exists():
            raise serializers.ValidationError(
                "A request has been submitted and is Pending Approval."
            )
        return data


class OrganisationPinCheckSerializer(serializers.Serializer):
    pin1 = serializers.CharField()
    pin2 = serializers.CharField()


class DelegateSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="get_full_name")

    class Meta:
        model = EmailUser
        fields = (
            "id",
            "name",
            "email",
        )


class OrganisationContactSerializer(serializers.ModelSerializer):
    user_status = serializers.SerializerMethodField()
    user_role = serializers.SerializerMethodField()
    full_name = serializers.ReadOnlyField()
    admin_user_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrganisationContact
        fields = "__all__"
        validators = [
            UniqueTogetherValidator(
                queryset=OrganisationContact.objects.all(),
                fields=["organisation", "email"],
                message="This organisation already has a contact with this email address.",
            )
        ]
        datatables_always_serialize = ("admin_user_count",)

    def get_user_status(self, obj):
        return obj.get_user_status_display()

    def get_user_role(self, obj):
        return obj.get_user_role_display()


class OrganisationContactAdminCountSerializer(OrganisationContactSerializer):
    admin_user_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrganisationContact
        fields = "__all__"
        datatables_always_serialize = ("admin_user_count",)


class BasicUserDelegationSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDelegation
        fields = (
            "user_full_name",
            "organisation",
        )


class BasicOrganisationContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganisationContact
        fields = (
            "user",
            "full_name",
            "user_role",
        )


class OrganisationCreateSerializer(serializers.Serializer):
    ledger_organisation_name = serializers.CharField(max_length=255)
    ledger_organisation_trading_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    ledger_organisation_abn = serializers.CharField(min_length=9, max_length=11)
    ledger_organisation_email = serializers.EmailField()
    admin_user_id = serializers.IntegerField()

    class Meta:
        fields = (
            "ledger_organisation_name",
            "ledger_organisation_trading_name",
            "ledger_organisation_abn",
            "ledger_organisation_email",
        )

    def validate_ledger_organisation_abn(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("ABN/ACN must be numeric.")

        if not len(value) == 9 and not len(value) == 11:
            raise serializers.ValidationError("ABN/ACN must be 9 or 11 digits long.")

        return value

    def validate_admin_user_id(self, value):
        try:
            EmailUser.objects.get(id=value)
        except EmailUser.DoesNotExist:
            raise serializers.ValidationError(
                f"The admin user that was selected is invalid. No email user found with id: {value}"
            )
        return value


class InternalOrganisationCreateSerializer(serializers.Serializer):
    ledger_organisation_name = serializers.CharField(max_length=255)
    ledger_organisation_trading_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True
    )
    ledger_organisation_abn = serializers.CharField(min_length=9, max_length=11)
    ledger_organisation_email = serializers.EmailField()

    class Meta:
        model = Organisation
        fields = (
            "ledger_organisation_name",
            "ledger_organisation_trading_name",
            "ledger_organisation_abn",
            "ledger_organisation_email",
        )


class OrganisationSerializer(serializers.ModelSerializer):
    pins = serializers.SerializerMethodField(read_only=True)
    delegates = serializers.SerializerMethodField(read_only=True)
    delegate_organisation_contacts = serializers.ListField(
        child=OrganisationContactSerializer(), read_only=True
    )
    ledger_organisation_name = serializers.CharField(read_only=True)
    contacts = OrganisationContactSerializer(many=True, read_only=True)

    class Meta:
        model = Organisation
        fields = (
            "id",
            "ledger_organisation_id",
            "ledger_organisation_name",
            "ledger_organisation_trading_name",
            "ledger_organisation_abn",
            "ledger_organisation_email",
            "phone_number",
            "pins",
            "delegates",
            "delegate_organisation_contacts",
            "contacts",
            "address",
        )

    def get_trading_name(self, obj):
        return obj.ledger_organisation_name

    def get_pins(self, obj):
        try:
            user = self.context["request"].user
            # Check if the request user is among the first five delegates in the organisation
            if can_manage_org(obj, user):
                return {
                    "one": obj.admin_pin_one,
                    "two": obj.admin_pin_two,
                    "three": obj.user_pin_one,
                    "four": obj.user_pin_two,
                }
            else:
                return None
        except KeyError:
            return None

    def get_delegates(self, obj):
        user_delegate_ids = UserDelegation.objects.filter(organisation=obj).values_list(
            "user", flat=True
        )
        return BasicOrganisationContactSerializer(
            obj.contacts.filter(
                user_status="active",
                user__in=user_delegate_ids,
            ).order_by("user_role", "first_name"),
            many=True,
            read_only=True,
        ).data


class OrganisationDetailsSerializer(serializers.ModelSerializer):
    organisation_name = serializers.CharField(
        source="ledger_organisation_name",
        max_length=255,
        default="",
    )
    organisation_email = serializers.EmailField(
        source="ledger_organisation_email",
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    organisation_trading_name = serializers.CharField(
        source="ledger_organisation_trading_name",
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    class Meta:
        model = Organisation
        fields = (
            "organisation_name",
            "organisation_email",
            "organisation_trading_name",
        )


class OrganisationKeyValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organisation
        fields = ["id", "ledger_organisation_name"]
        read_only_fields = ["id", "ledger_organisation_name"]


class OrganisationCheckExistSerializer(serializers.Serializer):
    # Validation Serializer for existing Organisations
    exists = serializers.BooleanField(default=False)
    id = serializers.IntegerField(default=0)
    first_five = serializers.CharField(allow_blank=True, required=False)
    user = serializers.IntegerField()
    abn = serializers.CharField()

    def validate(self, data):
        user = EmailUser.objects.get(id=data["user"])
        if data["exists"]:
            org = Organisation.objects.get(id=data["id"])
            if can_relink(org, user):
                raise serializers.ValidationError(
                    "Please contact {} to re-link to Organisation.".format(
                        data["first_five"]
                    )
                )
            if can_approve(org, user):
                raise serializers.ValidationError(
                    "Please contact {} to Approve your request.".format(
                        data["first_five"]
                    )
                )
        # Check no consultant request is pending approval for an ABN
        if (
            OrganisationRequest.objects.filter(
                abn=data["abn"], requester=user, role="consultant"
            )
            .exclude(status__in=("declined", "approved"))
            .exists()
        ):
            raise serializers.ValidationError(
                "A request has been submitted and is Pending Approval."
            )
        return data


class MyOrganisationsSerializer(OrganisationSerializer):
    is_admin = serializers.SerializerMethodField(read_only=True)
    is_consultant = serializers.SerializerMethodField(read_only=True)
    user_id = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Organisation
        fields = [
            "id",
            "ledger_organisation_id",
            "ledger_organisation_name",
            "ledger_organisation_trading_name",
            "ledger_organisation_abn",
            "ledger_organisation_email",
            "phone_number",
            "pins",
            "delegates",
            "delegate_organisation_contacts",
            "contacts",
            "address",
            "is_consultant",
            "is_admin",
            "user_id",
        ]

    def get_is_consultant(self, obj):
        user = self.context["request"].user
        return is_consultant(obj, user)

    def get_is_admin(self, obj):
        user = self.context["request"].user
        return can_admin_org(obj, user.id)

    def get_user_id(self, obj):
        user = self.context["request"].user
        return user.id


class OrgRequestRequesterSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = EmailUser
        fields = ("email", "mobile_number", "phone_number", "full_name")

    def get_full_name(self, obj):
        return obj.get_full_name()


class OrganisationRequestSerializer(serializers.ModelSerializer):
    identification = serializers.FileField()
    identification_url = serializers.SerializerMethodField()
    requester_name = serializers.SerializerMethodField(read_only=True)
    lodgement_date = serializers.DateTimeField(format="%d/%m/%Y", read_only=True)
    status = serializers.SerializerMethodField()
    ledger_organisation_name = serializers.SerializerMethodField()
    assigned_officer_name = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()

    class Meta:
        model = OrganisationRequest
        fields = "__all__"
        read_only_fields = [
            "requester",
            "requester_name",
            "lodgement_date",
            "assigned_officer",
        ]

    def get_role(self, obj):
        return obj.get_role_display()

    def get_requester_name(self, obj):
        email_user = EmailUser.objects.filter(id=obj.requester).first()
        if email_user:
            return email_user.get_full_name()
        return None

    def get_status(self, obj):
        return obj.get_status_display()

    def get_ledger_organisation_name(self, obj):
        if not obj.organisation:
            return obj.name
        return obj.organisation.ledger_organisation_name

    def get_assigned_officer_name(self, obj):
        email_user = EmailUser.objects.filter(id=obj.assigned_officer).first()
        if email_user:
            return email_user.get_full_name()
        return None

    def get_identification_url(self, obj):
        if obj.identification:
            return (
                f"/api/main/secure_file/{self.Meta.model._meta.model.__name__}/{obj.id}/identification/",
            )

        return None


class OrganisationRequestDTSerializer(OrganisationRequestSerializer):
    requester = serializers.SerializerMethodField()

    def get_requester(self, obj):
        email_user = retrieve_email_user(obj.requester)
        if email_user:
            return ContactSerializer(email_user).data
        return None


class UserOrganisationSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="organisation.name")
    abn = serializers.CharField(source="organisation.abn")

    class Meta:
        model = Organisation
        fields = ("id", "name", "abn")


class OrganisationRequestActionSerializer(serializers.ModelSerializer):
    who = serializers.SerializerMethodField()

    class Meta:
        model = OrganisationRequestUserAction
        fields = "__all__"

    def get_who(self, obj):
        email_user = EmailUser.objects.filter(id=obj.who).first()
        if email_user:
            return email_user.get_full_name()
        return None


class OrganisationActionSerializer(serializers.ModelSerializer):
    who = serializers.CharField(source="who_full_name")

    class Meta:
        model = OrganisationAction
        fields = "__all__"


class OrganisationRequestCommsSerializer(serializers.ModelSerializer):
    documents = serializers.SerializerMethodField()

    class Meta:
        model = OrganisationRequestLogEntry
        fields = "__all__"

    def get_documents(self, obj):
        return [[d.name, d._file.url] for d in obj.documents.all()]


class OrganisationCommsSerializer(serializers.ModelSerializer):
    documents = serializers.SerializerMethodField()
    document_urls = serializers.SerializerMethodField()

    class Meta:
        model = OrganisationLogEntry
        fields = "__all__"

    def get_documents(self, obj):
        return [[d.name, d._file.url] for d in obj.documents.all()]

    def get_document_urls(self, obj):
        return [
            get_secure_document_url(obj, "documents", d.id) for d in obj.documents.all()
        ]


class OrganisationRequestLogEntrySerializer(CommunicationLogEntrySerializer):
    class Meta:
        model = OrganisationRequestLogEntry
        fields = "__all__"
        read_only_fields = ("customer",)


class OrganisationLogEntrySerializer(CommunicationLogEntrySerializer):
    class Meta:
        model = OrganisationLogEntry
        fields = "__all__"
        read_only_fields = ("customer",)


class OrganisationUnlinkUserSerializer(serializers.Serializer):
    user = serializers.IntegerField()

    def validate(self, obj):
        user = None
        try:
            user = EmailUser.objects.get(id=obj["user"])
            obj["user_obj"] = user
        except EmailUser.DoesNotExist:
            raise serializers.ValidationError(
                "The user you want to unlink does not exist."
            )
        return obj


class OrgUserAcceptSerializer(serializers.Serializer):
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    mobile_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    phone_number = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )

    def validate(self, data):
        # Mobile and phone number for dbca user are updated from active directory
        # so need to skip these users from validation.
        domain = None
        if data["email"]:
            domain = data["email"].split("@")[1]
        if domain in settings.DEPT_DOMAINS:
            return data
        else:
            if not (data["mobile_number"] or data["phone_number"]):
                raise serializers.ValidationError(
                    "User must have an associated phone number or mobile number."
                )
        return data
