import json
import logging
import re

from django.conf import settings
from django.contrib.gis.gdal import SpatialReference
from django.contrib.gis.geos import GEOSGeometry, LinearRing, Polygon
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from ledger_api_client.ledger_models import EmailUserRO as EmailUser  # , Document
from ledger_api_client.ledger_models import Invoice

from leaseslicensing.components.approvals import email as approval_email
from leaseslicensing.components.approvals.models import Approval
from leaseslicensing.components.bookings import email as booking_email
from leaseslicensing.components.bookings.models import ApplicationFee, Booking
from leaseslicensing.components.compliances import email as compliance_email
from leaseslicensing.components.compliances.models import Compliance
from leaseslicensing.components.main.utils import (
    get_dbca_lands_and_waters_geos,
    get_gis_data_for_proposal,
)
from leaseslicensing.components.proposals import email as proposal_email
from leaseslicensing.components.proposals.email import (
    send_external_submit_email_notification,
    send_submit_email_notification,
)
from leaseslicensing.components.proposals.models import (
    AmendmentRequest,
    ProposalAct,
    ProposalAssessment,
    ProposalAssessmentAnswer,
    ProposalCategory,
    ProposalDeclinedDetails,
    ProposalDistrict,
    ProposalGeometry,
    ProposalGroup,
    ProposalLGA,
    ProposalRegion,
    ProposalTenure,
    ProposalUserAction,
    Referral,
)
from leaseslicensing.components.proposals.serializers import (
    ProposalAssessmentAnswerSerializer,
    ProposalGeometrySaveSerializer,
    SaveLeaseLicenceSerializer,
    SaveRegistrationOfInterestSerializer,
)
from leaseslicensing.components.tenure.models import (
    LGA,
    Act,
    Category,
    District,
    Region,
    SiteName,
    Tenure,
)
from leaseslicensing.helpers import is_assessor

logger = logging.getLogger(__name__)


def create_data_from_form(
    schema,
    post_data,
    file_data,
    post_data_index=None,
    special_fields=[],
    assessor_data=False,
):
    data = {}
    special_fields_list = []
    assessor_data_list = []
    comment_data_list = {}
    special_fields_search = SpecialFieldsSearch(special_fields)
    if assessor_data:
        assessor_fields_search = AssessorDataSearch()
        comment_fields_search = CommentDataSearch()
        for item in schema:
            data.update(_create_data_from_item(item, post_data, file_data, 0, ""))
            # _create_data_from_item(item, post_data, file_data, 0, '')
            special_fields_search.extract_special_fields(
                item, post_data, file_data, 0, ""
            )
            if assessor_data:
                assessor_fields_search.extract_special_fields(
                    item, post_data, file_data, 0, ""
                )
                comment_fields_search.extract_special_fields(
                    item, post_data, file_data, 0, ""
                )
        special_fields_list = special_fields_search.special_fields
        if assessor_data:
            assessor_data_list = assessor_fields_search.assessor_data
            comment_data_list = comment_fields_search.comment_data

    if assessor_data:
        return [data], special_fields_list, assessor_data_list, comment_data_list

    return [data], special_fields_list


def _extend_item_name(name, suffix, repetition):
    return f"{name}{suffix}-{repetition}"


def _create_data_from_item(item, post_data, file_data, repetition, suffix):
    item_data = {}

    if "name" in item:
        extended_item_name = item["name"]
    else:
        raise Exception("Missing name in item %s" % item["label"])

    if "children" not in item:
        if item["type"] in ["checkbox" "declaration"]:
            # item_data[item['name']] = post_data[item['name']]
            item_data[item["name"]] = extended_item_name in post_data
        elif item["type"] == "file":
            if extended_item_name in file_data:
                item_data[item["name"]] = str(file_data.get(extended_item_name))
                # TODO save the file here
            elif (
                extended_item_name + "-existing" in post_data
                and len(post_data[extended_item_name + "-existing"]) > 0
            ):
                item_data[item["name"]] = post_data.get(
                    extended_item_name + "-existing"
                )
            else:
                item_data[item["name"]] = ""
        else:
            if extended_item_name in post_data:
                if item["type"] == "multi-select":
                    item_data[item["name"]] = post_data.getlist(extended_item_name)
                else:
                    item_data[item["name"]] = post_data.get(extended_item_name)
    else:
        if "repetition" in item:
            item_data = generate_item_data(
                extended_item_name,
                item,
                item_data,
                post_data,
                file_data,
                len(post_data[item["name"]]),
                suffix,
            )
        else:
            item_data = generate_item_data(
                extended_item_name, item, item_data, post_data, file_data, 1, suffix
            )

    if "conditions" in item:
        for condition in item["conditions"].keys():
            for child in item["conditions"][condition]:
                item_data.update(
                    _create_data_from_item(
                        child, post_data, file_data, repetition, suffix
                    )
                )

    return item_data


