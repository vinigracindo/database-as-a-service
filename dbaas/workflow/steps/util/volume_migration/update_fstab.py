# -*- coding: utf-8 -*-
import logging
from util import full_stack
from workflow.steps.util.base import BaseStep
from workflow.exceptions.error_codes import DBAAS_0022
from util import scape_nfsaas_export_path
from workflow.steps.util.restore_snapshot import update_fstab

LOG = logging.getLogger(__name__)


class UpdateFstab(BaseStep):

    def __unicode__(self):
        return "Updating volume information..."

    def do(self, workflow_dict):
        try:
            volume = workflow_dict['volume']
            host = workflow_dict['host']
            old_volume = workflow_dict['old_volume']

            source_export_path = scape_nfsaas_export_path(
                old_volume.nfsaas_path)
            target_export_path = scape_nfsaas_export_path(volume.nfsaas_path)
            return_code, output = update_fstab(host=host,
                                               source_export_path=source_export_path,
                                               target_export_path=target_export_path)
            if return_code != 0:
                raise Exception(str(output))

            return True
        except Exception:
            traceback = full_stack()

            workflow_dict['exceptions']['error_codes'].append(DBAAS_0022)
            workflow_dict['exceptions']['traceback'].append(traceback)

            return False

    def undo(self, workflow_dict):
        LOG.info("Running undo...")
        try:
            volume = workflow_dict['volume']
            host = workflow_dict['host']
            old_volume = workflow_dict['old_volume']

            source_export_path = scape_nfsaas_export_path(volume.nfsaas_path)
            target_export_path = scape_nfsaas_export_path(
                old_volume.nfsaas_path)
            return_code, output = update_fstab(host=host,
                                               source_export_path=source_export_path,
                                               target_export_path=target_export_path)
            if return_code != 0:
                LOG.info(str(output))

            return True
        except Exception:
            traceback = full_stack()

            workflow_dict['exceptions']['error_codes'].append(DBAAS_0022)
            workflow_dict['exceptions']['traceback'].append(traceback)

            return False
