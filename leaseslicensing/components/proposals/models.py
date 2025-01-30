import calendar
import copy
import datetime
import json
import logging
import subprocess
from copy import deepcopy
from decimal import Decimal

from ckeditor.fields import RichTextField
from dateutil.relativedelta import relativedelta
from dirtyfields import DirtyFieldsMixin
from django.apps import apps
from django.conf import settings
from django.contrib.gis.db.models.fields import PolygonField
from django.contrib.gis.db.models.functions import Area
from django.contrib.postgres.fields import ArrayField
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models import F, JSONField, Max, Min, Q
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from ledger_api_client.ledger_models import EmailUserRO as EmailUser
from ledger_api_client.managed_models import SystemGroup
from rest_framework import serializers
from reversion.models import Version

from leaseslicensing import exceptions
from leaseslicensing.components.competitive_processes.email import (
    send_competitive_process_create_notification,
)
from leaseslicensing.components.competitive_processes.models import (
    CompetitiveProcess,
    CompetitiveProcessGeometry,
    CompetitiveProcessGroup,
    CompetitiveProcessParty,
)
from leaseslicensing.components.invoicing import utils as invoicing_utils
from leaseslicensing.components.invoicing.email import (
    send_new_invoice_raised_internal_notification,
)
from leaseslicensing.components.invoicing.models import Invoice, InvoicingDetails
from leaseslicensing.components.main.models import (  # Organisation as ledger_organisation, OrganisationAddress,
    ApplicationType,
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
from leaseslicensing.components.organisations.utils import (
    can_admin_org,
    get_admin_emails_for_organisation,
    get_organisation_ids_for_user,
)
from leaseslicensing.components.proposals.email import (
    send_amendment_email_notification,
    send_approver_approve_email_notification,
    send_approver_decline_email_notification,
    send_license_ready_for_invoicing_notification,
    send_pending_referrals_complete_email_notification,
    send_proposal_approval_email_notification,
    send_proposal_approver_sendback_email_notification,
    send_proposal_decline_email_notification,
    send_proposal_roi_approval_email_notification,
    send_referral_complete_email_notification,
    send_referral_email_notification,
)
from leaseslicensing.components.tenure.models import (
    GIS_DATA_MODEL_NAMES,
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
from leaseslicensing.helpers import is_approver, is_customer, user_ids_in_group
from leaseslicensing.ledger_api_utils import retrieve_email_user
from leaseslicensing.settings import (
    APPLICATION_TYPE_LEASE_LICENCE,
    APPLICATION_TYPE_REGISTRATION_OF_INTEREST,
    GROUP_NAME_APPROVER,
    GROUP_NAME_ASSESSOR,
    PROPOSAL_TYPE_AMENDMENT,
    PROPOSAL_TYPE_MIGRATION,
    PROPOSAL_TYPE_NEW,
    PROPOSAL_TYPE_RENEWAL,
    PROPOSAL_TYPE_TRANSFER,
)

logger = logging.getLogger(__name__)


def update_proposal_doc_filename(instance, filename):
    return f"proposals/{instance.proposal.id}/documents/{filename}"


def update_qaofficer_doc_filename(instance, filename):
    return f"proposals/{instance.proposal.id}/qaofficer/{filename}"


def update_referral_doc_filename(instance, filename):
    return f"proposals/{instance.referral.proposal.id}/referral/{filename}"


def update_proposal_required_doc_filename(instance, filename):
    return f"proposals/{instance.proposal.id}/required_documents/{filename}"


def update_requirement_doc_filename(instance, filename):
    return "proposals/{}/requirement_documents/{}".format(
        instance.requirement.proposal.id, filename
    )


def update_proposal_comms_log_filename(instance, filename):
    return f"proposals/{instance.log_entry.proposal.id}/{filename}"


def update_events_park_doc_filename(instance, filename):
    return "proposals/{}/events_park_documents/{}".format(
        instance.events_park.proposal.id, filename
    )


def update_pre_event_park_doc_filename(instance, filename):
    return "proposals/{}/pre_event_park_documents/{}".format(
        instance.pre_event_park.proposal.id, filename
    )


def update_additional_doc_filename(instance, filename):
    return "proposals/{}/additional_documents/{}/{}".format(
        instance.proposal.id,
        instance.proposal_additional_document_type.additional_document_type.name,
        filename,
    )


class AdditionalDocumentType(RevisionedMixin):
    name = models.CharField(max_length=255, null=True, blank=True)
    help_text = models.CharField(max_length=255, null=True, blank=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Additional Document Type"
        app_label = "leaseslicensing"
        ordering = ["name"]


class DefaultDocument(Document):
    input_name = models.CharField(max_length=255, null=True, blank=True)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted
    visible = models.BooleanField(
        default=True
    )  # to prevent deletion on file system, hidden and still be available in history

    class Meta:
        app_label = "leaseslicensing"
        abstract = True

    def delete(self):
        if self.can_delete:
            return super().delete()
        logger.info(
            "Cannot delete existing document object after Application has been submitted "
            "(including document submitted before Application pushback to status Draft): {}".format(
                self.name
            )
        )


class ShapefileDocumentQueryset(models.QuerySet):
    """Using a custom manager to make sure shapfiles are removed when a bulk .delete is called
    as having multiple files with the shapefile extensions in the same folder causes issues.
    """

    def delete(self):
        for obj in self:
            obj._file.delete()
        super().delete()


class ShapefileDocument(Document):
    objects = ShapefileDocumentQueryset.as_manager()
    proposal = models.ForeignKey(
        "Proposal", related_name="shapefile_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=500)
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

    def delete(self):
        if self.can_delete:
            self._file.delete()
            return super().delete()
        logger.info(
            "Cannot delete existing document object after Proposal has been submitted "
            "(including document submitted before Proposal pushback to status Draft): {}".format(
                self.name
            )
        )

    class Meta:
        app_label = "leaseslicensing"


class DeedPollDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="deed_poll_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Deed Poll Document"


class LegislativeRequirementsDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="legislative_requirements_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class RiskFactorsDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="risk_factors_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class KeyMilestonesDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="key_milestones_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class KeyPersonnelDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="key_personnel_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class StaffingDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="staffing_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class MarketAnalysisDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="market_analysis_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class AvailableActivitiesDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="available_activities_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class FinancialCapacityDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="financial_capacity_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class CapitalInvestmentDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="capital_investment_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class CashFlowDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="cash_flow_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ProfitAndLossDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="profit_and_loss_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class MiningTenementDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="mining_tenement_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class NativeTitleConsultationDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="native_title_consultation_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class AboriginalSiteDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="aboriginal_site_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class SignificantChangeDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="significant_change_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class BuildingRequiredDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="building_required_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class WetlandsImpactDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="wetlands_impact_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class EnvironmentallySensitiveDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="environmentally_sensitive_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class HeritageSiteDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="heritage_site_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class GroundDisturbingWorksDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="ground_disturbing_works_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ClearingVegetationDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="clearing_vegetation_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ConsistentPlanDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="consistent_plan_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ConsistentPurposeDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="consistent_purpose_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class LongTermUseDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="long_term_use_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ExclusiveUseDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="exclusive_use_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ProposedDeclineDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="proposed_decline_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Proposed Decline Document"


class ProposedApprovalDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="proposed_approval_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Proposed Approval Document"


class ProposalDocument(Document):
    proposal = models.ForeignKey(
        "Proposal", related_name="supporting_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Application Document"


class ReferralDocument(Document):
    referral = models.ForeignKey(
        "Referral", related_name="referral_documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(upload_to=update_referral_doc_filename, max_length=512)
    input_name = models.CharField(max_length=255, null=True, blank=True)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted

    def delete(self):
        if self.can_delete:
            return super(ProposalDocument, self).delete()
        logger.info(
            "Cannot delete existing document object after Application has been submitted "
            "(including document submitted before Application pushback to status Draft): {}".format(
                self.name
            )
        )

    class Meta:
        app_label = "leaseslicensing"


class RequirementDocument(Document):
    requirement = models.ForeignKey(
        "ProposalRequirement",
        related_name="requirement_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_requirement_doc_filename, max_length=512)
    input_name = models.CharField(max_length=255, null=True, blank=True)
    can_delete = models.BooleanField(
        default=True
    )  # after initial submit prevent document from being deleted
    visible = models.BooleanField(
        default=True
    )  # to prevent deletion on file system, hidden and still be available in history

    def delete(self):
        if self.can_delete:
            return super().delete()


class LeaseLicenceApprovalDocument(Document):
    proposal = models.ForeignKey(
        "Proposal",
        related_name="lease_licence_approval_documents",
        on_delete=models.CASCADE,
    )
    approval_type = models.ForeignKey(
        "leaseslicensing.ApprovalType",
        related_name="lease_licence_approval_documents",
        on_delete=models.CASCADE,
    )
    approval_type_document_type = models.ForeignKey(
        "leaseslicensing.ApprovalTypeDocumentType",
        related_name="lease_licence_approval_documents",
        on_delete=models.CASCADE,
    )
    _file = SecureFileField(upload_to=update_proposal_doc_filename, max_length=512)
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
        verbose_name = "Lease Licence Approval Document"


class ProposalApplicantDetails(models.Model):
    first_name = models.CharField(max_length=24, blank=True, default="")

    class Meta:
        app_label = "leaseslicensing"


class ProposalType(models.Model):
    # class ProposalType(RevisionedMixin):
    code = models.CharField(max_length=30, blank=True, null=True)
    description = models.CharField(max_length=200, blank=True, null=True)

    def __str__(self):
        # return 'id: {} code: {}'.format(self.id, self.code)
        return self.description

    class Meta:
        app_label = "leaseslicensing"


class ProposalManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "proposal_type", "org_applicant", "application_type", "approval"
            )
        )


class Proposal(LicensingModelVersioned, DirtyFieldsMixin):
    objects = ProposalManager()

    MODEL_PREFIX = "P"

    APPLICANT_TYPE_ORGANISATION = "ORG"
    APPLICANT_TYPE_INDIVIDUAL = "IND"
    APPLICANT_TYPE_PROXY = "PRX"
    APPLICANT_TYPE_SUBMITTER = "SUB"

    PROCESSING_STATUS_DRAFT = "draft"
    PROCESSING_STATUS_AMENDMENT_REQUIRED = "amendment_required"
    PROCESSING_STATUS_WITH_ASSESSOR = "with_assessor"
    PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS = "with_assessor_conditions"
    PROCESSING_STATUS_WITH_APPROVER = "with_approver"
    PROCESSING_STATUS_WITH_REFERRAL = "with_referral"
    # This processing status is for registration of interest proposals
    PROCESSING_STATUS_APPROVED_REGISTRATION_OF_INTEREST = (
        "approved_registration_of_interest"
    )
    PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS = "approved_competitive_process"
    PROCESSING_STATUS_APPROVED_EDITING_INVOICING = "approved_editing_invoicing"
    # This processing status is for lease / license proposals
    PROCESSING_STATUS_APPROVED = "approved"
    PROCESSING_STATUS_DECLINED = "declined"
    PROCESSING_STATUS_DISCARDED = "discarded"
    PROCESSING_STATUS_CHOICES = (
        (PROCESSING_STATUS_DRAFT, "Draft"),
        (PROCESSING_STATUS_WITH_ASSESSOR, "With Assessor"),
        (PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS, "With Assessor (Conditions)"),
        (PROCESSING_STATUS_WITH_APPROVER, "With Approver"),
        (PROCESSING_STATUS_WITH_REFERRAL, "With Referral"),
        (
            PROCESSING_STATUS_APPROVED_REGISTRATION_OF_INTEREST,
            "Approved (Registration of Interest)",
        ),
        (
            PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS,
            "Approved (Competitive Process)",
        ),
        (PROCESSING_STATUS_APPROVED_EDITING_INVOICING, "Approved (Editing Invoicing)"),
        (PROCESSING_STATUS_APPROVED, "Approved"),
        (PROCESSING_STATUS_DECLINED, "Declined"),
        (PROCESSING_STATUS_DISCARDED, "Discarded"),
    )

    # List of statuses from above that allow a customer to edit a proposal.
    CUSTOMER_EDITABLE_STATE = [
        PROCESSING_STATUS_DRAFT,
        PROCESSING_STATUS_AMENDMENT_REQUIRED,
    ]

    # List of statuses from above that allow a customer to view a proposal (read-only)
    CUSTOMER_VIEWABLE_STATE = [
        PROCESSING_STATUS_WITH_ASSESSOR,
        PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
        PROCESSING_STATUS_WITH_REFERRAL,
        PROCESSING_STATUS_WITH_APPROVER,
        PROCESSING_STATUS_APPROVED_REGISTRATION_OF_INTEREST,
        PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS,
        PROCESSING_STATUS_APPROVED_EDITING_INVOICING,
        PROCESSING_STATUS_APPROVED,
        PROCESSING_STATUS_DECLINED,
        PROCESSING_STATUS_DISCARDED,
    ]

    OFFICER_PROCESSABLE_STATE = [
        PROCESSING_STATUS_WITH_ASSESSOR,
        PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
        PROCESSING_STATUS_WITH_REFERRAL,  # <-- Be aware
        PROCESSING_STATUS_WITH_APPROVER,
    ]

    ID_CHECK_STATUS_CHOICES = (
        ("not_checked", "Not Checked"),
        ("awaiting_update", "Awaiting Update"),
        ("updated", "Updated"),
        ("accepted", "Accepted"),
    )

    COMPLIANCE_CHECK_STATUS_CHOICES = (
        ("not_checked", "Not Checked"),
        ("awaiting_returns", "Awaiting Returns"),
        ("completed", "Completed"),
        ("accepted", "Accepted"),
    )

    CHARACTER_CHECK_STATUS_CHOICES = (
        ("not_checked", "Not Checked"),
        ("accepted", "Accepted"),
    )

    REVIEW_STATUS_CHOICES = (
        ("not_reviewed", "Not Reviewed"),
        ("awaiting_amendments", "Awaiting Amendments"),
        ("amended", "Amended"),
        ("accepted", "Accepted"),
    )

    proposal_type = models.ForeignKey(
        ProposalType, blank=True, null=True, on_delete=models.SET_NULL
    )
    proposed_issuance_approval = JSONField(blank=True, null=True)
    ind_applicant = models.IntegerField(null=True, blank=True)  # EmailUserRO
    org_applicant = models.ForeignKey(
        Organisation,
        blank=True,
        null=True,
        related_name="org_applications",
        on_delete=models.SET_NULL,
    )
    proxy_applicant = models.IntegerField(null=True, blank=True)  # EmailUserRO
    lodgement_sequence = models.IntegerField(blank=True, default=0)
    lodgement_date = models.DateTimeField(blank=True, null=True)
    submitter = models.IntegerField(null=True)  # EmailUserRO
    assigned_officer = models.IntegerField(null=True)  # EmailUserRO
    assigned_approver = models.IntegerField(null=True)  # EmailUserRO
    approved_by = models.IntegerField(null=True)  # EmailUserRO
    processing_status = models.CharField(
        "Processing Status",
        max_length=35,
        choices=PROCESSING_STATUS_CHOICES,
        default=PROCESSING_STATUS_CHOICES[0][0],
    )
    prev_processing_status = models.CharField(max_length=30, blank=True, null=True)
    id_check_status = models.CharField(
        "Identification Check Status",
        max_length=30,
        choices=ID_CHECK_STATUS_CHOICES,
        default=ID_CHECK_STATUS_CHOICES[0][0],
    )
    compliance_check_status = models.CharField(
        "Return Check Status",
        max_length=30,
        choices=COMPLIANCE_CHECK_STATUS_CHOICES,
        default=COMPLIANCE_CHECK_STATUS_CHOICES[0][0],
    )
    character_check_status = models.CharField(
        "Character Check Status",
        max_length=30,
        choices=CHARACTER_CHECK_STATUS_CHOICES,
        default=CHARACTER_CHECK_STATUS_CHOICES[0][0],
    )
    review_status = models.CharField(
        "Review Status",
        max_length=30,
        choices=REVIEW_STATUS_CHOICES,
        default=REVIEW_STATUS_CHOICES[0][0],
    )
    approval = models.ForeignKey(
        "leaseslicensing.Approval",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proposals",
    )
    previous_application = models.ForeignKey(
        "self", blank=True, null=True, on_delete=models.SET_NULL
    )
    proposed_decline_status = models.BooleanField(default=False)
    # Special Fields
    title = models.CharField(max_length=255, null=True, blank=True)
    application_type = models.ForeignKey(ApplicationType, on_delete=models.PROTECT)
    approval_level = models.CharField(
        "Activity matrix approval level", max_length=255, null=True, blank=True
    )
    approval_level_document = models.ForeignKey(
        ProposalDocument,
        blank=True,
        null=True,
        related_name="approval_level_document",
        on_delete=models.SET_NULL,
    )
    approval_comment = models.TextField(blank=True)
    details_text = models.TextField(blank=True)
    added_internally = models.BooleanField(default=False)
    # If the proposal is created as part of migration of approvals
    migrated = models.BooleanField(default=False)
    original_leaselicence_number = models.CharField(
        max_length=255, blank=True, null=True
    )
    # Registration of Interest generates a Lease Licence
    generated_proposal = models.ForeignKey(
        "self",
        related_name="originating_proposal",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )
    # Registration of Interest generates a Competitive Process
    generated_competitive_process = models.OneToOneField(
        CompetitiveProcess,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="originating_proposal",
    )
    # Competitive Process generates a Lease Licence
    originating_competitive_process = models.ForeignKey(
        CompetitiveProcess,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="generated_proposal",
    )
    # When adding proposal as a party to an existing competitive process
    competitive_process_to_copy_to = models.ForeignKey(
        CompetitiveProcess,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proposals_added",
    )
    invoicing_details = models.OneToOneField(
        InvoicingDetails,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proposal",
    )
    # Registration of Interest additional form fields
    # proposal details
    exclusive_use = models.BooleanField(null=True)
    exclusive_use_text = models.TextField(blank=True)
    long_term_use = models.BooleanField(null=True)
    long_term_use_text = models.TextField(blank=True)
    consistent_purpose = models.BooleanField(null=True)
    consistent_purpose_text = models.TextField(blank=True)
    consistent_plan = models.BooleanField(null=True)
    consistent_plan_text = models.TextField(blank=True)
    # proposal impact
    clearing_vegetation = models.BooleanField(null=True)
    clearing_vegetation_text = models.TextField(blank=True)
    ground_disturbing_works = models.BooleanField(null=True)
    ground_disturbing_works_text = models.TextField(blank=True)
    heritage_site = models.BooleanField(null=True)
    heritage_site_text = models.TextField(blank=True)
    environmentally_sensitive = models.BooleanField(null=True)
    environmentally_sensitive_text = models.TextField(blank=True)
    wetlands_impact = models.BooleanField(null=True)
    wetlands_impact_text = models.TextField(blank=True)
    building_required = models.BooleanField(null=True)
    building_required_text = models.TextField(blank=True)
    significant_change = models.BooleanField(null=True)
    significant_change_text = models.TextField(blank=True)
    aboriginal_site = models.BooleanField(null=True)
    aboriginal_site_text = models.TextField(blank=True)
    native_title_consultation = models.BooleanField(null=True)
    native_title_consultation_text = models.TextField(blank=True)
    mining_tenement = models.BooleanField(null=True)
    mining_tenement_text = models.TextField(blank=True)
    # Lease Licence additional form fields
    # proposal details
    profit_and_loss_text = models.TextField(blank=True)
    cash_flow_text = models.TextField(blank=True)
    capital_investment_text = models.TextField(blank=True)
    financial_capacity_text = models.TextField(blank=True)
    available_activities_text = models.TextField(blank=True)
    market_analysis_text = models.TextField(blank=True)
    staffing_text = models.TextField(blank=True)
    # proposal impact
    key_personnel_text = models.TextField(blank=True)
    key_milestones_text = models.TextField(blank=True)
    risk_factors_text = models.TextField(blank=True)
    legislative_requirements_text = models.TextField(blank=True)
    site_name = models.ForeignKey(
        SiteName, blank=True, null=True, on_delete=models.PROTECT
    )
    proponent_reference_number = models.CharField(null=True, blank=True, max_length=50)
    # datetime_gis_data_first_fetched = models.DateTimeField(blank=True, null=True)
    # datetime_gis_data_last_fetched = models.DateTimeField(blank=True, null=True)

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Proposal"
        verbose_name_plural = "Proposals"

    def save(self, *args, **kwargs):
        # Clear out the cached
        cache.delete(settings.CACHE_KEY_MAP_PROPOSALS)
        super().save(*args, **kwargs)
        if not ProposalAssessment.objects.filter(proposal=self).exists():
            # Make sure every proposal has an assessment object
            ProposalAssessment.objects.create(proposal=self)

    @property
    def submitter_obj(self):
        if self.submitter:
            return retrieve_email_user(self.submitter)
        return None

    @property
    def relevant_applicant(self):
        if self.ind_applicant:
            return retrieve_email_user(self.ind_applicant)
        elif self.org_applicant:
            return self.org_applicant
        elif self.proxy_applicant:
            return retrieve_email_user(self.proxy_applicant)
        else:
            return retrieve_email_user(self.submitter)

    @property
    def relevant_applicant_name(self):
        relevant_applicant = self.relevant_applicant
        if isinstance(relevant_applicant, EmailUser):
            return relevant_applicant.get_full_name()
        else:
            # Organisation
            return relevant_applicant.name

    @property
    def reference(self):
        return f"{self.lodgement_number}-{self.lodgement_sequence}"

    @property
    def reversion_ids(self):
        current_revision_id = Version.objects.get_for_object(self).first().revision_id
        versions = (
            Version.objects.get_for_object(self)
            .select_related("revision__user")
            .filter(
                Q(revision__comment__icontains="status")
                | Q(revision_id=current_revision_id)
            )
        )
        version_ids = [[i.id, i.revision.date_created] for i in versions]
        return [
            dict(
                cur_version_id=version_ids[0][0],
                prev_version_id=version_ids[i + 1][0],
                created=version_ids[i][1],
            )
            for i in range(len(version_ids) - 1)
        ]

    @property
    def applicant(self):
        if self.org_applicant:
            return self.org_applicant
        elif self.ind_applicant:
            return self.proposal_applicant
        elif self.proxy_applicant:
            return retrieve_email_user(self.proxy_applicant)
        else:
            logger.error(
                f"Applicant for the proposal {self.lodgement_number} not found"
            )
            return "No Applicant"

    @property
    def registration_of_interests(self):
        if self.application_type == APPLICATION_TYPE_REGISTRATION_OF_INTEREST:
            return True

    @property
    def lease_licence(self):
        if self.application_type == APPLICATION_TYPE_LEASE_LICENCE:
            return True

    @property
    def applicant_emails(self):
        if self.org_applicant:
            return get_admin_emails_for_organisation(self.org_applicant.id)
        elif self.ind_applicant:
            email_user = retrieve_email_user(self.ind_applicant)
        elif self.proxy_applicant:
            email_user = retrieve_email_user(self.proxy_applicant)
        elif self.submitter:
            email_user = retrieve_email_user(self.submitter)
        else:
            raise Exception(
                _(
                    f"No applicant or submitter found for Proposal: {self.lodgement_number}"
                )
            )

        return [email_user.email]

    @property
    def applicant_name(self):
        if isinstance(self.applicant, Organisation):
            return f"{self.applicant.ledger_organisation_name}"
        elif isinstance(self.applicant, ProposalApplicant):
            return self.applicant.full_name
        elif isinstance(self.applicant, EmailUser):
            return f"{self.applicant.first_name} {self.applicant.last_name}"
        logger.error(f"Applicant for the proposal {self.lodgement_number} not found")
        return "No Applicant"

    @property
    def applicant_details(self):
        if isinstance(self.applicant, Organisation):
            return "{} \n{}".format(
                self.org_applicant.ledger_organisation_id.name,
                self.org_applicant.address,
            )
        else:
            # return "{} {}\n{}".format(
            return "{} {}".format(
                self.applicant.first_name,
                self.applicant.last_name,
                # self.applicant.addresses.all().first()
            )

    @property
    def applicant_address(self):
        if isinstance(self.applicant, Organisation):
            return self.org_applicant.address
        else:
            return self.applicant.residential_address

    @property
    def applicant_id(self):
        return self.applicant.id

    @property
    def applicant_type(self):
        if self.org_applicant:
            return self.APPLICANT_TYPE_ORGANISATION
        elif self.ind_applicant:
            return self.APPLICANT_TYPE_INDIVIDUAL
        elif self.proxy_applicant:
            return self.APPLICANT_TYPE_PROXY
        else:
            return self.APPLICANT_TYPE_SUBMITTER

    @property
    def applicant_field(self):
        if self.org_applicant:
            return "org_applicant"
        elif self.ind_applicant:
            return "ind_applicant"
        elif self.proxy_applicant:
            return "proxy_applicant"
        else:
            return "submitter"

    def qa_officers(self, name=None):
        if not name:
            return (
                QAOfficerGroup.objects.get(default=True)
                .members.all()
                .values_list("email", flat=True)
            )
        else:
            return (
                QAOfficerGroup.objects.get(name=name)
                .members.all()
                .values_list("email", flat=True)
            )

    @property
    def get_history(self):
        """Return the prev proposal versions"""
        history_list = []
        p = copy.deepcopy(self)
        while p.previous_application:
            history_list.append(
                dict(
                    id=p.previous_application.id,
                    modified=p.previous_application.modified_date,
                )
            )
            p = p.previous_application
        return history_list

    @property
    def is_assigned(self):
        return self.assigned_officer is not None

    @property
    def is_temporary(self):
        return self.processing_status == "temp"

    @property
    def can_user_edit(self):
        """
        :return: True if the proposal is in one of the editable status.
        """
        return self.processing_status in self.CUSTOMER_EDITABLE_STATE

    @property
    def can_user_view(self):
        """
        :return: True if the proposal is in one of the approved status.
        """
        return self.processing_status in self.CUSTOMER_VIEWABLE_STATE

    def user_has_object_permission(self, user_id):
        """Used by the secure documents api to determine if the user can view the instance and any attached documents"""
        if self.org_applicant:
            return can_admin_org(self.org_applicant, user_id)
        return user_id in [
            self.ind_applicant,
            self.submitter,
            self.proxy_applicant,
        ]

    def can_discard(self, request):
        return (
            is_approver(request)
            and self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER
            or is_customer(request)
            and request.user.id == self.submitter
        )

    @property
    def is_deletable(self):
        """
        An proposal can be deleted only if it is a draft and it hasn't been lodged yet
        :return:
        """
        return self.processing_status == "draft" and not self.lodgement_number

    @property
    def latest_referrals(self):
        referrals = self.referrals
        return referrals.all()[: settings.LATEST_REFERRAL_COUNT]

    @property
    def external_referral_invites(self):
        return self.external_referee_invites.filter(
            archived=False, datetime_first_logged_in__isnull=True
        )

    @property
    def assessor_assessment(self):
        qs = self.assessment.filter(referral=None)
        return qs[0] if qs else None

    @property
    def referral_assessments(self):
        qs = self.assessment.exclude(referral=None)
        return qs if qs else None

    @property
    def permit(self):
        return self.approval.licence_document._file.url if self.approval else None

    @property
    def allowed_assessors(self):
        group = None
        if self.processing_status in [
            Proposal.PROCESSING_STATUS_WITH_APPROVER,
        ]:
            group = self.get_approver_group()
        elif self.processing_status in [
            Proposal.PROCESSING_STATUS_WITH_REFERRAL,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
        ]:
            group = self.get_assessor_group()

        if not group:
            return []

        emailusers = []
        for id in group.get_system_group_member_ids():
            emailuser = retrieve_email_user(id)
            emailusers.append(emailuser)

        return emailusers

    @property
    def allowed_approvers(self):
        return user_ids_in_group()

    @property
    def compliance_assessors(self):
        # group = self.get_assessor_group()
        # return group.members if group else []
        return self.get_assessor_group().get_system_group_member_ids()

    @property
    def can_officer_process(self):
        """:return: True if the proposal is in one of the processable status for Assessor role."""
        return (
            True
            if self.processing_status in Proposal.OFFICER_PROCESSABLE_STATE
            else True
        )

    @property
    def amendment_requests(self):
        qs = AmendmentRequest.objects.filter(proposal=self)
        return qs

    # Check if there is an pending amendment request exist for the proposal
    @property
    def pending_amendment_request(self):
        qs = AmendmentRequest.objects.filter(proposal=self, status="requested")
        if qs:
            return True
        return False

    @property
    def is_amendment_proposal(self):
        if self.proposal_type == ProposalType.objects.get(code=PROPOSAL_TYPE_AMENDMENT):
            return True
        return False

    @property
    def additional_documents(self):
        return AdditionalDocument.objects.filter(
            proposal_additional_document_type__proposal=self
        )

    @property
    def additional_documents_missing(self):
        # Check if the proposal has all the required additional documents
        return (
            self.additional_document_types.filter(document__isnull=True)
            .annotate(name=F("additional_document_type__name"))
            .values("name")
        )

    def get_assessor_group(self):
        return SystemGroup.objects.get(name=GROUP_NAME_ASSESSOR)

    def get_approver_group(self):
        return SystemGroup.objects.get(name=GROUP_NAME_APPROVER)

    def __check_proposal_filled_out(self):
        if not self.data:
            raise exceptions.ProposalNotComplete()
        missing_fields = []
        required_fields = {}
        for k, v in required_fields.items():
            val = getattr(self, k)
            if not val:
                missing_fields.append(v)
        return missing_fields

    @property
    def assessor_recipients(self):
        logger.info("assessor_recipients")
        recipients = []
        group_ids = self.get_assessor_group().get_system_group_member_ids()
        for id in group_ids:
            logger.info(id)
            recipient = retrieve_email_user(id)
            recipients.append(recipient.email)
        return recipients

    @property
    def approver_recipients(self):
        logger.info("approver_recipients")
        recipients = []
        group_ids = self.get_approver_group().get_system_group_member_ids()
        for id in group_ids:
            logger.info(id)
            recipient = retrieve_email_user(id)
            recipients.append(recipient.email)
        return recipients

    # Check if the user is member of assessor group for the Proposal
    def is_assessor(self, user):
        return user.id in self.get_assessor_group().get_system_group_member_ids()

    # Check if the user is member of assessor group for the Proposal
    def is_approver(self, user):
        return user.id in self.get_assessor_group().get_system_group_member_ids()

    def can_action(self, user):
        if not self.can_assess(user):
            return False

    def can_assess(self, user):
        if self.processing_status in [
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
            Proposal.PROCESSING_STATUS_WITH_REFERRAL,
        ]:
            logger.info("self.__assessor_group().get_system_group_member_ids()")
            logger.info(self.get_assessor_group().get_system_group_member_ids())
            return user.id in self.get_assessor_group().get_system_group_member_ids()
        elif self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER:
            return user.id in self.get_approver_group().get_system_group_member_ids()
        else:
            return False

    def is_referee(self, user):
        """
        Returns whether `user` is a referee for this proposal
        """

        return (
            self.processing_status
            in [
                self.PROCESSING_STATUS_WITH_REFERRAL,
            ]
            and Referral.objects.filter(proposal=self, referral=user.id).exists()
        )

    def referee_can_edit_referral(self, user):
        """
        Returns whether `user` is a referrer who can still edit this proposal's referral
        """

        if self.is_referee(user):
            # Get this proposal's referral where the requesting user is the referee
            try:
                referral = Referral.objects.get(proposal=self, referral=user.id)
            except Referral.DoesNotExist:
                logger.warning(
                    f"Referral with Proposal: {self} and referral user id: {user.id} does not exist"
                )
                return False

            if referral.processing_status in [
                Referral.PROCESSING_STATUS_COMPLETED,
                Referral.PROCESSING_STATUS_RECALLED,
            ]:
                return False

            return True

        return False

    def can_edit_period(self, user):
        if (
            self.processing_status == Proposal.PROCESSING_STATUS_WITH_ASSESSOR
            or self.processing_status
            == Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS
        ):
            # return self.__assessor_group() in user.proposalassessorgroup_set.all()
            return user.id in self.get_assessor_group().get_system_group_member_ids()
        else:
            return False

    def assessor_comments_view(self, user):
        if (
            self.processing_status == Proposal.PROCESSING_STATUS_WITH_ASSESSOR
            or self.processing_status == Proposal.PROCESSING_STATUS_WITH_REFERRAL
            or self.processing_status
            == Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS
            or self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER
        ):
            try:
                referral = Referral.objects.get(proposal=self, referral=user.id)
            except Referral.DoesNotExist:
                referral = None
            if referral:
                return True
            elif user.id in self.get_assessor_group().get_system_group_member_ids():
                return True
            elif user.id in self.get_approver_group().get_system_group_member_ids():
                return True
            else:
                return False
        else:
            return False

    def has_assessor_mode(self, user):
        status_without_assessor = [
            Proposal.PROCESSING_STATUS_WITH_APPROVER,
            "approved",
            "waiting_payment",
            "declined",
            "draft",
        ]
        if self.processing_status in status_without_assessor:
            return False
        else:
            if self.assigned_officer:
                if self.assigned_officer == user.id:
                    # return self.__assessor_group() in user.proposalassessorgroup_set.all()
                    return (
                        user.id
                        in self.get_assessor_group().get_system_group_member_ids()
                    )
                else:
                    return False
            else:
                # return self.__assessor_group() in user.proposalassessorgroup_set.all()
                return (
                    user.id in self.get_assessor_group().get_system_group_member_ids()
                )

    def log_user_action(self, action, request):
        return ProposalUserAction.log_action(self, action, request.user.id)

    @transaction.atomic
    def update(self, request, viewset):
        from leaseslicensing.components.proposals.utils import save_proponent_data

        if self.can_user_edit:
            # Save the data first
            save_proponent_data(self, request, viewset)
            self.save()
        else:
            raise ValidationError("You can't edit this proposal at this moment")

    @transaction.atomic
    def send_referral(self, request, referral_email, referral_text):
        if self.processing_status not in [
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
            Proposal.PROCESSING_STATUS_WITH_REFERRAL,
        ]:
            raise exceptions.ProposalReferralCannotBeSent()

        self.processing_status = Proposal.PROCESSING_STATUS_WITH_REFERRAL
        self.save()

        referral_email = referral_email.lower()

        # Check if the user exists in the ledger database
        if not EmailUser.objects.filter(email__icontains=referral_email).exists():
            raise ValidationError(
                "The user you want to send the referral to does not have an account in ledger."
            )

        user = EmailUser.objects.get(email__icontains=referral_email)

        if Referral.objects.filter(referral=user.id, proposal=self).exists():
            raise ValidationError(
                "A referral has already been sent to this user for this proposal"
            )

        referral, created = Referral.objects.get_or_create(
            referral=user.id,
            proposal=self,
            defaults={
                "sent_by": request.user.id,
                "text": referral_text,
                "assigned_officer": request.user.id,
            },
        )
        if created:
            logger.info(f"Referral created: {referral}")

        # Create a log entry for the proposal
        self.log_user_action(
            ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(
                referral.id,
                self.lodgement_number,
                f"{user.get_full_name()}({user.email})",
            ),
            request,
        )

        # Create a log entry for the applicant
        self.applicant.log_user_action(
            ProposalUserAction.ACTION_SEND_REFERRAL_TO.format(
                referral.id,
                self.lodgement_number,
                f"{user.get_full_name()}({user.email})",
            ),
            request,
        )

        # send email
        send_referral_email_notification(
            referral,
            [
                user.email,
            ],
            request,
        )

    @transaction.atomic
    def assign_officer(self, request, officer):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if not self.can_assess(officer):
            raise ValidationError(
                "The selected person is not authorised to be assigned to this proposal"
            )
        if self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER:
            if officer.id != self.assigned_approver:
                self.assigned_approver = officer.id
                self.save()
                # Create a log entry for the proposal
                self.log_user_action(
                    ProposalUserAction.ACTION_ASSIGN_TO_APPROVER.format(
                        self.id,
                        f"{officer.get_full_name()}({officer.email})",
                    ),
                    request,
                )

                # Log entry for applicant
                self.applicant.log_user_action(
                    ProposalUserAction.ACTION_DECLINE.format(self.id), request
                )

        else:
            if officer.id != self.assigned_officer:
                self.assigned_officer = officer.id
                self.save()
                # Create a log entry for the proposal
                self.log_user_action(
                    ProposalUserAction.ACTION_ASSIGN_TO_ASSESSOR.format(
                        self.id,
                        f"{officer.get_full_name()}({officer.email})",
                    ),
                    request,
                )

                # Log entry for applicant
                self.applicant.log_user_action(
                    ProposalUserAction.ACTION_DECLINE.format(self.id), request
                )

    @transaction.atomic
    def assing_approval_level_document(self, request):
        approval_level_document = request.data["approval_level_document"]
        if approval_level_document != "null":
            try:
                document = self.documents.get(input_name=str(approval_level_document))
            except ProposalDocument.DoesNotExist:
                document, created = self.documents.get_or_create(
                    input_name=str(approval_level_document),
                    name=str(approval_level_document),
                )[0]
            document.name = str(approval_level_document)
            # commenting out below tow lines - we want to retain all past attachments - reversion can use them
            # if document._file and os.path.isfile(document._file.path):
            #    os.remove(document._file.path)
            document._file = approval_level_document
            document.save()
            d = ProposalDocument.objects.get(id=document.id)
            self.approval_level_document = d
            comment = f"Approval Level Document Added: {document.name}"
        else:
            self.approval_level_document = None
            comment = "Approval Level Document Deleted: {}".format(
                request.data["approval_level_document_name"]
            )
        # self.save()
        self.save(
            version_comment=comment
        )  # to allow revision to be added to reversion history
        self.log_user_action(
            ProposalUserAction.ACTION_APPROVAL_LEVEL_DOCUMENT.format(self.id),
            request,
        )

        # Log entry for applicant
        self.applicant.log_user_action(
            ProposalUserAction.ACTION_DECLINE.format(self.id), request
        )

        return self

    @transaction.atomic
    def unassign(self, request):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER:
            if self.assigned_approver:
                self.assigned_approver = None
                self.save()

                # Create a log entry for the proposal
                self.log_user_action(
                    ProposalUserAction.ACTION_UNASSIGN_APPROVER.format(self.id),
                    request,
                )

                # Log entry for applicant
                self.applicant.log_user_action(
                    ProposalUserAction.ACTION_DECLINE.format(self.id), request
                )
        else:
            if self.assigned_officer:
                self.assigned_officer = None
                self.save()

                # Create a log entry for the proposal
                self.log_user_action(
                    ProposalUserAction.ACTION_UNASSIGN_ASSESSOR.format(self.id),
                    request,
                )

                # Log entry for applicant
                self.applicant.log_user_action(
                    ProposalUserAction.ACTION_DECLINE.format(self.id), request
                )

    def add_default_requirements(self):
        # Add default standard requirements to Proposal
        default_requirements = ProposalStandardRequirement.objects.filter(
            application_type=self.application_type, default=True, obsolete=False
        )
        for req in default_requirements:
            r, created = ProposalRequirement.objects.get_or_create(
                proposal=self, standard_requirement=req
            )
            if created:
                logger.info(f"Created Proposal Requirement: {r}")

    def move_to_status(self, request, status, approver_comment):
        if not self.can_assess(request.user) and not self.is_referee(request.user):
            raise exceptions.ProposalNotAuthorized()

        if self.processing_status == status:
            return

        if status in [
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
            Proposal.PROCESSING_STATUS_WITH_APPROVER,
        ]:
            if (
                self.processing_status == Proposal.PROCESSING_STATUS_WITH_REFERRAL
                or self.can_user_edit
            ):
                raise ValidationError(
                    "You cannot change the current status at this time"
                )

            if self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER:
                self.approver_comment = ""
                if approver_comment:
                    self.approver_comment = approver_comment
                    self.save()
                    send_proposal_approver_sendback_email_notification(request, self)
            self.processing_status = status
            self.save()
            # Only add standard requirements if no requirements exist so far
            if (
                status == Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS
                and len(self.requirements.all()) == 0
            ):
                self.add_default_requirements()

            # Lock the proposal geometries associated with this proposal and owned by the current user
            ProposalGeometry.objects.filter(proposal=self).exclude(
                Q(locked=True) | ~Q(drawn_by=request.user.id)
            ).update(**{"locked": True})

            # Create a log entry for the proposal
            if self.processing_status == self.PROCESSING_STATUS_WITH_ASSESSOR:
                self.log_user_action(
                    ProposalUserAction.ACTION_BACK_TO_PROCESSING.format(self.id),
                    request,
                )
            elif (
                self.processing_status
                == self.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS
            ):
                self.log_user_action(
                    ProposalUserAction.ACTION_ENTER_REQUIREMENTS.format(self.id),
                    request,
                )
        elif status in [
            self.PROCESSING_STATUS_WITH_REFERRAL,
        ]:
            self.processing_status = status
            self.save()
        else:
            raise ValidationError("The provided status cannot be found.")

    def reissue_approval(self):
        if not self.processing_status == "approved":
            raise ValidationError(
                f"You cannot reissue Proposal: {self.lodgement_number} because it is not approved."
            )

        if not self.approval:
            raise ValidationError(
                f"You cannot reissue Proposal: {self.lodgement_number} because it has no approval attached."
            )

        if not self.approval.can_reissue:
            raise ValidationError(
                f"You cannot reissue Proposal: {self.lodgement_number}"
                f"because the can_renew method on the attached Approval: {self.approval} returns False."
            )

        self.processing_status = self.PROCESSING_STATUS_WITH_APPROVER
        self.save(
            version_comment="Reissue Approval: {}".format(
                self.approval.lodgement_number
            )
        )

    @transaction.atomic
    def proposed_decline(self, request, details):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if self.processing_status not in [
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
        ]:
            raise ValidationError(
                "You cannot propose to decline a proposal unless it's status is with assessor"
            )

        non_field_errors = []
        reason = details.get("reason")
        # Input validation check
        if not reason:
            non_field_errors.append("You must add details text")
        if non_field_errors:
            raise serializers.ValidationError(non_field_errors)

        ProposalDeclinedDetails.objects.update_or_create(
            proposal=self,
            defaults={
                "officer": request.user.id,
                "reason": reason,
                "cc_email": details.get("cc_email", None),
            },
        )
        self.proposed_decline_status = True
        approver_comment = ""
        self.move_to_status(
            request, Proposal.PROCESSING_STATUS_WITH_APPROVER, approver_comment
        )
        # Log proposal action
        self.log_user_action(
            ProposalUserAction.ACTION_PROPOSED_DECLINE.format(self.id), request
        )

        # Log entry for applicant
        self.applicant.log_user_action(
            ProposalUserAction.ACTION_DECLINE.format(self.id), request
        )

        send_approver_decline_email_notification(reason, request, self)

    @transaction.atomic
    def final_decline(self, request, details):
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()

        if self.processing_status != Proposal.PROCESSING_STATUS_WITH_APPROVER:
            raise ValidationError(
                "You cannot decline this proposal as it is not with approver"
            )

        cc_email = details.get("cc_email", None)

        # Add the initiator of the transfer to the cc list if appropriate
        if self.proposal_type.code == PROPOSAL_TYPE_TRANSFER:
            if self.approval.active_transfer:
                initiator_id = self.approval.active_transfer.initiator
                if initiator_id:
                    initiator = retrieve_email_user(initiator_id)
                    if not cc_email:
                        cc_email = initiator.email
                    elif initiator.email not in cc_email:
                        cc_email += f",{initiator.email}"
                else:
                    logger.warning(
                        f"Active transfer {self.approval.active_transfer} has no initiator"
                    )
            else:
                logger.warning(
                    f"Proposal of type {self.proposal_type} has no active transfer"
                )

        (
            proposal_decline,
            created,
        ) = ProposalDeclinedDetails.objects.update_or_create(
            proposal=self,
            defaults={
                "officer": request.user.id,
                "reason": details.get("reason"),
                "cc_email": cc_email,
            },
        )
        if created:
            logger.info(f"Created ProposalDeclinedDetails instance: {created}")

        self.proposed_decline_status = True
        self.processing_status = Proposal.PROCESSING_STATUS_DECLINED
        self.save()

        if (
            self.proposal_type.code == PROPOSAL_TYPE_TRANSFER
            and self.approval.active_transfer
        ):
            active_transfer = self.approval.active_transfer
            active_transfer.processing_status = (
                active_transfer.APPROVAL_TRANSFER_STATUS_DECLINED
            )
            active_transfer.save()

        # Log proposal action
        self.log_user_action(ProposalUserAction.ACTION_DECLINE.format(self.id), request)

        # Log entry for applicant
        self.applicant.log_user_action(
            ProposalUserAction.ACTION_DECLINE.format(self.id), request
        )

        send_proposal_decline_email_notification(self, request, proposal_decline)

    def store_proposed_approval_data(self, request, details):
        # Input validation check
        non_field_errors = []
        if not details.get("details"):
            non_field_errors.append("You must add details text")
        if (
            self.application_type.name == APPLICATION_TYPE_REGISTRATION_OF_INTEREST
            and not details.get("decision")
        ):
            non_field_errors.append("You must select a decision")
        elif self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE:
            if not details.get("approval_type"):
                non_field_errors.append("You must select an Approval Type")
            if not details.get("start_date"):
                non_field_errors.append("You must select a Start Date")
            if not details.get("expiry_date"):
                non_field_errors.append("You must select an Expiry Date")
        if non_field_errors:
            raise serializers.ValidationError(non_field_errors)

        # Store proposed approval values
        if self.application_type.name == APPLICATION_TYPE_REGISTRATION_OF_INTEREST:
            self.proposed_issuance_approval = {
                "details": details.get("details"),
                "cc_email": details.get("cc_email"),
                "decision": details.get("decision"),
                "record_management_number": details.get("record_management_number"),
            }
            if (
                not self.competitive_process_to_copy_to
                and details.get("decision")
                == settings.APPROVE_ADD_TO_EXISTING_COMPETITIVE_PROCESS
            ):
                try:
                    competitive_process_id = details.get("competitive_process")
                    competitive_process = CompetitiveProcess.objects.get(
                        id=competitive_process_id
                    )
                except CompetitiveProcess.DoesNotExist:
                    raise serializers.ValidationError(
                        f"No competitve process found with ID {competitive_process_id}"
                    )
                self.competitive_process_to_copy_to = competitive_process

        elif self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE:
            # start_date = details.get('start_date').strftime('%d/%m/%Y') if details.get('start_date') else None
            # expiry_date = details.get('expiry_date').strftime('%d/%m/%Y') if details.get('expiry_date') else None
            self.proposed_issuance_approval = {
                "approval_type": details.get("approval_type"),
                # "approval_sub_type": details.get("approval_sub_type"),
                "selected_document_types": details.get("selected_document_types"),
                # "approval_type_document_type": details.get("approval_type_document_type"),
                "cc_email": details.get("cc_email"),
                "details": details.get("details"),
                "record_management_number": details.get("record_management_number"),
                "start_date": details.get("start_date"),
                "expiry_date": details.get("expiry_date"),
            }
            # Check mandatory docs
            mandatory_doc_errors = []
            from leaseslicensing.components.approvals.models import (
                ApprovalTypeDocumentTypeOnApprovalType,
            )

            approval_type = details.get("approval_type")
            for (
                approval_type_document
            ) in ApprovalTypeDocumentTypeOnApprovalType.objects.filter(
                approval_type_id=approval_type, mandatory=True
            ):
                if not self.lease_licence_approval_documents.filter(
                    approval_type=approval_type_document.approval_type,
                    approval_type_document_type=approval_type_document.approval_type_document_type,
                ):
                    mandatory_doc_errors.append(
                        "Missing mandatory document/s: Approval Type {}, Document Type {}".format(
                            approval_type_document.approval_type,
                            approval_type_document.approval_type_document_type,
                        )
                    )
            if mandatory_doc_errors:
                raise serializers.ValidationError(mandatory_doc_errors)

        if self.processing_status in [
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS,
        ]:
            Proposal.PROCESSING_STATUS_WITH_ASSESSOR
            # Add date of approval and the approver to `proposed_issuance_approval` dictionary
            self.proposed_issuance_approval["assessed_on"] = timezone.now().timestamp()
            self.proposed_issuance_approval["assessed_by"] = request.user.id

        if self.processing_status == Proposal.PROCESSING_STATUS_WITH_APPROVER:
            # Add date of approval and the approver to `proposed_issuance_approval` dictionary
            self.proposed_issuance_approval["approved_on"] = timezone.now().timestamp()
            self.proposed_issuance_approval["approved_by"] = request.user.id

        self.save()

    @transaction.atomic
    def proposed_approval(self, request, details):
        # User check
        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        # Processing status check
        if not (
            (
                self.application_type.name == APPLICATION_TYPE_REGISTRATION_OF_INTEREST
                and self.processing_status == Proposal.PROCESSING_STATUS_WITH_ASSESSOR
            )
            or (
                self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE
                and self.processing_status
                == Proposal.PROCESSING_STATUS_WITH_ASSESSOR_CONDITIONS
            )
        ):
            raise ValidationError("You cannot propose for approval")

        self.store_proposed_approval_data(request, details)

        self.proposed_decline_status = False
        approver_comment = ""
        self.move_to_status(
            request, Proposal.PROCESSING_STATUS_WITH_APPROVER, approver_comment
        )
        self.assigned_officer = None
        self.save()
        # Log proposal action
        self.log_user_action(
            ProposalUserAction.ACTION_PROPOSED_APPROVAL.format(self.id), request
        )

        # Log entry for applicant
        self.applicant.log_user_action(
            ProposalUserAction.ACTION_DECLINE.format(self.id), request
        )

        send_approver_approve_email_notification(request, self)

    def preview_document(self, request, details):
        from leaseslicensing.components.approvals.document import (
            ApprovalDocumentGenerator,
        )

        document_generator = ApprovalDocumentGenerator()
        try:
            return document_generator.preview_approval_document()
        except NotImplementedError as e:
            raise e

    @transaction.atomic()
    def final_approval(self, request, details):
        from leaseslicensing.components.approvals.models import (
            Approval,
            ApprovalDocument,
            ApprovalType,
        )

        if not self.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        if self.processing_status != Proposal.PROCESSING_STATUS_WITH_APPROVER:
            raise ValidationError(
                "You cannot issue the approval if it is not with an approver"
            )
        if not self.applicant_address:
            raise ValidationError(
                "The proponent needs to have set their postal address before approving this proposal."
            )

        self.proposed_decline_status = False
        record_management_number = self.proposed_issuance_approval.get(
            "record_management_number", None
        )
        self.store_proposed_approval_data(request, details)

        # Log proposal action
        self.log_user_action(
            ProposalUserAction.ACTION_ISSUE_APPROVAL_.format(self.id), request
        )

        checking_proposal = self
        proposal_type_comment_names = {t1: t2 for t1, t2 in settings.PROPOSAL_TYPES}

        if (
            self.proposal_type.code == PROPOSAL_TYPE_AMENDMENT
            and self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE
        ):
            # Lease License (Amendment or Renewal)
            if self.previous_application.approval.id != self.approval.id:
                raise ValidationError(
                    "The previous application's approval does not match the current approval."
                )
            proposal_type_comment_name = proposal_type_comment_names[
                PROPOSAL_TYPE_AMENDMENT
            ]
            logger.info(f"Approval {proposal_type_comment_name} for {self}")

            start_date = details.get("start_date", None)
            expiry_date = details.get("expiry_date", None)
            approval_type = ApprovalType.objects.get(id=details["approval_type"])

            approval, created = Approval.objects.update_or_create(
                current_proposal=self.approval.current_proposal,
                defaults={
                    "issue_date": timezone.now(),  # Update the issue date as the old ones
                    # can be fetched from the reversion history
                    "expiry_date": datetime.datetime.strptime(
                        expiry_date, "%Y-%m-%d"
                    ).date(),
                    "start_date": datetime.datetime.strptime(
                        start_date, "%Y-%m-%d"
                    ).date(),
                    "status": Approval.APPROVAL_STATUS_CURRENT,
                    "current_proposal": self,
                    "renewal_review_notification_sent_to_assessors": False,
                    "renewal_notification_sent_to_holder": False,
                    "approval_type": approval_type,
                },
            )
            if created:
                logger.info(f"Created Approval: {approval}")

            # Update the approval documents
            self.generate_license_documents(
                approval, reason=ApprovalDocument.REASON_AMENDED
            )

            # Create a versioned approval save
            approval.save(
                version_comment=f"Confirmed Lease License - {proposal_type_comment_name}"
            )

            # TODO: Refine Amendment Requirements
            # (If the start date can be amended then that will create complications for invoicing)
            self.generate_compliances(approval, request)
            self.generate_invoicing_details()

            self.processing_status = (
                Proposal.PROCESSING_STATUS_APPROVED_EDITING_INVOICING
            )

            self.approved_by = request.user.id

            # Send notification email to applicant
            send_proposal_approval_email_notification(self, request)
            self.save(
                version_comment=(
                    f"Lease License Approval: {self.approval.lodgement_number} {ApprovalDocument.REASON_AMENDED}"
                )
            )
        elif (
            self.proposal_type.code == PROPOSAL_TYPE_TRANSFER
            and self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE
        ):
            from leaseslicensing.components.approvals.models import ApprovalTransfer

            approval = self.approval
            if approval.has_outstanding_compliances:
                raise ValidationError(
                    f"Unable to transfer lease license {approval} as it has outstanding compliances."
                )
            if approval.has_outstanding_invoices:
                raise ValidationError(
                    f"Unable to transfer lease license {approval} as it has outstanding invoices."
                )

            if approval.has_missing_gross_turnover_entries:
                raise ValidationError(
                    f"Unable to transfer lease license {approval} as it has missing gross turnover entries."
                )

            # Discard any future compliances and invoices for the current holder of the approval
            # as new invoices and compliances will be generated for the new holder
            approval.discard_future_compliances()
            approval.discard_future_invoices()

            # Set the current proposal for the approval to this transfer proposal
            approval.current_proposal = self
            approval_transfer = approval.transfers.filter(
                processing_status=ApprovalTransfer.APPROVAL_TRANSFER_STATUS_PENDING
            ).first()
            if not approval_transfer:
                raise ValidationError(
                    f"Unable to transfer lease license {approval} as there is no pending transfer."
                )
            approval_transfer.processing_status = (
                ApprovalTransfer.APPROVAL_TRANSFER_STATUS_ACCEPTED
            )
            approval_transfer.save()

            # Generate the approval documents
            self.generate_license_documents(
                approval, reason=ApprovalDocument.REASON_TRANSFERRED
            )

            # Create a versioned approval save
            proposal_type_comment_name = proposal_type_comment_names[
                PROPOSAL_TYPE_TRANSFER
            ]

            self.generate_compliances(approval, request, only_future=True)

            self.processing_status = (
                Proposal.PROCESSING_STATUS_APPROVED_EDITING_INVOICING
            )

            approval.save(
                version_comment=f"Confirmed Lease License - {proposal_type_comment_name}"
            )
            self.save(
                version_comment=(
                    f"Lease License Approval: {self.approval.lodgement_number} {ApprovalDocument.REASON_TRANSFERRED}"
                )
            )

        elif self.proposal_type.code in [
            PROPOSAL_TYPE_NEW,
            PROPOSAL_TYPE_RENEWAL,
            PROPOSAL_TYPE_MIGRATION,
        ]:
            if self.application_type.name == APPLICATION_TYPE_REGISTRATION_OF_INTEREST:
                if (
                    self.proposed_issuance_approval.get("decision")
                    == settings.APPROVE_LEASE_LICENCE
                    and not self.generated_proposal
                ):
                    lease_licence = (
                        self.create_lease_licence_from_registration_of_interest()
                    )

                    self.generated_proposal = lease_licence

                    # Copy over previous site name
                    copy_site_name(self, lease_licence)

                    # Copy over previous groups
                    copy_groups(self, lease_licence)

                    # Copy over previous proposal geometry
                    copy_proposal_geometry(self, lease_licence)

                    # Copy over previous gis data
                    copy_gis_data(self, lease_licence)

                    # Copy the ROI deed poll over if there is one
                    copy_deed_poll_documents(self, lease_licence)

                    self.processing_status = (
                        Proposal.PROCESSING_STATUS_APPROVED_REGISTRATION_OF_INTEREST
                    )
                elif (
                    self.proposed_issuance_approval.get("decision")
                    == settings.APPROVE_COMPETITIVE_PROCESS
                    and not self.generated_proposal
                ):
                    self.generate_competitive_process()
                    # Email notify all Competitive Process assessors
                    send_competitive_process_create_notification(
                        request,
                        self.generated_competitive_process,
                        details=details,
                    )

                    # Copy any relevant details from the ROI to the Competitive Process
                    copy_site_name_to_competitive_process(
                        self, self.generated_competitive_process
                    )
                    copy_groups_to_competitive_process(
                        self, self.generated_competitive_process
                    )
                    copy_proposal_geometry_to_competitive_process(
                        self, self.generated_competitive_process
                    )
                    copy_gis_data_to_competitive_process(
                        self, self.generated_competitive_process
                    )

                    # Add the applicant from the ROI as a party to the CP
                    create_competitive_process_party_from_proposal(
                        self, self.generated_competitive_process
                    )

                    self.processing_status = (
                        Proposal.PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS
                    )
                elif (
                    self.proposed_issuance_approval.get("decision")
                    == settings.APPROVE_ADD_TO_EXISTING_COMPETITIVE_PROCESS
                    and not self.generated_proposal
                ):
                    if not self.competitive_process_to_copy_to:
                        raise ValidationError(
                            f"No competitive process selected to copy to for ROI {self}"
                        )
                    # Add the applicant from the ROI as a party to the CP
                    create_competitive_process_party_from_proposal(
                        self, self.competitive_process_to_copy_to
                    )

                    self.processing_status = (
                        Proposal.PROCESSING_STATUS_APPROVED_COMPETITIVE_PROCESS
                    )
            elif self.application_type.name == APPLICATION_TYPE_LEASE_LICENCE:
                start_date = details.get("start_date", None)
                expiry_date = details.get("expiry_date", None)
                approval_type = ApprovalType.objects.get(id=details["approval_type"])

                approval, created = Approval.objects.update_or_create(
                    current_proposal=checking_proposal,
                    defaults={
                        "issue_date": timezone.now(),
                        "expiry_date": datetime.datetime.strptime(
                            expiry_date, "%Y-%m-%d"
                        ).date(),
                        "start_date": datetime.datetime.strptime(
                            start_date, "%Y-%m-%d"
                        ).date(),
                        "record_management_number": record_management_number,
                        "approval_type": approval_type,
                    },
                )
                # Generate the approval documents
                self.generate_license_documents(
                    approval, reason=ApprovalDocument.REASON_NEW
                )

                approval.save(
                    version_comment=f"Confirmed Lease License - {proposal_type_comment_names[PROPOSAL_TYPE_NEW]}"
                )

                self.approval = approval
                self.save()
                self.generate_compliances(approval, request)
                self.generate_invoicing_details()
                # Update the current proposal's status
                self.processing_status = (
                    Proposal.PROCESSING_STATUS_APPROVED_EDITING_INVOICING
                )
                send_license_ready_for_invoicing_notification(self, request)

                if self.proposal_type.code == PROPOSAL_TYPE_RENEWAL:
                    # Update the status for the approval that is being renewed from pending renewal back to current
                    self.previous_application.approval.status = (
                        Approval.APPROVAL_STATUS_CURRENT
                    )
                    self.previous_application.approval.save()

            self.approved_by = request.user.id

            # Send notification email to applicant
            send_proposal_approval_email_notification(self, request)

            if self.approval:
                self.save(
                    version_comment=f"Lease License Approval: {self.approval.lodgement_number}"
                )
                if self.approval.documents:
                    self.approval.documents.all().update(can_delete=False)
            else:
                self.save(
                    version_comment=f"Registration of Interest Approval: {self.lodgement_number}"
                )
        else:
            # Using this clause to raise an error when no other condition is met,
            # so nothing is written to the database without prior checks.
            raise ValidationError(
                "Proposal or Application type not supported for approval issuance"
            )

    @transaction.atomic
    def create_lease_licence_from_registration_of_interest(self):
        try:
            lease_licence_proposal = Proposal.objects.create(
                application_type=ApplicationType.objects.get(
                    name=APPLICATION_TYPE_LEASE_LICENCE
                ),
                submitter=None,
                ind_applicant=self.ind_applicant,
                org_applicant=self.org_applicant,
                proposal_type_id=self.proposal_type.id,
            )
        except IntegrityError as e:
            logger.exception(e)
            raise e
        except Exception as e:
            logger.exception(e)
            raise e
        else:
            if not self.org_applicant:
                original_applicant = ProposalApplicant.objects.get(proposal=self)
                # Creating a copy for the new proposal here. This will be invoked from renew and amend approval
                original_applicant.copy_self_to_proposal(lease_licence_proposal)

            for geo in self.proposalgeometry.all():
                # add geometry
                new_geo = deepcopy(geo)
                new_geo.proposal = lease_licence_proposal
                new_geo.copied_from = geo
                new_geo.id = None
                new_geo.drawn_by = geo.drawn_by
                new_geo.locked = geo.locked
                new_geo.save()

            send_proposal_roi_approval_email_notification(self, lease_licence_proposal)

            return lease_licence_proposal

    def generate_compliances(self, approval, request, only_future=False):
        today = timezone.now().date()
        from leaseslicensing.components.compliances.models import (
            Compliance,
            ComplianceUserAction,
        )

        # For amendment type of Proposal, check for copied requirements from previous proposal
        if self.proposal_type == PROPOSAL_TYPE_AMENDMENT:
            for r in self.requirements.filter(copied_from__isnull=False):
                cs = []
                cs = Compliance.objects.filter(
                    requirement=r.copied_from,
                    proposal=self.previous_application,
                    processing_status=Compliance.PROCESSING_STATUS_DUE,
                )
                if cs:
                    if r.is_deleted:
                        for c in cs:
                            c.processing_status = Compliance.PROCESSING_STATUS_DISCARDED
                            c.reminder_sent = True
                            c.post_reminder_sent = True
                            c.save()
                    if not r.is_deleted:
                        for c in cs:
                            c.proposal = self
                            c.approval = approval
                            c.requirement = r
                            c.save()

        requirements = self.requirements.exclude(is_deleted=True).exclude(
            standard_requirement__gross_turnover_required=True
        )

        # First, process all the requirements that are not related to gross turnover
        for req in requirements:
            if req.due_date and req.due_date >= today:
                current_date = req.due_date
                # create a first Compliance
                try:
                    compliance = Compliance.objects.get(
                        requirement=req, due_date=current_date
                    )
                except Compliance.DoesNotExist:
                    compliance = Compliance.objects.create(
                        proposal=self,
                        due_date=current_date,
                        processing_status=Compliance.PROCESSING_STATUS_FUTURE,
                        approval=approval,
                        requirement=req,
                    )
                    compliance.log_user_action(
                        ComplianceUserAction.ACTION_CREATE.format(compliance.id),
                        request,
                    )
                if req.recurrence:
                    while current_date < approval.expiry_date:
                        for x in range(req.recurrence_schedule):
                            # Weekly
                            if req.recurrence_pattern == 1:
                                current_date += relativedelta(weeks=1)
                            # Monthly
                            elif req.recurrence_pattern == 2:
                                current_date += relativedelta(months=1)
                            # Yearly
                            elif req.recurrence_pattern == 3:
                                current_date += relativedelta(years=1)

                        # Create the compliance
                        if current_date <= approval.expiry_date:
                            try:
                                compliance = Compliance.objects.get(
                                    requirement=req, due_date=current_date
                                )
                            except Compliance.DoesNotExist:
                                compliance = Compliance.objects.create(
                                    proposal=self,
                                    due_date=current_date,
                                    processing_status=Compliance.PROCESSING_STATUS_FUTURE,
                                    approval=approval,
                                    requirement=req,
                                )
                                compliance.log_user_action(
                                    ComplianceUserAction.ACTION_CREATE.format(
                                        compliance.id
                                    ),
                                    request,
                                )

        self.generate_gross_turnover_compliances(only_future=only_future)

    def generate_gross_turnover_compliances(self, only_future=False):
        from leaseslicensing.components.compliances.models import Compliance

        # Check if this proposal has any gross turnover based requirements
        if (
            not self.requirements.exclude(is_deleted=True)
            .filter(standard_requirement__gross_turnover_required=True)
            .exists()
        ):
            return

        try:
            annual_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_ANNUALLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                f"ProposalStandardRequirement not found: code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_ANNUALLY}"
            )
            raise

        try:
            quarterly_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                f"ProposalStandardRequirement not found: "
                f"code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY}"
            )
            raise

        try:
            monthly_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                f"ProposalStandardRequirement not found: "
                f"code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY}"
            )
            raise

        # All proposal that have gross turnover requirements require annual financial statements
        (
            annual_gross_turnover_requirement,
            created,
        ) = ProposalRequirement.objects.get_or_create(
            proposal=self,
            standard_requirement=annual_standard_requirement,
            is_deleted=False,
        )
        if created:
            logger.info(
                f"Created Proposal Requirement: {annual_gross_turnover_requirement}"
            )

        financial_years_included = invoicing_utils.financial_years_included_in_range(
            self.approval.start_date, self.approval.expiry_date
        )
        for financial_year in financial_years_included:
            if only_future and invoicing_utils.financial_year_has_passed(
                financial_year
            ):
                continue

            due_date = datetime.date(int(financial_year.split("-")[1]), 10, 31)
            compliance, created = Compliance.objects.get_or_create(
                proposal=self,
                approval=self.approval,
                requirement=annual_gross_turnover_requirement,
                due_date=due_date,
                processing_status=Compliance.PROCESSING_STATUS_FUTURE,
            )
            if created:
                logger.info(f"Compliance created: {compliance} for Proposal: {self}")
                compliance.text = (
                    "Please enter the gross turnover and upload an audited "
                    f"financial statement for the financial year {financial_year}"
                )
                compliance.save()

        invoicing_details = self.approval.current_proposal.invoicing_details

        if (
            invoicing_details.charge_method.key
            == settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ADVANCE
        ):
            # Don't generate quarterly or monthly compliances for percentage of gross turnover advance invoicing
            # Remove any such compliances
            deleted = self.compliances.filter(
                Q(requirement__standard_requirement=quarterly_standard_requirement)
                | Q(requirement__standard_requirement=monthly_standard_requirement)
            ).delete()
            if deleted[0] > 0:
                logger.info(
                    f"Deleted {deleted[0]} quarterly and monthly gross turnover compliances for Proposal: {self}"
                )
            return

        # If invoicing quarterly, generate quarterly financial statement compliances
        if (
            invoicing_details.invoicing_repetition_type.key
            == settings.REPETITION_TYPE_QUARTERLY
        ):
            (
                quarterly_gross_turnover_requirement,
                created,
            ) = ProposalRequirement.objects.get_or_create(
                proposal=self,
                standard_requirement=quarterly_standard_requirement,
                is_deleted=False,
            )
            if created:
                logger.info(
                    f"Created Proposal Requirement: {quarterly_gross_turnover_requirement}"
                )

            financial_quarters_included = (
                invoicing_utils.financial_quarters_included_in_range(
                    self.approval.start_date, self.approval.expiry_date
                )
            )

            for financial_quarter in financial_quarters_included:
                if invoicing_utils.financial_year_has_passed(financial_quarter[3]):
                    # Don't generate quarterly compliances if the financial year this quarter is part of has passed
                    continue

                if only_future and invoicing_utils.financial_quarter_has_passed(
                    financial_quarter
                ):
                    # Don't generate quarterly compliances if the only_future flag is True
                    # and the financial quarter has passed
                    continue

                year = int(financial_quarter[2])
                quarter = int(financial_quarter[0])
                month = invoicing_utils.month_from_quarter(quarter - 1)
                # This will make the compliance due date 1 month after the end of that financial quarter
                due_date = datetime.date(year, month, 1) + relativedelta(months=4)
                compliance, created = Compliance.objects.get_or_create(
                    proposal=self,
                    approval=self.approval,
                    requirement=quarterly_gross_turnover_requirement,
                    due_date=due_date,
                    processing_status=Compliance.PROCESSING_STATUS_FUTURE,
                )
                if created:
                    logger.info(
                        f"Compliance created: {compliance} for Proposal: {self}"
                    )
                    compliance.text = (
                        "Please enter the gross turnover and upload an audited "
                        f"financial statement for {financial_quarter[1]} {financial_quarter[3]}"
                    )
                    compliance.save()

            # Delete any future monthly gross turnvoer compliances
            deleted = Compliance.objects.filter(
                proposal=self,
                requirement__standard_requirement=monthly_standard_requirement,
                # Note: If we end up supporting changing charge methods mid cycle
                # then we need to come up with a solution here so that we don't delete any past compliances
                # that were submitted. Otherwise this comment block can be removed.
                # due_date__gt=timezone.now().date(),
                # processing_status=Compliance.PROCESSING_STATUS_FUTURE,
            ).delete()
            if deleted[0] > 0:
                logger.info(
                    f"Deleted {deleted[0]} monthly gross turnover compliances for Proposal: {self}"
                )

        # If invoicing monthly, generate monthly financial statement compliances
        elif (
            invoicing_details.invoicing_repetition_type.key
            == settings.REPETITION_TYPE_MONTHLY
        ):
            (
                monthly_gross_turnover_requirement,
                created,
            ) = ProposalRequirement.objects.get_or_create(
                proposal=self,
                standard_requirement=monthly_standard_requirement,
                is_deleted=False,
            )
            if created:
                logger.info(
                    f"Created Proposal Requirement: {monthly_gross_turnover_requirement}"
                )

            months_included = invoicing_utils.months_included_in_range(
                self.approval.start_date, self.approval.expiry_date
            )
            for month in months_included:
                financial_year = invoicing_utils.financial_year_from_date(month)
                if invoicing_utils.financial_year_has_passed(financial_year):
                    # Don't generate monthly compliances if the financial year this month is part of has passed
                    continue

                due_date = month + relativedelta(months=2)
                end_of_month = month.replace(
                    day=calendar.monthrange(month.year, month.month)[1]
                )

                if only_future and timezone.now().date() > end_of_month:
                    # Don't generate monthly compliances for months that have passed
                    # These periods will be covered by the annual compliances
                    continue

                compliance, created = Compliance.objects.get_or_create(
                    proposal=self,
                    approval=self.approval,
                    requirement=monthly_gross_turnover_requirement,
                    due_date=due_date,
                    processing_status=Compliance.PROCESSING_STATUS_FUTURE,
                )
                logger.debug(f"Compliance: {compliance}")
                if created:
                    logger.info(
                        f"Compliance created: {compliance} for Proposal: {self}"
                    )
                    compliance.text = (
                        "Please enter the gross turnover and upload an audited "
                        f"financial statement for {month.strftime('%b').upper()} {month.year}"
                    )
                    compliance.save()

            # Delete any future quarterly gross turnover compliances
            deleted = Compliance.objects.filter(
                proposal=self,
                requirement__standard_requirement=quarterly_standard_requirement,
                # Note: If we end up supporting changing charge methods mid cycle
                # then we need to come up with a solution here so that we don't delete any past compliances
                # that were submitted. Otherwise this comment block can be removed.
                # due_date__gt=timezone.now().date(),
                # processing_status=Compliance.PROCESSING_STATUS_FUTURE,
            ).delete()
            if deleted[0] > 0:
                logger.info(
                    f"Deleted {deleted[0]} quarterly gross turnover compliances for Proposal: {self}"
                )

    def generate_gross_turnover_requirements(self):
        approval = self.approval
        invoicing_details = approval.current_proposal.invoicing_details
        end_of_first_financial_year = invoicing_utils.end_of_next_financial_year(
            approval.start_date
        )
        first_annual_due_date = end_of_first_financial_year.replace(month=10).replace(
            day=31
        )

        try:
            annual_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_ANNUALLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                f"ProposalStandardRequirement not found: code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_ANNUALLY}"
            )
            raise

        try:
            quarterly_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                "ProposalStandardRequirement not found: "
                f"code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY}"
            )
            raise

        try:
            monthly_standard_requirement = ProposalStandardRequirement.objects.get(
                code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY
            )
        except ProposalStandardRequirement.DoesNotExist:
            logger.error(
                "ProposalStandardRequirement not found: "
                f"code={settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY}"
            )
            raise

        (
            annual_financial_statement_requirement,
            created,
        ) = ProposalRequirement.objects.update_or_create(
            proposal=self,
            standard_requirement=annual_standard_requirement,
            is_deleted=False,
            defaults={
                "due_date": first_annual_due_date,
                "reminder_date": end_of_first_financial_year + relativedelta(days=1),
                "recurrence": True,
                "recurrence_pattern": 3,  # Yearly
                "recurrence_schedule": 1,  # Every 1 year
            },
        )
        if created:
            logger.info(
                "New Annual Financial Statement Proposal Requirement: "
                f"{annual_financial_statement_requirement} created for {self}"
            )

        if (
            invoicing_details.charge_method.key
            == settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ADVANCE
        ):
            # Don't generate quarterly or monthly requirements for percentage of gross turnover advance invoicing
            # Remove any such requirements
            deleted = self.requirements.filter(
                Q(standard_requirement=quarterly_standard_requirement)
                | Q(standard_requirement=monthly_standard_requirement)
            ).delete()
            if deleted[0] > 0:
                logger.info(
                    f"Deleted {deleted[0]} quarterly and monthly gross turnover requirements for Proposal: {self}"
                )
            return

        if (
            invoicing_details.invoicing_repetition_type.key
            == settings.REPETITION_TYPE_QUARTERLY
        ):
            end_of_first_financial_quarter = (
                invoicing_utils.end_of_next_financial_quarter(approval.start_date)
            )
            first_quarterly_due_date = end_of_first_financial_quarter + relativedelta(
                days=30
            )

            (
                quarterly_financial_statement_requirement,
                created,
            ) = ProposalRequirement.objects.update_or_create(
                proposal=self,
                standard_requirement=quarterly_standard_requirement,
                is_deleted=False,
                defaults={
                    "due_date": first_quarterly_due_date,
                    "reminder_date": end_of_first_financial_year
                    + relativedelta(days=1),
                    "recurrence": True,
                    "recurrence_pattern": 2,  # Monthly
                    "recurrence_schedule": 3,  # Every 3 months
                },
            )

            # Remove any monthly gross turnover requirements as they are not needed when
            # invoicing quarterly
            ProposalRequirement.objects.filter(
                proposal=self,
                standard_requirement__code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY,
            ).update(is_deleted=True)

        if (
            invoicing_details.invoicing_repetition_type.key
            == settings.REPETITION_TYPE_MONTHLY
        ):
            end_of_first_month = approval.start_date + relativedelta(day=31)
            first_monthly_due_date = end_of_first_month + relativedelta(months=1)

            (
                monthly_financial_statement_requirement,
                created,
            ) = ProposalRequirement.objects.update_or_create(
                proposal=self,
                standard_requirement=monthly_standard_requirement,
                is_deleted=False,
                defaults={
                    "due_date": first_monthly_due_date,
                    "reminder_date": end_of_first_financial_year
                    + relativedelta(days=1),
                    "recurrence": True,
                    "recurrence_pattern": 2,  # Monthly
                    "recurrence_schedule": 1,  # Every 1 month
                },
            )

            if created:
                logger.info(
                    "New Monthly Financial Statement Proposal Requirement: "
                    f"{monthly_financial_statement_requirement} created for {self}"
                )

            # Remove any quarterly gross turnover requirements as they are not needed when
            # invoicing monthly
            ProposalRequirement.objects.filter(
                proposal=self,
                standard_requirement__code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY,
            ).update(is_deleted=True)

    def update_gross_turnover_requirements(self):
        """Called when the finance user is editing the invoicing details
        and changes the charge method or invoicing repetition type.

        This method ensures the necessary proposal requirements are created or deleted based on the
        invoicing details.
        """
        invoicing_details = self.invoicing_details

        # If the user has selected a non gross turnover based invoicing method then
        # mark any gross turnover requirements as deleted and delete any gross turnover compliances
        if invoicing_details.charge_method.key not in [
            settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ADVANCE,
            settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ARREARS,
        ]:
            gross_turnover_requirements = ProposalStandardRequirement.objects.filter(
                gross_turnover_required=True
            )
            deleted = self.requirements.filter(
                standard_requirement__in=gross_turnover_requirements,
            ).update(is_deleted=True)
            if deleted > 0:
                logger.info(
                    f"Deleted {deleted} gross turnover requirements for Proposal: {self}"
                )
            deleted = self.compliances.filter(
                requirement__standard_requirement__in=gross_turnover_requirements
            ).delete()
            if deleted[0] > 0:
                logger.info(
                    f"Deleted {deleted[0]} gross turnover compliances for Proposal: {self}"
                )
            return

        if (
            invoicing_details.charge_method.key
            == settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ADVANCE
        ):
            # Remove any quarterly and monthly gross turnover requirements as they are not needed when
            # invoicing in advance
            ProposalRequirement.objects.filter(
                standard_requirement__code__in=[
                    settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY,
                    settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY,
                ]
            ).update(is_deleted=True)

        if (
            invoicing_details.charge_method.key
            == settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ARREARS
        ):
            # Delete any proposal requirements of the wrong type
            if (
                invoicing_details.invoicing_repetition_type.key
                == settings.REPETITION_TYPE_QUARTERLY
            ):
                # When invoicing quarterly, delete any monthly gross turnover requirements
                ProposalRequirement.objects.filter(
                    standard_requirement__code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY,
                    proposal=invoicing_details.approval.current_proposal,
                    is_deleted=False,
                ).update(is_deleted=True)

                # Make sure there are quarterly gross turnover requirements
                quarterly_turnover_requirement = (
                    ProposalStandardRequirement.objects.get(
                        code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY
                    )
                )

                (
                    quarterly_financial_statement_requirement,
                    created,
                ) = ProposalRequirement.objects.get_or_create(
                    proposal=self,
                    standard_requirement=quarterly_turnover_requirement,
                    is_deleted=False,
                )
                if created:
                    logger.info(
                        "Created Gross Quarterly Turnover Proposal Requirement: "
                        f"{quarterly_financial_statement_requirement}"
                    )
                else:
                    quarterly_financial_statement_requirement.is_deleted = False
                    quarterly_financial_statement_requirement.save()

            elif (
                invoicing_details.invoicing_repetition_type.key
                == settings.REPETITION_TYPE_MONTHLY
            ):
                # When invoicing monthly, delete any quarterly gross turnover requirements
                ProposalRequirement.objects.filter(
                    standard_requirement__code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_QUARTERLY,
                    proposal=invoicing_details.approval.current_proposal,
                    is_deleted=False,
                ).update(is_deleted=True)

                # Make sure there are monthly gross turnover requirements
                monthly_turnover_requirement = ProposalStandardRequirement.objects.get(
                    code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_MONTHLY
                )

                (
                    monthly_financial_statement_requirement,
                    created,
                ) = ProposalRequirement.objects.get_or_create(
                    proposal=self,
                    standard_requirement=monthly_turnover_requirement,
                    is_deleted=False,
                )
                if created:
                    logger.info(
                        "Created Gross Monthly Turnover Proposal Requirement: "
                        f"{monthly_financial_statement_requirement}"
                    )
                else:
                    monthly_financial_statement_requirement.is_deleted = False
                    monthly_financial_statement_requirement.save()

        end_of_first_financial_year = invoicing_utils.end_of_next_financial_year(
            invoicing_details.proposal.approval.start_date
        )
        first_annual_due_date = end_of_first_financial_year.replace(month=10).replace(
            day=31
        )

        # Make sure there are annual gross turnover requirements
        annual_turnover_requirement = ProposalStandardRequirement.objects.get(
            code=settings.INVOICING_PERCENTAGE_GROSS_TURNOVER_ANNUALLY
        )

        (
            annual_financial_statement_requirement,
            created,
        ) = ProposalRequirement.objects.get_or_create(
            proposal=self,
            standard_requirement=annual_turnover_requirement,
            is_deleted=False,
        )
        if created:
            logger.info(
                f"Created Gross Annual Turnover Proposal Requirement: {annual_financial_statement_requirement}"
            )

        # Update the details
        annual_financial_statement_requirement.due_date = first_annual_due_date
        reminder_date = end_of_first_financial_year + relativedelta(days=1)
        logger.debug(f"reminder_date: {reminder_date}")
        annual_financial_statement_requirement.reminder_date = reminder_date
        annual_financial_statement_requirement.recurrence = True
        annual_financial_statement_requirement.recurrence_pattern = 3  # Annualy
        annual_financial_statement_requirement.recurrence_schedule = 1  # Every 1 year
        annual_financial_statement_requirement.save()

    @property
    def proposal_applicant(self):
        if not self.ind_applicant:
            logger.warning(
                f"Attempting to access ProposalApplicant for Proposal: {self} that has no ind_applicant"
            )
            return None

        try:
            proposal_applicant = ProposalApplicant.objects.get(proposal=self)
        except ProposalApplicant.DoesNotExist:
            from leaseslicensing.components.proposals.utils import (
                make_proposal_applicant_ready,
            )

            logger.warning(
                f"ProposalApplicant not found for Proposal: {self}. Creating a new one."
            )
            emailuser = EmailUser.objects.get(id=self.ind_applicant)
            make_proposal_applicant_ready(self, emailuser)
            proposal_applicant = ProposalApplicant.objects.get(proposal=self)
        except ProposalApplicant.MultipleObjectsReturned:
            logger.warning(
                f"Multiple ProposalApplicants found for Proposal: {self}. Using the first one."
            )
            proposal_applicant = ProposalApplicant.objects.filter(proposal=self).first()

        return proposal_applicant

    @transaction.atomic
    def renew_approval(self, request):
        previous_proposal = self
        try:
            renew_conditions = {
                "previous_application": previous_proposal,
                "processing_status": Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            }
            proposal = Proposal.objects.get(**renew_conditions)
            if proposal:
                raise ValidationError(
                    "A Renewal for this licence has already been lodged and is awaiting review."
                )
        except Proposal.DoesNotExist:
            proposal = self.amend_or_renew_approval(request, PROPOSAL_TYPE_RENEWAL)

        return proposal

    @transaction.atomic
    def amend_approval(self, request):
        previous_proposal = self
        try:
            amend_conditions = {
                "previous_application": previous_proposal,
                "proposal_type__code": PROPOSAL_TYPE_AMENDMENT,
            }
            proposal = Proposal.objects.get(**amend_conditions)
            if proposal.processing_status in (
                Proposal.PROCESSING_STATUS_WITH_ASSESSOR,
            ):
                raise ValidationError(
                    "An Amendment for this license has already been lodged and is awaiting review."
                )
        except Proposal.DoesNotExist:
            proposal = self.amend_or_renew_approval(request, PROPOSAL_TYPE_AMENDMENT)

        return proposal

    def get_related_items(self, **kwargs):
        return_list = []
        # count = 0
        # field_competitive_process = None
        related_field_names = [
            "generated_proposal",
            "originating_proposal",
            "generated_competitive_process",
            "approval",
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
        action_url = reverse("internal-proposal-detail", kwargs={"pk": self.id})
        related_item = RelatedItem(
            identifier=self.related_item_identifier,
            model_name=self._meta.verbose_name,
            descriptor=self.related_item_descriptor,
            action_url=f'<a href="{action_url}">View</a>',
            type="application",
        )
        return related_item

    @property
    def related_item_identifier(self):
        return self.lodgement_number

    @property
    def related_item_descriptor(self):
        """
        Returns this application's status as item description:
        """

        if self.application_type.name in [
            APPLICATION_TYPE_REGISTRATION_OF_INTEREST,
            APPLICATION_TYPE_LEASE_LICENCE,
        ]:
            return self.processing_status
        else:
            return "(return descriptor)"

    def generate_competitive_process(self):
        if self.generated_competitive_process:
            logger.warning(
                "Couldn't generate a competitive process. "
                f"Proposal {self} already has an associated Competitive Process: {self.generated_competitive_process}"
            )
            return

        new_competitive_process = CompetitiveProcess.objects.create()
        self.generated_competitive_process = new_competitive_process
        self.save()

    def generate_invoicing_details(self):
        if hasattr(self, "invoicing_details") and self.invoicing_details:
            logger.warning(
                "Couldn't generate an invoicing details. "
                f"Proposal {self} already has an associated Invoicing Details: {self.invoicing_details}"
            )
            return

        new_invoicing_details = InvoicingDetails.objects.create()
        self.invoicing_details = new_invoicing_details
        self.save()

    @transaction.atomic
    def save_invoicing_details(self, request, action):
        from leaseslicensing.components.invoicing.serializers import (
            InvoicingDetailsSerializer,
        )

        # Retrieve invoicing_details data
        proposal_data = request.data.get("proposal", {})
        invoicing_details_data = (
            proposal_data.get("invoicing_details", {}) if proposal_data else {}
        )

        # Save invoicing details
        id = invoicing_details_data.get("id")
        try:
            invoicing_details = InvoicingDetails.objects.get(id=id)
        except InvoicingDetails.DoesNotExist:
            raise serializers.ValidationError(
                _("Invoicing details with id {id} not found", code="invalid")
            )

        serializer = InvoicingDetailsSerializer(
            invoicing_details,
            data=invoicing_details_data,
            context={"request": request, "action": action},
        )
        serializer.is_valid(raise_exception=True)

        instance = serializer.save()
        return instance

    @transaction.atomic
    def finance_complete_editing(self, request, action):
        invoicing_details = self.save_invoicing_details(request, action)
        approval = invoicing_details.approval
        proposal = invoicing_details.proposal

        # For leases/licences being migrated prevent the generation of any past compliances
        # We assume any past compliances have been dealt with in a legacy system
        is_migration_proposal = proposal.proposal_type.code == PROPOSAL_TYPE_MIGRATION

        # For leases/licences being transferred prevent the generation of any past compliances
        is_transfer_proposal = proposal.proposal_type.code == PROPOSAL_TYPE_TRANSFER

        only_future_compliances = is_migration_proposal or is_transfer_proposal

        if (
            settings.CHARGE_METHOD_NO_RENT_OR_LICENCE_CHARGE
            == invoicing_details.charge_method.key
        ):
            # Nothing else needs to be done
            return

        if (
            settings.CHARGE_METHOD_ONCE_OFF_CHARGE
            == invoicing_details.charge_method.key
        ):
            # Generate a single once off charge invoice
            invoice_amount = invoicing_details.once_off_charge_amount
            if invoice_amount is None or invoice_amount <= Decimal("0.00"):
                raise serializers.ValidationError(
                    _(f"Invalid invoice amount: {invoice_amount}", code="invalid")
                )

            gst_free = approval.approval_type.gst_free

            invoice = Invoice(
                approval=self.approval,
                amount=invoice_amount,
                gst_free=gst_free,
            )
            if approval.proponent_reference_number:
                invoice.proponent_reference_number = approval.proponent_reference_number

            invoice.save()

            # send to the finance group so they can take action
            send_new_invoice_raised_internal_notification(invoice)

            self.processing_status = Proposal.PROCESSING_STATUS_APPROVED
            self.save()

            return

        if invoicing_details.charge_method.key in [
            settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ARREARS,
            settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ADVANCE,
        ]:
            # Generate requirements for the proponent to submit quarterly (or monthly) and annual financial statements
            self.generate_gross_turnover_requirements()

            # Generate compliances from the requirements
            self.generate_compliances(
                approval, request, only_future=only_future_compliances
            )

        # For all other charge methods, there may be one or more invoice records that need to be
        # generated immediately (any past periods and any current period i.e. that has started but not yet finished)

        # Only generate immeiate and backdated invoices for non-migration and non-transfer proposals
        if not is_migration_proposal and not is_transfer_proposal:
            invoicing_details.generate_immediate_invoices()

        # Generate the invoice schdule for any future invoices
        invoicing_details.generate_invoice_schedule()

        self.processing_status = Proposal.PROCESSING_STATUS_APPROVED
        self.save()

    def finance_cancel_editing(self, request, action):
        self.processing_status = Proposal.PROCESSING_STATUS_CURRENT
        self.save()

    @classmethod
    def get_proposals_for_emailuser(cls, emailuser_id):
        user_orgs = get_organisation_ids_for_user(emailuser_id)
        return cls.objects.filter(
            Q(org_applicant_id__in=user_orgs)
            | Q(submitter=emailuser_id)
            | Q(ind_applicant=emailuser_id)
            | Q(proxy_applicant=emailuser_id)
        )

    @property
    def groups_comma_list(self):
        return ", ".join([pg.group.name for pg in self.groups.all()])

    @property
    def groups_names_list(self):
        return self.groups.values_list("group__name", flat=True)

    @property
    def categories_list(self):
        return self.categories.values_list("category__name", flat=True)

    def generate_license_documents(self, approval, **kwargs):
        """
        Creates or updates documents for the approval, based on the documents provided by the assessor
        Args:
            approval:
                The approval object
            reason: str (optional)
                The reason for creating the document. Must be one of the values in ApprovalDocument.REASON_CHOICES.
                Defaults to ApprovalDocument.REASON_NEW.
        """

        from leaseslicensing.components.approvals.document import (
            ApprovalDocumentGenerator,
        )
        from leaseslicensing.components.approvals.models import ApprovalDocument

        reason = kwargs.get("reason", ApprovalDocument.REASON_NEW)

        # Get the approval type object
        document_generator = ApprovalDocumentGenerator()

        documents = self.verify_approval_documents(approval)

        license_documents = documents["license_documents"]
        cover_letter = documents["cover_letter"]
        sign_off_sheets = documents["sign_off_sheets"]

        approval.licence_document = (
            (
                document_generator.create_or_update_approval_document(
                    approval,
                    filepath=license_documents["documents"][0]._file.path,
                    filename_prefix="Approval-",
                    reason=reason,
                )
            )
            if license_documents["required"]
            else None
        )
        approval.cover_letter_document = (
            (
                document_generator.create_or_update_approval_document(
                    approval,
                    filepath=cover_letter["documents"][0]._file.path,
                    filename_prefix="CoverLetter-",
                    reason=reason,
                )
            )
            if cover_letter["required"]
            else None
        )
        approval.sign_off_sheet_document = (
            (
                document_generator.create_or_update_approval_document(
                    approval,
                    filepath=sign_off_sheets["documents"][0]._file.path,
                    filename_prefix="SignOffSheet-",
                    reason=reason,
                )
            )
            if sign_off_sheets["required"]
            else None
        )

    def amend_or_renew_approval(self, request, proposal_type_code):
        """
        Returns a new proposal from self that is either of type amendment or renewal
        """

        if proposal_type_code not in [PROPOSAL_TYPE_AMENDMENT, PROPOSAL_TYPE_RENEWAL]:
            raise ValidationError("Proposal type must be amendment or renewal")
        is_renewal = proposal_type_code == PROPOSAL_TYPE_RENEWAL

        previous_proposal = Proposal.objects.get(id=self.id)
        proposal = clone_proposal_with_status_reset(previous_proposal)
        proposal.proposal_type = ProposalType.objects.get(code=proposal_type_code)

        copy_proposal_proposed_issuance(
            previous_proposal, proposal, is_renewal=is_renewal
        )

        # Copy over previous proposal geometry
        copy_proposal_geometry(previous_proposal, proposal)

        copy_proposal_details(previous_proposal, proposal)

        # Copy over previous site name
        copy_site_name(previous_proposal, proposal)

        # Copy over previous groups
        copy_groups(previous_proposal, proposal)

        # Copy over previous gis data
        copy_gis_data(previous_proposal, proposal)

        copy_proposal_requirements(previous_proposal, proposal, is_renewal=is_renewal)

        # Create a log entry for the proposal
        user_action = (
            ProposalUserAction.ACTION_RENEW_PROPOSAL
            if is_renewal
            else ProposalUserAction.ACTION_AMEND_PROPOSAL
        )
        self.log_user_action(user_action.format(self.id), request)
        # Create a log entry for the organisation
        if self.org_applicant:
            self.org_applicant.log_user_action(
                user_action.format(self.id),
                request,
            )
        # Log entry for approval
        from leaseslicensing.components.approvals.models import ApprovalUserAction

        user_action = (
            ApprovalUserAction.ACTION_RENEW_APPROVAL
            if is_renewal
            else ApprovalUserAction.ACTION_AMEND_APPROVAL
        )
        self.approval.log_user_action(
            user_action.format(self.approval.id),
            request,
        )
        proposal.save(
            version_comment=(
                f"New {proposal.proposal_type.description} Application "
                f"created from origin {proposal.previous_application_id}"
            )
        )

        from leaseslicensing.components.proposals.utils import populate_gis_data

        # fetch gis data
        populate_gis_data(proposal, "proposalgeometry")

        return proposal

    def verify_approval_documents(self, approval):
        """
        Verifies the document types of the approval type document types for the approval type of the approval
        I.e. an approval type (a license) requires a certain amount of documents to be uploaded by the assessor.
        Typically these are a license document, a cover letter, and a sign-off sheet. For the system to be able
        to associate these document types with the respective fields in the model they have to be check marked as
        their respective type. That is the document type of the document type.
        This function verifies whether for each required typed document type exactly one document of that type
        is present.

        Args:
            approval: Approval

        Returns:
            dict: A dictionary containing the documents of each type
        """

        from leaseslicensing.components.approvals.models import ApprovalType

        # Get the approval type object
        approval_type = approval.approval_type

        mandatory_documenttypes = approval_type.approvaltypedocumenttypes.filter(
            approvaltypedocumenttypeonapprovaltype__mandatory=True
        )

        documents = {}
        documents["license_documents"] = {
            "documents": [],  # List of documents to check there is exactly one of
            "required": mandatory_documenttypes.filter(
                is_license_document=True
            ).exists(),  # Whether a document type of this type is required,
            # i.e. whether there must be one document of this type
        }
        documents["cover_letter"] = {
            "documents": [],
            "required": mandatory_documenttypes.filter(is_cover_letter=True).exists(),
        }
        documents["sign_off_sheets"] = {
            "documents": [],
            "required": mandatory_documenttypes.filter(is_sign_off_sheet=True).exists(),
        }
        documents["other_documents"] = {"documents": []}

        # Remove any documents of the wrong approval type to avoid confusion / bugs
        self.lease_licence_approval_documents.exclude(
            approval_type=approval_type
        ).delete()

        for document in self.lease_licence_approval_documents.all():
            if document.approval_type_id != approval_type.id:
                logger.warning(
                    f"Ignoring {ApprovalType.objects.get(id=document.approval_type.id)} "
                    f"document `{document}` for Approval of type `{approval_type}`."
                )
                continue

            if document.approval_type_document_type.is_license_document:
                documents["license_documents"]["documents"].append(document)
            elif document.approval_type_document_type.is_cover_letter:
                documents["cover_letter"]["documents"].append(document)
            elif document.approval_type_document_type.is_sign_off_sheet:
                documents["sign_off_sheets"]["documents"].append(document)
            else:
                documents["other_documents"]["documents"].append(document)
        if (
            documents["license_documents"]["required"]
            and len(documents["license_documents"]["documents"]) != 1
        ):
            raise ValidationError(
                f"There must be exactly one license document for {approval_type}, "
                f"but found {len(documents['license_documents']['documents'])}."
            )
        if (
            documents["cover_letter"]["required"]
            and len(documents["cover_letter"]["documents"]) != 1
        ):
            raise ValidationError(
                f"There must be exactly one cover letter for {approval_type}, "
                f"but found {len(documents['cover_letter']['documents'])}."
            )
        if (
            documents["sign_off_sheets"]["required"]
            and len(documents["sign_off_sheets"]["documents"]) != 1
        ):
            raise ValidationError(
                f"There must be exactly one sign-off sheet for {approval_type}, "
                f"but found {len(documents['sign_off_sheets']['documents'])}."
            )

        return documents

    def reset_invoicing(self):
        """Just used to speed up testing."""
        if not settings.DEBUG:
            raise ValidationError("This method is only available in DEBUG mode.")

        self.compliances.filter(
            requirement__standard_requirement__gross_turnover_required=True
        ).delete()
        self.requirements.filter(
            standard_requirement__gross_turnover_required=True
        ).delete()
        self.processing_status = Proposal.PROCESSING_STATUS_APPROVED_EDITING_INVOICING
        self.save()
        logger.debug(f"Proposal: {self} invoicing reset")

    def update_lease_licence_approval_documents_approval_type(self):
        self.lease_licence_approval_documents

    def mark_documents_not_deleteable(self):
        document_field_names = [
            documents_field
            for documents_field in self._meta.fields_map.keys()
            if "documents" in documents_field
        ]
        for document_field_name in document_field_names:
            getattr(self, document_field_name).all().update(can_delete=False)

    def purge_proposal(self):
        """Deletes all related objects and the proposal itself."""
        if not settings.DEBUG:
            raise ValidationError("This method is only available in DEBUG mode.")
        if self.approval:
            self.approval.purge_approval()

        self.delete()

    @classmethod
    @transaction.atomic
    def purge_proposals(cls):
        """Deletes all proposals, approvals, invoices and any objects that allow cascade delete from those objects."""
        if not settings.DEBUG:
            raise ValidationError("This method is only available in DEBUG mode.")
        for proposal in cls.objects.all():
            print(proposal)
            proposal.purge_proposal()


class ProposalApplicant(BaseApplicant):
    proposal = models.ForeignKey(
        Proposal, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        app_label = "leaseslicensing"

    def copy_self_to_proposal(self, target_proposal):
        ProposalApplicant.objects.create(
            proposal=target_proposal,
            emailuser_id=self.emailuser_id,
            first_name=self.first_name,
            last_name=self.last_name,
            dob=self.dob,
            residential_line1=self.residential_line1,
            residential_line2=self.residential_line2,
            residential_line3=self.residential_line3,
            residential_locality=self.residential_locality,
            residential_state=self.residential_state,
            residential_country=self.residential_country,
            residential_postcode=self.residential_postcode,
            postal_same_as_residential=self.postal_same_as_residential,
            postal_line1=self.postal_line1,
            postal_line2=self.postal_line2,
            postal_line3=self.postal_line3,
            postal_locality=self.postal_locality,
            postal_state=self.postal_state,
            postal_country=self.postal_country,
            postal_postcode=self.postal_postcode,
            email=self.email,
            phone_number=self.phone_number,
            mobile_number=self.mobile_number,
        )


class ProposalIdentifier(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="identifiers"
    )
    identifier = models.ForeignKey(Identifier, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "identifier")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land covered by legal act: {self.identifier}"


class ProposalVesting(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="vestings"
    )
    vesting = models.ForeignKey(
        Vesting, on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "vesting")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land covered by Vesting: {self.vesting}"


class ProposalName(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="names"
    )
    name = models.ForeignKey(Name, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "name")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land named: {self.name}"


class ProposalAct(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="acts"
    )
    act = models.ForeignKey(Act, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "act")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land covered by legal act: {self.act}"


class ProposalTenure(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="tenures"
    )
    tenure = models.ForeignKey(Tenure, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "tenure")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land of tenure: {self.tenure}"


class ProposalCategory(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="categories"
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "category")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land categorised as: {self.category}"


class ProposalGroup(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="groups"
    )
    group = models.ForeignKey(Group, on_delete=models.PROTECT)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "group")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} is in Group: {self.group}"


