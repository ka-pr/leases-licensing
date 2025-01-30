import datetime
import logging

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import FieldError, ValidationError
from django.db import models, transaction
from django.db.models import JSONField, Q
from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from ledger_api_client.ledger_models import EmailUserRO as EmailUser

from leaseslicensing.components.approvals.email import (
    send_approval_cancel_email_notification,
    send_approval_expire_email_notification,
    send_approval_reinstate_email_notification,
    send_approval_renewal_email_notification,
    send_approval_surrender_email_notification,
    send_approval_suspend_email_notification,
    send_approval_transfer_holder_email_notification,
    send_approval_transfer_transferee_email_notification,
)
from leaseslicensing.components.compliances.models import Compliance
from leaseslicensing.components.invoicing.models import Invoice
from leaseslicensing.components.invoicing.utils import clone_invoicing_details
from leaseslicensing.components.main.models import (
    BaseApplicant,
    CommunicationsLogEntry,
    Document,
    LicensingModelVersioned,
    RevisionedMixin,
    SecureFileField,
    UserAction,
)
from leaseslicensing.components.main.related_item import RelatedItem
from leaseslicensing.components.organisations.models import Organisation
from leaseslicensing.components.organisations.utils import get_organisation_ids_for_user
from leaseslicensing.components.proposals.models import (
    Proposal,
    ProposalType,
    ProposalUserAction,
    RequirementDocument,
    copy_gis_data,
    copy_groups,
    copy_proposal_details,
    copy_proposal_geometry,
    copy_proposal_requirements,
)
from leaseslicensing.helpers import is_customer, user_ids_in_group
from leaseslicensing.ledger_api_utils import retrieve_email_user
from leaseslicensing.settings import PROPOSAL_TYPE_AMENDMENT, PROPOSAL_TYPE_RENEWAL

logger = logging.getLogger(__name__)


def update_approval_doc_filename(instance, filename):
    return f"approval_documents/{instance.id}/{filename}"


def update_approval_comms_log_filename(instance, filename):
    return "proposals/{}/approvals/{}/communications/{}".format(
        instance.log_entry.approval.current_proposal.id,
        instance.log_entry.approval.id,
        filename,
    )


def update_approval_cancellation_doc_filename(instance, filename):
    return "/approval_cancellation_documents/{}/{}".format(
        instance.id,
        filename,
    )


def update_approval_surrender_doc_filename(instance, filename):
    return "approval_surrender_documents/{}/{}".format(
        instance.id,
        filename,
    )


def update_approval_suspension_doc_filename(instance, filename):
    return "approval_suspension_documents/{}/{}".format(
        instance.id,
        filename,
    )