def generate_item_data(
    item_name, item, item_data, post_data, file_data, repetition, suffix
):
    item_data_list = []
    for rep in range(0, repetition):
        child_data = {}
        for child_item in item.get("children"):
            child_data.update(
                _create_data_from_item(
                    child_item, post_data, file_data, 0, f"{suffix}-{rep}"
                )
            )
        item_data_list.append(child_data)

        item_data[item["name"]] = item_data_list
    return item_data


class AssessorDataSearch:
    def __init__(self, lookup_field="canBeEditedByAssessor"):
        self.lookup_field = lookup_field
        self.assessor_data = []

    def extract_assessor_data(self, item, post_data):
        values = []
        res = {"name": item, "assessor": "", "referrals": []}
        for k in post_data:
            if re.match(item, k):
                values.append({k: post_data[k]})
        if values:
            for v in values:
                for k, v in v.items():
                    parts = k.split(f"{item}-")
                    if len(parts) > 1:
                        # split parts to see if referall
                        ref_parts = parts[1].split("Referral-")
                        if len(ref_parts) > 1:
                            # Referrals
                            res["referrals"].append(
                                {
                                    "value": v,
                                    "email": ref_parts[1],
                                    "full_name": EmailUser.objects.get(
                                        email=ref_parts[1].lower()
                                    ).get_full_name(),
                                }
                            )
                        elif k.split("-")[-1].lower() == "assessor":
                            # Assessor
                            res["assessor"] = v

        return res

    def extract_special_fields(self, item, post_data, file_data, repetition, suffix):
        item_data = {}
        if "name" in item:
            extended_item_name = item["name"]
        else:
            raise Exception("Missing name in item %s" % item["label"])

        if "children" not in item:
            if "conditions" in item:
                for condition in item["conditions"].keys():
                    for child in item["conditions"][condition]:
                        item_data.update(
                            self.extract_special_fields(
                                child, post_data, file_data, repetition, suffix
                            )
                        )

            if item.get(self.lookup_field):
                self.assessor_data.append(
                    self.extract_assessor_data(extended_item_name, post_data)
                )

        else:
            if "repetition" in item:
                item_data = self.generate_item_data_special_field(
                    extended_item_name,
                    item,
                    item_data,
                    post_data,
                    file_data,
                    len(post_data[item["name"]]),
                    suffix,
                )
            else:
                item_data = self.generate_item_data_special_field(
                    extended_item_name, item, item_data, post_data, file_data, 1, suffix
                )

            if "conditions" in item:
                for condition in item["conditions"].keys():
                    for child in item["conditions"][condition]:
                        item_data.update(
                            self.extract_special_fields(
                                child, post_data, file_data, repetition, suffix
                            )
                        )

        return item_data

    def generate_item_data_special_field(
        self, item_name, item, item_data, post_data, file_data, repetition, suffix
    ):
        item_data_list = []
        for rep in range(0, repetition):
            child_data = {}
            for child_item in item.get("children"):
                child_data.update(
                    self.extract_special_fields(
                        child_item, post_data, file_data, 0, f"{suffix}-{rep}"
                    )
                )
            item_data_list.append(child_data)

            item_data[item["name"]] = item_data_list
        return item_data