class ProposalRegion(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="regions"
    )
    region = models.ForeignKey(Region, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "region")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land located in Region: {self.region}"


class ProposalDistrict(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="districts"
    )
    district = models.ForeignKey(
        District, on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "district")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land located in District: {self.district}"


class ProposalLGA(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="lgas"
    )
    lga = models.ForeignKey(LGA, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("proposal", "lga")

    def __str__(self):
        return f"Proposal: {self.proposal.lodgement_number} includes land located in LGA: {self.lga}"


class ProposalAdditionalDocumentType(models.Model):
    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="additional_document_types"
    )
    additional_document_type = models.ForeignKey(
        AdditionalDocumentType, on_delete=models.CASCADE
    )

    class Meta:
        app_label = "leaseslicensing"


class AdditionalDocument(DefaultDocument):
    _file = SecureFileField(upload_to=update_additional_doc_filename, max_length=512)
    proposal_additional_document_type = models.ForeignKey(
        ProposalAdditionalDocumentType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="document",
    )

    class Meta:
        app_label = "leaseslicensing"


class ProposalGeometryManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .annotate(area=Area(Cast("polygon", PolygonField(geography=True))))
        )


class ProposalGeometry(models.Model):
    objects = ProposalGeometryManager()

    proposal = models.ForeignKey(
        Proposal, on_delete=models.CASCADE, related_name="proposalgeometry"
    )
    polygon = PolygonField(srid=4326, blank=True, null=True)
    intersects = models.BooleanField(default=False)
    copied_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, blank=True, null=True
    )
    drawn_by = models.IntegerField(blank=True, null=True)  # EmailUserRO
    source_type = models.CharField(
        max_length=255, blank=True, choices=settings.SOURCE_CHOICES
    )
    source_name = models.CharField(max_length=255, blank=True)
    locked = models.BooleanField(default=False)

    class Meta:
        app_label = "leaseslicensing"

    @property
    def area_sqm(self):
        if not hasattr(self, "area") or not self.area:
            logger.warning(f"ProposalGeometry: {self.id} has no area")
            return None
        return self.area.sq_m

    @property
    def area_sqhm(self):
        if not hasattr(self, "area") or not self.area:
            logger.warning(f"ProposalGeometry: {self.id} has no area")
            return None
        return self.area.sq_m / 10000