class ApprovalDocument(Document, RevisionedMixin):
    REASON_NEW = "new"
    REASON_AMENDED = "amended"
    REASON_RENEWED = "renewed"
    REASON_REISSUED = "reissued"
    REASON_INVOICING_UPDATED = "invoicing_updated"
    REASON_TRANSFERRED = "transferred"
    REASON_CHOICES = (
        (REASON_NEW, "New"),
        (REASON_AMENDED, "Amended"),
        (REASON_RENEWED, "Renewed"),
        (REASON_REISSUED, "Reissued"),
        (REASON_INVOICING_UPDATED, "Invoicing updated"),
        (REASON_TRANSFERRED, "Transferred"),
    )

    approval = models.ForeignKey(
        "Approval", related_name="documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_approval_doc_filename, max_length=512)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted
    reason = models.CharField(max_length=25, choices=REASON_CHOICES, default="new")

    def delete(self):
        if self.can_delete:
            return super().delete()
        logger.info(
            "Cannot delete existing document object after Application has been submitted "
            f"(including document submitted before Application pushback to status Draft): {self.name}"
        )

    def user_has_object_permission(self, user_id):
        """Used by the secure documents api to determine if the user can view the instance and any attached documents"""
        return self.approval.user_has_object_permission(user_id)

    @property
    def approval_lodgement_sequence(self):
        return self.approval.lodgement_sequence


class Meta:
    app_label = "leaseslicensing"


class ApprovalType(RevisionedMixin):
    APPROVAL_TYPE_LEASE = "lease"
    APPROVAL_TYPE_LICENCE = "licence"
    APPROVAL_TYPE_CHOICES = (
        (APPROVAL_TYPE_LEASE, "Lease"),
        (APPROVAL_TYPE_LICENCE, "Licence"),
    )
    type = models.CharField(max_length=10, choices=APPROVAL_TYPE_CHOICES, null=True)
    name = models.CharField(max_length=200, unique=True)
    details_placeholder = models.CharField(max_length=200, blank=True)
    gst_free = models.BooleanField(default=False)
    approvaltypedocumenttypes = models.ManyToManyField(
        "ApprovalTypeDocumentType",
        through="ApprovalTypeDocumentTypeOnApprovalType",
        related_name="approval_type",
    )

    class Meta:
        app_label = "leaseslicensing"

    def __str__(self):
        return self.name

    def save(self, **kwargs):
        cache.delete(settings.CACHE_KEY_APPROVAL_TYPES_DICTIONARY)
        super().save(**kwargs)


class ApprovalTypeDocumentType(RevisionedMixin):
    name = models.CharField(max_length=200, unique=True)
    # Whether this document type is the license document
    is_license_document = models.BooleanField(default=False)
    # Whether this document type is the cover letter
    is_cover_letter = models.BooleanField(default=False)
    # Whether this document type is the sign off sheet
    is_sign_off_sheet = models.BooleanField(default=False)

    class Meta:
        app_label = "leaseslicensing"
        # A document must be either-or or none
        constraints = [
            models.CheckConstraint(
                check=Q(
                    ~Q(is_license_document=True, is_cover_letter=True),
                    ~Q(is_license_document=True, is_sign_off_sheet=True),
                    ~Q(is_cover_letter=True, is_sign_off_sheet=True),
                    _connector="AND",
                ),
                name="only_one_or_none_of_license_document_cover_letter_sign_off_sheet",
            )
        ]

    def __str__(self):
        return self.name

    @property
    def is_typed(self):
        return (
            self.is_license_document or self.is_cover_letter or self.is_sign_off_sheet
        )

    @property
    def type_display(self):
        if self.is_license_document:
            return "License document"
        elif self.is_cover_letter:
            return "Cover letter"
        elif self.is_sign_off_sheet:
            return "Sign-off sheet"
        else:
            return "Other document"


class ApprovalTypeDocumentTypeOnApprovalType(RevisionedMixin):
    approval_type = models.ForeignKey(
        ApprovalType,
        related_name="approvaltype_approvaltypedocumenttypes",
        on_delete=models.CASCADE,
    )
    approval_type_document_type = models.ForeignKey(
        ApprovalTypeDocumentType, on_delete=models.CASCADE
    )
    mandatory = models.BooleanField(default=False)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("approval_type", "approval_type_document_type")

    def __str__(self):
        mandatory = " (mandatory)" if self.mandatory else ""
        return f"{self.approval_type} - {self.approval_type_document_type}{mandatory}"


class Approval(LicensingModelVersioned):
    MODEL_PREFIX = "L"

    APPROVAL_STATUS_CURRENT = "current"
    APPROVAL_STATUS_CURRENT_PENDING_RENEWAL_REVIEW = "current_pending_renewal_review"
    APPROVAL_STATUS_CURRENT_PENDING_RENEWAL = "current_pending_renewal"
    APPROVAL_STATUS_EXPIRED = "expired"
    APPROVAL_STATUS_CANCELLED = "cancelled"
    APPROVAL_STATUS_SURRENDERED = "surrendered"
    APPROVAL_STATUS_SUSPENDED = "suspended"
    APPROVAL_STATUS_EXTENDED = "extended"
    APPROVAL_STATUS_AWAITING_PAYMENT = "awaiting_payment"
    APPROVAL_STATUS_CURRENT_EDITING_INVOICING = "current_editing_invoicing"

    CURRENT_APPROVAL_STATUSES = [
        APPROVAL_STATUS_CURRENT,
        APPROVAL_STATUS_CURRENT_PENDING_RENEWAL_REVIEW,
        APPROVAL_STATUS_CURRENT_PENDING_RENEWAL,
        APPROVAL_STATUS_CURRENT_EDITING_INVOICING,
    ]

    STATUS_CHOICES = (
        (APPROVAL_STATUS_CURRENT, "Current"),
        (
            APPROVAL_STATUS_CURRENT_PENDING_RENEWAL_REVIEW,
            "Current (Pending Renewal Review)",
        ),
        (APPROVAL_STATUS_CURRENT_PENDING_RENEWAL, "Current (Pending Renewal)"),
        (APPROVAL_STATUS_EXPIRED, "Expired"),
        (APPROVAL_STATUS_CANCELLED, "Cancelled"),
        (APPROVAL_STATUS_SURRENDERED, "Surrendered"),
        (APPROVAL_STATUS_SUSPENDED, "Suspended"),
        (APPROVAL_STATUS_EXTENDED, "Extended"),
        (APPROVAL_STATUS_AWAITING_PAYMENT, "Awaiting Payment"),
        (APPROVAL_STATUS_CURRENT_EDITING_INVOICING, "Current (Editing Invoicing)"),
    )
    approval_type = models.ForeignKey(ApprovalType, on_delete=models.PROTECT, null=True)
    status = models.CharField(
        max_length=40, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0]
    )
    licence_document = models.ForeignKey(
        ApprovalDocument,
        blank=True,
        null=True,
        related_name="licence_document",
        on_delete=models.SET_NULL,
    )
    cover_letter_document = models.ForeignKey(
        ApprovalDocument,
        blank=True,
        null=True,
        related_name="cover_letter_document",
        on_delete=models.SET_NULL,
    )
    sign_off_sheet = models.ForeignKey(
        ApprovalDocument,
        blank=True,
        null=True,
        related_name="sign_off_sheet",
        on_delete=models.SET_NULL,
    )
    current_proposal = models.ForeignKey(
        Proposal, related_name="approvals", null=True, on_delete=models.SET_NULL
    )
    renewal_document = models.ForeignKey(
        ApprovalDocument,
        blank=True,
        null=True,
        related_name="renewal_document",
        on_delete=models.SET_NULL,
    )
    renewal_review_notification_sent_to_assessors = models.BooleanField(default=False)
    renewal_notification_sent_to_holder = models.BooleanField(default=False)

    # This field holds the date that the approval was most recently issued (since they may be reissued)
    issue_date = models.DateTimeField()

    # This field holds the date the approval was first issued
    original_issue_date = models.DateField(auto_now_add=True)

    start_date = models.DateField()
    expiry_date = models.DateField()
    surrender_details = JSONField(blank=True, null=True)
    suspension_details = JSONField(blank=True, null=True)
    extracted_fields = JSONField(blank=True, null=True)
    cancellation_details = models.TextField(blank=True)
    extend_details = models.TextField(blank=True)
    cancellation_date = models.DateField(blank=True, null=True)
    set_to_cancel = models.BooleanField(default=False)
    set_to_suspend = models.BooleanField(default=False)
    set_to_surrender = models.BooleanField(default=False)

    # application_type = models.ForeignKey(ApplicationType, null=True, blank=True)
    renewal_count = models.PositiveSmallIntegerField(
        "Number of times an Approval has been renewed", default=0
    )
    # For leases that are migrated
    original_leaselicence_number = models.CharField(
        max_length=255, blank=True, null=True
    )
    migrated = models.BooleanField(default=False)
    record_management_number = models.CharField(max_length=100, blank=True, null=True)
    lodgement_sequence = models.IntegerField(blank=True, default=0)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("lodgement_number", "issue_date")
        verbose_name = "Lease/License"

    @property
    def applicant(self):
        return self.current_proposal.applicant

    @property
    def holder(self):
        if self.is_org_applicant:
            return self.applicant.ledger_organisation_name
        elif self.is_ind_applicant:
            return self.current_proposal.proposal_applicant.full_name
        else:
            return "Applicant not yet assigned"

    @property
    def submitter(self):
        if self.current_proposal:
            return self.current_proposal.submitter
        return None

    @property
    def proxy_applicant(self):
        if self.current_proposal:
            return self.current_proposal.proxy_applicant
        return None

    @property
    def linked_applications(self):
        return Proposal.objects.filter(
            Q(approval=self)
            | Q(
                previous_application__approval=self,
                previous_application__proposal_type__code=settings.PROPOSAL_TYPE_AMENDMENT,
            )
        ).values_list("lodgement_number", flat=True)

    @property
    def applicant_type(self):
        if self.is_org_applicant:
            return "org_applicant"
        elif self.is_proxy_applicant:
            return "proxy_applicant"
        else:
            # return None
            return "submitter"

    @property
    def is_org_applicant(self):
        return (
            self.current_proposal
            and self.current_proposal.org_applicant
            and isinstance(self.current_proposal.org_applicant, Organisation)
        )

    @property
    def is_ind_applicant(self):
        return (
            self.current_proposal
            and self.current_proposal.ind_applicant
            and isinstance(
                retrieve_email_user(self.current_proposal.ind_applicant), EmailUser
            )
        )

    @property
    def is_proxy_applicant(self):
        return (
            True
            if self.current_proposal and self.current_proposal.proxy_applicant
            else False
        )

    @property
    def applicant_id(self):
        if self.is_org_applicant:
            # return self.org_applicant.organisation.id
            return self.applicant.id
        elif self.is_proxy_applicant:
            return self.proxy_applicant  # .id
        else:
            # return None
            return self.submitter

    @property
    def region(self):
        return self.current_proposal.region.name

    @property
    def district(self):
        return self.current_proposal.district.name

    @property
    def tenure(self):
        return self.current_proposal.tenure.name

    @property
    def activity(self):
        return self.current_proposal.activity

    @property
    def title(self):
        return self.current_proposal.title

    @property
    def reference(self):
        return f"L{self.id}"

    @property
    def can_reissue(self):
        return (
            self.status == self.APPROVAL_STATUS_CURRENT
            or self.status == self.APPROVAL_STATUS_SUSPENDED
            or self.status == self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL
        )

    @property
    def can_reinstate(self):
        return (
            self.status == Approval.APPROVAL_STATUS_CANCELLED
            or self.status == Approval.APPROVAL_STATUS_SUSPENDED
            or self.status == Approval.APPROVAL_STATUS_SURRENDERED
        ) and self.can_action

    @property
    def has_draft_transfer(self):
        return self.transfers.filter(
            processing_status=ApprovalTransfer.APPROVAL_TRANSFER_STATUS_DRAFT
        ).exists()

    @property
    def has_pending_transfer(self):
        return self.transfers.filter(
            processing_status=ApprovalTransfer.APPROVAL_TRANSFER_STATUS_PENDING,
        ).exists()

    @property
    def has_pending_renewal(self):
        return self.status == self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL

    @property
    def has_draft_renewal(self):
        if self.status != self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL:
            return False

        renewal_conditions = {
            "previous_application": self.current_proposal,
            "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_RENEWAL),
        }
        return Proposal.objects.filter(**renewal_conditions).exists()

    @property
    def active_renewal(self):
        renewal_conditions = {
            "previous_application": self.current_proposal,
            "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_RENEWAL),
        }
        renewal_proposal = Proposal.objects.filter(**renewal_conditions).first()
        if not renewal_proposal:
            return {}
        return {
            "id": renewal_proposal.id,
            "processing_status": renewal_proposal.processing_status,
        }

    @property
    def has_draft_amendment(self):
        amendment_conditions = {
            "previous_application": self.current_proposal,
            "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_AMENDMENT),
        }
        return Proposal.objects.filter(**amendment_conditions).exists()

    @property
    def active_amendment(self):
        amendment_conditions = {
            "previous_application": self.current_proposal,
            "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_AMENDMENT),
        }
        amendment_proposal = Proposal.objects.filter(**amendment_conditions).first()
        if not amendment_proposal:
            return {}
        return {
            "id": amendment_proposal.id,
            "processing_status": amendment_proposal.processing_status,
        }

    @property
    def has_outstanding_compliances(self):
        return self.compliances.filter(
            processing_status__in=[
                Compliance.PROCESSING_STATUS_DUE,
                Compliance.PROCESSING_STATUS_WITH_ASSESSOR,
                Compliance.PROCESSING_STATUS_WITH_REFERRAL,
                Compliance.PROCESSING_STATUS_OVERDUE,
            ],
        ).exists()

    @property
    def has_outstanding_invoices(self):
        return self.invoices.filter(
            Q(date_due__lte=timezone.now().date()) | Q(date_due__isnull=True),
            status__in=[
                Invoice.INVOICE_STATUS_PENDING_UPLOAD_ORACLE_INVOICE,
                Invoice.INVOICE_STATUS_UNPAID,
            ],
        ).exists()

    @property
    def has_missing_gross_turnover_entries(self):
        return self.invoicing_details.has_missing_gross_turnover_entries

    @property
    def can_initiate_transfer(self):
        if self.has_pending_transfer:
            return False
        if self.has_outstanding_compliances:
            return False
        if self.has_outstanding_invoices:
            return False

        return self.status == self.APPROVAL_STATUS_CURRENT

    @property
    def active_transfer(self):
        return self.transfers.filter(
            processing_status__in=[
                ApprovalTransfer.APPROVAL_TRANSFER_STATUS_DRAFT,
                ApprovalTransfer.APPROVAL_TRANSFER_STATUS_PENDING,
            ]
        ).first()

    @property
    def allowed_assessor_ids(self):
        return user_ids_in_group(settings.GROUP_NAME_ASSESSOR)

    @property
    def allowed_assessors(self):
        emailusers = []
        for id in self.allowed_assessor_ids():
            emailuser = retrieve_email_user(id)
            emailusers.append(
                {
                    "id": id,
                    "first_name": emailuser.first_name,
                    "last_name": emailuser.last_name,
                    "email": emailuser.email,
                }
            )
        return emailusers

    @property
    def is_issued(self):
        return self.licence_number is not None and len(self.licence_number) > 0

    @property
    def can_action(self):
        if not (self.set_to_cancel or self.set_to_suspend or self.set_to_surrender):
            return True
        else:
            return False

    @property
    def can_renew(self):
        if self.status not in [
            self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL,
            self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL_REVIEW,
        ]:
            return False

        renewal_conditions = {
            "previous_application": self.current_proposal,
            "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_RENEWAL),
        }
        return not Proposal.objects.filter(**renewal_conditions).exists()

    # copy amend_renew() from ML?
    @property
    def can_amend(self):
        try:
            # Note: As of now there are no Renewal Documents implemented for leases. Assuming we will add them,
            # at the time we do so, they will be implemented as a new ApprovalTypeDocumentType object
            # with a new boolean field `is_renewal_document`. We can already check the approval for a document
            # of that type with that field now and simply treat any approval as if it were not meant to have
            # a renewal document. Thus any underlying logic and model definition can largely be left intact until
            # there is more informationm on renewal documents in leases available.
            can_provide_renewal_document = ApprovalTypeDocumentType.objects.filter(
                approval_type=self.approval_type, is_renewal_document=True
            ).exists()
        except FieldError:
            logger.warning(
                f"{self.approval_type} {self.lodgement_number} does not allow for renewal documents"
            )
            can_provide_renewal_document = False

        if (
            not can_provide_renewal_document
            and self.renewal_notification_sent_to_holder
        ):
            # Renewal document is not provided for this approval type. Don't need to check for it on the model.
            return False
        elif self.renewal_document and self.renewal_notification_sent_to_holder:
            return False
        else:
            amend_conditions = {
                "previous_application": self.current_proposal,
                "proposal_type": ProposalType.objects.get(code=PROPOSAL_TYPE_AMENDMENT),
            }
            proposals = Proposal.objects.filter(**amend_conditions)
            if proposals:
                if proposals.count() > 1:
                    logging.error(
                        f"Approval: {self.lodgement_number} has more than one current amendment proposals"
                    )
                return False
        return True

    @property
    def approved_by(self):
        return self.current_proposal.approved_by

    @property
    def invoicing_details(self):
        return self.current_proposal.invoicing_details

    @property
    def requirement_docs(self):
        if self.current_proposal:
            requirement_ids = (
                self.current_proposal.requirements.all()
                .exclude(is_deleted=True)
                .values_list("id", flat=True)
            )
            if requirement_ids:
                req_doc = RequirementDocument.objects.filter(
                    requirement__in=requirement_ids, visible=True
                )
                return req_doc
        else:
            logger.warning(
                f"Approval {self.lodgement_number} does not have current_proposal"
            )
        return None

    @property
    def proponent_reference_number(self):
        return self.current_proposal.proponent_reference_number

    @property
    def crown_land_rent_review_dates(self):
        """Crown land rent review dates for an approval are calculated dynamically
        based on the review_once_every field from the invoicing details object and the start
        and expiry date for the approva. Default is 5 years. Will return an empty list if
        no review interval is set or if no review is needed (i.e. the approval lasts less time
        than the review_once_every value)."""
        review_once_every = self.current_proposal.invoicing_details.review_once_every
        if not review_once_every:
            logger.warning(
                f"Approval {self.lodgement_number} "
                "does not have a crown land rent review interval set. Returning empty list."
            )
            return []

        review_dates = []
        review_date = self.start_date + relativedelta(years=review_once_every)
        expiry_date = self.expiry_date
        while review_date < expiry_date:
            review_dates.append(review_date)
            review_date = review_date + relativedelta(years=review_once_every)

        return review_dates

    @property
    def crown_land_rent_review_due_today(self):
        today = timezone.localtime(timezone.now()).date()
        return today in self.crown_land_rent_review_dates

    def crown_land_rent_review_reminder_due_in(self, months=12):
        today = timezone.localtime(timezone.now()).date()
        return today + relativedelta(months=months) in self.crown_land_rent_review_dates

    @property
    def next_crown_land_rent_review_date(self):
        for review_date in self.crown_land_rent_review_dates:
            if review_date > timezone.now().date():
                return review_date
        return None

    def has_invoice_issue_date_in(self, days: int) -> bool:
        today = timezone.localtime(timezone.now()).date()
        return (today + relativedelta(days=days)).strftime(
            "%d/%m/%Y"
        ) in self.invoicing_details.preview_invoices_issue_dates

    def user_has_object_permission(self, user_id):
        """Used by the secure documents api to determine if the user can view the instance and any attached documents"""
        return self.current_proposal.user_has_object_permission(user_id)

    @property
    def renewed_from_id(self):
        return self.current_proposal.previous_application.approval.id

    @property
    def renewed_from(self):
        return self.current_proposal.previous_application.approval

    def discard_future_compliances(self):
        # Method to use when transferring, cancelling or surrending an approval
        # Discards all future compliances for the approval
        # In the case of a transfer, new compliances will be generated for the new approval holder
        updated_count = self.compliances.filter(
            processing_status=Compliance.PROCESSING_STATUS_FUTURE,
        ).update(processing_status=Compliance.PROCESSING_STATUS_DISCARDED)
        logger.info(
            f"Discarded {updated_count} future compliances for Approval: {self}"
        )

    def reinstate_discarded_compliances(self):
        # Method to use when reinstating a cancelled or surrendered approval
        # Reinstates all discarded compliances for the approval
        updated_count = self.compliances.filter(
            processing_status=Compliance.PROCESSING_STATUS_DISCARDED,
        ).update(processing_status=Compliance.PROCESSING_STATUS_FUTURE)
        logger.info(
            f"Reinstated {updated_count} discarded compliances for Approval: {self}"
        )
        # Note even though they are all makred as 'future' they will be processed
        # as 'due' on the next run of the update_compliance_status management command

    def discard_future_invoices(self):
        # Method to use when transferring, cancelling or surrending an approval
        # Discards all invoices that are pending or unpaid where the due date is in the future
        # or where there is no due date
        updated_count = self.invoices.filter(
            Q(date_due__gt=timezone.now().date()) | Q(date_due__isnull=True),
            status__in=[
                Invoice.INVOICE_STATUS_PENDING_UPLOAD_ORACLE_INVOICE,
                Invoice.INVOICE_STATUS_UNPAID,
            ],
        ).update(status=Invoice.INVOICE_STATUS_DISCARDED)
        logger.info(f"Discarded {updated_count} future invoices for Approval: {self}")

    def reinstate_discarded_invoices(self):
        # Method to use when reinstating a cancelled or surrendered approval
        # Reinstates all discarded invoices for the approval
        reinstated_invoice_count = self.invoices.filter(
            status=Invoice.INVOICE_STATUS_DISCARDED,
        ).count()
        for invoice in self.invoices.filter(
            status=Invoice.INVOICE_STATUS_DISCARDED,
        ):
            if not invoice.date_due:
                invoice.status = Invoice.INVOICE_STATUS_PENDING_UPLOAD_ORACLE_INVOICE
                invoice.save()
            else:
                invoice.status = Invoice.INVOICE_STATUS_UNPAID
                invoice.save()

        logger.info(
            f"Reinstated {reinstated_invoice_count} discarded invoices for Approval: {self}"
        )

    @classmethod
    def get_approvals_for_emailuser(cls, emailuser_id):
        user_orgs = get_organisation_ids_for_user(emailuser_id)
        return cls.objects.filter(
            Q(current_proposal__org_applicant_id__in=user_orgs)
            | Q(current_proposal__submitter=emailuser_id)
            | Q(current_proposal__ind_applicant=emailuser_id)
            | Q(current_proposal__proxy_applicant=emailuser_id)
        )

    def review_renewal(self, can_be_renewed):
        if not can_be_renewed:
            # The approval will be left in current status to expire naturally
            self.status = Approval.APPROVAL_STATUS_CURRENT
            self.save()
            return

        # Send email to holder letting them know that the approval is able to be renewed
        send_approval_renewal_email_notification(self)
        self.status = self.APPROVAL_STATUS_CURRENT_PENDING_RENEWAL
        self.renewal_notification_sent_to_holder = True
        self.save()

    def log_user_action(self, action, request):
        return ApprovalUserAction.log_action(self, action, request.user.id)

    @transaction.atomic
    def expire_approval(self, user):
        today = timezone.localtime(timezone.now()).date()
        if self.status == Approval.APPROVAL_STATUS_CURRENT and self.expiry_date < today:
            self.status = Approval.APPROVAL_STATUS_EXPIRED
            self.save()
            send_approval_expire_email_notification(self)
            proposal = self.current_proposal
            ApprovalUserAction.log_action(
                self,
                ApprovalUserAction.ACTION_EXPIRE_APPROVAL.format(self.id),
                user.id,
            )
            ProposalUserAction.log_action(
                proposal,
                ProposalUserAction.ACTION_EXPIRED_APPROVAL_.format(proposal.id),
                user.id,
            )

    @transaction.atomic
    def approval_cancellation(self, request, details):
        if request.user.id not in self.allowed_assessor_ids:
            raise ValidationError("You do not have access to cancel this approval")
        if not self.can_reissue and self.can_action:
            raise ValidationError(
                "You cannot cancel approval if it is not current or suspended"
            )
        self.cancellation_date = details.get("cancellation_date").strftime("%Y-%m-%d")
        self.cancellation_details = details.get("cancellation_details")
        cancellation_date = datetime.datetime.strptime(
            self.cancellation_date, "%Y-%m-%d"
        )
        cancellation_date = cancellation_date.date()
        self.cancellation_date = cancellation_date  # test hack
        today = timezone.now().date()
        if cancellation_date <= today:
            if not self.status == Approval.APPROVAL_STATUS_CANCELLED:
                self.cancel(request.user)
        else:
            self.set_to_cancel = True
            self.save()

    @transaction.atomic
    def cancel(self, user):
        self.status = Approval.APPROVAL_STATUS_CANCELLED
        self.set_to_cancel = False
        self.save(version_comment="status_change: Approval cancelled")
        self.discard_future_compliances()
        self.discard_future_invoices()
        send_approval_cancel_email_notification(self)
        ApprovalUserAction.log_action(
            self,
            ApprovalUserAction.ACTION_CANCEL_APPROVAL.format(self.id),
            user.id,
        )
        ProposalUserAction.log_action(
            self.current_proposal,
            ProposalUserAction.ACTION_CANCEL_APPROVAL.format(self.current_proposal.id),
            user.id,
        )

    @transaction.atomic
    def approval_suspension(self, request, details):
        if request.user.id not in self.allowed_assessor_ids:
            raise ValidationError("You do not have access to suspend this approval")
        if not self.can_reissue and self.can_action:
            raise ValidationError(
                "You cannot suspend approval if it is not current or suspended"
            )
        if details.get("to_date"):
            to_date = details.get("to_date").strftime("%d/%m/%Y")
        else:
            to_date = ""
        self.suspension_details = {
            "from_date": details.get("from_date").strftime("%d/%m/%Y"),
            "to_date": to_date,
            "details": details.get("suspension_details"),
        }
        today = timezone.now().date()
        from_date = datetime.datetime.strptime(
            self.suspension_details["from_date"], "%d/%m/%Y"
        )
        from_date = from_date.date()
        if from_date <= today:
            if not self.status == Approval.APPROVAL_STATUS_SUSPENDED:
                self.commence_suspension(request.user)
        else:
            self.set_to_suspend = True
            self.save()

    @transaction.atomic
    def commence_suspension(self, user):
        self.status = Approval.APPROVAL_STATUS_SUSPENDED
        self.set_to_suspend = False
        self.save(version_comment="status_change: Approval suspended")
        send_approval_suspend_email_notification(self)
        ApprovalUserAction.log_action(
            self,
            ApprovalUserAction.ACTION_SUSPEND_APPROVAL.format(self.id),
            user.id,
        )
        ProposalUserAction.log_action(
            self.current_proposal,
            ProposalUserAction.ACTION_SUSPEND_APPROVAL.format(self.current_proposal.id),
            user.id,
        )

    @transaction.atomic
    def reinstate_approval(self, request):
        if request.user.id not in self.allowed_assessor_ids:
            raise ValidationError("You do not have access to reinstate this approval")

        if not self.can_reinstate:
            raise ValidationError(
                "You cannot reinstate an approval unless it has been cancelled, surrended or suspended"
            )

        if self.expiry_date <= timezone.now().date():
            raise ValidationError("You cannot reinstate an expired approval")

        if self.status == Approval.APPROVAL_STATUS_CANCELLED:
            self.cancellation_details = ""
            self.cancellation_date = None

        if self.status == Approval.APPROVAL_STATUS_SURRENDERED:
            self.surrender_details = {}

        if self.status == Approval.APPROVAL_STATUS_SUSPENDED:
            self.suspension_details = {}

        self.status = Approval.APPROVAL_STATUS_CURRENT
        self.renewal_review_notification_sent_to_assessors = (
            False  # Should be able to renew again
        )

        self.save(version_comment="status_change: Approval reinstated")

        # If any compliances or invoices were discarded when the approval was
        # cancelled or surrendered, reinstate them
        self.reinstate_discarded_compliances()
        self.reinstate_discarded_invoices()

        send_approval_reinstate_email_notification(self, request)

        # Log approval action
        self.log_user_action(
            ApprovalUserAction.ACTION_REINSTATE_APPROVAL.format(self.id),
            request,
        )

        # Log entry for proposal
        self.current_proposal.log_user_action(
            ProposalUserAction.ACTION_REINSTATE_APPROVAL.format(
                self.current_proposal.id
            ),
            request,
        )

    @transaction.atomic
    def approval_surrender(self, request, details):
        orgs_for_user = get_organisation_ids_for_user(request.user.id)
        if self.applicant_id not in orgs_for_user:
            if request.user.id not in self.allowed_assessor_ids and not is_customer(
                request
            ):
                raise ValidationError(
                    "You do not have access to surrender this approval"
                )
        if not self.can_reissue and self.can_action:
            raise ValidationError(
                "You cannot surrender approval if it is not current or suspended"
            )
        self.surrender_details = {
            "surrender_date": details.get("surrender_date").strftime("%d/%m/%Y"),
            "details": details.get("surrender_details"),
        }
        today = timezone.now().date()
        surrender_date = datetime.datetime.strptime(
            self.surrender_details["surrender_date"], "%d/%m/%Y"
        )
        surrender_date = surrender_date.date()
        if surrender_date <= today:
            if not self.status == Approval.APPROVAL_STATUS_SURRENDERED:
                self.surrender(request.user)
        else:
            self.set_to_surrender = True
            self.save()

    @transaction.atomic
    def surrender(self, user):
        self.status = Approval.APPROVAL_STATUS_SURRENDERED
        self.set_to_surrender = False
        self.save(version_comment="status_change: Approval surrendered")
        self.discard_future_compliances()
        self.discard_future_invoices()
        send_approval_surrender_email_notification(self)
        ApprovalUserAction.log_action(
            self,
            ApprovalUserAction.ACTION_SURRENDER_APPROVAL.format(self.id),
            user.id,
        )
        ProposalUserAction.log_action(
            self.current_proposal,
            ProposalUserAction.ACTION_SURRENDER_APPROVAL.format(
                self.current_proposal.id
            ),
            user.id,
        )

    @property
    def as_related_item(self):
        related_item = RelatedItem(
            identifier=self.related_item_identifier,
            model_name=self._meta.verbose_name,
            descriptor=self.related_item_descriptor,
            action_url=f'<a href=/internal/approval/{self.id} target="_blank">Open</a>',
            type="lease_license",
        )
        return related_item

    @property
    def related_item_identifier(self):
        return self.lodgement_number

    @property
    def related_item_descriptor(self):
        """
        Returns this license's expiry date as item description
        """

        return self.expiry_date

    def purge_approval(self):
        if not settings.DEBUG:
            raise ValidationError("This method can only be used in DEBUG mode")
        for invoice in self.invoices.all():
            invoice.purge_invoice()

        self.transfers.all().delete()
        self.delete()


