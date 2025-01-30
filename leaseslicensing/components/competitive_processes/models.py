import logging

from django.contrib.gis.db.models.fields import PolygonField
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from ledger_api_client.managed_models import SystemGroup

from leaseslicensing import settings
from leaseslicensing.components.competitive_processes.email import (
    send_winner_notification,
)
from leaseslicensing.components.main.models import (
    ApplicationType,
    CommunicationsLogEntry,
    Document,
    LicensingModelVersioned,
    SecureFileField,
    UserAction,
)
from leaseslicensing.components.main.related_item import RelatedItem
from leaseslicensing.components.organisations.models import Organisation
from leaseslicensing.components.tenure.models import (
    LGA,
    Act,
    Category,
    District,
    Group,
    Identifier,
    Name,
    Region,
    SiteName,
    Tenure,
    Vesting,
)
from leaseslicensing.helpers import belongs_to_by_user_id, is_internal
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger("leaseslicensing")


class CompetitiveProcessManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "originating_proposal",
            )
            .prefetch_related(
                "competitive_process_parties",
            )
        )


class CompetitiveProcess(LicensingModelVersioned):
    """A class to represent a competitive process"""

    objects = CompetitiveProcessManager()

    MODEL_PREFIX = "CP"

    # For status
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_IN_PROGRESS_UNLOCKED = "in_progress_unlocked"
    STATUS_DISCARDED = "discarded"
    STATUS_COMPLETED_APPLICATION = "completed_application"
    STATUS_COMPLETED_DECLINED = "completed_declined"
    STATUS_CHOICES = (
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_IN_PROGRESS_UNLOCKED, "In Progress (Unlocked)"),
        (STATUS_DISCARDED, "Discarded"),
        (STATUS_COMPLETED_APPLICATION, "Completed (Application)"),
        (STATUS_COMPLETED_DECLINED, "Completed (Declined)"),
    )

    status = models.CharField(
        "Status",
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_CHOICES[0][0],
    )
    assigned_officer_id = models.IntegerField(null=True, blank=True)  # EmailUserRO
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    modified_at = models.DateTimeField(auto_now=True, null=True)
    site_name = models.ForeignKey(
        SiteName, blank=True, null=True, on_delete=models.PROTECT
    )
    site_comments = models.TextField(blank=True)

    winner = models.ForeignKey(
        "CompetitiveProcessParty", null=True, blank=True, on_delete=models.CASCADE
    )
    details = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Competitive Process"
        verbose_name_plural = "Competitive Processes"
        ordering = ("modified_at",)

    @transaction.atomic
    def create_lease_licence_from_competitive_process(self):
        from leaseslicensing.components.proposals.models import Proposal, ProposalType

        lease_licence = Proposal.objects.create(
            application_type=ApplicationType.objects.get(
                name=settings.APPLICATION_TYPE_LEASE_LICENCE
            ),
            submitter=None,
            ind_applicant=self.winner.person_id,
            org_applicant=self.winner.organisation,
            proposal_type_id=ProposalType.objects.get(
                code=settings.PROPOSAL_TYPE_NEW
            ).id,
        )

        lease_licence.originating_competitive_process = self

        # add geometry
        from copy import deepcopy

        if hasattr(self, "originating_proposal"):
            for geo in self.originating_proposal.proposalgeometry.all():
                new_geo = deepcopy(geo)
                new_geo.proposal = lease_licence
                new_geo.copied_from = geo
                new_geo.id = None
                new_geo.save()

        return lease_licence

    def save(self, *args, **kwargs):
        from leaseslicensing.components.proposals.models import Proposal

        # Get the current winner before saving a potential change to the outcome
        if self.pk:
            current_cp = CompetitiveProcess.objects.get(pk=self.pk)
            current_winner = current_cp.winner
            if (
                current_winner
                and self.status == CompetitiveProcess.STATUS_IN_PROGRESS_UNLOCKED
            ):
                # Get the current winner's proposal
                current_winner_proposal = self.winner_proposal(current_winner.id)

                # If the outcome has changed, discard the current winner's proposal if it exists
                if current_winner_proposal and self.winner != current_winner:
                    current_winner_proposal.processing_status = (
                        Proposal.PROCESSING_STATUS_DISCARDED
                    )
                    current_winner_proposal.save()
                    logger.info(
                        f"Discarded proposal {current_winner_proposal} of previous winner."
                    )

        super().save(*args, **kwargs)

    def discard(self, request):
        if not self.can_user_process(request.user):
            raise ValidationError(
                "You do not have permission to discard this competitive process."
            )
        self.status = CompetitiveProcess.STATUS_DISCARDED
        self.save(version_comment=f"Discarded competitive process {self.pk}")

    def complete(self, request):
        if self.winner:
            self.status = CompetitiveProcess.STATUS_COMPLETED_APPLICATION

            # Only create a license proposal if there is no proposal for the winner yet
            if not self.winner_proposal():
                # 1. Create proposal for the winner
                lease_licence = self.create_lease_licence_from_competitive_process()

                self.generated_proposal.add(lease_licence)

                # 2. Send email to the winner
                send_winner_notification(request, self, lease_licence)
            else:
                logger.info(
                    f"Competitive Process {self.pk} completed, but there is already a winner's proposal."
                )
        else:
            self.status = CompetitiveProcess.STATUS_COMPLETED_DECLINED
        self.save(version_comment=f"Completed competitive process {self.pk}")

    def unlock(self, request):
        """
        Unlocks the competitive process and makes it available for editing again.
        Unlock action changes status to `In Progress (Unlocked)`, allowing to change
        the Outcome.
        The outcome can only be changed if the proposal of the previous winner
        has not been approved yet.
        Changing the outcome will discard the the previous winner's proposal when
        the competitive process is next saved, which allows to select another
        winner (or no winner).
        """

        from leaseslicensing.components.proposals.models import Proposal

        generated_proposal = self.winner_proposal()

        # Cannot unlock if the proposal has been approved
        if generated_proposal and generated_proposal.processing_status in [
            Proposal.PROCESSING_STATUS_APPROVED_REGISTRATION_OF_INTEREST,
            Proposal.PROCESSING_STATUS_APPROVED_EDITING_INVOICING,
            Proposal.PROCESSING_STATUS_APPROVED,
        ]:
            raise ValidationError("The generated proposal has already been approved.")

        with transaction.atomic():
            # Unlock the competitive process geometries (not those from the originating proposal)
            self.competitive_process_geometries.all().update(locked=False)

            # Set the status of the competitive process to in progress
            self.status = CompetitiveProcess.STATUS_IN_PROGRESS_UNLOCKED
            self.save(version_comment=f"Unlocked competitive process {self.pk}")

    @property
    def site(self):
        return "site_name"

    @property
    def group(self):
        return "group_name"

    @property
    def generated_from_registration_of_interest(self):
        if hasattr(self, "originating_proposal"):
            if self.originating_proposal:
                return True
        return False

    @property
    def is_assigned(self):
        if self.assigned_officer_id:
            return True
        return False

    @property
    def assigned_officer(self):
        if self.is_assigned:
            return retrieve_email_user(self.assigned_officer_id)
        return None

    def get_related_items(self, **kwargs):
        return_list = []
        # count = 0
        # field_competitive_process = None
        related_field_names = [
            "originating_proposal",
            "generated_proposal",
        ]
        all_fields = self._meta.get_fields()
        for a_field in all_fields:
            if a_field.name in related_field_names:
                field_objects = []
                if a_field.is_relation:
                    if a_field.many_to_many:
                        pass
                    elif a_field.many_to_one:  # foreign key
                        field_objects = [
                            getattr(self, a_field.name),
                        ]
                    elif a_field.one_to_many:  # reverse foreign key
                        field_objects = a_field.related_model.objects.filter(
                            **{a_field.remote_field.name: self}
                        )
                    elif a_field.one_to_one:
                        if hasattr(self, a_field.name):
                            field_objects = [
                                getattr(self, a_field.name),
                            ]
                for field_object in field_objects:
                    if field_object:
                        related_item = field_object.as_related_item
                        return_list.append(related_item)

        # serializer = RelatedItemsSerializer(return_list, many=True)
        # return serializer.data
        return return_list

    @property
    def as_related_item(self):
        related_item = RelatedItem(
            identifier=self.related_item_identifier,
            model_name=self._meta.verbose_name,
            descriptor=self.related_item_descriptor,
            action_url=f'<a href=/internal/competitive_process/{self.id} target="_blank">Open</a>',
            type="competitive_process",
        )
        return related_item

    @property
    def related_item_identifier(self):
        return self.lodgement_number

    @property
    def related_item_descriptor(self):
        """
        Returns this competitive process' status as item description
        """

        return self.status

    def can_user_view(self, request):
        return is_internal(request)

    def can_user_process(self, user):
        return self.status not in [
            CompetitiveProcess.STATUS_DISCARDED,
            CompetitiveProcess.STATUS_COMPLETED_DECLINED,
            CompetitiveProcess.STATUS_COMPLETED_APPLICATION,
        ] and self.is_user_competitive_process_editor(user.id)

    def can_user_unlock(self, user):
        return self.status in [
            CompetitiveProcess.STATUS_COMPLETED_DECLINED,
            CompetitiveProcess.STATUS_COMPLETED_APPLICATION,
        ] and self.is_user_competitive_process_editor(user.id)

    def is_user_competitive_process_editor(self, user_id):
        return belongs_to_by_user_id(user_id, settings.GROUP_COMPETITIVE_PROCESS_EDITOR)

    def assign_to(self, user_id, request):
        with transaction.atomic():
            self.assigned_officer_id = user_id
            self.save()

    def unassign(self, request):
        with transaction.atomic():
            self.assigned_officer_id = None
            self.save()

    def log_user_action(self, action, request):
        return CompetitiveProcessUserAction.log_action(self, action, request.user.id)

    @property
    def allowed_editors(self):
        group = None
        if self.status in [
            CompetitiveProcess.STATUS_IN_PROGRESS,
            CompetitiveProcess.STATUS_IN_PROGRESS_UNLOCKED,
        ]:
            group = SystemGroup.objects.get(
                name=settings.GROUP_COMPETITIVE_PROCESS_EDITOR
            )

        users = (
            list(
                map(
                    lambda id: retrieve_email_user(id),
                    group.get_system_group_member_ids(),
                )
            )
            if group
            else []
        )
        return users

    def winner_proposal(self, winner_id=None):
        """
        Returns the generated proposal for the winning party or None if there is no winning party.
        Allows to pass a winner_id to get the generated proposal for a specific party.
        """

        from leaseslicensing.components.proposals.models import Proposal

        if not winner_id:
            winner_id = self.winner_id

        # Get the winning party
        try:
            winning_party = (
                CompetitiveProcessParty.objects.get(pk=winner_id) if winner_id else None
            )
        except CompetitiveProcessParty.DoesNotExist:
            logger.warning(f"Winning party {winner_id} does not exist.")
            winning_party = None

        # Get the generated proposals for the winning party or an empty Proposal queryset
        # when there is no winning party
        if not winning_party:
            generated_proposals = Proposal.objects.none()
        elif winning_party.is_person:
            generated_proposals = self.generated_proposal.filter(
                ind_applicant=winning_party.person_id
            )
        elif winning_party.is_organisation:
            generated_proposals = self.generated_proposal.filter(
                org_applicant=winning_party.organisation_id
            )
        else:
            raise ValidationError(
                "Winning party is neither a person nor an organisation."
            )

        # May happen if the competitive process has a selected winner but has been discarded,
        # i.e. no proposal has been generated
        if winning_party and len(generated_proposals) == 0:
            logger.warning("No generated proposals found for the winning party.")

        # Get the generated lease/license proposal / generated proposal
        # that is still active (not discarded)
        generated_proposal = generated_proposals.filter(
            ~Q(processing_status=Proposal.PROCESSING_STATUS_DISCARDED)
        )
        if len(generated_proposal) > 1:
            raise ValidationError(
                f"""There are more than one proposals that have not been discarded for the winning party: """
                f"""{", ".join([p.lodgement_number for p in generated_proposal])}."""
            )
        # There might be no valid application, because the applicant or an officer
        # might have discarded the application
        if not generated_proposal.exists():
            generated_proposal = None
        else:
            generated_proposal = generated_proposal.first()

        return generated_proposal

    @classmethod
    @transaction.atomic
    def purge_competitive_processes(cls):
        """Purge multiple competitive processes and all related items"""
        if not settings.DEBUG:
            raise ValidationError(
                "Purging competitive processes is only allowed in DEBUG mode."
            )
        cls.objects.all().delete()


