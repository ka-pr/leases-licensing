"""
When making invoicing calculations in the leases and licensing system there are 2 main time perdiods
that may be useful depending on the type of charge method being used.

1. Financial year - 1st July to 30th June (financial quarters are also important)
2. Sequential year - 12 months from the start date of the approval
(i.e. If an approval starts on the 5th of April 2030 the last day of that sequential year is the 4th of April 2031)

"""

import calendar
import datetime
import logging
import math

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone
from ledger_api_client import utils as ledger_api_client_utils
from rest_framework import serializers

from leaseslicensing.components.invoicing.email import (
    send_new_invoice_raised_notification,
)
from leaseslicensing.components.invoicing.models import (
    Invoice,
    InvoicingDetails,
    PercentageOfGrossTurnover,
)
from leaseslicensing.components.organisations.models import (
    Organisation,
    OrganisationContact,
)
from leaseslicensing.helpers import gst_from_total
from leaseslicensing.ledger_api_utils import retrieve_email_user

logger = logging.getLogger(__name__)


def years_elapsed_since(date):
    return relativedelta(timezone.now(), date).years


def financial_quarter_label(quarter):
    return ["JUL-SEP", "OCT-DEC", "JAN-MAR", "APR-JUN"][quarter - 1]


def financial_quarter_from_date(date):
    month = date.month
    if month in [1, 2, 3]:
        return 3
    elif month in [4, 5, 6]:
        return 4
    elif month in [7, 8, 9]:
        return 1
    elif month in [10, 11, 12]:
        return 2


def month_from_quarter(quarter):
    """Returns the month number for the start of the financial quarter provided (0 indexed)"""
    return [10, 1, 4, 7][quarter - 1]


def month_from_cpi_quarter(quarter: int) -> int:
    """CPI quarters work differently to financial quarters.
    1 = 3 (MAR Quarter), 2 = 6 (JUN Quarter), 3 = 9 (SEP Quarter), 4 = 12 (DEC Quarter)
    """
    return quarter * 3


def month_string_from_date(date):
    return month_string_from_month(date.month)


def month_string_from_month(month):
    return [
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    ][month - 1]


def financial_year_from_date(date):
    if not isinstance(date, datetime.date):
        raise ValueError("Date must be a datetime.date object")
    month = date.month
    year = date.year
    if month < 7:
        return f"{year - 1}-{year}"
    else:
        return f"{year}-{year + 1}"


def ordinal_suffix_of(i):
    j = i % 10
    k = i % 100
    if j == 1 and k != 11:
        return "%sst" % i
    if j == 2 and k != 12:
        return "%snd" % i
    if j == 3 and k != 13:
        return "%srd" % i
    return "%sth" % i


def sequential_year_has_passed(date):
    return date.year < timezone.now().year


def financial_year_has_passed(financial_year):
    """Takes a financial year in the format '2030-2031'
    and returns True if the current date is after the end of that financial year"""
    end_of_financial_year = timezone.datetime.strptime(
        financial_year.split("-")[1], "%Y"
    ).replace(month=6, day=30, tzinfo=timezone.utc)
    return timezone.now() > end_of_financial_year


def financial_quarter_has_passed(financial_quarter):
    """Takes a financial quarter in the format (3, 'Q3', 2021, '2020-2021')
    and returns True if the current date is after the end of that financial quarter"""
    start_month = month_from_quarter(financial_quarter[0] - 1)
    end_month = start_month + 2
    calendar_year = financial_quarter[2]
    start_of_financial_quarter = timezone.datetime.strptime(
        f"{calendar_year}-{start_month}-1", "%Y-%m-%d"
    ).replace(tzinfo=timezone.utc)
    end_of_financial_quarter = start_of_financial_quarter + relativedelta(months=2)
    end_of_financial_quarter = end_of_financial_quarter.replace(
        day=calendar.monthrange(calendar_year, end_month)[1]
    ).date()
    return timezone.now().date() > end_of_financial_quarter


def financial_years_included_in_range(start_date, end_date):
    """Returns a list of financial years included in the date range provided.
    The date range is inclusive of the start date and end date.
    """
    financial_years = []
    for year in range(start_date.year, end_date.year + 1):
        financial_years.append(f"{year}-{year + 1}")
    return financial_years


