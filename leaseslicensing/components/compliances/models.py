import datetime
import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from ledger_api_client.ledger_models import EmailUserRO as EmailUser

from leaseslicensing.components.compliances.email import (
    send_amendment_email_notification,
    send_compliance_accept_email_notification,
    send_due_email_notification,
    send_external_submit_email_notification,
    send_internal_due_email_notification,
    send_internal_notification_only_email,
    send_internal_reminder_email_notification,
    send_notification_only_email,
    send_pending_referrals_complete_email_notification,
    send_referral_complete_email_notification,
    send_referral_email_notification,
    send_reminder_email_notification,
    send_submit_email_notification,
)
from leaseslicensing.components.main.models import (
    CommunicationsLogEntry,
    Document,
    LicensingModelVersioned,
    RevisionedMixin,
    SecureFileField,
    UserAction,
)
from leaseslicensing.components.main.utils import is_department_user
from leaseslicensing.components.proposals.models import ProposalRequirement
from leaseslicensing.exceptions import ComplianceNotAuthorized
from leaseslicensing.helpers import is_assessor, is_compliance_referee
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger(__name__)


class ComplianceManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "proposal", "proposal__application_type", "approval", "requirement"
            )
        )


class Compliance(LicensingModelVersioned):
    objects = ComplianceManager()

    MODEL_PREFIX = "C"

    PROCESSING_STATUS_DUE = "due"
    PROCESSING_STATUS_FUTURE = "future"
    PROCESSING_STATUS_WITH_ASSESSOR = "with_assessor"
    PROCESSING_STATUS_WITH_REFERRAL = "with_referral"
    PROCESSING_STATUS_APPROVED = "approved"
    PROCESSING_STATUS_DISCARDED = "discarded"
    PROCESSING_STATUS_OVERDUE = "overdue"

    PROCESSING_STATUS_CHOICES = (
        (PROCESSING_STATUS_DUE, "Due"),
        (PROCESSING_STATUS_FUTURE, "Future"),
        (PROCESSING_STATUS_WITH_ASSESSOR, "With Assessor"),
        (PROCESSING_STATUS_WITH_REFERRAL, "With Referral"),
        (PROCESSING_STATUS_APPROVED, "Approved"),
        (PROCESSING_STATUS_DISCARDED, "Discarded"),
        (PROCESSING_STATUS_OVERDUE, "Overdue"),
    )

    CUSTOMER_STATUS_DUE = "due"
    CUSTOMER_STATUS_FUTURE = "future"
    CUSTOMER_STATUS_WITH_ASSESSOR = "with_assessor"
    CUSTOMER_STATUS_APPROVED = "approved"
    CUSTOMER_STATUS_DISCARDED = "discarded"
    CUSTOMER_STATUS_OVERDUE = "overdue"

    CUSTOMER_STATUS_CHOICES = (
        (CUSTOMER_STATUS_DUE, "Due"),
        (CUSTOMER_STATUS_FUTURE, "Future"),
        (CUSTOMER_STATUS_WITH_ASSESSOR, "Under Review"),
        (CUSTOMER_STATUS_APPROVED, "Approved"),
        (CUSTOMER_STATUS_DISCARDED, "Discarded"),
        (CUSTOMER_STATUS_OVERDUE, "Overdue"),
    )

    proposal = models.ForeignKey(
        "leaseslicensing.Proposal", related_name="compliances", on_delete=models.CASCADE
    )
    approval = models.ForeignKey(
        "leaseslicensing.Approval", related_name="compliances", on_delete=models.CASCADE
    )
    due_date = models.DateField()
    text = models.TextField(blank=True)
    processing_status = models.CharField(
        choices=PROCESSING_STATUS_CHOICES, max_length=20
    )
    customer_status = models.CharField(
        choices=CUSTOMER_STATUS_CHOICES,
        max_length=20,
        default=CUSTOMER_STATUS_CHOICES[1][0],
    )
    assigned_to = models.IntegerField(null=True)  # EmailUserRO
    requirement = models.ForeignKey(
        ProposalRequirement,
        blank=True,
        null=True,
        related_name="compliance_requirement",
        on_delete=models.SET_NULL,
    )
    lodgement_date = models.DateTimeField(blank=True, null=True)
    submitter = models.IntegerField(null=True)  # EmailUserRO
    reminder_sent = models.BooleanField(default=False)
    post_reminder_sent = models.BooleanField(default=False)
    gross_turnover = models.DecimalField(
        max_digits=15, decimal_places=2, blank=True, null=True
    )

    class Meta:
        app_label = "leaseslicensing"
        ordering = (
            "approval__lodgement_number",
            "lodgement_number",
        )

    @property
    def approval_number(self):
        return self.approval.lodgement_number if self.approval else ""

    @property
    def title(self):
        return self.proposal.title

    @property
    def holder(self):
        return self.proposal.applicant_name

    @property
    def assigned_to_name(self):
        if self.assigned_to:
            emailuser = retrieve_email_user(self.assigned_to)
            return emailuser.get_full_name()
        return "Unassigned"

    @property
    def reference(self):
        # return 'C{0:06d}'.format(self.id)
        return self.lodgement_number

    @property
    def allowed_assessors(self):
        return self.proposal.compliance_assessors

    @property
    def can_user_view(self):
        """
        :return: True if the compliance is not in the editable status for external user.
        """
        return (
            self.customer_status == Compliance.PROCESSING_STATUS_WITH_ASSESSOR
            or self.customer_status == Compliance.PROCESSING_STATUS_APPROVED
        )

    def is_referee(self, user_id):
        return ComplianceReferral.objects.filter(
            compliance=self,
            processing_status=ComplianceReferral.PROCESSING_STATUS_WITH_REFERRAL,
            referral=user_id,
        ).exists()

    @property
    def can_process(self):
        """
        :return: True if the compliance is ready for assessment.
        """
        return self.processing_status == Compliance.PROCESSING_STATUS_WITH_ASSESSOR

    @property
    def amendment_requests(self):
        return ComplianceAmendmentRequest.objects.filter(compliance=self)

    @property
    def current_amendment_requests(self):
        return self.amendment_requests.filter(
            status=ComplianceAmendmentRequest.STATUS_CHOICE_REQUESTED
        )

    @property
    def application_type(self):
        if self.proposal.application_type:
            return self.proposal.application_type.name_display
        return None

    @property
    def latest_referrals(self):
        return ComplianceReferral.objects.filter(compliance=self).order_by(
            "-lodged_on"
        )[: settings.LATEST_REFERRAL_COUNT]

    @property
    def applicant_emails(self):
        return self.proposal.applicant_emails

    @property
    def submitter_emailuser(self):
        if self.submitter:
            return retrieve_email_user(self.submitter)
        if self.proposal.submitter:
            return retrieve_email_user(self.proposal.submitter)
        logger.warning(
            f"Submitter not found for Compliance: {self.lodgement_number} "
            f"and Proposal: {self.proposal.lodgement_number}"
        )
        return None

    @property
    def submitter_email(self):
        if self.submitter_emailuser:
            return self.submitter_emailuser.email
        logger.warning(
            f"Submitter not found for Compliance: {self.lodgement_number} "
            f"sand Proposal: {self.proposal.lodgement_number}"
        )
        return None

    @property
    def gross_turnover_required(self):
        if self.requirement.standard_requirement:
            return self.requirement.standard_requirement.gross_turnover_required
        return False

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not hasattr(self, "assessment"):
            ComplianceAssessment.objects.create(
                compliance=self,
            )

    def submit(self, request):
        with transaction.atomic():
            if self.processing_status == Compliance.PROCESSING_STATUS_DISCARDED:
                raise ValidationError(
                    "You cannot submit this compliance with requirements as it has been discarded."
                )
            if self.processing_status in [
                Compliance.PROCESSING_STATUS_FUTURE,
                Compliance.PROCESSING_STATUS_DUE,
            ]:
                self.processing_status = Compliance.PROCESSING_STATUS_WITH_ASSESSOR
                self.customer_status = Compliance.PROCESSING_STATUS_WITH_ASSESSOR
                self.submitter = request.user.id

                if self.amendment_requests:
                    qs = self.amendment_requests.filter(status="requested")
                    if qs:
                        for q in qs:
                            q.status = "amended"
                            q.save()

            self.lodgement_date = timezone.now()

            self.save(version_comment=f"Compliance Submitted: {self.id}")
            self.proposal.save(version_comment=f"Compliance Submitted: {self.id}")
            self.log_user_action(
                ComplianceUserAction.ACTION_SUBMIT_REQUEST.format(self.id), request
            )

            send_external_submit_email_notification(request, self)

            send_submit_email_notification(request, self)

            self.documents.all().update(can_delete=False)

    def delete_document(self, request, document):
        with transaction.atomic():
            try:
                if document:
                    doc = self.documents.get(id=document[2])
                    doc.delete()
                return self
            except Document.DoesNotExist:
                raise ValidationError("Document not found")

    def assign_to(self, user, request):
        with transaction.atomic():
            self.assigned_to = user
            self.save()
            self.log_user_action(
                ComplianceUserAction.ACTION_ASSIGN_TO.format(
                    retrieve_email_user(user).get_full_name()
                ),
                request,
            )

    def unassign(self, request):
        with transaction.atomic():
            self.assigned_to = None
            self.save()
            self.log_user_action(ComplianceUserAction.ACTION_UNASSIGN, request)

    def accept(self, request):
        with transaction.atomic():
            self.processing_status = Compliance.PROCESSING_STATUS_APPROVED
            self.customer_status = Compliance.PROCESSING_STATUS_APPROVED
            self.save()
            self.log_user_action(
                ComplianceUserAction.ACTION_CONCLUDE_REQUEST.format(self.id), request
            )
            send_compliance_accept_email_notification(self, request)

    @transaction.atomic
    def send_reminder(self, emailuser_id):
        reminder_sent = False
        today = timezone.localtime(timezone.now()).date()

        if (
            self.due_date < today
            and self.lodgement_date is None
            and self.post_reminder_sent is False
        ):
            send_reminder_email_notification(self)
            send_internal_reminder_email_notification(self)
            self.post_reminder_sent = True
            self.save()
            ComplianceUserAction.log_action(
                self,
                ComplianceUserAction.ACTION_REMINDER_SENT.format(self.id),
                emailuser_id,
            )
            logger.info(
                "Post due date reminder sent for Compliance {} ".format(
                    self.lodgement_number
                )
            )
            reminder_sent = True

        # if today is with 14 days of due_date, and email reminder is not sent
        # (deals with Compliances created with the reminder period)
        elif (
            self.due_date >= today
            and today
            >= self.due_date
            - datetime.timedelta(days=settings.COMPLIANCES_DAYS_PRIOR_TO_SEND_REMINDER)
            and self.reminder_sent is False
        ):
            if self.requirement.notification_only:
                send_notification_only_email(self)
                send_internal_notification_only_email(self)
                self.reminder_sent = True
                self.processing_status = Compliance.PROCESSING_STATUS_APPROVED
                self.customer_status = Compliance.PROCESSING_STATUS_APPROVED
                self.save()
            else:
                send_due_email_notification(self)
                send_internal_due_email_notification(self)
                self.reminder_sent = True
                self.save()
            ComplianceUserAction.log_action(
                self,
                ComplianceUserAction.ACTION_REMINDER_SENT.format(self.id),
                emailuser_id,
            )
            logger.info(
                "Pre due date reminder sent for Compliance {} ".format(
                    self.lodgement_number
                )
            )
            reminder_sent = True
        return reminder_sent

    @transaction.atomic
    def send_referral(self, request, referral_email, referral_text):
        if self.processing_status not in [
            Compliance.PROCESSING_STATUS_WITH_ASSESSOR,
            Compliance.PROCESSING_STATUS_WITH_REFERRAL,
        ]:
            raise ComplianceNotAuthorized()

        referral_email = referral_email.lower()
        self.processing_status = Compliance.PROCESSING_STATUS_WITH_REFERRAL
        self.save()

        # Check if the user is in ledger
        try:
            user = EmailUser.objects.get(email__icontains=referral_email)
        except EmailUser.DoesNotExist:
            # Validate if it is a deparment user
            department_user = is_department_user(referral_email)
            if not department_user:
                raise ValidationError(
                    "The user you want to send the referral to is not a member of the department"
                )
            # Check if the user is in ledger or create

            user, created = EmailUser.objects.get_or_create(
                email=department_user["email"].lower()
            )
            if created:
                user.first_name = department_user["given_name"]
                user.last_name = department_user["surname"]
                user.save()

        referral = None
        try:
            referral = ComplianceReferral.objects.get(referral=user.id, compliance=self)
            raise ValidationError("A referral has already been sent to this user")
        except ComplianceReferral.DoesNotExist:
            # Create Referral
            referral = ComplianceReferral.objects.create(
                compliance=self,
                referral=user.id,
                sent_by=request.user.id,
                text=referral_text,
                assigned_officer=request.user.id,
            )

        # Create a log entry for the proposal
        self.log_user_action(
            ComplianceUserAction.ACTION_SEND_REFERRAL_TO.format(
                referral.id,
                self.lodgement_number,
                f"{user.get_full_name()}({user.email})",
            ),
            request,
        )

        send_referral_email_notification(
            referral,
            [
                user.email,
            ],
            request,
        )

    def log_user_action(self, action, request):
        return ComplianceUserAction.log_action(self, action, request.user.id)

    def user_has_object_permission(self, user_id):
        if self.referrals.filter(referral=user_id).exists():
            return True
        return self.approval.user_has_object_permission(user_id)

    def switch_status(self, user_id, new_processing_status):
        if self.processing_status == new_processing_status:
            return

        if not is_assessor(user_id) or not is_compliance_referee(user_id, self):
            raise ValidationError(
                "Only an assessor or referee can change the status of a compliance"
            )