class CompetitiveProcessGeometry(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess,
        on_delete=models.CASCADE,
        related_name="competitive_process_geometries",
    )
    polygon = PolygonField(srid=4326, blank=True, null=True)
    intersects = models.BooleanField(default=False)
    drawn_by = models.IntegerField(blank=True, null=True)  # EmailUserRO
    source_type = models.CharField(
        max_length=255, blank=True, choices=settings.SOURCE_CHOICES
    )
    source_name = models.CharField(max_length=255, blank=True)
    locked = models.BooleanField(default=False)

    class Meta:
        app_label = "leaseslicensing"


def update_competitive_process_doc_filename(instance, filename):
    return "competitive_process_documents/{}/{}".format(
        instance.id,
        filename,
    )


class CompetitiveProcessDocument(Document):
    competitive_process = models.ForeignKey(
        CompetitiveProcess,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="competitive_process_documents",
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    _file = SecureFileField(
        upload_to=update_competitive_process_doc_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class CompetitiveProcessParty(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="competitive_process_parties",
    )
    person_id = models.IntegerField(null=True, blank=True)  # EmailUserRO
    organisation = models.ForeignKey(
        Organisation,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )
    invited_at = models.DateField(null=True, blank=True)
    removed_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    modified_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ["invited_at"]
        constraints = [
            models.CheckConstraint(
                # Either party_person or party_organisation must be None
                check=Q(person_id=None, organisation__isnull=False)
                | Q(person_id__isnull=False, organisation=None),
                name="either_one_null",
            )
        ]

    def __str__(self):
        if self.person_id:
            return f"Person: {self.person} is a party to Competitive Process: {self.competitive_process}"
        return f"Organisation: {self.organisation} is a party to Competitive Process: {self.competitive_process}"

    @property
    def is_person(self):
        return bool(self.person_id)

    @property
    def person(self):
        if not self.person_id:
            return None
        return retrieve_email_user(self.person_id)

    @property
    def is_organisation(self):
        return bool(self.organisation)

    @property
    def email_address(self):
        if self.is_person:
            return self.person.email
        else:
            return self.organisation.ledger_organisation_email


class PartyDetail(models.Model):
    competitive_process_party = models.ForeignKey(
        CompetitiveProcessParty,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="party_details",
    )
    detail = models.TextField(blank=True)
    created_by_id = models.IntegerField(null=True, blank=True)  # EmailUserRO
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    modified_at = models.DateTimeField(auto_now=True, null=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ["created_at"]

    @property
    def created_by(self):
        if self.created_by_id:
            person = retrieve_email_user(self.created_by_id)
            return person
        return None


def update_party_detail_doc_filename(instance, filename):
    return f"/party_detail_documents/{instance.id}/{filename}"


class PartyDetailDocument(Document):
    party_detail = models.ForeignKey(
        PartyDetail,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="party_detail_documents",
    )
    _file = SecureFileField(upload_to=update_party_detail_doc_filename, max_length=512)

    class Meta:
        app_label = "leaseslicensing"

    @property
    def secure_url(self):
        from leaseslicensing.components.main.utils import get_secure_file_url

        return get_secure_file_url(self, "_file")


def update_competitive_process_comms_log_filename(instance, filename):
    return "{}/competitive_process/{}/communications/{}".format(
        settings.MEDIA_APP_DIR, instance.log_entry.competitive_process.id, filename
    )


class CompetitiveProcessLogDocument(Document):
    log_entry = models.ForeignKey(
        "CompetitiveProcessLogEntry", related_name="documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=update_competitive_process_comms_log_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class CompetitiveProcessLogEntry(CommunicationsLogEntry):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, related_name="comms_logs", on_delete=models.CASCADE
    )

    def __str__(self):
        return f"{self.reference} - {self.subject}"

    class Meta:
        app_label = "leaseslicensing"

    def save(self, **kwargs):
        # save the competitive process id if the reference not provided
        if not self.reference:
            self.reference = self.competitive_process.id
        super().save(**kwargs)


class CompetitiveProcessUserAction(UserAction):
    ACTION_CREATE_CUSTOMER_ = "Create customer {}"
    ACTION_ASSIGN_TO = "Assign to {}"
    ACTION_UNASSIGN = "Unassign"

    competitive_process = models.ForeignKey(
        CompetitiveProcess, related_name="action_logs", on_delete=models.CASCADE
    )

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-when",)

    @classmethod
    def log_action(cls, competitive_process, action, user):
        return cls.objects.create(
            competitive_process=competitive_process, who=user, what=str(action)
        )


class CompetitiveProcessGroup(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="groups"
    )
    group = models.ForeignKey(Group, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "group")

    def __str__(self):
        return f"Competitive Process: {self.competitive_process.lodgement_number} is in Group: {self.group}"


class CompetitiveProcessIdentifier(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="identifiers"
    )
    identifier = models.ForeignKey(Identifier, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "identifier")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land covered by legal act: {self.identifier}"
        )


class CompetitiveProcessVesting(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="vestings"
    )
    vesting = models.ForeignKey(
        Vesting, on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "vesting")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land covered by Vesting: {self.vesting}"
        )


class CompetitiveProcessName(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="names"
    )
    name = models.ForeignKey(Name, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "name")

    def __str__(self):
        return f"Competitive Process: {self.competitive_process.lodgement_number} includes land named: {self.name}"


class CompetitiveProcessAct(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="acts"
    )
    act = models.ForeignKey(Act, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "act")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land covered by legal act: {self.act}"
        )