class ProposalLogDocument(Document):
    log_entry = models.ForeignKey(
        "ProposalLogEntry", related_name="documents", on_delete=models.CASCADE
    )
    _file = SecureFileField(
        upload_to=update_proposal_comms_log_filename, max_length=512
    )

    class Meta:
        app_label = "leaseslicensing"


class ProposalLogEntry(CommunicationsLogEntry):
    proposal = models.ForeignKey(
        Proposal, related_name="comms_logs", on_delete=models.CASCADE
    )

    def __str__(self):
        return f"{self.reference} - {self.subject}"

    class Meta:
        app_label = "leaseslicensing"

    def save(self, **kwargs):
        # save the proposal reference if the reference not provided
        if not self.reference:
            self.reference = self.proposal.reference
        super().save(**kwargs)


class ProposalOtherDetails(models.Model):
    LICENCE_PERIOD_CHOICES = (
        ("2_months", "2 months"),
        ("1_year", "1 Year"),
        ("3_year", "3 Years"),
        ("5_year", "5 Years"),
        ("7_year", "7 Years"),
        ("10_year", "10 Years"),
    )
    preferred_licence_period = models.CharField(
        "Preferred licence period",
        max_length=40,
        choices=LICENCE_PERIOD_CHOICES,
        null=True,
        blank=True,
    )
    nominated_start_date = models.DateField(blank=True, null=True)
    insurance_expiry = models.DateField(blank=True, null=True)
    other_comments = models.TextField(blank=True)
    # if credit facilities for payment of fees is required
    credit_fees = models.BooleanField(default=False)
    # if credit/ cash payment docket books are required
    credit_docket_books = models.BooleanField(default=False)
    docket_books_number = models.CharField(
        "Docket books number", max_length=20, blank=True
    )
    proposal = models.OneToOneField(
        Proposal, related_name="other_details", null=True, on_delete=models.CASCADE
    )

    class Meta:
        app_label = "leaseslicensing"

    @property
    def proposed_end_date(self):
        end_date = None
        if self.preferred_licence_period and self.nominated_start_date:
            if self.preferred_licence_period == "2_months":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+2)
                    - relativedelta(days=1)
                )
            if self.preferred_licence_period == "1_year":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+12)
                    - relativedelta(days=1)
                )
            if self.preferred_licence_period == "3_year":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+36)
                    - relativedelta(days=1)
                )
            if self.preferred_licence_period == "5_year":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+60)
                    - relativedelta(days=1)
                )
            if self.preferred_licence_period == "7_year":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+84)
                    - relativedelta(days=1)
                )
            if self.preferred_licence_period == "10_year":
                end_date = (
                    self.nominated_start_date
                    + relativedelta(months=+120)
                    - relativedelta(days=1)
                )
        return end_date


