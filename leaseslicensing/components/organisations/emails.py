import logging

# from django.core.urlresolvers import reverse
from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.utils.encoding import smart_str

from leaseslicensing.components.emails.emails import TemplateEmailBase
from leaseslicensing.helpers import convert_external_url_to_internal_url
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger(__name__)

SYSTEM_NAME = settings.SYSTEM_NAME_SHORT + " Automated Message"


class OrganisationRequestAcceptNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "Your organisation request has been accepted."
        self.html_template = (
            "leaseslicensing/emails/organisation_request_accept_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_request_accept_notification.txt"
        )


class OrganisationAccessGroupRequestAcceptNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "New organisation request has been submitted."
        self.html_template = (
            "leaseslicensing/emails/org_access_group_request_accept_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/org_access_group_request_accept_notification.txt"
        )


class OrganisationRequestNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "An organisation request has been submitted for approval"
        self.html_template = (
            "leaseslicensing/emails/organisation_request_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_request_notification.txt"
        )


class OrganisationRequestDeclineNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "Your organisation request has been declined."
        self.html_template = (
            "leaseslicensing/emails/organisation_request_decline_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_request_decline_notification.txt"
        )


class OrganisationLinkNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = f"{settings.DEP_NAME} - Confirmation - Account linked."
        self.html_template = (
            "leaseslicensing/emails/organisation_link_notification.html"
        )
        self.txt_template = "leaseslicensing/emails/organisation_link_notification.txt"


class OrganisationUnlinkNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "You have been unlinked from an organisation."
        self.html_template = (
            "leaseslicensing/emails/organisation_unlink_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_unlink_notification.txt"
        )


class OrganisationContactAdminUserNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "You have been linked as Company Admin Role."
        self.html_template = (
            "leaseslicensing/emails/organisation_contact_admin_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_contact_admin_notification.txt"
        )


class OrganisationContactUserNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "You have been linked as Company User Role."
        self.html_template = (
            "leaseslicensing/emails/organisation_contact_user_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_contact_user_notification.txt"
        )


class OrganisationContactSuspendNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "You have been suspended as Company User."
        self.html_template = (
            "leaseslicensing/emails/organisation_contact_suspend_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_contact_suspend_notification.txt"
        )


class OrganisationContactReinstateNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "You have been Reinstated as Company User."
        self.html_template = (
            "leaseslicensing/emails/organisation_contact_reinstate_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_contact_reinstate_notification.txt"
        )


class OrganisationContactDeclineNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "Your organisation link request has been declined."
        self.html_template = (
            "leaseslicensing/emails/organisation_contact_decline_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_contact_decline_notification.txt"
        )


class OrganisationAddressUpdatedNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "An organisation" "s address has been updated"
        self.html_template = (
            "leaseslicensing/emails/organisation_address_updated_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_address_updated_notification.txt"
        )


class OrganisationIdUploadNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "An organisation" "s identification has been uploaded"
        self.html_template = (
            "leaseslicensing/emails/organisation_id_upload_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_id_upload_notification.txt"
        )


class OrganisationRequestLinkNotificationEmail(TemplateEmailBase):
    def __init__(self):
        super().__init__()
        self.subject = "An organisation request to be linked has been sent for approval"
        self.html_template = (
            "leaseslicensing/emails/organisation_request_link_notification.html"
        )
        self.txt_template = (
            "leaseslicensing/emails/organisation_request_link_notification.txt"
        )


def send_organisation_id_upload_email_notification(
    emails, organisation, org_contact, request
):
    email = OrganisationIdUploadNotificationEmail()

    context = {"organisation": organisation}

    msg = email.send(emails, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, org_contact, sender=sender)


def send_organisation_request_link_email_notification(org_request, request, contact):
    email = OrganisationRequestLinkNotificationEmail()

    url = request.build_absolute_uri(f"/external/organisations/manage/{org_request.id}")

    context = {
        "request": org_request,
        "url": url,
    }

    msg = email.send(contact, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, org_request, request.user.id, sender=sender)