class CompetitiveProcessTenure(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="tenures"
    )
    tenure = models.ForeignKey(Tenure, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "tenure")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land of tenure: {self.tenure}"
        )


class CompetitiveProcessCategory(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="categories"
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "category")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land categorised as: {self.category}"
        )


class CompetitiveProcessRegion(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="regions"
    )
    region = models.ForeignKey(Region, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "region")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land located in Region: {self.region}"
        )


class CompetitiveProcessDistrict(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="districts"
    )
    district = models.ForeignKey(
        District, on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "district")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land located in District: {self.district}"
        )


class CompetitiveProcessLGA(models.Model):
    competitive_process = models.ForeignKey(
        CompetitiveProcess, on_delete=models.CASCADE, related_name="lgas"
    )
    lga = models.ForeignKey(LGA, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("competitive_process", "lga")

    def __str__(self):
        return (
            f"Competitive Process: {self.competitive_process.lodgement_number} "
            f"includes land located in LGA: {self.lga}"
        )


class CPShapefileDocumentQueryset(models.QuerySet):
    """Using a custom manager to make sure shapfiles are removed when a bulk .delete is called
    as having multiple files with the shapefile extensions in the same folder causes issues.
    """

    def delete(self):
        for obj in self:
            obj._file.delete()
        super().delete()


class CPShapefileDocument(Document):
    objects = CPShapefileDocumentQueryset.as_manager()
    competitive_process = models.ForeignKey(
        CompetitiveProcess, related_name="shapefile_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=update_competitive_process_doc_filename, max_length=500
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted
    can_hide = models.BooleanField(
        default=False
    )  # after initial submit, document cannot be deleted but can be hidden
    hidden = models.BooleanField(
        default=False
    )  # after initial submit prevent document from being deleted

    class Meta:
        app_label = "leaseslicensing"
