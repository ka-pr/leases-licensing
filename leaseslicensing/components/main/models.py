import logging
import os

from django.core.files.storage import FileSystemStorage
from django.db import models
from django.db.models import Q
from django.forms import ValidationError
from django_countries.fields import CountryField
from ledger_api_client.ledger_models import EmailUserRO as EmailUser
from reversion.models import Version

from leaseslicensing import settings
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger(__name__)


class RevisionedMixin(models.Model):
    """
    A model tracked by reversion through the save method.
    """

    def save(self, **kwargs):
        from reversion import revisions

        if kwargs.pop("no_revision", False):
            super().save(**kwargs)
        else:
            with revisions.create_revision():
                if "version_user" in kwargs:
                    revisions.set_user(kwargs.pop("version_user", None))
                if "version_comment" in kwargs:
                    # Increment the lodgement sequence on every save with a version comment.
                    # Versions are only commented on concluding saves (e.g. on submit),
                    # typically when the status changes, i.e. the first save of a new status
                    # is also the first version to use the incremented sequence.
                    if hasattr(self, "lodgement_sequence"):
                        self.lodgement_sequence += 1
                    revisions.set_comment(kwargs.pop("version_comment", ""))
                super().save(**kwargs)

    def reverse_fk_versions(self, reverse_attr, **kwargs):
        """
        Returns a list of a model's one-to-many foreign key relation versions
        selected by filter expression. E.g. because Proposal is a property of
        ProposalGeometry, there is a 1:N relation Proposal -> ProposalGeometry
        and a reverse foreign key lookup of proposal geometries would always
        return all geometries belonging to a proposal id even though some did
        not exist at a specific version of the proposal.
        This function returns only those versions that existed a certain revision
        id.

        Args:
            reverse_attr (str):
                The attribute in the model to query for
            lookup (dict, optional):
                The filter expression to apply, e.g. `{'__lte':1234}`,
                Defaults to empty dict `{}`, i.e. no filter applies.
                Does not get used when `lookup_filter=` is used
            lookup_filter (Q-expression, optional):
                A Q-expression to filter `reverse_attr` queryset

        Examples:
            - geometry_versions = model_instance.reverse_fk_versions(
                "proposalgeometry",
                lookup={"__lte": 1234})
            - geometry_versions = model_instance.reverse_fk_versions(
                "proposalgeometry",
                lookup_filter=Q(revision_id__lte=1234)), i.e. can
                add negation and more complex expressions
        """

        # How to filter the revision table
        lookup = kwargs.get("lookup", {})
        lookup_filter = kwargs.get("lookup_filter", None)
        if not lookup_filter:
            # The lookup filter to apply
            lookup_filter = Q(
                **{f"revision_id{k}": f"{v}" for k, v in iter(lookup.items())}
            )

        # Reverse foreign key queryset
        if hasattr(self, reverse_attr):
            reverse_fk_qs = getattr(self, reverse_attr).all()
        else:
            raise ValidationError(
                f"{self.__class__.__name__} has no attribute {reverse_attr}"
            )

        # A list of filtered attribute versions
        rfk_versions = []
        for obj in reverse_fk_qs:
            version = [
                p
                for p in Version.objects.get_for_object(obj)
                .select_related("revision")
                .filter(lookup_filter)
            ]
            rfk_versions += version

        return list(set(rfk_versions))

    @property
    def created_date(self):
        return Version.objects.get_for_object(self).last().revision.date_created

    @property
    def modified_date(self):
        return Version.objects.get_for_object(self).first().revision.date_created

    class Meta:
        abstract = True


