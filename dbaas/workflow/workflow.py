# -*- coding: utf-8 -*-
from django.utils.module_loading import import_by_path
import logging
import time

LOG = logging.getLogger(__name__)


def start_workflow(workflow_dict, task=None):
    try:
        if not 'steps' in workflow_dict:
            return False
        workflow_dict['step_counter'] = 0

        workflow_dict['msgs'] = []
        workflow_dict['status'] = 0
        workflow_dict['total_steps'] = len(workflow_dict['steps'])

        for step in workflow_dict['steps']:
            workflow_dict['step_counter'] += 1

            my_class = import_by_path(step)
            my_instance = my_class()

            time_now = str(time.strftime("%m/%d/%Y %H:%M:%S"))

            msg = "\n%s - Step %i of %i - %s" % (time_now, workflow_dict['step_counter'], workflow_dict['total_steps'],str(my_instance))

            LOG.info(msg)

            if task:
                workflow_dict['msgs'].append(msg)
                task.update_details(persist=True, details=msg)

            if my_instance.do(workflow_dict) != True:
                workflow_dict['status'] = 0
                raise Exception

            workflow_dict['status'] = 1
            task.update_details(persist=True, details="DONE!")
            LOG.info(task.details)

        return True

    except Exception, e:
        print e

        workflow_dict['steps'] = workflow_dict[
            'steps'][:workflow_dict['step_counter']]
        stop_workflow(workflow_dict)

        return False


def stop_workflow(workflow_dict, task=None):
    LOG.info("Running undo...")

    try:

        for step in workflow_dict['steps'][::-1]:

            my_class = import_by_path(step)
            my_instance = my_class()

            if 'step_counter' in workflow_dict:
                workflow_dict['step_counter'] -= 1
                LOG.info("Step %i %s " %
                        (workflow_dict['step_counter'], str(my_instance)))
            my_instance.undo(workflow_dict)

        return True
    except Exception, e:
        print e
        return False