def financial_quarters_included_in_range(start_date, end_date):
    """Returns a list of financial quarters included in the date range provided.
    The date range is inclusive of the start date and end date.
    """
    financial_quarters = []
    current_date = start_date

    while current_date <= end_date:
        if 7 <= current_date.month <= 9:
            quarter = 1
        elif 10 <= current_date.month <= 12:
            quarter = 2
        elif 1 <= current_date.month <= 3:
            quarter = 3
        else:
            quarter = 4
        financial_year = financial_year_from_date(current_date)
        financial_quarter = (
            quarter,
            f"Q{quarter}",
            current_date.year,
            financial_year,
        )
        if financial_quarter not in financial_quarters:
            financial_quarters.append(financial_quarter)
        current_date += relativedelta(months=1)

    return financial_quarters


def months_included_in_range(
    start_date: datetime.date,
    end_date: datetime.date,
) -> list:
    """Returns a list of months included in the date range provided.
    The date range is inclusive of the start date and end date.
    """

    if start_date >= end_date:
        logger.warning(
            f"Start date {start_date} is after end date {end_date} (or the same date)"
        )
        return []

    months = []
    for year in range(start_date.year, end_date.year + 1):
        for month_index in range(1, 13):
            logger.debug(f"Month index: {month_index}")
            try:
                compare_date_1 = datetime.date(year, month_index, start_date.day)
            except ValueError:
                compare_date_1 = datetime.date(
                    year, month_index, calendar.monthrange(year, month_index)[1]
                )
            try:
                compare_date_2 = datetime.date(year, month_index, end_date.day)
            except ValueError:
                compare_date_2 = datetime.date(
                    year, month_index, calendar.monthrange(year, month_index)[1]
                )

            if not compare_date_1 >= start_date or not compare_date_2 <= end_date:
                continue

            logger.debug(f"Month included: {month_index}")

            months.append(datetime.date(year, month_index, 1))
    return months


def end_of_next_financial_year(date):
    """Return the last day of the financial year after the date provided"""
    end_of_next_financial_year = (
        timezone.datetime.strptime(f"{date.year}", "%Y")
        .replace(month=6, day=30, tzinfo=timezone.utc)
        .date()
    )
    if not end_of_next_financial_year > date:
        return end_of_next_financial_year + relativedelta(years=1)
    return end_of_next_financial_year


def end_of_next_financial_quarter(date, start_month=3):
    """Return the last day of the financial quarter after the date provided"""
    quarters = quarters_from_start_month(start_month)

    for quarter in quarters:
        end_of_quarter = datetime.datetime(
            date.year,
            quarter,
            calendar.monthrange(date.year, quarter)[1],
            tzinfo=timezone.utc,
        ).date()
        if end_of_quarter > date:
            return end_of_quarter
    year = date.year + 1
    quarter = quarters[0]
    end_of_quarter = datetime.datetime(
        year, quarter, calendar.monthrange(year, quarter)[1]
    ).date()
    return end_of_quarter


def end_of_month(date):
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


def quarters_from_start_month(start_month):
    if start_month > 3:
        raise ValueError("Start month must be between 1 and 3")
    return [start_month + i * 3 for i in range(0, 4)]


def dates_overlap(datetime1, datetime2):
    return datetime1[0] <= datetime2[1] and datetime2[0] <= datetime1[1]


def years_in_date_range(start_date, end_date):
    # relative delta in years rounded up a year
    relative_delta = relativedelta(start_date, end_date)
    remainder = (
        relative_delta.days
        + relative_delta.hours
        + relative_delta.minutes
        + relative_delta.seconds
        + relative_delta.microseconds
    )
    if remainder > 0:
        return relative_delta.years + 1
    return relative_delta.years


def days_difference(start_date, end_date):
    return abs((start_date - end_date).days + 1)


def months_difference(start_date, end_date):
    relative_delta = relativedelta(start_date, end_date)
    return abs((relative_delta.years * 12) + relative_delta.months)


def quarters_difference(start_date, end_date):
    return abs(math.ceil(months_difference(start_date, end_date) / 3))


def years_difference(start_date, end_date):
    return abs(math.ceil(months_difference(start_date, end_date) / 12))


def period_contains_leap_year_day(start_date, end_date):
    """Returns True if the period contains the 29th of February"""
    if start_date.year != end_date.year:
        if start_date.year % 4 == 0 and (
            start_date.year % 100 != 0 or start_date.year % 400 == 0
        ):
            if start_date.month < 3 or (start_date.month == 2 and start_date.day == 29):
                return True
        if end_date.year % 4 == 0 and (
            end_date.year % 100 != 0 or end_date.year % 400 == 0
        ):
            if end_date.month > 2 or (end_date.month == 2 and end_date.day == 29):
                return True
        if end_date.year - start_date.year > 1:
            return True
    elif start_date.year % 4 == 0 and (
        start_date.year % 100 != 0 or start_date.year % 400 == 0
    ):
        if start_date.month < 3 or (start_date.month == 2 and start_date.day == 29):
            if end_date.month > 2 or (end_date.month == 2 and end_date.day == 29):
                return True
    return False


