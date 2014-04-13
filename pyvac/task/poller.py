# -*- coding: utf-8 -*-

import logging

from celery.task import Task, subtask

from pyvac.task.worker import (
    WorkerPending,
    WorkerAccepted,
    WorkerAcceptedNotified,
    WorkerDenied,
    WorkerApproved,
)
from pyvac.models import DBSession, Request


log = logging.getLogger(__name__)


class Poller(Task):

    name = 'poller'

    worker_tasks = {
        'PENDING': WorkerPending,
        'ACCEPTED_MANAGER': WorkerAccepted,
        'ACCEPTED_NOTIFIED': WorkerAcceptedNotified,
        'DENIED': WorkerDenied,
        'APPROVED_ADMIN': WorkerApproved,
    }

    def run(self, *args, **kwargs):
        self.log = log
        # init database connection
        session = DBSession()

        statuses = ['PENDING',
                    'ACCEPTED_MANAGER',
                    'DENIED',
                    'APPROVED_ADMIN',
                    'CANCELED',
                    'ERROR']
        for status in statuses:
            requests = Request.by_status(session, status)
            self.log.info('number of requests for %s: %d' %
                          (status, len(requests)))

        req_pending = Request.by_status(session, 'PENDING')
        self.log.info('number of PENDING requests: %d' % len(req_pending))

        req_accepted = Request.by_status(session, 'ACCEPTED_MANAGER')
        self.log.info('number of ACCEPTED_MANAGER requests: %d' %
                      len(req_accepted))

        req_accepted_notified = Request.by_status(session, 'ACCEPTED_MANAGER',
                                                  notified=True)
        self.log.info('number of ACCEPTED_NOTIFIED requests: %d' %
                      len(req_accepted_notified))

        req_denied = Request.by_status(session, 'DENIED')
        self.log.info('number of DENIED requests: %d' % len(req_denied))

        req_approved = Request.by_status(session, 'APPROVED_ADMIN')
        self.log.info('number of APPROVED_ADMIN requests: %d' %
                      len(req_approved))

        req_list = []
        req_list.extend(req_pending)
        req_list.extend(req_accepted)
        req_list.extend(req_denied)
        req_list.extend(req_approved)
        req_list.extend(req_accepted_notified)

        for req in req_list:
            self.log.info('selecting task for req type %r' % req.status)

            check_status = req.status
            if req.status == 'ACCEPTED_MANAGER' and req.notified:
                check_status = 'ACCEPTED_NOTIFIED'

            req_task = self.worker_tasks[check_status]
            self.log.info('task selected %r' % req_task.name)

            data = {
                'req_id': req.id,
            }

            async_result = subtask(req_task).delay(data=data)
            self.log.info('task scheduled %r' % async_result)

        return True