class CommentDataSearch:
    def __init__(self, lookup_field="canBeEditedByAssessor"):
        self.lookup_field = lookup_field
        self.comment_data = {}

    def extract_comment_data(self, item, post_data):
        res = {}
        values = []
        for k in post_data:
            if re.match(item, k):
                values.append({k: post_data[k]})
        if values:
            for v in values:
                for k, v in v.items():
                    parts = k.split(f"{item}")
                    if len(parts) > 1:
                        ref_parts = parts[1].split("-comment-field")
                        if len(ref_parts) > 1:
                            res = {f"{item}": v}
        return res

    def extract_special_fields(self, item, post_data, file_data, repetition, suffix):
        item_data = {}
        if "name" in item:
            extended_item_name = item["name"]
        else:
            raise Exception("Missing name in item %s" % item["label"])

        if "children" not in item:
            self.comment_data.update(
                self.extract_comment_data(extended_item_name, post_data)
            )

        else:
            if "repetition" in item:
                item_data = self.generate_item_data_special_field(
                    extended_item_name,
                    item,
                    item_data,
                    post_data,
                    file_data,
                    len(post_data[item["name"]]),
                    suffix,
                )
            else:
                item_data = self.generate_item_data_special_field(
                    extended_item_name, item, item_data, post_data, file_data, 1, suffix
                )

        if "conditions" in item:
            for condition in item["conditions"].keys():
                for child in item["conditions"][condition]:
                    item_data.update(
                        self.extract_special_fields(
                            child, post_data, file_data, repetition, suffix
                        )
                    )

        return item_data

    def generate_item_data_special_field(
        self, item_name, item, item_data, post_data, file_data, repetition, suffix
    ):
        item_data_list = []
        for rep in range(0, repetition):
            child_data = {}
            for child_item in item.get("children"):
                child_data.update(
                    self.extract_special_fields(
                        child_item, post_data, file_data, 0, f"{suffix}-{rep}"
                    )
                )
            item_data_list.append(child_data)

            item_data[item["name"]] = item_data_list
        return item_data


class SpecialFieldsSearch:
    def __init__(self, lookable_fields):
        self.lookable_fields = lookable_fields
        self.special_fields = {}

    def extract_special_fields(self, item, post_data, file_data, repetition, suffix):
        item_data = {}
        if "name" in item:
            extended_item_name = item["name"]
        else:
            raise Exception("Missing name in item %s" % item["label"])

        if "children" not in item:
            for f in self.lookable_fields:
                if item["type"] in ["checkbox" "declaration"]:
                    val = None
                    val = item.get(f, None)
                    if val:
                        item_data[f] = extended_item_name in post_data
                        self.special_fields.update(item_data)
                else:
                    if extended_item_name in post_data:
                        val = None
                        val = item.get(f, None)
                        if val:
                            if item["type"] == "multi-select":
                                item_data[f] = ",".join(
                                    post_data.getlist(extended_item_name)
                                )
                            else:
                                item_data[f] = post_data.get(extended_item_name)
                            self.special_fields.update(item_data)
        else:
            if "repetition" in item:
                item_data = self.generate_item_data_special_field(
                    extended_item_name,
                    item,
                    item_data,
                    post_data,
                    file_data,
                    len(post_data[item["name"]]),
                    suffix,
                )
            else:
                item_data = self.generate_item_data_special_field(
                    extended_item_name, item, item_data, post_data, file_data, 1, suffix
                )

        if "conditions" in item:
            for condition in item["conditions"].keys():
                for child in item["conditions"][condition]:
                    item_data.update(
                        self.extract_special_fields(
                            child, post_data, file_data, repetition, suffix
                        )
                    )

        return item_data

    def generate_item_data_special_field(
        self, item_name, item, item_data, post_data, file_data, repetition, suffix
    ):
        item_data_list = []
        for rep in range(0, repetition):
            child_data = {}
            for child_item in item.get("children"):
                child_data.update(
                    self.extract_special_fields(
                        child_item, post_data, file_data, 0, f"{suffix}-{rep}"
                    )
                )
            item_data_list.append(child_data)

            item_data[item["name"]] = item_data_list
        return item_data


def save_proponent_data(instance, request, viewset):
    logger.debug("save_proponent_data")
    if (
        instance.application_type.name
        == settings.APPLICATION_TYPE_REGISTRATION_OF_INTEREST
    ):
        save_proponent_data_registration_of_interest(instance, request, viewset)
    elif instance.application_type.name == settings.APPLICATION_TYPE_LEASE_LICENCE:
        save_proponent_data_lease_licence(instance, request, viewset)


def save_proponent_data_registration_of_interest(instance, request, viewset):
    # proposal
    proposal_data = request.data.get("proposal") if request.data.get("proposal") else {}
    logger.debug("proposal_data = " + str(proposal_data))
    serializer = SaveRegistrationOfInterestSerializer(
        instance,
        data=proposal_data,
        context={
            "action": viewset.action,
        },
    )
    logger.debug("Validating SaveRegistrationOfInterestSerializer")
    serializer.is_valid(raise_exception=True)
    instance = serializer.save()

    logger.debug("Saving groups data.")
    save_groups_data(instance, proposal_data["groups"])

    if request.data.get("proposal_geometry"):
        save_geometry(instance, request, viewset)
    if viewset.action == "submit":
        check_geometry(instance)