class ProposalRequest(models.Model):
    proposal = models.ForeignKey(
        Proposal, related_name="proposalrequest_set", on_delete=models.CASCADE
    )
    subject = models.CharField(max_length=200, blank=True)
    text = models.TextField(blank=True)
    # fficer = models.ForeignKey(EmailUser, null=True, on_delete=models.SET_NULL)
    officer = models.IntegerField(null=True)  # EmailUserRO

    def __str__(self):
        return f"{self.subject} - {self.text}"

    class Meta:
        app_label = "leaseslicensing"


class ComplianceRequest(ProposalRequest):
    REASON_CHOICES = (
        (
            "outstanding",
            "There are currently outstanding returns for the previous licence",
        ),
        ("other", "Other"),
    )
    reason = models.CharField(
        "Reason", max_length=30, choices=REASON_CHOICES, default=REASON_CHOICES[0][0]
    )

    class Meta:
        app_label = "leaseslicensing"


class AmendmentReason(models.Model):
    reason = models.CharField("Reason", max_length=125)

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Proposal Amendment Reason"  # display name in Admin
        verbose_name_plural = "Proposal Amendment Reasons"

    def __str__(self):
        return self.reason


class AmendmentRequest(ProposalRequest):
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
        AmendmentReason, blank=True, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        app_label = "leaseslicensing"

    @transaction.atomic
    def generate_amendment(self, request):
        if not self.proposal.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()

        if self.status == AmendmentRequest.STATUS_CHOICE_REQUESTED:
            proposal = self.proposal
            if proposal.processing_status != Proposal.PROCESSING_STATUS_DRAFT:
                proposal.processing_status = Proposal.PROCESSING_STATUS_DRAFT
                proposal.save(
                    version_comment=f"Proposal amendment requested {request.data.get('reason', '')}"
                )

                # Mark any related documents that the assessor may have attached to the proposal as not delete-able
                self.proposal.mark_documents_not_deleteable()

            # Create a log entry for the proposal
            proposal.log_user_action(
                ProposalUserAction.ACTION_ID_REQUEST_AMENDMENTS, request
            )

            # Create a log entry for the applicant
            proposal.applicant.log_user_action(
                ProposalUserAction.ACTION_REQUESTED_AMENDMENT.format(proposal.id),
                request,
            )

            # send email
            send_amendment_email_notification(self, request, self.proposal)

        self.save()

    def user_has_object_permission(self, user_id):
        return self.proposal.user_has_object_permission(user_id)