def update_proposal_compliance_filename(instance, filename):
    return f"compliance_documents/{instance.id}/{filename}"


class ComplianceDocument(Document):
    compliance = models.ForeignKey(
        "Compliance", related_name="documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=update_proposal_compliance_filename, max_length=512
    )
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super().delete()
        logger.info(
            "Cannot delete existing document object after Compliance has been submitted "
            "(including document submitted before Compliance pushback to status Due): {}".format(
                self.name
            )
        )

    def user_has_object_permission(self, user_id):
        return self.compliance.user_has_object_permission(user_id)

    class Meta:
        app_label = "leaseslicensing"


class ComplianceUserAction(UserAction):
    ACTION_CREATE = "Create compliance {}"
    ACTION_SUBMIT_REQUEST = "Submit compliance {}"
    ACTION_ASSIGN_TO = "Assign to {}"
    ACTION_UNASSIGN = "Unassign"
    ACTION_DECLINE_REQUEST = "Decline request"
    ACTION_ID_REQUEST_AMENDMENTS = "Request amendments"
    ACTION_REMINDER_SENT = "Reminder sent for compliance {}"
    ACTION_STATUS_CHANGE = "Change status to Due for compliance {}"
    ACTION_SEND_REFERRAL_TO = "Send referral to {} for compliance {}"
    ACTION_REMIND_REFERRAL = "Send reminder to {} for compliance {}"
    ACTION_RESEND_REFERRAL_TO = "Resend referral to {} for compliance {}"
    RECALL_REFERRAL = "Referral {} for compliance {} has been recalled"
    CONCLUDE_REFERRAL = "{}: Referral {} for compliance {} has been concluded"

    ACTION_CONCLUDE_REQUEST = "Conclude request {}"

    @classmethod
    def log_action(cls, compliance, action, user):
        return cls.objects.create(compliance=compliance, who=user, what=str(action))

    compliance = models.ForeignKey(
        Compliance, related_name="action_logs", on_delete=models.CASCADE
    )

    class Meta:
        app_label = "leaseslicensing"


