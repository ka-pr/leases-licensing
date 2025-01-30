import logging
import re
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from ledger_api_client.managed_models import SystemGroup, SystemGroupPermission
from rest_framework.serializers import ValidationError

logger = logging.getLogger(__name__)


def today():
    return timezone.localtime(timezone.now()).date()


def gst_from_total(total_inc_gst):
    # Only to be used for totals that include gst
    # as will not return 0.00 for totals that do not include gst
    gst_rate = Decimal(settings.LEDGER_GST).quantize(Decimal("0.01"))
    gst = gst_rate / (100 + gst_rate) * total_inc_gst
    return Decimal(gst).quantize(Decimal("0.01"))


def user_ids_in_group(group_name):
    try:
        system_group = SystemGroup.objects.get(name=group_name)
        return system_group.get_system_group_member_ids()
    except SystemGroup.DoesNotExist:
        logger.warning(f"SystemGroup {group_name} does not exist.")
        return []


def belongs_to_by_user_id(user_id, group_name):
    system_group = SystemGroup.objects.filter(name=group_name).first()
    return system_group and user_id in system_group.get_system_group_member_ids()


def emails_list_for_group(group_name):
    sgp = SystemGroupPermission.objects.filter(system_group__name=group_name).only(
        "emailuser"
    )
    return [sgp.emailuser.email for sgp in sgp]


def belongs_to(request, group_name):
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True

    return belongs_to_by_user_id(request.user.id, group_name)


def is_competitive_process_editor(request):
    return belongs_to(request, settings.GROUP_COMPETITIVE_PROCESS_EDITOR)


def is_leaseslicensing_admin(request):
    return belongs_to(request, settings.ADMIN_GROUP)


def is_assessor(request):
    return belongs_to(request, settings.GROUP_NAME_ASSESSOR)


def is_approver(request):
    return belongs_to(request, settings.GROUP_NAME_APPROVER)


def is_finance_officer(request):
    return belongs_to(request, settings.GROUP_FINANCE)


def is_organisation_access_officer(request):
    return belongs_to(request, settings.GROUP_NAME_ORGANISATION_ACCESS)


def is_referee(request, proposal=None):
    from leaseslicensing.components.proposals.models import Referral

    qs = Referral.objects.filter(referral=request.user.id)
    if proposal:
        qs = qs.filter(proposal=proposal)

    return qs.exists()


def is_compliance_referee(request, compliance=None):
    from leaseslicensing.components.compliances.models import ComplianceReferral

    qs = ComplianceReferral.objects.filter(referral=request.user.id)
    if compliance:
        qs = qs.filter(compliance=compliance)

    return qs.exists()


def in_dbca_domain(request):
    return request.user.is_staff


def is_in_organisation_contacts(request, organisation):
    return request.user.email in organisation.contacts.all().values_list(
        "email", flat=True
    )


def is_department_user(request):
    return request.user.is_authenticated and request.user.is_staff


def is_customer(request):
    return request.user.is_authenticated and not request.user.is_staff


def is_internal(request):
    return is_department_user(request)


def convert_external_url_to_internal_url(url):
    if settings.SITE_SUBDOMAIN_INTERNAL_SUFFIX not in url:
        # Add the internal subdomain suffix to the url
        url = f"{settings.SITE_SUBDOMAIN_INTERNAL_SUFFIX}.{settings.SITE_DOMAIN}".join(
            url.split("." + settings.SITE_DOMAIN)
        )
    return url


def convert_internal_url_to_external_url(url):
    if settings.SITE_SUBDOMAIN_INTERNAL_SUFFIX in url:
        # remove '-internal'. This email is for external submitters
        url = "".join(url.split(settings.SITE_SUBDOMAIN_INTERNAL_SUFFIX))
    return url


def get_instance_identifier(instance):
    """Checks the instance for the attributes specified in settings"""
    for field in settings.ACTION_LOGGING_IDENTIFIER_FIELDS:
        if hasattr(instance, field):
            return getattr(instance, field)
    raise AttributeError(
        f"Model instance has no valid identifier to use for logging. Tried: {settings.ACTION_LOGGING_IDENTIFIER_FIELDS}"
    )


def get_lodgement_number_prefixes():
    """Returns the prefixes of the models that have a lodgement_number field"""
    cache_key = settings.CACHE_KEY_LODGEMENT_NUMBER_PREFIXES
    prefixes = cache.get(cache_key)
    if prefixes is None:
        leaseslicensing = apps.get_app_config("leaseslicensing")
        prefixes = {}
        for model_string in leaseslicensing.models:
            model = apps.get_model("leaseslicensing", model_string)
            if (hasattr(model, "lodgement_number")) and (
                hasattr(model, "MODEL_PREFIX")
            ):
                prefixes[model.MODEL_PREFIX] = model
        cache.set(cache_key, prefixes, settings.CACHE_TIMEOUT_2_HOURS)
    return prefixes


def get_model_by_lodgement_number_prefix(prefix):
    """Returns the model class for the prefix"""
    return get_lodgement_number_prefixes()[prefix]


def get_model_by_lodgement_number(lodgement_number):
    """Returns the model class for the lodgement number"""
    lodgment_number = re.search("([A-Z]+)([0-9]+)", lodgement_number or "")
    if not lodgment_number:
        # Returning a ValidationError here, so the response text can be evaluated in the
        # Ajax error handler of the respective datatable.
        raise ValidationError(
            "A valid lodgement number starts with one or more capital letters followed by a series of digits."
        )

    lodgement_number_prefix = lodgment_number.group(1)
    return get_model_by_lodgement_number_prefix(lodgement_number_prefix)