class Assessment(ProposalRequest):
    STATUS_CHOICES = (
        ("awaiting_assessment", "Awaiting Assessment"),
        ("assessed", "Assessed"),
        ("assessment_expired", "Assessment Period Expired"),
    )
    assigned_assessor = models.IntegerField()  # EmailUserRO
    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0]
    )
    date_last_reminded = models.DateField(null=True, blank=True)
    comment = models.TextField(blank=True)
    purpose = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"


class ProposalDeclinedDetails(models.Model):
    proposal = models.OneToOneField(Proposal, on_delete=models.CASCADE)
    officer = models.IntegerField()  # EmailUserRO
    reason = models.TextField(blank=True)
    cc_email = models.TextField(null=True)

    class Meta:
        app_label = "leaseslicensing"


class ProposalOnHold(models.Model):
    proposal = models.OneToOneField(Proposal, on_delete=models.CASCADE)
    officer = models.IntegerField()  # EmailUserRO
    comment = models.TextField(blank=True)
    documents = models.ForeignKey(
        ProposalDocument,
        blank=True,
        null=True,
        related_name="onhold_documents",
        on_delete=models.SET_NULL,
    )

    class Meta:
        app_label = "leaseslicensing"


class ProposalStandardRequirement(RevisionedMixin):
    text = models.TextField()
    code = models.CharField(max_length=50, unique=True)
    obsolete = models.BooleanField(default=False)
    application_type = models.ForeignKey(
        ApplicationType, null=True, blank=True, on_delete=models.SET_NULL
    )
    gross_turnover_required = models.BooleanField(default=False)
    default = models.BooleanField(default=False)

    def __str__(self):
        return self.code

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Proposal Standard Condition"
        verbose_name_plural = "Proposal Standard Conditions"