def get_oracle_code(invoice):
    """Returns the oracle code for the approval provided"""
    if not invoice:
        raise ValueError("Invoice must be provided")

    # If the invoice itself has an oracle code, use that
    if invoice.oracle_code:
        return invoice.oracle_code.code

    if not invoice.approval:
        raise ValueError(f"Invoice: {invoice} has no approval")

    if not invoice.approval.invoicing_details:
        raise ValueError(f"Approval: {invoice.approval} has no invoicing details")

    # If the approval has an oracle code, use that
    if invoice.approval.invoicing_details.oracle_code:
        return invoice.approval.invoicing_details.oracle_code

    raise serializers.ValidationError(f"No oracle code found for Invoice: {invoice}")


def generate_ledger_invoice(invoice: Invoice) -> None:
    """Takes a leases licensing invoice record and generates a future ledger invoice via api
    Then attaches the oracle invoice pdf to the ledger invoice."""
    logger.info(
        f"Creating future ledger invoice for Invoice: {invoice.lodgement_number}"
    )
    approval = invoice.approval

    description = f"{approval.approval_type} {approval.lodgement_number}: "
    if invoice.ad_hoc:
        description += invoice.description
    else:
        description += f"{approval.invoicing_details.charge_method}"

    price_incl_tax = invoice.amount
    price_excl_tax = invoice.amount

    if not approval.approval_type.gst_free:
        price_excl_tax = price_incl_tax - gst_from_total(price_incl_tax)

    if settings.DEBUG:
        # Convert to int for testing purposes as ledger expects only whole numbers
        price_incl_tax = int(price_incl_tax)
        price_excl_tax = int(price_excl_tax)

    ledger_order_lines = []

    oracle_code = get_oracle_code(invoice)

    ledger_order_lines.append(
        {
            "ledger_description": description,
            "quantity": 1,
            "price_excl_tax": str(price_excl_tax),
            "price_incl_tax": str(price_incl_tax),
            "oracle_code": oracle_code,
            "line_status": settings.LEDGER_DEFAULT_LINE_STATUS,
        },
    )
    logger.info(
        f"Setting ledger order lines {ledger_order_lines} for Invoice: {invoice.lodgement_number}"
    )

    # We need a fake request as we are adding the proponent as the request.user
    fake_request = ledger_api_client_utils.FakeRequestSessionObj()

    basket_params = {
        "products": ledger_order_lines,
        "vouchers": [],
        "system": settings.PAYMENT_SYSTEM_PREFIX,
        "tax_override": True,
        "custom_basket": True,
        "booking_reference": str(invoice.lodgement_number),
        "no_payment": True,
    }
    logger.info(
        f"Setting basket parameters: {basket_params} for Invoice: {invoice.lodgement_number}"
    )

    if isinstance(approval.applicant, Organisation):
        organisation = approval.applicant
        basket_params["organisation"] = organisation.ledger_organisation_id
        admin_contact = organisation.contacts.filter(
            user_role=OrganisationContact.USER_ROLE_CHOICE_ADMIN,
            user_status=OrganisationContact.USER_STATUS_CHOICE_ACTIVE,
        ).first()
        if not admin_contact:
            logger.error(
                f"Unable to retrieve admin contact for organisation: {organisation}"
            )
            return
        fake_request.user = retrieve_email_user(admin_contact.user)
    else:
        fake_request.user = retrieve_email_user(approval.applicant.emailuser_id)

    logger.info(
        f"Setting request user {fake_request.user} for Invoice Record: {invoice.lodgement_number}"
    )

    logger.info(
        f"Creating basket session for Invoice Record: {invoice.lodgement_number}"
    )
    basket_hash = ledger_api_client_utils.create_basket_session(
        fake_request, fake_request.user.id, basket_params
    )
    basket_hash = basket_hash.split("|")[0]
    invoice_text = f"Leases Licensing Invoice {invoice.lodgement_number}"
    if approval.current_proposal.proponent_reference_number:
        invoice_text += (
            f"(Proponent Ref: {approval.current_proposal.proponent_reference_number})"
        )
    return_preload_url = (
        f"{settings.LEASES_LICENSING_EXTERNAL_URL}"
        f"/api/invoicing/ledger-api-invoice-success-callback/{invoice.uuid}"
    )

    logger.info(
        f"Creating future invoice for Invoice Record: {invoice.lodgement_number}"
    )
    future_invoice = ledger_api_client_utils.process_create_future_invoice(
        basket_hash, invoice_text, return_preload_url
    )

    if 200 != future_invoice["status"]:
        logger.error(
            f"Failed to create future Invoice {invoice.lodgement_number} with basket_hash "
            f"{basket_hash}, invoice_text {invoice_text}, return_preload_url {return_preload_url}"
        )
        return

    data = future_invoice["data"]
    invoice.order_number = data["order"]
    invoice.basket_id = data["basket_id"]
    invoice.invoice_reference = data["invoice"]
    invoice.save()

    # Attach the oracle invoice to ledger invoice
    response = ledger_api_client_utils.update_ledger_oracle_invoice(
        invoice.invoice_reference,
        invoice.oracle_invoice_number,
        invoice.invoice_pdf.path,
    )

    if 200 != response["status"]:
        serializers.ValidationError(
            {
                "ledger api client error": [
                    f"Failed to attach oracle invoice to ledger invoice: { response['message'] }"
                ]
            }
        )

    # Send request for payment to proponent
    send_new_invoice_raised_notification(invoice)


