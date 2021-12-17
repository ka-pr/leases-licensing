import datetime
import logging
import pytz
import os

from django.contrib.auth.models import Group
from django.core.files import File

from leaseslicensing import settings
#from mooringlicensing.components.approvals.models import AgeGroup, AdmissionType
from leaseslicensing.components.main.models import (
        ApplicationType, 
        GlobalSettings, 
        #NumberOfDaysType,
        #NumberOfDaysSetting
        )
#from leaseslicensing.components.payments_ml.models import OracleCodeItem, FeeItemStickerReplacement
from leaseslicensing.components.proposals.models import (
        ProposalType, 
        Proposal, 
        #StickerPrintingContact
        )

logger = logging.getLogger(__name__)


class DefaultDataManager(object):

    def __init__(self):
        # Proposal Types
        for item in settings.PROPOSAL_TYPES:
            try:
                myType, created = ProposalType.objects.get_or_create(code=item[0])
                if created:
                    myType.description = item[1]
                    myType.save()
                    logger.info("Created ProposalType: {}".format(item[1]))
            except Exception as e:
                logger.error('{}, ProposalType: {}'.format(e, item[1]))


        # Application Types
        for item in settings.APPLICATION_TYPES:
            try:
                myType, created = ApplicationType.objects.get_or_create(name=item[0])
                if created:
                    #myType.description = item[1]
                    #myType.save()
                    logger.info("Created ApplicationType: {}".format(item[1]))
            except Exception as e:
                logger.error('{}, ApplicationType: {}'.format(e, item[1]))

        # Store
        for item in GlobalSettings.keys:
            try:
                obj, created = GlobalSettings.objects.get_or_create(key=item[0])
                if created:
                    if item[0] in GlobalSettings.keys_for_file:
                        with open(GlobalSettings.default_values[item[0]], 'rb') as doc_file:
                            obj._file.save(os.path.basename(GlobalSettings.default_values[item[0]]), File(doc_file), save=True)
                        obj.save()
                    else:
                        obj.value = item[1]
                        obj.save()
                    logger.info("Created {}: {}".format(item[0], item[1]))
            except Exception as e:
                logger.error('{}, Key: {}'.format(e, item[0]))

        ## Groups
        #for group_name in settings.CUSTOM_GROUPS:
        #    try:
        #        group, created = Group.objects.get_or_create(name=group_name)
        #        if created:
        #            logger.info("Created group: {}".format(group_name))
        #    except Exception as e:
        #        logger.error('{}, Group name: {}'.format(e, group_name))


        ## Oracle account codes
        #today = datetime.datetime.now(pytz.timezone(settings.TIME_ZONE)).date()
        #for application_type in ApplicationType.objects.all():
        #    if not application_type.oracle_code_items.count() > 0:
        #        try:
        #            oracle_code_item = OracleCodeItem.objects.create(
        #                application_type=application_type,
        #                date_of_enforcement=today,
        #            )
        #            logger.info("Created oracle code item: {}".format(oracle_code_item))
        #        except Exception as e:
        #            logger.error('{}, failed to create oracle code item'.format(application_type))