class ProposalUserAction(UserAction):
    ACTION_CREATE_CUSTOMER_ = "Create customer {}"
    ACTION_CREATE_PROFILE_ = "Create profile {}"
    ACTION_LODGE_APPLICATION = "Lodge proposal {}"
    ACTION_ASSIGN_TO_ASSESSOR = "Assign proposal {} to {} as the assessor"
    ACTION_UNASSIGN_ASSESSOR = "Unassign assessor from proposal {}"
    ACTION_ASSIGN_TO_APPROVER = "Assign proposal {} to {} as the approver"
    ACTION_UNASSIGN_APPROVER = "Unassign approver from proposal {}"
    ACTION_ACCEPT_ID = "Accept ID"
    ACTION_RESET_ID = "Reset ID"
    ACTION_ID_REQUEST_UPDATE = "Request ID update"
    ACTION_ACCEPT_CHARACTER = "Accept character"
    ACTION_RESET_CHARACTER = "Reset character"
    ACTION_ACCEPT_REVIEW = "Accept review"
    ACTION_RESET_REVIEW = "Reset review"
    ACTION_ID_REQUEST_AMENDMENTS = "Request amendments"
    ACTION_SEND_FOR_ASSESSMENT_TO_ = "Send for assessment to {}"
    ACTION_SEND_ASSESSMENT_REMINDER_TO_ = "Send assessment reminder to {}"
    ACTION_DECLINE = "Decline proposal {}"
    ACTION_ENTER_CONDITIONS = "Enter requirement"
    ACTION_CREATE_CONDITION_ = "Create requirement {}"
    ACTION_ISSUE_APPROVAL_ = "Issue Licence for proposal {}"
    ACTION_AWAITING_PAYMENT_APPROVAL_ = "Awaiting Payment for proposal {}"
    ACTION_UPDATE_APPROVAL_ = "Update Licence for proposal {}"
    ACTION_EXPIRED_APPROVAL_ = "Expire Approval for proposal {}"
    ACTION_DISCARD_PROPOSAL = "Discard proposal {}"
    ACTION_APPROVAL_LEVEL_DOCUMENT = "Assign Approval level document {}"

    # Assessors
    ACTION_SAVE_ASSESSMENT_ = "Save assessment {}"
    ACTION_CONCLUDE_ASSESSMENT_ = "Conclude assessment {}"
    ACTION_PROPOSED_APPROVAL = "Application {} has been proposed for approval"
    ACTION_PROPOSED_DECLINE = "Application {} has been proposed for decline"
    ACTION_REQUESTED_AMENDMENT = "Amendment requested for Application: {}"

    # Referrals
    ACTION_SEND_REFERRAL_TO = "Send referral {} for proposal {} to {}"
    ACTION_RESEND_REFERRAL_TO = "Resend referral {} for proposal {} to {}"
    ACTION_REMIND_REFERRAL = "Send reminder for referral {} for proposal {} to {}"
    ACTION_ENTER_REQUIREMENTS = "Enter Requirements for proposal {}"
    ACTION_BACK_TO_PROCESSING = "Back to processing for proposal {}"
    RECALL_REFERRAL = "Referral {} for proposal {} has been recalled"
    CONCLUDE_REFERRAL = "{}: Referral {} for proposal {} has been concluded"
    ACTION_REFERRAL_DOCUMENT = "Assign Referral document {}"
    ACTION_REFERRAL_ASSIGN_TO_ASSESSOR = (
        "Assign Referral  {} of proposal {} to {} as the assessor"
    )
    ACTION_REFERRAL_UNASSIGN_ASSESSOR = (
        "Unassign assessor from Referral {} of proposal {}"
    )

    # Approval
    ACTION_REISSUE_APPROVAL = "Reissue licence for proposal {}"
    ACTION_CANCEL_APPROVAL = "Cancel licence for proposal {}"
    ACTION_EXTEND_APPROVAL = "Extend licence"
    ACTION_SUSPEND_APPROVAL = "Suspend licence for proposal {}"
    ACTION_REINSTATE_APPROVAL = "Reinstate licence for proposal {}"
    ACTION_SURRENDER_APPROVAL = "Surrender licence for proposal {}"
    ACTION_RENEW_PROPOSAL = "Create Renewal proposal for proposal {}"
    ACTION_AMEND_PROPOSAL = "Create Amendment proposal for proposal {}"
    ACTION_QA_OFFICER_COMPLETED = "QA Officer Assessment Completed {}"

    # monthly invoicing by cron
    ACTION_SEND_BPAY_INVOICE = "Send BPAY invoice {} for proposal {} to {}"
    ACTION_SEND_MONTHLY_INVOICE = "Send monthly invoice {} for proposal {} to {}"
    ACTION_SEND_MONTHLY_CONFIRMATION = (
        "Send monthly confirmation for booking ID {}, for proposal {} to {}"
    )
    ACTION_SEND_PAYMENT_DUE_NOTIFICATION = (
        "Send monthly invoice/BPAY payment due notification {} for proposal {} to {}"
    )

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-when",)

    @classmethod
    def log_action(cls, proposal, action, user):
        return cls.objects.create(proposal=proposal, who=user, what=str(action))

    proposal = models.ForeignKey(
        Proposal, related_name="action_logs", on_delete=models.CASCADE
    )