def save_proponent_data_lease_licence(instance, request, viewset):
    # proposal
    proposal_data = request.data.get("proposal") if request.data.get("proposal") else {}
    logger.debug(f"proposal_data = {proposal_data}")

    serializer = SaveLeaseLicenceSerializer(
        instance,
        data=proposal_data,
        context={
            "action": viewset.action,
        },
    )
    serializer.is_valid(raise_exception=True)
    instance = serializer.save()

    save_groups_data(instance, proposal_data["groups"])

    if request.data.get("proposal_geometry"):
        save_geometry(instance, request, viewset)
    if viewset.action == "submit":
        check_geometry(instance)


def _save_answer_dict(answer_dict):
    answer_obj = ProposalAssessmentAnswer.objects.get(id=int(answer_dict["id"]))
    serializer = ProposalAssessmentAnswerSerializer(answer_obj, answer_dict)
    serializer.is_valid(raise_exception=True)
    answer_obj = serializer.save()
    return answer_obj


def save_referral_data(proposal, request, referral_completed=False):
    with transaction.atomic():
        proposal_data = (
            request.data.get("proposal") if request.data.get("proposal") else {}
        )

        # Save checklist answers
        if (
            "referral_assessments" in proposal_data
            and proposal_data["referral_assessments"]
        ):
            for assessment in proposal_data["referral_assessments"]:
                # For each assessment
                if assessment["referral"]["referral"]["id"] == request.user.id:
                    # When this assessment is for the accessing user
                    for section, answers in assessment["section_answers"].items():
                        # Save answers
                        if not assessment["completed"]:
                            for answer_dict in answers:
                                answer_obj = _save_answer_dict(answer_dict)
                                # Not yet sure what the intention for answer_ob is
                                # but just printing as it wasn't accessed.
                                print(answer_obj)
                    if referral_completed:
                        # Make this assessment completed
                        # TODO Why is `assessment` reassigned here?
                        # There is already a `ProposalAssessment` object to work with.
                        assessment = ProposalAssessment.objects.get(
                            id=int(assessment["id"])
                        )
                        assessment.completed = True
                        assessment.submitter = request.user.id
                        assessment.save()
                        assessment.referral.complete(request)

        # Referrals are not allowed to edit geometry


def save_assessor_data(proposal, request, viewset):
    logger.debug("save_assessor_data")
    with transaction.atomic():
        proposal_data = {}
        if request.data.get("proposal"):
            # request.data is like {'proposal': {'id': ..., ...}}
            proposal_data = request.data.get("proposal")
        else:
            # request.data is a dictionary of the proposal {'id': ..., ...}
            proposal_data = request.data

        save_groups_data(proposal, proposal_data["groups"])

        # Save checklist answers
        if is_assessor(request):
            # When this assessment is for the accessing user
            if (
                "assessor_assessment" in proposal_data
                and proposal_data["assessor_assessment"]
            ):
                for section, answers in proposal_data["assessor_assessment"][
                    "section_answers"
                ].items():
                    for answer_dict in answers:
                        answer_obj = _save_answer_dict(answer_dict)
                        # Not yet sure what the intention for answer_ob is but just printing as it wasn't accessed.
                        print(answer_obj)
        # Save geometry
        if request.data.get(
            "proposal_geometry"
        ):  # To save geometry, it should be named 'proposal_geometry'
            save_geometry(proposal, request, viewset)


def check_geometry(instance):
    for geom in instance.proposalgeometry.all():
        if not geom.intersects:
            raise ValidationError(
                "One or more polygons does not intersect with a relevant layer"
            )