class ApprovalLogEntry(CommunicationsLogEntry):
    approval = models.ForeignKey(
        Approval, related_name="comms_logs", on_delete=models.CASCADE
    )

    class Meta:
        app_label = "leaseslicensing"

    def save(self, **kwargs):
        # save the reference if the reference not provided
        if not self.reference:
            self.reference = self.approval.id
        super().save(**kwargs)


class ApprovalLogDocument(Document):
    log_entry = models.ForeignKey(
        "ApprovalLogEntry",
        related_name="documents",
        null=True,
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(
        upload_to=update_approval_comms_log_filename, null=True, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class ApprovalCancellationDocument(Document):
    approval = models.ForeignKey(
        Approval,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_cancellation_documents",
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    _file = SecureFileField(
        upload_to=update_approval_cancellation_doc_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class ApprovalSurrenderDocument(Document):
    approval = models.ForeignKey(
        Approval,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_surrender_documents",
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    _file = SecureFileField(
        upload_to=update_approval_surrender_doc_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class ApprovalSuspensionDocument(Document):
    approval = models.ForeignKey(
        Approval,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approval_suspension_documents",
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    _file = SecureFileField(
        upload_to=update_approval_suspension_doc_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class ApprovalTransfer(LicensingModelVersioned):
    MODEL_PREFIX = "LT"

    APPROVAL_TRANSFER_STATUS_DRAFT = "draft"
    APPROVAL_TRANSFER_STATUS_CANCELLED = "cancelled"
    APPROVAL_TRANSFER_STATUS_PENDING = "pending"
    APPROVAL_TRANSFER_STATUS_DECLINED = "declined"
    APPROVAL_TRANSFER_STATUS_ACCEPTED = "accepted"

    APPROVAL_TRANSFER_STATUS_CHOICES = (
        (APPROVAL_TRANSFER_STATUS_DRAFT, "Draft"),
        (APPROVAL_TRANSFER_STATUS_CANCELLED, "Cancelled"),
        (APPROVAL_TRANSFER_STATUS_PENDING, "Pending"),
        (APPROVAL_TRANSFER_STATUS_DECLINED, "Declined"),
        (APPROVAL_TRANSFER_STATUS_ACCEPTED, "Accepted"),
    )

    TRANSFEREE_TYPE_ORGANISATION = "organisation"
    TRANSFEREE_TYPE_INDIVIDUAL = "individual"

    TRANSFEREE_TYPE_CHOICES = (
        (TRANSFEREE_TYPE_ORGANISATION, "Organisation"),
        (TRANSFEREE_TYPE_INDIVIDUAL, "Individual"),
    )

    processing_status = models.CharField(
        max_length=10,
        choices=APPROVAL_TRANSFER_STATUS_CHOICES,
        default=APPROVAL_TRANSFER_STATUS_DRAFT,
        null=False,
        blank=False,
    )
    approval = models.ForeignKey(
        Approval,
        null=False,
        blank=False,
        on_delete=models.PROTECT,
        related_name="transfers",
    )
    transferee_type = models.CharField(
        max_length=12,
        choices=TRANSFEREE_TYPE_CHOICES,
        default=TRANSFEREE_TYPE_ORGANISATION,
        null=False,
        blank=False,
    )
    transferee = models.IntegerField(null=True, blank=True)
    initiator = models.IntegerField(null=True, blank=True)
    datetime_created = models.DateTimeField(auto_now_add=True)
    datetime_updated = models.DateTimeField(auto_now=True)
    datetime_initiated = models.DateTimeField(null=True, blank=True)
    datetime_expiry = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-lodgement_number",)

    @property
    def transferee_name(self):
        if not self.transferee:
            return None

        if self.transferee_type == ApprovalTransfer.TRANSFEREE_TYPE_ORGANISATION:
            organisation = Organisation.objects.get(id=self.transferee)
            return f"{organisation.ledger_organisation_name} ({organisation.ledger_organisation_abn})"

        if self.transferee_type == ApprovalTransfer.TRANSFEREE_TYPE_INDIVIDUAL:
            user = EmailUser.objects.get(id=self.transferee)
            return user.get_full_name()

    def save(self, **kwargs):
        # Complain when attempting to create a new approval transfer for
        # an approval that already has a draft or pending approval transfer
        if self.pk is None and self.approval.active_transfer:
            raise ValidationError(
                "Unable to create a new approval transfer as there is "
                "already a draft or pending approval transfer for this approval"
            )
        super().save(**kwargs)

    def is_transferee(self, user_id):
        return (
            self.transferee_type == ApprovalTransfer.TRANSFEREE_TYPE_INDIVIDUAL
            and self.transferee == user_id
        )

    def is_transferee_user_org(self, user_id):
        user_orgs = get_organisation_ids_for_user(user_id)
        return (
            self.transferee_type == ApprovalTransfer.TRANSFEREE_TYPE_ORGANISATION
            and self.transferee in user_orgs
        )

    def user_has_object_permission(self, user_id):
        return (
            self.approval.user_has_object_permission(user_id)
            or self.is_transferee(user_id)
            or self.is_transferee_user_org(user_id)
        )

    def cancel(self, user):
        if self.processing_status != self.APPROVAL_TRANSFER_STATUS_DRAFT:
            raise ValidationError(
                "Unable to cancel approval transfer as it is not in draft status"
            )

        self.processing_status = self.APPROVAL_TRANSFER_STATUS_CANCELLED
        self.save()
        logger.info(f"Cancelled ApprovalTransfer {self}")

    @property
    def has_supporting_documents(self):
        return self.approval_transfer_supporting_documents.exists()

    @transaction.atomic
    def initiate(self, user_id):
        from leaseslicensing.components.proposals.utils import (
            make_proposal_applicant_ready,
        )

        if self.processing_status != self.APPROVAL_TRANSFER_STATUS_DRAFT:
            raise ValidationError(
                "Unable to initiate approval transfer as it is not in draft status"
            )

        self.processing_status = self.APPROVAL_TRANSFER_STATUS_PENDING
        self.initiator = user_id
        self.datetime_initiated = timezone.now()
        self.save()

        ind_applicant = None
        org_applicant = None
        if self.transferee_type == self.TRANSFEREE_TYPE_ORGANISATION:
            org_applicant = Organisation.objects.get(id=self.transferee)
        else:
            ind_applicant = self.transferee

        # Create a transfer proposal with all the same data as the original lease licence proposal
        proposal_type = ProposalType.objects.get(code=settings.PROPOSAL_TYPE_TRANSFER)

        # Don't use self.approval.current_proposal here as the reference
        # would be incorrect after saving transfer_proposal
        original_proposal = Proposal.objects.get(id=self.approval.current_proposal.id)

        transfer_proposal = original_proposal
        transfer_proposal.pk = None  # When saved will create a new proposal
        transfer_proposal.lodgement_number = None
        transfer_proposal.submitter = None
        transfer_proposal.processing_status = Proposal.PROCESSING_STATUS_DRAFT
        transfer_proposal.ind_applicant = ind_applicant
        transfer_proposal.org_applicant = org_applicant
        transfer_proposal.proposal_type = proposal_type

        # Clone the invoicing details object (includes any necessary child objects)
        transfer_proposal.invoicing_details = clone_invoicing_details(
            self.approval.current_proposal.invoicing_details
        )

        transfer_proposal.save()

        if ind_applicant:
            transferee_user = retrieve_email_user(ind_applicant)
            make_proposal_applicant_ready(transfer_proposal, transferee_user)

        # Query the original proposal again so we have the correct reference
        original_proposal = Proposal.objects.get(id=self.approval.current_proposal.id)

        # Copy over data from original proposal
        copy_groups(original_proposal, transfer_proposal)
        copy_proposal_geometry(original_proposal, transfer_proposal)
        copy_proposal_details(original_proposal, transfer_proposal)
        copy_gis_data(original_proposal, transfer_proposal)
        copy_proposal_requirements(original_proposal, transfer_proposal)

        send_approval_transfer_holder_email_notification(self.approval)

        # Email the transferee to inform them that the approval transfer has been initiated
        send_approval_transfer_transferee_email_notification(
            self.approval, transfer_proposal
        )

        logger.info(f"Initiated ApprovalTransfer {self}")
        return transfer_proposal


def supporting_documents_filename(instance, filename):
    return f"approval-transfer/{instance.id}/documents/{filename}"


class ApprovalTransferApplicant(BaseApplicant):
    approval_transfer = models.OneToOneField(
        ApprovalTransfer,
        related_name="applicant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    class Meta:
        app_label = "leaseslicensing"

    @classmethod
    def instantiate_from_request_user(cls, user, approval_transfer):
        (
            approval_transfer_applicant,
            created,
        ) = ApprovalTransferApplicant.objects.get_or_create(
            approval_transfer=approval_transfer
        )
        if created:
            logger.info(
                f"Created ApprovalTransferApplicant {approval_transfer_applicant} from request user {user}"
            )
            approval_transfer_applicant.emailuser_id = user.id
            approval_transfer_applicant.first_name = user.first_name
            approval_transfer_applicant.last_name = user.last_name
            approval_transfer_applicant.dob = user.dob

            approval_transfer_applicant.residential_line1 = (
                user.residential_address.line1
            )
            approval_transfer_applicant.residential_line2 = (
                user.residential_address.line2
            )
            approval_transfer_applicant.residential_line3 = (
                user.residential_address.line3
            )
            approval_transfer_applicant.residential_locality = (
                user.residential_address.locality
            )
            approval_transfer_applicant.residential_state = (
                user.residential_address.state
            )
            approval_transfer_applicant.residential_country = (
                user.residential_address.country
            )
            approval_transfer_applicant.residential_postcode = (
                user.residential_address.postcode
            )

            approval_transfer_applicant.postal_same_as_residential = (
                user.postal_same_as_residential
            )
            approval_transfer_applicant.postal_line1 = user.postal_address.line1
            approval_transfer_applicant.postal_line2 = user.postal_address.line2
            approval_transfer_applicant.postal_line3 = user.postal_address.line3
            approval_transfer_applicant.postal_locality = user.postal_address.locality
            approval_transfer_applicant.postal_state = user.postal_address.state
            approval_transfer_applicant.postal_country = user.postal_address.country
            approval_transfer_applicant.postal_postcode = user.postal_address.postcode

            approval_transfer_applicant.email = user.email
            approval_transfer_applicant.phone_number = user.phone_number
            approval_transfer_applicant.mobile_number = user.mobile_number

            approval_transfer_applicant.save()


class ApprovalTransferDocument(Document):
    approval_transfer = models.ForeignKey(
        ApprovalTransfer,
        related_name="approval_transfer_supporting_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=supporting_documents_filename, max_length=512)
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
        verbose_name = "Approval Transfer Supporting Document"


class ApprovalUserAction(UserAction):
    ACTION_CREATE_APPROVAL = "Create Lease/License {}"
    ACTION_UPDATE_APPROVAL = "Update Lease/License {}"
    ACTION_UPDATE_APPROVAL_INVOICING_DETAILS = (
        "Update Lease/License {} Invoicing Details. Comment text: {}"
    )
    ACTION_REVIEW_INVOICING_DETAILS_BASE_FEE_APPROVAL = "Base Fee changed from {} to {}"
    ACTION_EXPIRE_APPROVAL = "Expire Lease/License {}"
    ACTION_CANCEL_APPROVAL = "Cancel Lease/License {}"
    ACTION_EXTEND_APPROVAL = "Extend Lease/License {}"
    ACTION_SUSPEND_APPROVAL = "Suspend Lease/License {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate Lease/License {}"
    ACTION_SURRENDER_APPROVAL = "Surrender Lease/License {}"
    ACTION_RENEW_APPROVAL = "Create Renewal Application for Lease/License {}"
    ACTION_AMEND_APPROVAL = "Create Amendment Application for Lease/License {}"
    ACTION_AD_HOC_INVOICE_GENERATED_APPROVAL = (
        "Ad-hoc Invoice {} generated for Lease/License {}"
    )

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-when",)

    @classmethod
    def log_action(cls, approval, action, user):
        return cls.objects.create(approval=approval, who=user, what=str(action))

    approval = models.ForeignKey(
        Approval, related_name="action_logs", on_delete=models.CASCADE
    )


@receiver(pre_delete, sender=Approval)
def delete_documents(sender, instance, *args, **kwargs):
    for document in instance.documents.all():
        try:
            document.delete()
        except ProtectedError:
            logger.info(f"Document: {document} is protected. Unable to delete.")