class ComplianceLogEntry(CommunicationsLogEntry):
    compliance = models.ForeignKey(
        Compliance, related_name="comms_logs", on_delete=models.CASCADE
    )

    def save(self, **kwargs):
        # save the request id if the reference not provided
        if not self.reference:
            self.reference = self.compliance.id
        super().save(**kwargs)

    class Meta:
        app_label = "leaseslicensing"


def update_compliance_comms_log_filename(instance, filename):
    return "proposals/{}/compliance/communications/{}".format(
        instance.log_entry.compliance.proposal.id, filename
    )


class ComplianceLogDocument(Document):
    log_entry = models.ForeignKey(
        "ComplianceLogEntry", related_name="documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=update_compliance_comms_log_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class CompRequest(models.Model):
    compliance = models.ForeignKey(Compliance, on_delete=models.CASCADE)
    subject = models.CharField(max_length=200, blank=True)
    text = models.TextField(blank=True)
    officer = models.IntegerField()  # EmailUserRO

    class Meta:
        app_label = "leaseslicensing"


class ComplianceAmendmentReason(models.Model):
    reason = models.CharField("Reason", max_length=125)

    class Meta:
        verbose_name = "Compliance Amendment Reason"
        app_label = "leaseslicensing"

    def __str__(self):
        return self.reason


class ComplianceAmendmentRequest(CompRequest):
    STATUS_CHOICE_REQUESTED = "requested"
    STATUS_CHOICE_AMENDED = "amended"

    STATUS_CHOICES = (
        (STATUS_CHOICE_REQUESTED, "Requested"),
        (STATUS_CHOICE_AMENDED, "Amended"),
    )
    status = models.CharField(
        "Status", max_length=30, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0]
    )
    reason = models.ForeignKey(
        ComplianceAmendmentReason, blank=True, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        app_label = "leaseslicensing"

    @transaction.atomic
    def generate_amendment(self, request):
        if self.status != ComplianceAmendmentRequest.STATUS_CHOICE_REQUESTED:
            return

        compliance = self.compliance
        if compliance.processing_status != Compliance.PROCESSING_STATUS_DUE:
            compliance.processing_status = Compliance.PROCESSING_STATUS_DUE
            compliance.customer_status = Compliance.PROCESSING_STATUS_DUE
            compliance.save()

        # Create a log entry for the proposal
        compliance.log_user_action(
            ComplianceUserAction.ACTION_ID_REQUEST_AMENDMENTS, request
        )

        send_amendment_email_notification(self, request, compliance)

    def user_has_object_permission(self, user):
        return self.compliance.user_has_object_permission(user)


class ComplianceAssessment(RevisionedMixin):
    compliance = models.OneToOneField(
        Compliance, related_name="assessment", on_delete=models.CASCADE
    )
    completed = models.BooleanField(default=False)
    submitter = models.IntegerField(blank=True, null=True)  # EmailUserRO
    assessor_comment = models.TextField(blank=True)
    deficiency_comment = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"


class ComplianceReferral(RevisionedMixin):
    PROCESSING_STATUS_WITH_REFERRAL = "with_referral"
    PROCESSING_STATUS_RECALLED = "recalled"
    PROCESSING_STATUS_COMPLETED = "completed"

    PROCESSING_STATUS_CHOICES = (
        (PROCESSING_STATUS_WITH_REFERRAL, "Pending"),
        (PROCESSING_STATUS_RECALLED, "Recalled"),
        (PROCESSING_STATUS_COMPLETED, "Completed"),
    )
    lodged_on = models.DateTimeField(auto_now_add=True)
    compliance = models.ForeignKey(
        Compliance, related_name="referrals", on_delete=models.CASCADE
    )
    sent_by = models.IntegerField()  # EmailUserRO
    referral = models.IntegerField()  # EmailUserRO
    is_external = models.BooleanField(default=False)
    linked = models.BooleanField(default=False)
    processing_status = models.CharField(
        "Processing Status",
        max_length=30,
        choices=PROCESSING_STATUS_CHOICES,
        default=PROCESSING_STATUS_CHOICES[0][0],
    )
    text = models.TextField(blank=True)  # Comments from the assessor to the referee
    referral_text = models.TextField(
        blank=True
    )  # Comments from the referee when they complete the referral
    assigned_officer = models.IntegerField()  # EmailUserRO
    comment = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-lodged_on",)

    @property
    def applicant(self):
        return self.compliance.proposal.applicant

    @property
    def referral_as_email_user(self):
        return retrieve_email_user(self.referral)

    @transaction.atomic
    def recall(self, request):
        if not is_assessor(request):
            raise ComplianceNotAuthorized()

        self.processing_status = ComplianceReferral.PROCESSING_STATUS_RECALLED
        self.save()

        # Log an action for the compliance
        self.compliance.log_user_action(
            ComplianceUserAction.RECALL_REFERRAL.format(self.id, self.compliance.id),
            request,
        )

        self.process_last_pending_referral(request)

    @transaction.atomic
    def remind(self, request):
        if not is_assessor(request):
            raise ComplianceNotAuthorized()

        # Create a log entry for the proposal
        self.compliance.log_user_action(
            ComplianceUserAction.ACTION_REMIND_REFERRAL.format(
                self.id,
                self.compliance.id,
                f"{self.referral_as_email_user.get_full_name()}",
            ),
            request,
        )

        # send email
        send_referral_email_notification(
            self,
            [
                self.referral_as_email_user.email,
            ],
            request,
            reminder=True,
        )

    @transaction.atomic
    def resend(self, request):
        if not is_assessor(request):
            raise ComplianceNotAuthorized()
        self.processing_status = ComplianceReferral.PROCESSING_STATUS_WITH_REFERRAL
        self.compliance.processing_status = Compliance.PROCESSING_STATUS_WITH_REFERRAL
        self.compliance.save()
        self.save()

        # Create a log entry for the compliance
        self.compliance.log_user_action(
            ComplianceUserAction.ACTION_RESEND_REFERRAL_TO.format(
                self.id,
                self.compliance.id,
                f"{self.referral_as_email_user.get_full_name()}",
            ),
            request,
        )

        # send email
        # recipients = self.referral_group.members_list
        # ~leaving the comment above here in case we need to send to the whole group
        send_referral_email_notification(
            self,
            [
                self.referral_as_email_user.email,
            ],
            request,
        )

    @transaction.atomic
    def complete(self, request):
        referral_text = request.data.get("referral_text", None)
        if referral_text:
            self.referral_text = referral_text
        self.processing_status = ComplianceReferral.PROCESSING_STATUS_COMPLETED
        self.save()

        # Log proposal action
        self.compliance.log_user_action(
            ComplianceUserAction.CONCLUDE_REFERRAL.format(
                request.user.get_full_name(), self.id, self.compliance.lodgement_number
            ),
            request,
        )

        send_referral_complete_email_notification(self, request)

        self.process_last_pending_referral(request)

    def process_last_pending_referral(self, request):
        # If this was the last pending referral for the compliance update the status and send a notification
        if ComplianceReferral.objects.filter(
            compliance=self.compliance,
            processing_status=ComplianceReferral.PROCESSING_STATUS_WITH_REFERRAL,
        ).exists():
            return
        self.compliance.processing_status = Compliance.PROCESSING_STATUS_WITH_ASSESSOR
        self.compliance.save()

        send_pending_referrals_complete_email_notification(self, request)


def compliance_referral_document_upload_to(instance, filename):
    return f"compliance_referral_documents/{instance.id}/{filename}"


class ComplianceReferralDocument(Document):
    compliance_referral = models.ForeignKey(
        ComplianceReferral, related_name="referral_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=compliance_referral_document_upload_to, max_length=512
    )
    input_name = models.CharField(max_length=255, null=True, blank=True)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super().delete()

        logger.info(
            "Cannot delete existing document object after Application has been submitted "
            "(including document submitted before Application pushback to status Draft): {}".format(
                self.name
            )
        )

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("compliance_referral", "-uploaded_date")