def save_geometry(instance, request, viewset):
    logger.debug("saving geometry")
    # geometry
    proposal_geometry_str = request.data.get("proposal_geometry")
    # geometry_list = []
    proposal_geometry = json.loads(proposal_geometry_str)
    lands_geos_data = get_dbca_lands_and_waters_geos()
    e4283 = SpatialReference("EPSG:4283")  # EPSG string
    polygons_to_delete = list(instance.proposalgeometry.all())
    for feature in proposal_geometry.get("features"):
        polygon = None
        intersects = False
        if feature.get("geometry").get("type") == "Polygon":
            feature_dict = feature.get("geometry")
            geos_repr = GEOSGeometry(f"{feature_dict}")
            geos_repr_transform = geos_repr.transform(e4283, clone=True)
            for geom in lands_geos_data:
                if geom.intersects(geos_repr_transform):
                    intersects = True
                    break
            linear_ring = LinearRing(feature_dict.get("coordinates")[0])
            polygon = Polygon(linear_ring)
        if proposal_geometry and feature.get("id"):
            proposalgeometry = ProposalGeometry.objects.get(id=feature.get("id"))
            polygons_to_delete.remove(proposalgeometry)
            serializer = ProposalGeometrySaveSerializer(
                proposalgeometry,
                data={
                    "proposal_id": instance.id,
                    "polygon": polygon,
                    "intersects": intersects,
                },
                context={
                    "action": viewset.action,
                },
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        elif proposal_geometry:
            print("new polygon")
            serializer = ProposalGeometrySaveSerializer(
                data={
                    "proposal_id": instance.id,
                    "polygon": polygon,
                    "intersects": intersects,
                },
                context={
                    "action": viewset.action,
                },
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
    print("polygons_to_delete")
    print(polygons_to_delete)
    # delete polygons not returned from the front end
    [poly.delete() for poly in polygons_to_delete]


def proposal_submit(proposal, request):
    # import ipdb; ipdb.set_trace()
    with transaction.atomic():
        if proposal.can_user_edit:
            proposal.submitter = request.user.id
            proposal.lodgement_date = timezone.now()
            proposal.training_completed = True
            reasons = []
            if proposal.amendment_requests:
                qs = proposal.amendment_requests.filter(status="requested")
                if qs:
                    for q in qs:
                        if q.reason is not None:
                            reasons.append(q.reason)
                        q.status = "amended"
                        q.save()

            # Create a log entry for the proposal
            proposal.log_user_action(
                ProposalUserAction.ACTION_LODGE_APPLICATION.format(proposal.id), request
            )
            # Create a log entry for the organisation
            # proposal.applicant.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(proposal.id),request)
            # applicant_field = getattr(proposal, proposal.applicant_field)
            # 20220128 Ledger to handle EmailUser logging?
            # applicant_field.log_user_action(ProposalUserAction.ACTION_LODGE_APPLICATION.format(proposal.id),request)
            # 20220128 - update ProposalAssessorGroup, ProposalApproverGroup as SystemGroups
            ret1 = send_submit_email_notification(request, proposal)
            ret2 = send_external_submit_email_notification(request, proposal)

            # proposal.save_form_tabs(request)
            if ret1 and ret2:
                proposal.processing_status = "with_assessor"
                # TODO: do we need the following 2?
                # proposal.documents.all().update(can_delete=False)
                # proposal.required_documents.all().update(can_delete=False)

                # Reason can be an object, e.g. `AmendmentReason`
                reasons = [r.reason if hasattr(r, "reason") else r for r in reasons]
                reason = ",".join(reasons)
                proposal.save(
                    version_comment=f"Requested proposal amendments done {reason}"
                )
            else:
                raise ValidationError(
                    "An error occurred while submitting proposal (Submit email notifications failed)"
                )
            # Create assessor checklist with the current assessor_list type questions
            # Assessment instance already exits then skip.
            # TODO: fix ProposalAssessment if still required
            proposal.make_questions_ready()
            # try:
            #    assessor_assessment=ProposalAssessment.objects.get
            # (proposal=proposal,referral_group=None, referral_assessment=False)
            # except ProposalAssessment.DoesNotExist:
            #    assessor_assessment=ProposalAssessment.objects.create
            # (proposal=proposal,referral_group=None, referral_assessment=False)
            #    checklist=ChecklistQuestion.objects.filter
            # (list_type='assessor_list', application_type=proposal.application_type, obsolete=False)
            #    for chk in checklist:
            #        try:
            #            chk_instance=ProposalAssessmentAnswer.objects.get
            # (question=chk, assessment=assessor_assessment)
            #        except ProposalAssessmentAnswer.DoesNotExist:
            #            chk_instance=ProposalAssessmentAnswer.objects.create
            # (question=chk, assessment=assessor_assessment)

            return proposal

        else:
            raise ValidationError("You can't edit this proposal at this moment")


def is_payment_officer(user):
    from leaseslicensing.components.proposals.models import PaymentOfficerGroup

    try:
        group = PaymentOfficerGroup.objects.get(default=True)
    except PaymentOfficerGroup.DoesNotExist:
        group = None
    if group:
        if user in group.members.all():
            return True
    return False


def test_proposal_emails(request):
    """Script to test all emails (listed below) from the models"""
    # setup
    if not (settings.PRODUCTION_EMAIL):
        recipients = [request.user.email]
        approval = Approval.objects.filter(migrated=False).last()
        proposal = Approval.current_proposal
        # These don't make sense, just uncommenting to get the code passing flake8
        referral = Referral.objects.last()
        amendment_request = AmendmentRequest.objects.last()
        reason = "Not enough information"
        proposal_decline = ProposalDeclinedDetails.objects.last()
        compliance = Compliance.objects.last()

        application_fee = ApplicationFee.objects.last()
        api = Invoice.objects.get(
            reference=application_fee.application_fee_invoices.last().invoice_reference
        )

        booking = Booking.objects.last()
        bi = Invoice.objects.get(reference=booking.invoices.last().invoice_reference)

        proposal_email.send_qaofficer_email_notification(
            proposal, recipients, request, reminder=False
        )
        proposal_email.send_qaofficer_complete_email_notification(
            proposal, recipients, request, reminder=False
        )
        proposal_email.send_referral_email_notification(
            referral, recipients, request, reminder=False
        )
        proposal_email.send_referral_complete_email_notification(referral, request)
        proposal_email.send_amendment_email_notification(
            amendment_request, request, proposal
        )
        proposal_email.send_submit_email_notification(request, proposal)
        proposal_email.send_external_submit_email_notification(request, proposal)
        proposal_email.send_approver_decline_email_notification(
            reason, request, proposal
        )
        proposal_email.send_approver_approve_email_notification(request, proposal)
        proposal_email.send_proposal_decline_email_notification(
            proposal, request, proposal_decline
        )
        proposal_email.send_proposal_approver_sendback_email_notification(
            request, proposal
        )
        proposal_email.send_proposal_approval_email_notification(proposal, request)

        approval_email.send_approval_expire_email_notification(approval)
        approval_email.send_approval_cancel_email_notification(approval)
        approval_email.send_approval_suspend_email_notification(approval, request)
        approval_email.send_approval_surrender_email_notification(approval, request)
        approval_email.send_approval_renewal_email_notification(approval)
        approval_email.send_approval_reinstate_email_notification(approval, request)

        compliance_email.send_amendment_email_notification(
            amendment_request, request, compliance, is_test=True
        )
        compliance_email.send_reminder_email_notification(compliance, is_test=True)
        compliance_email.send_internal_reminder_email_notification(
            compliance, is_test=True
        )
        compliance_email.send_due_email_notification(compliance, is_test=True)
        compliance_email.send_internal_due_email_notification(compliance, is_test=True)
        compliance_email.send_compliance_accept_email_notification(
            compliance, request, is_test=True
        )
        compliance_email.send_external_submit_email_notification(
            request, compliance, is_test=True
        )
        compliance_email.send_submit_email_notification(
            request, compliance, is_test=True
        )

        booking_email.send_application_fee_invoice_tclass_email_notification(
            request, proposal, api, recipients, is_test=True
        )
        booking_email.send_application_fee_confirmation_tclass_email_notification(
            request, application_fee, api, recipients, is_test=True
        )
        booking_email.send_invoice_tclass_email_notification(
            request.user, booking, bi, recipients, is_test=True
        )
        booking_email.send_confirmation_tclass_email_notification(
            request.user, booking, bi, recipients, is_test=True
        )


def save_site_name(instance, site_name):
    if site_name:
        site_name, created = SiteName.objects.get_or_create(name=site_name)
        instance.site_name = site_name


def save_groups_data(instance, groups_data):
    logger.debug("groups_data = " + str(groups_data))
    if groups_data and len(groups_data) > 0:
        group_ids = []
        for group_data in groups_data:
            logger.debug("group_data: %s", group_data)
            group = group_data["group"]
            ProposalGroup.objects.get_or_create(proposal=instance, group_id=group["id"])
            group_ids.append(group["id"])
        ProposalGroup.objects.filter(proposal=instance).exclude(
            group_id__in=group_ids
        ).delete()


def populate_gis_data(proposal):
    logger.debug("Populating GIS data for Proposal: " + proposal.lodgement_number)
    populate_gis_data_lands_and_waters(proposal)  # Covers Acts, Tenures, Categories
    populate_gis_data_regions(proposal)
    populate_gis_data_districts(proposal)
    populate_gis_data_lgas(proposal)
    logger.debug("Finished GIS data for Proposal: " + proposal.lodgement_number)


def populate_gis_data_lands_and_waters(proposal):
    properties = [
        "leg_tenure",
        "leg_act",
        "category",
    ]
    gis_data_lands_and_waters = get_gis_data_for_proposal(
        proposal, "public:dbca_legislated_lands_and_waters", properties
    )
    if gis_data_lands_and_waters is None:
        logger.warn(
            "No GIS Lands and waters data found for proposal %s",
            proposal.lodgement_number,
        )
        return

    logger.debug("gis_data_lands_and_waters = " + str(gis_data_lands_and_waters))

    if gis_data_lands_and_waters[properties[0]]:
        for tenure_name in gis_data_lands_and_waters[properties[0]]:
            tenure, created = Tenure.objects.get_or_create(name=tenure_name)
            if created:
                logger.info(f"New Tenure created from GIS Data: {tenure}")
            ProposalTenure.objects.get_or_create(proposal=proposal, tenure=tenure)

    if gis_data_lands_and_waters[properties[1]]:
        for act_name in gis_data_lands_and_waters[properties[1]]:
            act, created = Act.objects.get_or_create(name=act_name)
            if created:
                logger.info(f"New Act created from GIS Data: {act}")
            ProposalAct.objects.get_or_create(proposal=proposal, act=act)

    if gis_data_lands_and_waters[properties[2]]:
        for category_name in gis_data_lands_and_waters[properties[2]]:
            category, created = Category.objects.get_or_create(name=category_name)
            if created:
                logger.info(f"New Category created from GIS Data: {category}")
            ProposalCategory.objects.get_or_create(proposal=proposal, category=category)


def populate_gis_data_regions(proposal):
    properties = [
        "drg_region_name",
    ]
    gis_data_regions = get_gis_data_for_proposal(
        proposal, "cddp:dbca_regions", properties
    )
    if gis_data_regions is None:
        logger.warn(
            "No GIS Region data found for proposal %s", proposal.lodgement_number
        )
        return

    logger.debug("gis_data_regions = " + str(gis_data_regions))

    if gis_data_regions[properties[0]]:
        for region_name in gis_data_regions[properties[0]]:
            region, created = Region.objects.get_or_create(name=region_name)
            if created:
                logger.info(f"New Region created from GIS Data: {region}")
            ProposalRegion.objects.get_or_create(proposal=proposal, region=region)


def populate_gis_data_districts(proposal):
    properties = [
        "district",
    ]
    gis_data_districts = get_gis_data_for_proposal(
        proposal, "cddp:dbca_districts", properties
    )
    if gis_data_districts is None:
        logger.warn(
            "No GIS District data found for proposal %s", proposal.lodgement_number
        )
        return

    logger.debug("gis_data_districts = " + str(gis_data_districts))

    if gis_data_districts[properties[0]]:
        for district_name in gis_data_districts[properties[0]]:
            district, created = District.objects.get_or_create(name=district_name)
            if created:
                logger.info(f"New Region created from GIS Data: {district}")
            ProposalDistrict.objects.get_or_create(proposal=proposal, district=district)


def populate_gis_data_lgas(proposal):
    properties = [
        "lga_label",
    ]
    gis_data_lgas = get_gis_data_for_proposal(
        proposal, "cddp:local_gov_authority", properties
    )
    if gis_data_lgas is None:
        logger.warn("No GIS LGA data found for proposal %s", proposal.lodgement_number)
        return

    logger.debug("gis_data_lgas = " + str(gis_data_lgas))

    if gis_data_lgas[properties[0]]:
        for lga_name in gis_data_lgas[properties[0]]:
            lga, created = LGA.objects.get_or_create(name=lga_name)
            if created:
                logger.info(f"New LGA created from GIS Data: {lga}")
            ProposalLGA.objects.get_or_create(proposal=proposal, lga=lga)
