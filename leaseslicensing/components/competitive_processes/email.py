import logging
import re

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.urls import reverse
from django.utils.encoding import smart_str
from ledger_api_client.managed_models import SystemGroup

from leaseslicensing.components.emails.emails import TemplateEmailBase
from leaseslicensing.components.organisations.utils import (
    get_admin_emails_for_organisation,
)
from leaseslicensing.helpers import convert_internal_url_to_external_url
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger(__name__)


def send_winner_notification(request, competitive_process, lease_licence):
    email = TemplateEmailBase(
        subject="Winning, Leases and Licence proposal is ready",
        html_template="leaseslicensing/emails/competitive_processes/send_winner_notification.html",
        txt_template="leaseslicensing/emails/competitive_processes/send_winner_notification.txt",
    )

    url = request.build_absolute_uri(
        reverse("external-proposal-detail", kwargs={"proposal_pk": lease_licence.id})
    )
    url = convert_internal_url_to_external_url(url)

    context = {"competitive_process": competitive_process, "url": url}

    winner = competitive_process.winner
    if winner.is_person:
        id = winner.person.id
        to_email = retrieve_email_user(id).email
    else:
        to_email = get_admin_emails_for_organisation(winner.organisation.id)

    msg = email.send(to_email, context=context)

    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    log_competitive_process = _log_competitive_process_email(
        msg, competitive_process, sender=sender
    )

    return log_competitive_process


def send_competitive_process_create_notification(
    request, competitive_process, **kwargs
):
    """
    Sends email notification to the assessor group for Competitive Process
    (`GROUP_COMPETITIVE_PROCESS_EDITOR`)
    """

    details = kwargs.get("details", {})
    cc_email_str = details.get("cc_email", None)
    bcc_email_str = details.get("bcc_email", None)
    cc_emails = re.split(r"[\s,;]+", cc_email_str) if cc_email_str else []
    bcc_emails = re.split(r"[\s,;]+", bcc_email_str) if bcc_email_str else []

    email = TemplateEmailBase(
        subject="New Competitive Process ready for processing.",
        html_template="leaseslicensing/emails/competitive_processes/competitive_process_create_notification.html",
        txt_template="leaseslicensing/emails/competitive_processes/competitive_process_create_notification.txt",
    )
    url = request.build_absolute_uri(
        reverse(
            "internal-competitiveprocess-detail", kwargs={"pk": competitive_process.id}
        )
    )
    context = {"competitive_process": competitive_process, "url": url}

    # Once a competitive process is created (either as result of a registration of interest proposal or from
    # the competitive process dashboard page) the assessor group for competitive processes will receive a
    # notification email from the system, including the link to access the competitive process.
    group = SystemGroup.objects.get(name=settings.GROUP_COMPETITIVE_PROCESS_EDITOR)
    ids = group.get_system_group_member_ids()
    logger.debug(f"Sending email to {ids}")
    email_user_emails = [retrieve_email_user(id).email for id in ids]
    msg = email.send(email_user_emails, cc=cc_emails, bcc=bcc_emails, context=context)

    sender = request.user if request else settings.DEFAULT_FROM_EMAIL
    log_competitive_process = _log_competitive_process_email(
        msg, competitive_process, sender=sender
    )

    return log_competitive_process


def _log_competitive_process_email(email_message, competitive_process, sender=None):
    from leaseslicensing.components.competitive_processes.models import (
        CompetitiveProcessLogEntry,
    )

    if isinstance(email_message, (EmailMultiAlternatives, EmailMessage)):
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
        text = smart_str(email_message)
        subject = ""
        if hasattr(competitive_process, "originating_proposal"):
            to = retrieve_email_user(
                competitive_process.originating_proposal.submitter
            ).email
        else:
            to = ""
        fromm = (
            smart_str(sender)
            if sender
            else f"{settings.SYSTEM_NAME} (Automated Message)"
        )
        all_ccs = ""

    staff = sender.id

    kwargs = {
        "subject": subject,
        "text": text,
        "competitive_process": competitive_process,
        "staff": staff,
        "to": to,
        "fromm": fromm,
        "cc": all_ccs,
    }

    email_entry = CompetitiveProcessLogEntry.objects.create(**kwargs)

    return email_entry