class ReferralRecipientGroup(models.Model):
    # site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    # members = models.ManyToManyField(EmailUser)
    members = ArrayField(models.IntegerField(), blank=True)  # EmailUserRO

    def __str__(self):
        # return 'Referral Recipient Group'
        return self.name

    @property
    def filtered_members(self):
        return self.members.all()  # ?? Doesn't look very filtered

    @property
    def members_list(self):
        return list(self.members.all().values_list("email", flat=True))

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Referral group"
        verbose_name_plural = "Referral groups"


class QAOfficerGroup(models.Model):
    # site = models.OneToOneField(Site, default='1')
    name = models.CharField(max_length=30, unique=True)
    # members = models.ManyToManyField(EmailUser)
    members = ArrayField(models.IntegerField(), blank=True)  # EmailUserRO
    default = models.BooleanField(default=False)

    def __str__(self):
        return "QA Officer Group"

    @property
    def filtered_members(self):
        return self.members.all()  # ?? Doesn't look very filtered

    @property
    def members_list(self):
        return list(self.members.all().values_list("email", flat=True))

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "QA group"
        verbose_name_plural = "QA group"

    def _clean(self):
        try:
            default = QAOfficerGroup.objects.get(default=True)
        except QAOfficerGroup.DoesNotExist:
            default = None

        if default and self.default:
            raise ValidationError(
                "There can only be one default proposal QA Officer group"
            )


class Referral(RevisionedMixin):
    SENT_CHOICES = ((1, "Sent From Assessor"), (2, "Sent From Referral"))

    PROCESSING_STATUS_WITH_REFERRAL = "with_referral"
    PROCESSING_STATUS_RECALLED = "recalled"
    PROCESSING_STATUS_COMPLETED = "completed"

    PROCESSING_STATUS_CHOICES = (
        (PROCESSING_STATUS_WITH_REFERRAL, "Pending"),
        (PROCESSING_STATUS_RECALLED, "Recalled"),
        (PROCESSING_STATUS_COMPLETED, "Completed"),
    )
    lodged_on = models.DateTimeField(auto_now_add=True)
    proposal = models.ForeignKey(
        Proposal, related_name="referrals", on_delete=models.CASCADE
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
    document = models.ForeignKey(
        ReferralDocument,
        blank=True,
        null=True,
        related_name="referral_document",
        on_delete=models.SET_NULL,
    )
    assigned_officer = models.IntegerField()  # EmailUserRO

    comment_map = models.TextField(blank=True)
    comment_proposal_details = models.TextField(blank=True)
    comment_proposal_impact = models.TextField(blank=True)
    comment_gis_data = models.TextField(blank=True)
    comment_categorisation = models.TextField(blank=True)
    comment_deed_poll = models.TextField(blank=True)
    comment_additional_documents = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ("-lodged_on",)

    def __str__(self):
        return f"Referral: {self.id} for Proposal: {self.proposal.lodgement_number}"

    # Methods
    @property
    def application_type(self):
        return self.proposal.application_type.name

    @property
    def latest_referrals(self):
        return Referral.objects.filter(sent_by=self.referral, proposal=self.proposal)[
            :2
        ]

    @property
    def referral_assessment(self):
        # qs=self.assessment.filter(referral_assessment=True, referral_group=self.referral_group)
        qs = self.assessment.filter(referral_assessment=True)
        if qs:
            return qs[0]
        else:
            return None

    @property
    def can_be_completed(self):
        return True
        # Referral cannot be completed until second level referral sent by referral has been completed/recalled
        qs = Referral.objects.filter(
            sent_by=self.referral,
            proposal=self.proposal,
            processing_status=Referral.PROCESSING_STATUS_WITH_REFERRAL,
        )
        if qs:
            return False
        else:
            return True

    @property
    def allowed_assessors(self):
        return user_ids_in_group(settings.GROUP_NAME_ASSESSOR)

    def can_process(self, user):
        referral_user = retrieve_email_user(self.referral)
        # True if the request user is the referrer and the proposal is in referral status
        return referral_user.id == user.id and self.processing_status in [
            Referral.PROCESSING_STATUS_WITH_REFERRAL,
        ]

    @transaction.atomic
    def assign_officer(self, request, officer):
        if not self.can_process(request.user):
            raise exceptions.ProposalNotAuthorized()
        if not self.can_process(officer):
            raise ValidationError(
                "The selected person is not authorised to be assigned to this Referral"
            )
        if officer != self.assigned_officer:
            self.assigned_officer = officer
            self.save()
            self.proposal.log_user_action(
                ProposalUserAction.ACTION_REFERRAL_ASSIGN_TO_ASSESSOR.format(
                    self.id,
                    self.proposal.id,
                    f"{officer.get_full_name()}({officer.email})",
                ),
                request,
            )

    transaction.atomic

    def unassign(self, request):
        if not self.can_process(request.user):
            raise exceptions.ProposalNotAuthorized()

        if self.assigned_officer:
            self.assigned_officer = None
            self.save()

            # Create a log entry for the proposal
            self.proposal.log_user_action(
                ProposalUserAction.ACTION_REFERRAL_UNASSIGN_ASSESSOR.format(
                    self.id, self.proposal.id
                ),
                request,
            )

            # Log entry for applicant
            self.applicant.log_user_action(
                ProposalUserAction.ACTION_DECLINE.format(self.id), request
            )

    @transaction.atomic
    def recall(self, request):
        if not self.proposal.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()
        self.processing_status = Referral.PROCESSING_STATUS_RECALLED
        self.save()

        # Log an action for the proposal
        self.proposal.log_user_action(
            ProposalUserAction.RECALL_REFERRAL.format(self.id, self.proposal.id),
            request,
        )

        # Log an action for the applicant
        self.proposal.applicant.log_user_action(
            ProposalUserAction.RECALL_REFERRAL.format(self.id, self.proposal.id),
            request,
        )

    @property
    def referral_as_email_user(self):
        return retrieve_email_user(self.referral)

    @transaction.atomic
    def remind(self, request):
        if not self.proposal.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()

        # Create a log entry for the proposal
        self.proposal.log_user_action(
            ProposalUserAction.ACTION_REMIND_REFERRAL.format(
                self.id,
                self.proposal.id,
                f"{self.referral_as_email_user.get_full_name()}",
            ),
            request,
        )

        # Create a log entry for the applicant
        self.proposal.applicant.log_user_action(
            ProposalUserAction.ACTION_REMIND_REFERRAL.format(
                self.id,
                self.proposal.id,
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
        if not self.proposal.can_assess(request.user):
            raise exceptions.ProposalNotAuthorized()

        self.processing_status = Referral.PROCESSING_STATUS_WITH_REFERRAL
        self.proposal.processing_status = Proposal.PROCESSING_STATUS_WITH_REFERRAL
        self.proposal.save()
        self.save()

        # Create a log entry for the proposal
        self.proposal.log_user_action(
            ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(
                self.id,
                self.proposal.id,
                f"{self.referral_as_email_user.get_full_name()}",
            ),
            request,
        )

        # Create a log entry for the applicant
        self.proposal.applicant.log_user_action(
            ProposalUserAction.ACTION_RESEND_REFERRAL_TO.format(
                self.id,
                self.proposal.id,
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
        )

    @transaction.atomic
    def complete(self, request):
        self.processing_status = Referral.PROCESSING_STATUS_COMPLETED
        self.add_referral_document(request)
        self.save()

        # Log proposal action
        self.proposal.log_user_action(
            ProposalUserAction.CONCLUDE_REFERRAL.format(
                request.user.get_full_name(),
                self.id,
                self.proposal.lodgement_number,
            ),
            request,
        )

        # Create a log entry for the applicant
        self.proposal.applicant.log_user_action(
            ProposalUserAction.CONCLUDE_REFERRAL.format(
                request.user.get_full_name(),
                self.id,
                self.proposal.lodgement_number,
            ),
            request,
        )

        send_referral_complete_email_notification(self, request)

        # Check if this was the last pending referral for the proposal
        if not Referral.objects.filter(
            proposal=self.proposal,
            processing_status=Referral.PROCESSING_STATUS_WITH_REFERRAL,
        ).exists():
            # Change the status back to what it was before this referral was requested
            self.proposal.processing_status = Proposal.PROCESSING_STATUS_WITH_ASSESSOR
            self.proposal.save()

            send_pending_referrals_complete_email_notification(self, request)

    @transaction.atomic
    def add_referral_document(self, request):
        if "referral_document" not in request.data:
            return self

        referral_document = request.data["referral_document"]
        if referral_document != "null":
            try:
                document = self.referral_documents.get(
                    input_name=str(referral_document)
                )
            except ReferralDocument.DoesNotExist:
                document, created = self.referral_documents.get_or_create(
                    input_name=str(referral_document),
                    name=str(referral_document),
                )[0]
            document.name = str(referral_document)
            document._file = referral_document
            document.save()
            d = ReferralDocument.objects.get(id=document.id)
            self.document = d
            comment = f"Referral Document Added: {document.name}"
        else:
            self.document = None
            comment = "Referral Document Deleted"

        # to allow revision to be added to reversion history
        self.save(version_comment=comment)

        self.proposal.log_user_action(
            ProposalUserAction.ACTION_REFERRAL_DOCUMENT.format(self.id),
            request,
        )

        # Log entry for applicant
        self.proposal.applicant.log_user_action(
            ProposalUserAction.ACTION_DECLINE.format(self.id), request
        )

        return self

    @property
    def title(self):
        return self.proposal.title

    @property
    def applicant(self):
        return self.proposal.applicant

    @property
    def can_be_processed(self):
        return self.processing_status == Referral.PROCESSING_STATUS_WITH_REFERRAL

    def can_assess_referral(self, user):
        return self.processing_status == Referral.PROCESSING_STATUS_WITH_REFERRAL


class ExternalRefereeInvite(RevisionedMixin):
    email = models.EmailField()
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    organisation = models.CharField(max_length=100)
    datetime_sent = models.DateTimeField(null=True, blank=True)
    datetime_first_logged_in = models.DateTimeField(null=True, blank=True)
    proposal = models.ForeignKey(
        Proposal, related_name="external_referee_invites", on_delete=models.CASCADE
    )
    sent_by = models.IntegerField()
    invite_text = models.TextField(blank=True)
    archived = models.BooleanField(default=False)

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "External Referral"
        verbose_name_plural = "External Referrals"

    def __str__(self):
        return_str = (
            f"{self.first_name} {self.last_name} ({self.email}) [{self.organisation}]"
        )
        if self.archived:
            return_str += " - Archived"
        return return_str

    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def user_has_object_permission(self, user_id):
        emailuser = retrieve_email_user(user_id)
        if emailuser and emailuser.email == self.email:
            return True
        return self.proposal.user_has_object_permission(user_id)


class ProposalRequirement(RevisionedMixin):
    RECURRENCE_PATTERNS = [(1, "Weekly"), (2, "Monthly"), (3, "Yearly")]
    standard_requirement = models.ForeignKey(
        ProposalStandardRequirement, null=True, blank=True, on_delete=models.SET_NULL
    )
    free_requirement = models.TextField(null=True, blank=True)
    standard = models.BooleanField(default=True)
    proposal = models.ForeignKey(
        Proposal, related_name="requirements", on_delete=models.CASCADE
    )
    due_date = models.DateField(null=True, blank=True, default=None)
    reminder_date = models.DateField(null=True, blank=True)
    recurrence = models.BooleanField(default=False)
    recurrence_pattern = models.SmallIntegerField(
        choices=RECURRENCE_PATTERNS, default=1
    )
    recurrence_schedule = models.IntegerField(null=True, blank=True)
    copied_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, blank=True, null=True
    )
    is_deleted = models.BooleanField(default=False)
    copied_for_renewal = models.BooleanField(default=False)
    require_due_date = models.BooleanField(default=False)
    # To determine if requirement has been added by referral and the group of referral who added it
    # Null if added by an assessor
    referral_group = models.ForeignKey(
        ReferralRecipientGroup,
        null=True,
        blank=True,
        related_name="requirement_referral_groups",
        on_delete=models.SET_NULL,
    )
    referral = models.ForeignKey(
        Referral, blank=True, null=True, on_delete=models.SET_NULL
    )
    source = models.IntegerField(null=True)  # EmailUserRO
    notification_only = models.BooleanField(default=False)
    req_order = models.IntegerField(null=True, blank=True)

    class Meta:
        app_label = "leaseslicensing"
        ordering = ["proposal", "req_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["proposal", "req_order"],
                name="unique requirement order per proposal",
            )
        ]

    def __str__(self):
        if self.free_requirement:
            return f"Proposal {self.proposal}: {self.free_requirement}"
        elif self.standard_requirement:
            return f"Proposal {self.proposal}: {self.standard_requirement.text}"
        else:
            return f"Proposal {self.proposal}: {self.id}"

    def save(self, **kwargs):
        # import ipdb; ipdb.set_trace()
        # set the req_order if saving for the first time
        if not self.id:
            max_req_order = (
                ProposalRequirement.objects.filter(proposal_id=self.proposal_id)
                .aggregate(max_req_order=Max("req_order"))
                .get("max_req_order")
            )
            if not max_req_order:
                self.req_order = 1
            else:
                self.req_order = max_req_order + 1

        # Make sure proposal requirements have either a free requirement or a standard requirement
        if not self.free_requirement and not self.standard_requirement:
            raise ValueError(
                "You must enter either a free requirement or a standard requirement"
            )

        super().save(**kwargs)

    def get_next_due_date(self, due_date):
        for x in range(self.recurrence_schedule):
            if self.recurrence_pattern == 1:
                due_date += relativedelta(weeks=1)
            elif self.recurrence_pattern == 2:
                due_date += relativedelta(month=1)
            elif self.recurrence_pattern == 3:
                due_date += relativedelta(years=1)
        return due_date

    def swap_obj(self, up):
        increment = -1
        swap_increment = None
        for req in ProposalRequirement.objects.filter(
            proposal_id=self.proposal_id, is_deleted=False
        ).order_by("req_order"):
            increment += 1
            if req.id == self.id:
                break
        if up:
            swap_increment = increment - 1
        else:
            swap_increment = increment + 1

        return ProposalRequirement.objects.filter(
            proposal_id=self.proposal_id, is_deleted=False
        ).order_by("req_order")[swap_increment]

    def move_up(self):
        # ignore deleted reqs
        if self.req_order == ProposalRequirement.objects.filter(
            is_deleted=False, proposal_id=self.proposal_id
        ).aggregate(min_req_order=Min("req_order")).get("min_req_order"):
            pass
        else:
            self.swap(self.swap_obj(True))

    def move_down(self):
        # ignore deleted reqs
        if self.req_order == ProposalRequirement.objects.filter(
            is_deleted=False, proposal_id=self.proposal_id
        ).aggregate(max_req_order=Max("req_order")).get("max_req_order"):
            pass
        else:
            self.swap(self.swap_obj(False))

    def swap(self, other):
        new_self_position = other.req_order
        new_other_position = self.req_order
        # null out both values to prevent a db constraint error on save()
        self.req_order = None
        self.save()
        other.req_order = None
        other.save()
        # set new positions
        self.req_order = new_self_position
        self.save()
        other.req_order = new_other_position
        other.save()

    @property
    def requirement(self):
        return (
            self.standard_requirement.text if self.standard else self.free_requirement
        )

    def can_referral_edit(self, user):
        if self.proposal.processing_status in [
            Proposal.PROCESSING_STATUS_WITH_REFERRAL,
        ]:
            if self.referral_group:
                group = ReferralRecipientGroup.objects.filter(id=self.referral_group.id)
                # user=request.user
                if group and group[0] in user.referralrecipientgroup_set.all():
                    return True
                else:
                    return False
            elif self.proposal.is_referee(user):
                # True if this referral user's requirement
                if (
                    hasattr(self.referral, "referral")
                    and self.referral.referral == user.id
                    and self.source == user.id
                ):
                    return True
                else:
                    return False
        return False

    @transaction.atomic
    def add_documents(self, request):
        # save the files
        data = json.loads(request.data.get("data"))
        if not data.get("update"):
            documents_qs = self.requirement_documents.filter(
                input_name="requirement_doc", visible=True
            )
            documents_qs.delete()
        for idx in range(data["num_files"]):
            _file = request.data.get("file-" + str(idx))
            document = self.requirement_documents.create(_file=_file, name=_file.name)
            document.input_name = data["input_name"]
            document.can_delete = True
            document.save()
        # end save documents
        self.save()

        return


class SectionChecklist(RevisionedMixin):
    """
    This object is per section per type(assessor/referral) grouping the ChecklistQuestion objects
    """

    SECTION_MAP = "map"
    SECTION_PROPOSAL_DETAILS = "proposal_details"
    SECTION_PROPOSAL_IMPACT = "proposal_impact"
    SECTION_OTHER = "other"
    SECTION_DEED_POLL = "deed_poll"
    SECTION_ADDITIONAL_DOCUMENTS = "additional_documents"
    SECTION_CHOICES = (
        (SECTION_MAP, "Map"),
        (SECTION_PROPOSAL_DETAILS, "Proposal Details"),
        (SECTION_PROPOSAL_IMPACT, "Proposal Impact"),
        (SECTION_OTHER, "Other"),
        (SECTION_DEED_POLL, "Deed Poll"),
        (SECTION_ADDITIONAL_DOCUMENTS, "Additional Documents"),
    )
    LIST_TYPE_ASSESSOR = "assessor_list"
    LIST_TYPE_REFERRAL = "referral_list"
    LIST_TYPE_CHOICES = (
        (LIST_TYPE_ASSESSOR, "Assessor Checklist"),
        (LIST_TYPE_REFERRAL, "Referral Checklist"),
    )

    application_type = models.ForeignKey(
        ApplicationType, blank=True, null=True, on_delete=models.SET_NULL
    )
    section = models.CharField(
        "Section", max_length=50, choices=SECTION_CHOICES, default=SECTION_CHOICES[0][0]
    )
    list_type = models.CharField(
        "Checklist type",
        max_length=30,
        choices=LIST_TYPE_CHOICES,
        default=LIST_TYPE_CHOICES[0][0],
    )
    enabled = models.BooleanField(default=True)

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Section Questions"
        verbose_name_plural = "Section Questions"

    def __str__(self):
        return f"Questions for {self.get_section_display()}:"

    @property
    def number_of_questions(self):
        return "{}/{}".format(
            self.number_of_enabled_questions, self.number_of_total_questions
        )

    @property
    def number_of_total_questions(self):
        return (
            self.questions.count() if self.questions else 0
        )  # 'questions' is a related_name of ChecklistQuestion

    @property
    def number_of_enabled_questions(self):
        return (
            self.questions.filter(enabled=True).count()
            if self.questions and self.questions.filter(enabled=True)
            else 0
        )  # 'questions' is a related_name of ChecklistQuestion


class ChecklistQuestion(RevisionedMixin):
    ANSWER_TYPE_CHOICES = (("yes_no", "Yes/No type"), ("free_text", "Free text type"))
    text = models.TextField()  # This is question text
    answer_type = models.CharField(
        "Answer type",
        max_length=30,
        choices=ANSWER_TYPE_CHOICES,
        default=ANSWER_TYPE_CHOICES[0][0],
    )
    enabled = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=1)
    section_checklist = models.ForeignKey(
        SectionChecklist,
        blank=True,
        null=True,
        related_name="questions",
        on_delete=models.SET_NULL,
    )
    shown_to_others = models.BooleanField(
        "Comment", default=False, help_text="When checked, question is shown to others"
    )  # When True, this QA is shown to other parties.  Of course not editable, though.

    def __str__(self):
        return self.text

    class Meta:
        app_label = "leaseslicensing"
        ordering = [
            "order",
        ]


class ProposalAssessment(RevisionedMixin):
    proposal = models.ForeignKey(
        Proposal, related_name="assessment", on_delete=models.CASCADE
    )
    completed = models.BooleanField(default=False)
    submitter = models.IntegerField(blank=True, null=True)  # EmailUserRO
    referral = models.ForeignKey(
        Referral,
        related_name="assessment",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )  # When referral is none, this ProposalAssessment is for assessor.
    # comments and deficiencies

    # ROI comment fields
    assessor_comment_map = models.TextField(blank=True)
    assessor_comment_proposal_details = models.TextField(blank=True)
    assessor_comment_proposal_impact = models.TextField(blank=True)
    assessor_comment_gis_data = models.TextField(blank=True)
    assessor_comment_categorisation = models.TextField(blank=True)
    assessor_comment_deed_poll = models.TextField(blank=True)
    assessor_comment_additional_documents = models.TextField(blank=True)

    # Lease License comment fields
    assessor_comment_tourism_proposal_details = models.TextField(blank=True)
    assessor_comment_general_proposal_details = models.TextField(blank=True)

    # ROI comment fields
    deficiency_comment_map = models.TextField(blank=True)
    deficiency_comment_proposal_details = models.TextField(blank=True)
    deficiency_comment_proposal_impact = models.TextField(blank=True)
    deficiency_comment_gis_data = models.TextField(blank=True)
    deficiency_comment_categorisation = models.TextField(blank=True)
    deficiency_comment_deed_poll = models.TextField(blank=True)
    deficiency_comment_additional_documents = models.TextField(blank=True)

    # Lease License comment fields
    deficiency_comment_tourism_proposal_details = models.TextField(blank=True)
    deficiency_comment_general_proposal_details = models.TextField(blank=True)

    class Meta:
        app_label = "leaseslicensing"
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "proposal",
                    "referral",
                ],
                name="unique_per_proposal_per_assessor_or_referral",
            ),
        ]

    @property
    def checklist(self):
        return self.answers.all()

    @property
    def referral_assessment(self):
        # When self.referral != null, this assessment is for referral, otherwise this assessment is for assessor.
        return True if self.referral else False