def clone_invoicing_details(
    invoicing_details_to_clone: InvoicingDetails,
) -> InvoicingDetails:
    # Clone the top level object first
    invoicing_details = InvoicingDetails.objects.get(id=invoicing_details_to_clone.id)
    invoicing_details.pk = None
    invoicing_details.save()

    # Clear out prefetched items cache etc.
    invoicing_details.refresh_from_db()

    if not invoicing_details.charge_method:
        logger.warning(
            f"Invoicing details {invoicing_details} has no charge method so cannot clone. Returning original object."
        )
        return invoicing_details_to_clone

    charge_method_key = invoicing_details.charge_method.key

    if charge_method_key in [
        # There are no nested objects to clone for these charge methods
        settings.CHARGE_METHOD_ONCE_OFF_CHARGE,
        settings.CHARGE_METHOD_NO_RENT_OR_LICENCE_CHARGE,
        settings.CHARGE_METHOD_BASE_FEE_PLUS_ANNUAL_CPI_CUSTOM,
    ]:
        return invoicing_details

    if charge_method_key == settings.CHARGE_METHOD_BASE_FEE_PLUS_ANNUAL_CPI:
        for custom_cpi_year in invoicing_details_to_clone.custom_cpi_years.all():
            custom_cpi_year.pk = None
            custom_cpi_year.invoicing_details = invoicing_details
            custom_cpi_year.save()
        return invoicing_details

    if charge_method_key == settings.CHARGE_METHOD_BASE_FEE_PLUS_FIXED_ANNUAL_INCREMENT:
        for (
            annual_increment_amount
        ) in invoicing_details_to_clone.annual_increment_amounts.all():
            annual_increment_amount.pk = None
            annual_increment_amount.invoicing_details = invoicing_details
            annual_increment_amount.save()
        return invoicing_details

    if (
        charge_method_key
        == settings.CHARGE_METHOD_BASE_FEE_PLUS_FIXED_ANNUAL_PERCENTAGE
    ):
        for (
            annual_increment_percentage
        ) in invoicing_details_to_clone.annual_increment_percentages.all():
            annual_increment_percentage.pk = None
            annual_increment_percentage.invoicing_details = invoicing_details
            annual_increment_percentage.save()
        return invoicing_details

    # Clone gross turnover percentages for both arrears and advance GTO invoicing
    for (
        gross_turnover_percentage
    ) in invoicing_details_to_clone.gross_turnover_percentages.all():
        original_pk = gross_turnover_percentage.pk
        gross_turnover_percentage.pk = None
        gross_turnover_percentage.invoicing_details = invoicing_details
        gross_turnover_percentage.save()

        # For arrears GTO invoicing we need to clone the quarters or months
        if (
            charge_method_key
            == settings.CHARGE_METHOD_PERCENTAGE_OF_GROSS_TURNOVER_IN_ARREARS
        ):
            original_gross_turnover_percentage = PercentageOfGrossTurnover.objects.get(
                id=original_pk
            )

            if (
                invoicing_details.invoicing_repetition_type.key
                == settings.REPETITION_TYPE_QUARTERLY
            ):
                for quarter in original_gross_turnover_percentage.quarters.all():
                    quarter.pk = None
                    quarter.gross_turnover_percentage = gross_turnover_percentage
                    quarter.save()

            elif (
                invoicing_details.invoicing_repetition_type.key
                == settings.REPETITION_TYPE_MONTHLY
            ):
                for month in original_gross_turnover_percentage.months.all():
                    month.pk = None
                    month.gross_turnover_percentage = gross_turnover_percentage
                    month.save()

    return invoicing_details
