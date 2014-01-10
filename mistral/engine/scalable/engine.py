# -*- coding: utf-8 -*-
#
# Copyright 2013 - Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import json

import pika
from oslo.config import cfg
from mistral.openstack.common import log as logging
from mistral.db import api as db_api
from mistral import exceptions
from mistral.engine import abstract_engine as abs_eng
from mistral.engine import states
from mistral.engine import workflow


LOG = logging.getLogger(__name__)


class ScalableEngine(abs_eng.AbstractEngine):
    @classmethod
    def _notify_task_executors(cls, tasks):
        opts = cfg.CONF.rabbit

        creds = pika.PlainCredentials(opts.rabbit_user,
                                      opts.rabbit_password)
        params = pika.ConnectionParameters(opts.rabbit_host,
                                           opts.rabbit_port,
                                           opts.rabbit_virtual_host,
                                           creds)

        conn = pika.BlockingConnection(params)
        LOG.debug("Connected to RabbitMQ server [params=%s]" % params)

        try:
            channel = conn.channel()
            channel.queue_declare(queue=opts.rabbit_task_queue)

            for task in tasks:
                msg = json.dumps(task)
                channel.basic_publish(exchange='',
                                      routing_key=opts.rabbit_task_queue,
                                      body=msg)
                LOG.info("Submitted task for execution: '%s'" % msg)
        finally:
            conn.close()

    @classmethod
    def start_workflow_execution(cls, workbook_name, target_task_name):
        wb_dsl = cls._get_wb_dsl(workbook_name)
        dsl_tasks = workflow.find_workflow_tasks(wb_dsl, target_task_name)
        db_api.start_tx()

        # Persist execution and tasks in DB.
        try:
            execution = cls._create_execution(workbook_name, target_task_name)

            tasks = cls._create_tasks(dsl_tasks, wb_dsl,
                                      workbook_name, execution['id'])

            cls._notify_task_executors(workflow.find_tasks_to_start(tasks))

            db_api.commit_tx()

            return execution
        except Exception:
            raise exceptions.EngineException("Cannot perform task"
                                             " creating in DB")
        finally:
            db_api.end_tx()

    @classmethod
    def convey_task_result(cls, workbook_name, execution_id,
                           task_id, state, result):
        db_api.start_tx()

        #TODO(rakhmerov): validate state transition

        # Update task state.
        task = db_api.task_update(workbook_name, execution_id, task_id,
                                  {"state": state, "result": result})
        execution = db_api.execution_get(workbook_name, execution_id)

        # Determine what tasks need to be started.
        tasks = db_api.tasks_get(workbook_name, execution_id)

        try:
            if cls._determine_workflow_is_finished(workbook_name,
                                                   execution, task):
                db_api.commit_tx()
                return task

            if workflow.is_success(tasks):
                db_api.execution_update(workbook_name, execution_id, {
                    "state": states.SUCCESS
                })

                db_api.commit_tx()
                LOG.info("Execution finished with success: %s" % execution)

                return task

            cls._notify_task_executors(workflow.find_tasks_to_start(tasks))

            db_api.commit_tx()
            return task
        except Exception:
            raise exceptions.EngineException("Cannot perform task or"
                                             " execution updating in DB")
        finally:
            db_api.end_tx()


def get_engine():
    return ScalableEngine