class ApplicationType(models.Model):
    name = models.CharField(
        max_length=64, unique=True, choices=settings.APPLICATION_TYPES
    )
    order = models.PositiveSmallIntegerField(default=0)
    visible = models.BooleanField(default=True)
    is_gst_exempt = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "name"]
        app_label = "leaseslicensing"
        verbose_name = "Proposal Category"
        verbose_name_plural = "Proposal Categories"

    @staticmethod
    def get_application_type_by_name(name):
        try:
            return ApplicationType.objects.get(name=name)
        except ApplicationType.DoesNotExist:
            return None

    @property
    def name_display(self):
        return self.get_name_display()

    @property
    def confirmation_text(self):
        text = ""
        if self.name == "registration_of_interest":
            text = "Registration of Interest"
        if self.name == "lease_licence":
            text = "Lease or Licence"
        return text

    def __str__(self):
        return self.name


# @python_2_unicode_compatible
class Question(models.Model):
    CORRECT_ANSWER_CHOICES = (
        ("answer_one", "Answer one"),
        ("answer_two", "Answer two"),
        ("answer_three", "Answer three"),
        ("answer_four", "Answer four"),
    )
    question_text = models.TextField(blank=False)
    answer_one = models.CharField(max_length=200, blank=True)
    answer_two = models.CharField(max_length=200, blank=True)
    answer_three = models.CharField(max_length=200, blank=True)
    answer_four = models.CharField(max_length=200, blank=True)
    # answer_five = models.CharField(max_length=200, blank=True)
    correct_answer = models.CharField(
        "Correct Answer",
        max_length=40,
        choices=CORRECT_ANSWER_CHOICES,
        default=CORRECT_ANSWER_CHOICES[0][0],
    )
    application_type = models.ForeignKey(
        ApplicationType, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        # ordering = ['name']
        app_label = "leaseslicensing"

    def __str__(self):
        return self.question_text

    @property
    def correct_answer_value(self):
        return getattr(self, self.correct_answer)


# @python_2_unicode_compatible
class UserAction(models.Model):
    who = models.IntegerField()  # EmailUserRO
    who_full_name = models.CharField(max_length=200, default="")
    when = models.DateTimeField(null=False, blank=False, auto_now_add=True)
    what = models.TextField(blank=False)

    def __str__(self):
        return "{what} ({who} at {when})".format(
            what=self.what, who=self.who, when=self.when
        )

    def save(self, *args, **kwargs):
        if not self.who_full_name:
            email_user = retrieve_email_user(self.who)
            if email_user:
                self.who_full_name = email_user.get_full_name()
            else:
                self.who_full_name = "Anonymous User"
        super().save(*args, **kwargs)
        logger.info("Logged User Action: %s", self)

    class Meta:
        abstract = True
        app_label = "leaseslicensing"


class CommunicationsLogEntry(models.Model):
    TYPE_CHOICES = [
        ("email", "Email"),
        ("phone", "Phone Call"),
        ("mail", "Mail"),
        ("person", "In Person"),
        ("onhold", "On Hold"),
        ("onhold_remove", "Remove On Hold"),
        ("with_qaofficer", "With QA Officer"),
        ("with_qaofficer_completed", "QA Officer Completed"),
        ("referral_complete", "Referral Completed"),
    ]
    DEFAULT_TYPE = TYPE_CHOICES[0][0]

    to = models.TextField(blank=True, verbose_name="To")
    fromm = models.CharField(max_length=200, blank=True, verbose_name="From")
    cc = models.TextField(blank=True, verbose_name="cc")
    type = models.CharField(max_length=35, choices=TYPE_CHOICES, default=DEFAULT_TYPE)
    reference = models.CharField(max_length=100, blank=True)
    subject = models.CharField(
        max_length=200, blank=True, verbose_name="Subject / Description"
    )
    text = models.TextField(blank=True)
    customer = models.IntegerField(null=True)  # EmailUserRO
    staff = models.IntegerField()  # EmailUserRO
    created = models.DateTimeField(auto_now_add=True, null=False, blank=False)

    class Meta:
        app_label = "leaseslicensing"


# @python_2_unicode_compatible
class Document(models.Model):
    name = models.CharField(
        max_length=255, blank=True, verbose_name="name", help_text=""
    )
    description = models.TextField(blank=True, verbose_name="description", help_text="")
    uploaded_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "leaseslicensing"
        abstract = True

    @property
    def path(self):
        # return self.file.path
        # return self._file.path
        # comment above line to fix the error "The '_file' attribute has no
        # file associated with it." when adding comms log entry.
        if self._file:
            return self._file.path
        else:
            return ""

    @property
    def filename(self):
        return os.path.basename(self.path)

    def __str__(self):
        return self.name or self.filename


# @python_2_unicode_compatible
class SystemMaintenance(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()

    def duration(self):
        """Duration of system maintenance (in mins)"""
        return (
            int((self.end_date - self.start_date).total_seconds() / 60.0)
            if self.end_date and self.start_date
            else ""
        )
        # return (datetime.now(tz=tz) - self.start_date).total_seconds()/60.

    duration.short_description = "Duration (mins)"

    class Meta:
        app_label = "leaseslicensing"
        verbose_name_plural = "System maintenance"

    def __str__(self):
        return "System Maintenance: {} ({}) - starting {}, ending {}".format(
            self.name, self.description, self.start_date, self.end_date
        )


class TemporaryDocumentCollection(models.Model):
    class Meta:
        app_label = "leaseslicensing"


upload_protected_files_storage = FileSystemStorage(
    location=settings.PROTECTED_MEDIA_ROOT, base_url="/protected_media"
)


class SecureFileField(models.FileField):
    def __init__(self, *args, **kwargs):
        kwargs["storage"] = upload_protected_files_storage
        super().__init__(*args, **kwargs)


class TemporaryDocument(Document):
    temp_document_collection = models.ForeignKey(
        TemporaryDocumentCollection,
        related_name="documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(max_length=255)

    class Meta:
        app_label = "leaseslicensing"

    @property
    def secure_url(self):
        from leaseslicensing.components.main.utils import get_secure_file_url

        return get_secure_file_url(self, "_file")


class LicensingModel(models.Model):
    lodgement_number = models.CharField(max_length=9, null=True, blank=True)

    class Meta:
        abstract = True
        app_label = "leaseslicensing"

    def __str__(self):
        return (
            self.lodgement_number
            if self.lodgement_number
            else f"{self._MODEL_PREFIX()}{'?'*6}"
        )

    def _MODEL_PREFIX(self):
        if not hasattr(self, "MODEL_PREFIX"):
            raise NotImplementedError(
                f"{self.__class__.__name__} model has no `MODEL_PREFIX` attribute"
            )
        return self.MODEL_PREFIX

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.lodgement_number:
            new_lodgement_id = f"{self._MODEL_PREFIX()}{self.pk:06d}"
            self.lodgement_number = new_lodgement_id
            self.save()

    @property
    def model_name(self):
        return self._meta.model_name


class LicensingModelVersioned(LicensingModel, RevisionedMixin):
    class Meta:
        abstract = True
        app_label = "leaseslicensing"

    @property
    def lodgement_versions(self):
        """
        Returns lodgement data for all commented versions of this model,
        as well as the the most recent data set.
        """

        first = Version.objects.get_for_object(self).first()
        if not first:
            return []
        current_revision_id = first.revision_id
        versions = self.revision_versions().filter(
            ~Q(revision__comment="") | Q(revision_id=current_revision_id)
        )

        return self.versions_to_lodgement_dict(versions)

    def versions_to_lodgement_dict(self, versions_qs):
        """
        Returns a dictionary of revision id, comment, lodgement number, lodgement sequence,
        lodgement date for versions queryset of this model to be used in the fronend.
        """

        rr = []
        for obj in versions_qs:
            rr.append(
                dict(
                    revision_id=obj.revision_id,
                    revision_comment=obj.revision.comment.strip(),
                    lodgement_number=obj.field_dict.get("lodgement_number", None),
                    lodgement_sequence=obj.field_dict.get("lodgement_sequence", None),
                    lodgement_date=obj.field_dict.get("lodgement_date", None),
                )
            )

        return rr

    def revision_versions(self):
        """
        Returns all versions of this model
        """

        return Version.objects.get_for_object(self).select_related("revision")

    def revision_version(self, revision_id):
        """
        Returns the version of this model for revision id `revision_id`
        """

        return self.revision_versions().filter(revision_id=revision_id)[0]


class BaseApplicant(RevisionedMixin):
    emailuser_id = models.IntegerField(null=True, blank=True)

    # Name, etc
    first_name = models.CharField(
        max_length=128, blank=True, verbose_name="Given name(s)"
    )
    last_name = models.CharField(max_length=128, blank=True)
    dob = models.DateField(
        auto_now=False,
        auto_now_add=False,
        null=True,
        blank=True,
        verbose_name="date of birth",
        help_text="",
    )

    # Residential address
    residential_line1 = models.CharField("Line 1", max_length=255, blank=True)
    residential_line2 = models.CharField("Line 2", max_length=255, blank=True)
    residential_line3 = models.CharField("Line 3", max_length=255, blank=True)
    residential_locality = models.CharField("Suburb / Town", max_length=255, blank=True)
    residential_state = models.CharField(max_length=255, default="WA", blank=True)
    residential_country = CountryField(
        default="AU", blank=True, blank_label="(Select a country)"
    )
    residential_postcode = models.CharField(max_length=10, blank=True)

    # Postal address
    postal_same_as_residential = models.BooleanField(default=False)
    postal_line1 = models.CharField("Line 1", max_length=255, blank=True)
    postal_line2 = models.CharField("Line 2", max_length=255, blank=True)
    postal_line3 = models.CharField("Line 3", max_length=255, blank=True)
    postal_locality = models.CharField("Suburb / Town", max_length=255, blank=True)
    postal_state = models.CharField(max_length=255, default="WA", blank=True)
    postal_country = CountryField(
        default="AU", blank=True, blank_label="(Select a country)"
    )
    postal_postcode = models.CharField(max_length=10, blank=True)

    # Contact
    email = models.EmailField(
        null=True,
        blank=True,
    )
    phone_number = models.CharField(
        max_length=50, null=True, blank=True, verbose_name="phone number", help_text=""
    )
    mobile_number = models.CharField(
        max_length=50, null=True, blank=True, verbose_name="mobile number", help_text=""
    )

    class Meta:
        app_label = "leaseslicensing"
        abstract = True

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def residential_address(self):
        # Mapping from ProposalApplicant to residential_address property
        address_mapping = {
            "residential_line1": "line1",
            "residential_line2": "line2",
            "residential_line3": "line3",
            "residential_postcode": "postcode",
            "residential_locality": "locality",
            "residential_state": "state",
            "residential_country": "country",
        }
        return {
            address_mapping[k]: v
            for k, v in self.__dict__.items()
            if k in address_mapping.keys()
        }

    @property
    def postal_address(self):
        # Mapping from ProposalApplicant to postal_address property
        address_mapping = {
            "postal_line1": "line1",
            "postal_line2": "line2",
            "postal_line3": "line3",
            "postal_postcode": "postcode",
            "postal_locality": "locality",
            "postal_state": "state",
            "postal_country": "country",
        }
        return {
            address_mapping[k]: v
            for k, v in self.__dict__.items()
            if k in address_mapping.keys()
        }

    def log_user_action(self, action, request):
        try:
            emailuser = retrieve_email_user(self.emailuser_id)
        except EmailUser.DoesNotExist:
            logger.warning(
                f"Tried to log user action for proposal applicant {self.id} "
                f"but couldn't find ledger user with id {self.emailuser_id}"
            )
            return
        return emailuser.log_user_action(action, request)