def send_organisation_reinstate_email_notification(
    linked_user, linked_by, organisation, request
):
    email = OrganisationContactReinstateNotificationEmail()

    context = {
        "user": linked_user,
        "linked_by": linked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]

    msg = email.send(linked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, linked_user.id, sender=sender)


def send_organisation_contact_suspend_email_notification(
    linked_user, linked_by, organisation, request
):
    email = OrganisationContactSuspendNotificationEmail()

    context = {
        "user": linked_user,
        "linked_by": linked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]
    msg = email.send(linked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, linked_user.id, sender=sender)


def send_organisation_contact_decline_email_notification(
    user_contact, deleted_by, organisation, request
):
    email = OrganisationContactDeclineNotificationEmail()

    context = {
        "user": user_contact,
        "linked_by": deleted_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]
    msg = email.send(user_contact.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, user_contact.id, sender=sender)


def send_organisation_contact_user_email_notification(
    linked_user, linked_by, organisation, request
):
    email = OrganisationContactUserNotificationEmail()

    context = {
        "user": linked_user,
        "linked_by": linked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]
    msg = email.send(linked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, linked_user.id, sender=sender)


def send_organisation_contact_adminuser_email_notification(
    linked_user, linked_by, organisation, request
):
    email = OrganisationContactAdminUserNotificationEmail()

    context = {
        "user": linked_user,
        "linked_by": linked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]
    msg = email.send(linked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, linked_user.id, sender=sender)


def send_organisation_link_email_notification(
    linked_user, linked_by, organisation, request
):
    email = OrganisationLinkNotificationEmail()

    context = {
        "user": linked_user,
        "linked_by": linked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]

    msg = email.send(linked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, linked_user.id, sender=sender)