class ProposalAssessmentAnswer(RevisionedMixin):
    checklist_question = models.ForeignKey(
        ChecklistQuestion, related_name="answers", on_delete=models.CASCADE
    )
    answer_yes_no = models.BooleanField(null=True)
    proposal_assessment = models.ForeignKey(
        ProposalAssessment,
        related_name="answers",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    answer_text = models.CharField(max_length=256, blank=True, null=True)

    def __str__(self):
        return self.checklist_question.text

    @property
    def shown_to_others(self):
        return self.checklist_question.shown_to_others

    class Meta:
        app_label = "leaseslicensing"
        verbose_name = "Assessment answer"
        verbose_name_plural = "Assessment answers"


@transaction.atomic
def clone_proposal_with_status_reset(original_proposal):
    application_type = ApplicationType.objects.get(name="lease_licence")

    try:
        cloned_proposal = Proposal.objects.create(
            application_type=application_type,
            ind_applicant=original_proposal.ind_applicant,
            org_applicant=original_proposal.org_applicant,
            previous_application=original_proposal,
            approval=original_proposal.approval,
        )
    except IntegrityError as e:
        logger.exception(e)
        raise e
    except Exception as e:
        logger.exception(e)
        raise e
    else:
        if not original_proposal.org_applicant:
            original_applicant = ProposalApplicant.objects.get(
                proposal=original_proposal
            )
            # Creating a copy for the new proposal here. This will be invoked from renew and amend approval
            original_applicant.copy_self_to_proposal(cloned_proposal)
        return cloned_proposal


def clone_documents(proposal, original_proposal, media_prefix):
    for proposal_document in ProposalDocument.objects.filter(proposal_id=proposal.id):
        proposal_document._file.name = "proposals/{}/documents/{}".format(
            proposal.id, proposal_document.name
        )
        proposal_document.can_delete = True
        proposal_document.save()

    for referral in proposal.referrals.all():
        for referral_document in ReferralDocument.objects.filter(referral=referral):
            referral_document._file.name = "proposals/{}/referral/{}".format(
                proposal.id, referral_document.name
            )
            referral_document.can_delete = True
            referral_document.save()

    for requirement in proposal.requirements.all():
        for requirement_document in RequirementDocument.objects.filter(
            requirement=requirement
        ):
            requirement_document._file.name = (
                "proposals/{}/requirement_documents/{}".format(
                    proposal.id, requirement_document.name
                )
            )
            requirement_document.can_delete = True
            requirement_document.save()

    for log_entry_document in ProposalLogDocument.objects.filter(
        log_entry__proposal_id=proposal.id
    ):
        log_entry_document._file.name = log_entry_document._file.name.replace(
            str(original_proposal.id), str(proposal.id)
        )
        log_entry_document.can_delete = True
        log_entry_document.save()

    # copy documents on file system and reset can_delete flag
    # Not 100% sure this will work after implementing the secure file storage
    media_dir = f"{media_prefix}/{settings.MEDIA_APP_DIR}"
    subprocess.call(
        "cp -pr {0}/proposals/{1} {0}/proposals/{2}".format(
            media_dir, original_proposal.id, proposal.id
        ),
        shell=True,
    )


def _clone_documents(proposal, original_proposal, media_prefix):
    for proposal_document in ProposalDocument.objects.filter(
        proposal=original_proposal.id
    ):
        proposal_document.proposal = proposal
        proposal_document.id = None
        proposal_document._file.name = "proposals/{}/documents/{}".format(
            proposal.id, proposal_document.name
        )
        proposal_document.can_delete = True
        proposal_document.save()

    # copy documents on file system and reset can_delete flag
    # Not 100% sure this will work after implementing the secure file storage
    media_dir = f"{media_prefix}/{settings.MEDIA_APP_DIR}"
    subprocess.call(
        "cp -pr {0}/proposals/{1} {0}/proposals/{2}".format(
            media_dir, original_proposal.id, proposal.id
        ),
        shell=True,
    )


def search_reference(reference_number):
    from leaseslicensing.components.approvals.models import Approval
    from leaseslicensing.components.compliances.models import Compliance

    proposal_list = Proposal.objects.all().exclude(
        processing_status=Proposal.PROCESSING_STATUS_DISCARDED
    )
    approval_list = (
        Approval.objects.all()
        .order_by("lodgement_number", "-issue_date")
        .distinct("lodgement_number")
    )
    compliance_list = Compliance.objects.all().exclude(
        processing_status=Compliance.PROCESSING_STATUS_FUTURE
    )
    record = {}
    try:
        result = proposal_list.get(lodgement_number=reference_number)
        record = {"id": result.id, "type": "proposal"}
    except Proposal.DoesNotExist:
        try:
            result = approval_list.get(lodgement_number=reference_number)
            record = {"id": result.id, "type": "approval"}
        except Approval.DoesNotExist:
            try:
                for c in compliance_list:
                    if c.reference == reference_number:
                        record = {"id": c.id, "type": "compliance"}
            except Exception as e:
                logger.exception(e)
                raise ValidationError(
                    "Record with provided reference number does not exist"
                )
    if record:
        return record
    else:
        raise ValidationError("Record with provided reference number does not exist")


class HelpPage(models.Model):
    HELP_TEXT_EXTERNAL = 1
    HELP_TEXT_INTERNAL = 2
    HELP_TYPE_CHOICES = (
        (HELP_TEXT_EXTERNAL, "External"),
        (HELP_TEXT_INTERNAL, "Internal"),
    )

    application_type = models.ForeignKey(
        ApplicationType, null=True, on_delete=models.SET_NULL
    )
    content = RichTextField()
    description = models.CharField(max_length=256, blank=True, null=True)
    help_type = models.SmallIntegerField(
        "Help Type", choices=HELP_TYPE_CHOICES, default=HELP_TEXT_EXTERNAL
    )
    version = models.SmallIntegerField(default=1, blank=False, null=False)

    class Meta:
        app_label = "leaseslicensing"
        unique_together = ("application_type", "help_type", "version")


def copy_site_name(proposalFrom: Proposal, proposalTo: Proposal) -> None:
    """Copies the site name from proposalFrom to proposalTo"""
    proposalTo.site_name = proposalFrom.site_name
    proposalTo.save()


def copy_groups(proposalFrom, proposalTo):
    for group in proposalFrom.groups.all():
        ProposalGroup.objects.get_or_create(proposal=proposalTo, group=group.group)


def copy_proposal_geometry(proposalFrom: Proposal, proposalTo: Proposal) -> None:
    for proposal_geometry in ProposalGeometry.objects.filter(proposal=proposalFrom):
        ProposalGeometry.objects.get_or_create(
            proposal=proposalTo,
            polygon=proposal_geometry.polygon,
            intersects=True,
            copied_from=proposal_geometry,
            drawn_by=proposal_geometry.drawn_by,
            locked=True,
        )


def copy_gis_data(proposalFrom: Proposal, proposalTo: Proposal) -> None:
    for gis_model in GIS_DATA_MODEL_NAMES:
        model_class = apps.get_model("leaseslicensing", f"proposal{gis_model}")
        models = model_class.objects.filter(proposal=proposalFrom)
        for model in models:
            model_class.objects.get_or_create(
                proposal=proposalTo, **{f"{gis_model}": getattr(model, f"{gis_model}")}
            )


def copy_proposal_proposed_issuance(
    proposalFrom: Proposal, proposalTo: Proposal, **kwargs
) -> None:
    """
    Pre-populates assessor's proposed issuance dict to the new proposal, deleting previous
    details, approved on, and approved by information
    Args:
        proposalFrom: previous Proposal
        proposalTo: new Proposal
    """

    is_renewal = kwargs.get("is_renewal", False)

    proposed_issuance = {
        k: v for k, v in proposalFrom.proposed_issuance_approval.items()
    }
    proposed_issuance["details"] = None  # Assessor needs to fill this in
    proposed_issuance["approved_on"] = None  # Populated by the system
    proposed_issuance["approved_by"] = None  # Populated by the system

    if is_renewal:
        # Change start and expiry dates for renewal according to previous approval dates
        time_format = "%Y-%m-%d"
        original_start_date = datetime.datetime.strptime(
            proposed_issuance["start_date"], time_format
        )
        original_expiry_date = datetime.datetime.strptime(
            proposed_issuance["expiry_date"], time_format
        )
        proposed_issuance["start_date"] = (
            original_expiry_date + datetime.timedelta(days=1)
        ).strftime(
            time_format
        )  # Start date is the day after the expiry date of the original proposal
        proposed_issuance["expiry_date"] = (
            original_expiry_date + (original_expiry_date - original_start_date)
        ).strftime(
            time_format
        )  # Expiry date is after the same duration as the original proposal

    proposalTo.proposed_issuance_approval = proposed_issuance


def copy_proposal_details(proposalFrom: Proposal, proposalTo: Proposal) -> None:
    """
    Copies over any tourism, general, prn str type proposal details and documents
    """

    details_fields = [
        "profit_and_loss",
        "cash_flow",
        "capital_investment",
        "financial_capacity",
        "available_activities",
        "market_analysis",
        "staffing",
        "key_personnel",
        "key_milestones",
        "risk_factors",
        "legislative_requirements",
    ]

    for field in details_fields:
        f_text = f"{field}_text"
        setattr(proposalTo, f_text, getattr(proposalFrom, f_text))
        f_doc = f"{field}_documents"
        for doc in getattr(proposalFrom, f_doc).all():
            new_doc = deepcopy(doc)
            new_doc.proposal = proposalTo
            new_doc.can_delete = True
            new_doc.hidden = False
            new_doc.id = None
            new_doc.save()

    for field in ["proponent_reference_number", "site_name_id"]:
        setattr(proposalTo, field, getattr(proposalFrom, field))


def copy_deed_poll_documents(proposalFrom: Proposal, proposalTo: Proposal) -> None:
    if not proposalFrom.deed_poll_documents.exists():
        return

    for deed_poll_document in proposalFrom.deed_poll_documents.all():
        new_doc = deepcopy(deed_poll_document)
        new_doc.proposal = proposalTo
        new_doc.can_delete = True
        new_doc.hidden = False
        new_doc.id = None
        new_doc.save()


def copy_proposal_requirements(
    proposalFrom: Proposal, proposalTo: Proposal, **kwargs
) -> None:
    """
    Copies all requirements and requirement documents from previous proposal
    """

    req = proposalFrom.requirements.all().exclude(is_deleted=True)

    if req:
        for r in req:
            new_r = deepcopy(r)
            new_r.proposal = proposalTo
            new_r.copied_from = r
            new_r.copied_for_renewal = (
                True  # Note: This field is not actually used in any business logic
            )
            if new_r.due_date:
                new_r.due_date = None
                new_r.reminder_date = None
                new_r.require_due_date = True
            new_r.id = None
            new_r.save()

    for requirement in proposalTo.requirements.all():
        for requirement_document in RequirementDocument.objects.filter(
            requirement=requirement.copied_from
        ):
            requirement_document.requirement = requirement
            requirement_document.id = None
            requirement_document._file.name = (
                "proposals/{}/requirement_documents/{}".format(
                    proposalTo.id,
                    requirement_document.name,
                )
            )
            requirement_document.can_delete = True
            requirement_document.save()


# Functions for copying data from a proposal to a competitive process
def copy_site_name_to_competitive_process(
    proposal: Proposal, competitive_process: CompetitiveProcess
) -> None:
    competitive_process.site_name = proposal.site_name
    competitive_process.save()


def copy_groups_to_competitive_process(
    proposal: Proposal, competitive_process: CompetitiveProcess
) -> None:
    for group in proposal.groups.all():
        CompetitiveProcessGroup.objects.get_or_create(
            competitive_process=competitive_process, group=group.group
        )


def copy_proposal_geometry_to_competitive_process(
    proposal: Proposal, competitive_process: CompetitiveProcess
) -> None:
    for proposal_geometry in ProposalGeometry.objects.filter(proposal=proposal):
        CompetitiveProcessGeometry.objects.get_or_create(
            competitive_process=competitive_process,
            polygon=proposal_geometry.polygon,
            intersects=True,
            drawn_by=proposal_geometry.drawn_by,
            locked=True,
        )


def copy_gis_data_to_competitive_process(
    proposal: Proposal, competitive_process: CompetitiveProcess
) -> None:
    for gis_model in GIS_DATA_MODEL_NAMES:
        model_class_from = apps.get_model("leaseslicensing", f"proposal{gis_model}")
        model_class_to = apps.get_model(
            "leaseslicensing", f"competitiveprocess{gis_model}"
        )
        models_from = model_class_from.objects.filter(proposal=proposal)
        for model in models_from:
            model_class_to.objects.get_or_create(
                competitive_process=competitive_process,
                **{f"{gis_model}": getattr(model, f"{gis_model}")},
            )


def create_competitive_process_party_from_proposal(
    proposal: Proposal, competitive_process: CompetitiveProcess
) -> None:
    if proposal.ind_applicant:
        CompetitiveProcessParty.objects.get_or_create(
            competitive_process=competitive_process,
            person_id=proposal.ind_applicant,
            invited_at=competitive_process.created_at,
        )
    elif proposal.org_applicant:
        CompetitiveProcessParty.objects.get_or_create(
            competitive_process=competitive_process,
            organisation=proposal.org_applicant,
            invited_at=competitive_process.created_at,
        )
    else:
        logger.warning(
            f"Proposal {proposal.id} has no applicant, cannot create competitive process party"
        )