def send_organisation_request_email_notification(org_request, request, contact):
    email = OrganisationRequestNotificationEmail()

    url = request.build_absolute_uri(f"/internal/organisations/access/{org_request.id}")

    url = convert_external_url_to_internal_url(url)

    context = {
        "request": request.data,
        "url": url,
    }
    msg = email.send(contact, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_request_email(msg, org_request, sender=sender)


def send_organisation_unlink_email_notification(
    unlinked_user, unlinked_by, organisation, request
):
    email = OrganisationUnlinkNotificationEmail()

    context = {
        "user": unlinked_user,
        "unlinked_by": unlinked_by,
        "organisation": organisation,
    }
    all_ccs = []
    if organisation.email:
        cc_list = organisation.email
        if cc_list:
            all_ccs = [cc_list]

    msg = email.send(unlinked_user.email, cc=all_ccs, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_email(msg, organisation, unlinked_user.id, sender=sender)


def send_organisation_request_accept_email_notification(
    org_request, organisation, requester, request
):
    email = OrganisationRequestAcceptNotificationEmail()

    context = {"request": org_request}

    msg = email.send(requester.email, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_request_email(msg, org_request, sender=sender)
    _log_org_email(msg, organisation, org_request.requester, sender=sender)


def send_org_access_group_request_accept_email_notification(
    org_request, request, recipient_list
):
    email = OrganisationAccessGroupRequestAcceptNotificationEmail()

    url = request.build_absolute_uri(f"/internal/organisations/access/{org_request.id}")

    url = convert_external_url_to_internal_url(url)

    context = {
        "name": request.data.get("name"),
        "abn": request.data.get("abn"),
        "url": url,
    }

    msg = email.send(recipient_list, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_request_email(msg, org_request, sender=sender)

    # commenting out because Organisation does not yet exist - only OrganisationRequest exists
    _log_org_email(msg, org_request.organisation, org_request.requester, sender=sender)


def send_organisation_request_decline_email_notification(org_request, request):
    email = OrganisationRequestDeclineNotificationEmail()
    context = {"request": org_request}
    requester_email_user = retrieve_email_user(org_request.requester)
    msg = email.send(requester_email_user.email, context=context)
    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    _log_org_request_email(msg, org_request, sender=sender)


def send_organisation_address_updated_email_notification(
    address_updated_by, ledger_organisation, wc_organisation, request
):
    from leaseslicensing.components.organisations.models import OrganisationContact

    email = OrganisationAddressUpdatedNotificationEmail()

    context = {
        "address_updated_by": address_updated_by,
        "organisation": ledger_organisation,
    }

    for org_contact in OrganisationContact.objects.filter(
        user_role="organisation_admin", organisation=wc_organisation
    ):
        msg = email.send(org_contact.email, context=context)
        sender = request.user if request else settings.DEFAULT_FROM_EMAIL
        # Log customer as None for now since OrganisationContact does not have a
        # ledger email user integer field yet
        _log_org_email(msg, wc_organisation, customer=None, sender=sender)


def _log_org_request_email(email_message, organisation_request, sender=None):
    from leaseslicensing.components.organisations.models import (
        OrganisationRequestLogEntry,
    )

    if isinstance(
        email_message,
        (
            EmailMultiAlternatives,
            EmailMessage,
        ),
    ):
        # Note: This will log the plain text body (not the html body)
        text = email_message.body
        subject = email_message.subject
        fromm = smart_str(sender) if sender else smart_str(email_message.from_email)
        # the to email is normally a list
        if isinstance(email_message.to, list):
            to = ",".join(email_message.to)
        else:
            to = smart_str(email_message.to)
        # we log the cc and bcc in the same cc field of the log entry as a ',' comma separated string
        all_ccs = []
        if email_message.cc:
            all_ccs += list(email_message.cc)
        if email_message.bcc:
            all_ccs += list(email_message.bcc)
        all_ccs = ",".join(all_ccs)

    else:
        requester_email_user = retrieve_email_user(organisation_request.requester)
        text = smart_str(email_message)
        subject = ""
        to = requester_email_user.email
        fromm = smart_str(sender) if sender else SYSTEM_NAME
        all_ccs = ""

    customer = organisation_request.requester

    staff = sender.id

    kwargs = {
        "subject": subject,
        "text": text,
        "request": organisation_request,
        "customer": customer,
        "staff": staff,
        "to": to,
        "fromm": fromm,
        "cc": all_ccs,
    }

    email_entry = OrganisationRequestLogEntry.objects.create(**kwargs)

    return email_entry


def _log_org_email(email_message, organisation, customer, sender=None):
    from leaseslicensing.components.organisations.models import OrganisationLogEntry

    if isinstance(
        email_message,
        (
            EmailMultiAlternatives,
            EmailMessage,
        ),
    ):
        # Note: This will log the plain text body (not the html body)
        text = email_message.body
        subject = email_message.subject
        fromm = smart_str(sender) if sender else smart_str(email_message.from_email)
        # the to email is normally a list
        if isinstance(email_message.to, list):
            to = ",".join(email_message.to)
        else:
            to = smart_str(email_message.to)
        # we log the cc and bcc in the same cc field of the log entry as a ',' comma separated string
        all_ccs = []
        if email_message.cc:
            all_ccs += list(email_message.cc)
        if email_message.bcc:
            all_ccs += list(email_message.bcc)
        all_ccs = ",".join(all_ccs)

    else:
        email_user = retrieve_email_user(customer)
        text = smart_str(email_message)
        subject = ""
        to = email_user.email
        fromm = smart_str(sender) if sender else SYSTEM_NAME
        all_ccs = ""

    staff = sender.id

    kwargs = {
        "subject": subject,
        "text": text,
        "organisation": organisation,
        "customer": customer,
        "staff": staff,
        "to": to,
        "fromm": fromm,
        "cc": all_ccs,
    }
    logger.debug(f"kwargs: {kwargs}")
    logger.debug(f"type(organisation): {type(organisation)}")

    email_entry = OrganisationLogEntry.objects.create(**kwargs)

    return email_entry
