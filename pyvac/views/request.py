# -*- coding: utf-8 -*-
import re
import json
import logging
from datetime import datetime, timedelta
try:
    from collections import OrderedDict
except ImportError:
    OrderedDict = dict
from dateutil.relativedelta import relativedelta

from .base import View

from pyramid.httpexceptions import HTTPFound
from pyramid.url import route_url
from pyramid.settings import asbool

from pyvac.models import (
    Request, VacationType, User, RequestHistory, UserPool, Pool,
)
# from pyvac.helpers.i18n import trans as _
from pyvac.helpers.calendar import delFromCal
from pyvac.helpers.ldap import LdapCache
from pyvac.helpers.holiday import get_holiday
from pyvac.helpers.util import daterange, JsonHTTPNotFound

import yaml
try:
    from yaml import CSafeLoader as YAMLLoader
except ImportError:
    from yaml import SafeLoader as YAMLLoader

log = logging.getLogger(__name__)


class Send(View):

    def get_target_user(self, logged_user):
        if self.user.is_admin:
            sudo_user_id = int(self.request.params.get('sudo_user'))
            if sudo_user_id != -1:
                user = User.by_id(self.session, sudo_user_id)
                if user:
                    return user
        return logged_user

    def render(self):
        try:
            form_date_from = self.request.params.get('date_from')
            if ' - ' not in form_date_from:
                msg = 'Invalid format for period.'
                self.request.session.flash('error;%s' % msg)
                return HTTPFound(location=route_url('home', self.request))

            dates = self.request.params.get('date_from').split(' - ')
            date_from = datetime.strptime(dates[0], '%d/%m/%Y')
            date_to = datetime.strptime(dates[1], '%d/%m/%Y')
            breakdown = self.request.params.get('breakdown')

            # retrieve holidays for user so we can remove them from selection
            holidays = get_holiday(self.user, year=date_from.year,
                                   use_datetime=True)

            submitted = [d for d in daterange(date_from, date_to)
                         if d.isoweekday() not in [6, 7]
                         and d not in holidays]
            days = float(len(submitted))

            days_diff = (date_to - date_from).days
            if days_diff < 0:
                msg = 'Invalid format for period.'
                self.request.session.flash('error;%s' % msg)
                return HTTPFound(location=route_url('home', self.request))

            if (date_to == date_from) and days > 1:
                # same day, asking only for one or less day duration
                msg = 'Invalid value for days.'
                self.request.session.flash('error;%s' % msg)
                return HTTPFound(location=route_url('home', self.request))

            if days <= 0:
                msg = 'Invalid value for days.'
                self.request.session.flash('error;%s' % msg)
                return HTTPFound(location=route_url('home', self.request))

            # check if user is sudoed
            check_user = self.get_target_user(self.user)
            pool = dict([(k, v.amount) for k, v in check_user.pool.items()])
            # retrieve future requests for user so we can check overlap
            futures = [d for req in
                       Request.by_user_future(self.session, check_user)
                       for d in daterange(req.date_from, req.date_to)]
            intersect = set(futures) & set(submitted)
            if intersect:
                err_intersect = True
                # must check for false warning in case of half day requests
                if len(intersect) == 1:
                    # only one date in conflict, check if it's for an half-day
                    dt = intersect.pop()
                    # retrieve the request for this date
                    req = [req for req in
                           Request.by_user_future(self.session, check_user)
                           for d in daterange(req.date_from, req.date_to)
                           if d == dt]
                    if len(req) < 2:
                        req = req.pop()
                        if req.label != breakdown:
                            # intersect is false, it's not the same halfday
                            err_intersect = False
                            log.debug('False positive on intersect '
                                      'for %s (%s): request: %d (%s)' %
                                      (date_from, breakdown, req.id,
                                       req.label))

                if err_intersect:
                    msg = 'Invalid period: days already requested.'
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

            vac_type = VacationType.by_id(self.session,
                                          int(self.request.params.get('type')))

            if not self.user.is_admin:
                # check if vacation requires user role
                if (vac_type.visibility
                        and self.user.role not in vac_type.visibility):
                    msg = 'You are not allowed to use type: %s' % vac_type.name
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

            # check RTT usage access
            if vac_type.name == u'RTT':
                if self.user.has_feature('disable_rtt'):
                    msg = 'You are not allowed to use type: %s' % vac_type.name
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

            # label field is used when requesting half day
            label = u''
            if breakdown != 'FULL':
                # handle half day
                if (days > 1):
                    msg = ('AM/PM option must be used only when requesting a '
                           'single day.')
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))
                else:
                    days = 0.5
                    label = unicode(breakdown)

            # check RTT usage
            if vac_type.name == u'RTT':
                rtt_pool = check_user.pool.get('RTT')
                if rtt_pool is not None and rtt_pool.amount <= 0:
                    msg = 'No RTT left to take.'
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))
                # check that we have enough RTT to take
                if rtt_pool is not None and days > rtt_pool.amount:
                    msg = 'You only have %s RTT to use.' % rtt_pool.amount
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))
                # check that we request vacations in the allowed year
                if rtt_pool is not None:
                    if (date_from < rtt_pool.date_start or
                            date_to > rtt_pool.date_end):
                        msg = ('RTT can only be used between %s and %s' %
                               (rtt_pool.date_start.strftime('%d/%m/%Y'),
                                rtt_pool.date_end.strftime('%d/%m/%Y')))
                        self.request.session.flash('error;%s' % msg)
                        return HTTPFound(location=route_url('home', self.request)) # noqa

            message = None
            # check Exceptionnel mandatory field
            if vac_type.name == u'Exceptionnel':
                message = self.request.params.get('exception_text')
                message = message.strip() if message else message
                if not message:
                    msg = ('You must provide a reason for %s requests' %
                           vac_type.name)
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))
                # check size
                if len(message) > 140:
                    msg = ('%s reason must not exceed 140 characters' %
                           vac_type.name)
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

            # check for Compensatoire type (LU holiday recovery)
            if vac_type.name == u'Compensatoire':
                to_recover = self.request.params.get('recovered_holiday')
                if to_recover == '-1':
                    msg = 'You must select a date for %s' % vac_type.name
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

                recover_date = datetime.strptime(to_recover, '%d/%m/%Y')
                vac_class = vac_type.get_class(check_user.country)
                if vac_class:
                    error = vac_class.validate_request(check_user, None, days,
                                                       recover_date, date_to)
                    if error is not None:
                        self.request.session.flash('error;%s' % error)
                        return HTTPFound(location=route_url('home',
                                                            self.request))
                    message = to_recover

            # check Récupération reason field
            if vac_type.name == u'Récupération':
                message = self.request.params.get('exception_text')
                message = message.strip() if message else message
                # check size
                if message and len(message) > 140:
                    msg = ('%s reason must not exceed 140 characters' %
                           vac_type.name)
                    self.request.session.flash('error;%s' % msg)
                    return HTTPFound(location=route_url('home', self.request))

            # check CP usage
            if vac_type.name == u'CP':
                cp_class = check_user.get_cp_class(self.session)

                if cp_class:
                    # only FR and LU have a dedicated CP class to use

                    # convert days to hours for LU if needed
                    days = cp_class.convert_days(days)

                    error = cp_class.validate_request(check_user, pool, days,
                                                      date_from, date_to)
                    if error is not None:
                        self.request.session.flash('error;%s' % error)
                        return HTTPFound(location=route_url('home',
                                                            self.request))

            # create the request
            # default values
            target_status = u'PENDING'
            target_user = self.user
            target_notified = False

            sudo_use = False
            if self.user.is_admin:
                sudo_user_id = int(self.request.params.get('sudo_user'))
                if sudo_user_id != -1:
                    user = User.by_id(self.session, sudo_user_id)
                    if user:
                        sudo_use = True
                        target_user = user
                        target_status = u'APPROVED_ADMIN'
                        target_notified = True

            # save pool status when making the request
            pool_status = json.dumps(pool)

            request = Request(date_from=date_from,
                              date_to=date_to,
                              days=days,
                              vacation_type=vac_type,
                              status=target_status,
                              user=target_user,
                              notified=target_notified,
                              label=label,
                              message=message,
                              pool_status=pool_status,
                              )
            self.session.add(request)
            self.session.flush()
            # create history entry
            sudo_user = None
            if sudo_use:
                sudo_user = self.user
            RequestHistory.new(self.session, request, '', target_status,
                               target_user, pool_status, message=message,
                               sudo_user=sudo_user)

            UserPool.decrement_request(self.session, request)

            if request and not sudo_use:
                msg = 'Request sent to your manager.'
                self.request.session.flash('info;%s' % msg)
                # call celery task directly, do not wait for polling
                from celery.registry import tasks
                from celery.task import subtask
                req_task = tasks['worker_pending']
                data = {'req_id': request.id}
                subtask(req_task).apply_async(kwargs={'data': data},
                                              countdown=5)
                log.info('scheduling task worker_pending for %s' % data)

            if request and sudo_use:
                # save who performed this action
                request.last_action_user_id = self.user.id
                settings = self.request.registry.settings
                if 'pyvac.celery.yaml' in settings:
                    # with open(settings['pyvac.celery.yaml']) as fdesc:
                    #     Conf = yaml.load(fdesc, YAMLLoader)
                    # caldav_url = Conf.get('caldav').get('url')
                    # request.add_to_cal(caldav_url, self.session)
                    msg = 'Request added to calendar and DB.'
                    self.request.session.flash('info;%s' % msg)

        except Exception as exc:
            log.error(exc)
            msg = ('An error has occured while processing this request: %r'
                   % exc)
            self.request.session.flash('error;%s' % msg)

        return HTTPFound(location=route_url('home', self.request))


class List(View):
    """
    List all user requests
    """

    def get_conflict(self, requests):
        """ Returns requests conflicts """
        conflicts = {}
        for req in requests:
            req.conflict = [req2.summary for req2 in
                            Request.in_conflict_ou(self.session, req)]
            if req.conflict:
                req.conflict = {'': req.conflict}
                if req.id not in conflicts:
                    conflicts[req.id] = {}
                conflicts[req.id][''] = '\n'.join(req.conflict[''])

        return conflicts

    def get_conflict_by_teams(self, requests, users_teams):
        """ Returns requests conflicts by teams """
        conflicts = {}
        for req in requests:
            user_teams = users_teams.get(req.user.dn, [])
            matched = {}
            # for all requests in conflict with current req
            for req2 in Request.in_conflict(self.session, req):
                # if we have some match between request teams
                # and conflicting request teams
                conflict_teams = users_teams.get(req2.user.dn, [])
                common_set = set(conflict_teams) & set(user_teams)
                if common_set:
                    for team in common_set:
                        if team not in matched:
                            matched[team] = []
                        matched[team].append(req2.summary)

            req.conflict = matched
            if req.conflict:
                for team in req.conflict:
                    if req.id not in conflicts:
                        conflicts[req.id] = {}
                    conflicts[req.id][team] = ('\n'.join([team] +
                                               req.conflict[team]))

        return conflicts

    def render(self):

        req_list = {'requests': [], 'conflicts': {}}
        requests = []
        if self.user.is_admin:
            country = self.user.country
            requests = Request.all_for_admin_per_country(self.session,
                                                         country)
            # check if admin user is also a manager, in this case merge all
            # requests
            requests_manager = Request.by_manager(self.session, self.user)
            # avoid duplicate entries
            req_to_add = [req for req in requests_manager
                          if req not in requests]
            requests.extend(req_to_add)
        elif self.user.is_super:
            requests = Request.by_manager(self.session, self.user)

        req_list['requests'] = requests

        # always add our requests
        for req in Request.by_user(self.session, self.user):
            if req not in req_list['requests']:
                req_list['requests'].append(req)

        # split requests by past/next
        today = datetime.now()
        if self.user.is_admin:
            # for admin, display request from 1st of month
            today = today.replace(day=1)

        past_req = [req for req in req_list['requests']
                    if req.date_to < today and req.status not in
                    ['PENDING', 'ACCEPTED_MANAGER']]

        next_req = [req for req in req_list['requests']
                    if req not in past_req]

        req_list['past'] = past_req
        req_list['next'] = next_req

        # only retrieve conflicts for super users
        # only retrieve conflicts for next requests, not past ones
        if req_list['next'] and self.user.is_super:
            conflicts = {}

            settings = self.request.registry.settings
            use_ldap = False
            if 'pyvac.use_ldap' in settings:
                use_ldap = asbool(settings.get('pyvac.use_ldap'))

            if use_ldap:
                ldap = LdapCache()
                users_teams = {}
                for team, members in ldap.list_teams().iteritems():
                    for member in members:
                        users_teams.setdefault(member, []).append(team)

                conflicts = self.get_conflict_by_teams(req_list['next'],
                                                       users_teams)
            else:
                conflicts = self.get_conflict(req_list['next'])

            req_list['conflicts'] = conflicts

        return req_list


class Accept(View):
    """
    Accept a request
    """

    def render(self):

        req_id = self.request.params.get('request_id')
        req = Request.by_id(self.session, req_id)
        if not req:
            return ''

        data = {'req_id': req.id}

        only_manager = False
        # we should handle the case where the admin is also a user manager
        if (self.user.ldap_user and (req.user.manager_dn == self.user.dn)
                and (req.status == 'PENDING')):
            only_manager = True

        if self.user.is_admin and not only_manager:
            # create history entry
            RequestHistory.new(self.session, req,
                               req.status, 'APPROVED_ADMIN',
                               self.user)
            req.update_status('APPROVED_ADMIN')
            # save who performed this action
            req.last_action_user_id = self.user.id

            task_name = 'worker_approved'
            settings = self.request.registry.settings
            with open(settings['pyvac.celery.yaml']) as fdesc:
                Conf = yaml.load(fdesc, YAMLLoader)
            data['caldav.url'] = Conf.get('caldav').get('url')
        else:
            # create history entry
            RequestHistory.new(self.session, req,
                               req.status, 'ACCEPTED_MANAGER',
                               self.user)
            req.update_status('ACCEPTED_MANAGER')
            # save who performed this action
            req.last_action_user_id = self.user.id

            task_name = 'worker_accepted'

        self.session.flush()

        # call celery task directly, do not wait for polling
        from celery.registry import tasks
        from celery.task import subtask
        req_task = tasks[task_name]

        subtask(req_task).apply_async(kwargs={'data': data}, countdown=5)

        log.info('scheduling task %s for req_id: %d' % (task_name,
                                                        data['req_id']))
        return req.status


class Refuse(View):
    """
    Refuse a request
    """

    def render(self):

        req_id = self.request.params.get('request_id')
        req = Request.by_id(self.session, req_id)
        if not req:
            return ''
        reason = self.request.params.get('reason')

        req.reason = reason
        RequestHistory.new(self.session, req,
                           req.status, 'DENIED',
                           self.user, reason=reason)
        req.update_status('DENIED')
        # save who performed this action
        req.last_action_user_id = self.user.id
        # refund userpool
        req.refund_userpool(self.session)

        self.session.flush()

        # call celery task directly, do not wait for polling
        from celery.registry import tasks
        from celery.task import subtask
        req_task = tasks['worker_denied']
        data = {'req_id': req.id}
        subtask(req_task).apply_async(kwargs={'data': data}, countdown=5)

        log.info('scheduling task worker_denied for %s' % data)

        return req.status


class Cancel(View):
    """
    Cancel a request and remove entry from calendar if needed.
    """

    def render(self):

        req_id = self.request.params.get('request_id')
        req = Request.by_id(self.session, req_id)
        if not req:
            return ''

        # check if request have already been consumed
        if not self.user.is_admin:
            today = datetime.now()
            if req.date_from <= today:
                log.error('User %s tried to CANCEL consumed request %d.' %
                          (self.user.login, req.id))
                return req.status

        # delete from calendar
        if req.status == 'APPROVED_ADMIN' and req.ics_url:
            settings = self.request.registry.settings
            with open(settings['pyvac.celery.yaml']) as fdesc:
                Conf = yaml.load(fdesc, YAMLLoader)
            caldav_url = Conf.get('caldav').get('url')
            delFromCal(caldav_url, req.ics_url)

        RequestHistory.new(self.session, req,
                           req.status, 'CANCELED',
                           self.user)
        req.update_status('CANCELED')
        # save who performed this action
        req.last_action_user_id = self.user.id
        # refund userpool
        req.refund_userpool(self.session)

        self.session.flush()
        return req.status


class Export(View):
    """
    Display form to export requests
    """
    def render(self):
        from datetime import datetime
        start = datetime(2014, 5, 1)
        today = datetime.now()
        entries = []
        for year in reversed(range(start.year, today.year + 1)):
            for month in reversed(range(1, 13)):
                temp = datetime(year, month, 1)
                entries.append(('%d/%d' % (month, year),
                               temp.strftime('%B %Y')))

        export_day_tooltip = """\
Example: If you use 20 for Feb month, export will be from 21 Jan to 20 Feb.
"""

        return {'months': entries,
                'current_month': '%d/%d' % (today.month, today.year),
                'export_day_tooltip': export_day_tooltip}


class Exported(View):
    """
    Export all requests of a month to csv
    """
    def render(self):

        exported = {}
        if self.user.is_admin:
            country = self.user.country
            month, year = self.request.params.get('month').split('/')
            month = int(month)
            year = int(year)
            sage_order = int(self.request.params.get('sage_order', 0))
            log.info('exporting for: %d/%d' % (int(month), int(year)))

            export_month = int(self.request.params.get('export_month', 0)) # noqa
            export_day = int(self.request.params.get('export_day', 0))
            boundary_date = int(self.request.params.get('boundary_date', 0))

            if export_day:
                last_month_date = datetime(year, month, boundary_date, 23, 59, 59) # noqa
                first_month_date = last_month_date - relativedelta(months=1)
                first_month_date = first_month_date.replace(
                    day=boundary_date + 1, hour=0, minute=0, second=0,
                    microsecond=0)
                log.info('exporting from %s -> %s' % (first_month_date,
                                                      last_month_date))

                all_reqs = Request.get_by_month(
                    self.session, country, month, year,
                    sage_order=sage_order,
                    first_month_date=first_month_date,
                    last_month_date=last_month_date
                )

                # don't filter for LU country
                if country == 'lu':
                    requests = all_reqs
                else:
                    # filter requests which overlap the boundary date, only
                    # keep the ones which are ending in the selected period
                    requests = []
                    for req in all_reqs:
                        if req.date_from < first_month_date <= req.date_to:
                            log.info('using overlapping req: %r' % req.summary)
                            requests.append(req)
                        elif req.date_from <= last_month_date < req.date_to:
                            log.info('discarding overlapping req: %r' %
                                     req.summary)
                        else:
                            requests.append(req)
            else:
                # assume it's export_month as it's a radio button choice
                all_reqs = Request.get_by_month(self.session, country,
                                                month, year,
                                                sage_order=sage_order)
                # don't filter for LU country
                if country == 'lu':
                    requests = all_reqs
                else:
                    # filter request which overlap 2 months, only keep the ones
                    # which are ending in the selected month
                    requests = []
                    for req in all_reqs:
                        if req.date_from.month != req.date_to.month:
                            if req.date_to.month == int(month):
                                log.info('using overlapping req: %r'
                                         % req.summary)
                                requests.append(req)
                            else:
                                log.info('discarding overlapping req: %r' %
                                         req.summary)
                        else:
                            requests.append(req)

            data = []
            header = ('%s,%s,%s,%s,%s,%s,%s,%s,%s,%s' %
                      ('#', 'registration_number', 'lastname', 'firstname',
                       'from', 'to', 'number', 'type', 'label', 'message'))
            data.append(header)
            for idx, req in enumerate(requests, start=1):
                data.append('%d,%s' % (idx, req.summarycsv))
            exported = '\n'.join(data)

        return {u'exported': exported}


class Prevision(View):
    """
    Display future CP used per user
    """
    def render(self):
        from datetime import datetime
        today = datetime.now()
        end_date = datetime(today.year, 10, 31)

        previsions = Request.get_previsions(self.session, end_date)

        users_per_id = dict([(user.id, user)
                             for user in User.find(self.session)])

        settings = self.request.registry.settings
        use_ldap = False
        if 'pyvac.use_ldap' in settings:
            use_ldap = asbool(settings.get('pyvac.use_ldap'))

        user_attr = {}
        users_teams = {}
        if use_ldap:
            # synchronise user groups/roles
            User.sync_ldap_info(self.session)
            ldap = LdapCache()
            user_attr = ldap.get_users_units()
            users_teams = {}
            for team, members in ldap.list_teams().iteritems():
                for member in members:
                    users_teams.setdefault(member, []).append(team)

        return {'users_per_id': users_per_id,
                'use_ldap': use_ldap,
                'ldap_info': user_attr,
                'users_teams': users_teams,
                'previsions': previsions,
                'today': today,
                'end_date': end_date,
                }


class Off(View):
    """
    Return all users who are on vacation for given date

    If no date provided, default to current date
    Results can be filtered in querystring by user name or nickname (LDAP uid)
    """
    def update_response(self, response):
        """ Do nothing """

    def render(self):
        duration = self.request.params.get('duration')

        def fmt_req_type(req):
            label = ' %s' % req.label if req.label else ''
            if duration and req.days > 1:
                label = '%s (until %s)' % (label,
                                           req.date_to.strftime('%d/%m/%Y'))
            return 'OFF%s' % label

        filter_nick = self.request.params.get('nick')
        filter_name = self.request.params.get('name')
        filter_date = self.request.params.get('date')
        # strict if provided will disable partial search for nicknames
        strict = self.request.params.get('strict')

        # remove unwanted chars from filter_date
        if filter_date:
            filter_date = re.sub('[^\d+]', '', filter_date)

        if filter_nick:
            # retrieve all availables nicknames
            all_nick = [nick.lower()
                        for nick in User.get_all_nicknames(self.session)]
            if strict:
                match = filter_nick.lower() in all_nick
            else:
                match = set([nick for nick in all_nick
                             if filter_nick.lower() in nick.lower()])
            if not match:
                # filter_nick does not match any known uid, stop here
                return JsonHTTPNotFound({'message': ('%s not found'
                                                     % filter_nick)})

        requests = Request.get_active(self.session, filter_date)
        data_name = dict([(req.user.name.lower(), fmt_req_type(req))
                          for req in requests])
        data_nick = dict([(req.user.nickname, fmt_req_type(req))
                          for req in requests])

        ret = val = None
        if filter_nick:
            val = data_nick.get(filter_nick.lower())
            if val:
                ret = {filter_nick: val}
            elif not strict:
                val = dict([(k, v) for k, v in data_nick.items()
                            if filter_nick.lower() in k])
                return val
            else:
                return {}

        if filter_name:
            val = data_name.get(filter_name.lower())
            if val:
                ret = {filter_name: val}
            else:
                val = dict([(k, v) for k, v in data_name.items()
                            if filter_name.lower() in k])
                return val

        data_name = OrderedDict(sorted(data_name.items(), key=lambda t: t[0]))
        return ret if ret else data_name


class PoolHistory(View):
    """
    Display pool history balance changes for given user
    """

    def get_new_history(self, user, today, year):
        """Retrieve pool history using Pool and UserPool models."""
        # group userpools per vacation_type
        if datetime.now().year == year:
            pools = {}
            for up in user.pools:
                if up.pool.pool_group:
                    pools[up.pool.name] = up
                else:
                    pools[up.pool.vacation_type.name] = up
        else:
            pools = {}
            ups = UserPool.by_user(self.session, user)
            # only select active pool for the selected year of pool history
            for up in ups:
                if up.pool.date_start <= today <= up.pool.date_end:
                    if up.pool.pool_group:
                        p2 = Pool.by_pool_group_raw(self.session,
                                                    up.pool.pool_group)
                        for pool in p2:
                            pools[pool.name] = UserPool.by_user_pool_id(
                                self.session, user.id, pool.id)
                    else:
                        pools[up.pool.vacation_type.name] = up

        pool_history = {}
        if 'RTT' in pools:
            pool_history['RTT'] = pools['RTT'].get_pool_history(self.session, self.user) # noqa

        history = []
        if 'restant' in pools:
            restant = pools['restant'].get_pool_history(self.session, self.user) # noqa
            acquis = pools['acquis'].get_pool_history(self.session, self.user)
            pool_restant = restant[0]['value']
            restant[0]['value'] = 0
            if acquis:
                pool_acquis = acquis.pop(0)['value']
            else:
                pool_acquis = 0
            history = sorted(restant + acquis)

        cp_history = []
        for idx, entry in enumerate(history, start=1):
            if entry['name'] == 'acquis':
                pool_acquis = round(pool_acquis, 2) + entry['value']
            else:
                pool_restant = round(pool_restant, 2) + entry['value']
            item = {
                'date': entry['date'],
                'value': entry['value'],
                'name': entry['name'],
                'restant': pool_restant,
                'acquis': pool_acquis,
                'flavor': entry.get('flavor', ''),
                'req_id': entry.get('req_id'),
            }
            # if idx == len(history):
            #     item['acquis'] = round(item['acquis'])

            cp_history.append(item)

        skip_idx = []
        for idx, entry in enumerate(cp_history):
            try:
                next_entry = cp_history[idx + 1]
            except:
                next_entry = None
            # merge events in case of split decrement when using 2 pools
            if next_entry and entry['req_id'] and (entry['date'] == next_entry['date']) and (entry['req_id'] == next_entry['req_id']): # noqa
                # check for refunded request
                # refunded requests are when both value are either
                # positive or negative
                if ((entry['value'] > 0) and (next_entry['value'] < 0)) or ((entry['value'] < 0) and (next_entry['value'] > 0)): # noqa
                    next_entry['flavor'] = '%s refunded' % next_entry['flavor']
                    continue
                if entry['name'] == 'restant':
                    skip_idx.append(idx)
                    entry['restant'] = 0
                else:
                    entry['restant'] = 0
                    skip_idx.append(idx + 1)
                entry['value'] += next_entry['value']

        cp_history = [i for idx, i in enumerate(cp_history)
                      if idx not in skip_idx]

        # remove duplicate 1st entry as we merged 2 lines into one
        if len(cp_history) > 1 and (cp_history[0]['acquis'] == cp_history[1]['acquis']) and (cp_history[0]['restant'] == cp_history[1]['restant']): # noqa
            cp_history.pop(0)
        pool_history['CP'] = cp_history

        return pool_history

    def get_old_history(self, user, today, year):
        """Retrieve pool history using epoch recomputing."""
        pool_history = {}
        pool_history['RTT'] = User.get_rtt_history(self.session, user, year)

        if today.year > year:
            if user.country == 'lu':
                today = datetime(year, 12, 31)
            else:
                today = datetime(year, 5, 31)

        history, restant = User.get_cp_history(self.session, user, year, today)

        vac_class = user.get_cp_class(self.session)

        cp_history = []
        pool_acquis = 0
        pool_restant = 0
        for idx, entry in enumerate(history):
            if idx == 0:
                pool_restant = restant[entry['date']]

            if entry['value'] < 0:
                if user.country == 'lu':
                    pool_restant, pool_acquis = vac_class.consume(
                        taken=entry['value'],
                        restant=pool_restant,
                        acquis=pool_acquis)
                else:
                    _, pool_restant, pool_acquis, _ = vac_class.consume(
                        taken=entry['value'],
                        restant=pool_restant,
                        acquis=pool_acquis,
                        n_1=0,
                        extra=0)
            else:
                pool_acquis = pool_acquis + entry['value']

            item = {
                'date': entry['date'],
                'value': entry['value'],
                'restant': pool_restant,
                'acquis': pool_acquis,
                'flavor': entry.get('flavor', ''),
            }
            cp_history.append(item)

        pool_history['CP'] = cp_history

        return pool_history

    def render(self):
        user = User.by_id(self.session,
                          int(self.request.matchdict['user_id']))

        if self.user.has_no_role:
            # can only see own requests
            if user.id != self.user.id:
                return HTTPFound(location=route_url('list_request',
                                                    self.request))

        if self.user.is_manager:
            # can only see own requests and managed user requests
            if ((user.id != self.user.id)
                    and (user.manager_id != self.user.id)):
                return HTTPFound(location=route_url('list_request',
                                                    self.request))

        today = datetime.now()
        year = int(self.request.params.get('year', today.year))

        start = datetime(2014, 5, 1)
        years = [item for item in reversed(range(start.year, today.year + 1))]

        if today.year > year:
            if user.country == 'lu':
                today = datetime(year, 12, 31)
            else:
                today = datetime(year, 5, 31)

        if year >= 2018:
            pool_history = self.get_new_history(user, today, year)
        else:
            pool_history = self.get_old_history(user, today, year)

        ret = {'user': user,
               'year': year,
               'years': years,
               'pool_history': pool_history}

        return ret


class History(View):
    """
    Display all history entries for given request
    """
    def render(self):
        request = Request.by_id(self.session,
                                int(self.request.matchdict['req_id']))

        if self.user.has_no_role:
            # can only see own requests
            if request.user.id != self.user.id:
                return HTTPFound(location=route_url('list_request',
                                                    self.request))

        if self.user.is_manager:
            # can only see own requests and managed user requests
            if ((request.user.id != self.user.id)
                    and (request.user.manager_id != self.user.id)):
                return HTTPFound(location=route_url('list_request',
                                                    self.request))

        if request:
            return {u'history': request.history, 'req': request}

        return {}


class OverviewMixin(object):

    def get_users_stats(self, users_per_id):
        entity_length = len(users_per_id)
        if not entity_length:
            return

        # retrieve today's squad off members
        today = datetime.now()
        today_off = []
        today_requests = []
        requests = Request.get_active(self.session)
        for req in requests:
            if req.user.id not in users_per_id:
                continue
            today_requests.append(req)
            if req.user not in today_off:
                today_off.append(req.user)

        # retrieve active requests since 15 days ago
        date_from = today - relativedelta(days=15)

        all_reqs = []
        for user_id, user in users_per_id.items():
            user_req = Request.by_user_future_approved(self.session, user,
                                                       date_from=date_from)
            all_reqs.extend(user_req)

        # compute current month squad presence percentages
        data_months = {}
        for req in all_reqs:
            for dt in req.dates:
                if dt.month not in data_months:
                    data_months[dt.month] = {}
                if dt.day not in data_months[dt.month]:
                    data_months[dt.month][dt.day] = []
                if req.user.login not in data_months[dt.month][dt.day]:
                    data_months[dt.month][dt.day].append(req.user.login)

        data_days_current = []
        labels = []
        start_date = today - timedelta(days=15)
        stop_date = today + timedelta(days=15)
        for x in daterange(start_date, stop_date):
            labels.append("'%s'" % x.strftime('%d/%m'))
            perc = ((entity_length - len(data_months.get(x.month, {}).get(x.day, []))) / float(entity_length) * 100)  # noqa
            perc = round(perc, 2)
            if x.isoweekday() in [6, 7]:
                perc = 0.0
            data_days_current.append(perc)

        labels = '[%s]' % ','.join(labels)
        return {'users_per_id': users_per_id,
                'data_days_current': data_days_current,
                'today_requests': today_requests,
                'labels': labels,
                'today': today,
                'today_off': today_off,
                'today_off_length': len(today_off),
                'entity_length': entity_length,
                }


class SquadOverview(OverviewMixin, View):
    """Display a dashboard of request and presence for squad leaders."""
    squad_leaders = {}

    def get_squad_stats(self, target_squad, users_entity):
        # retrieve squad members
        users_per_id = {}
        for user in User.find(self.session):
            if target_squad in users_entity.get(user.dn, []):
                users_per_id[user.id] = user

        return self.get_users_stats(users_per_id)

    def render(self):
        # synchronise user groups/roles
        User.sync_ldap_info(self.session)
        ldap = LdapCache()
        users_entity = {}
        for team, members in ldap.list_teams().iteritems():
            for member in members:
                users_entity.setdefault(member, []).append(team)

        # keep only managed users for managers
        # use all users for admin
        overviews = {}
        if self.user.is_admin or self.user.has_feature('squad_overview_full'):
            for _, target_squad in self.squad_leaders.items():
                squad_stats = self.get_squad_stats(target_squad, users_entity)
                overviews.update({target_squad: squad_stats})
        elif self.user.is_manager:
            # retrieve logged squad leader
            target_squad = self.squad_leaders[self.user.login]
            squad_stats = self.get_squad_stats(target_squad, users_entity)
            overviews = {target_squad: squad_stats}
        else:
            return HTTPFound(location=route_url('home', self.request))

        return {'users_entity': users_entity, 'overviews': overviews}


class ChapterOverview(OverviewMixin, View):
    """Display a dashboard of request and presence for chapter leaders."""
    chapter_leaders = {}

    def get_chapter_stats(self, target_chapter, users_entity):
        # retrieve chapter members
        users_per_id = {}
        for user_dn, chapters in users_entity.iteritems():
            if target_chapter not in chapters:
                continue
            user = User.by_dn(self.session, user_dn)
            if not user:
                continue
            users_per_id[user.id] = user

        return self.get_users_stats(users_per_id)

    def render(self):
        # synchronise user groups/roles
        User.sync_ldap_info(self.session)
        ldap = LdapCache()
        users_entity = {}
        for chapter, members in ldap.list_chapters().iteritems():
            for member in members:
                users_entity.setdefault(member, []).append(chapter)

        # keep only managed users for managers
        # use all users for admin
        overviews = {}
        if self.user.is_admin or self.user.has_feature('chapter_overview_full'):  # noqa
            for _, target_chapter in self.chapter_leaders.items():
                chapter_stats = self.get_chapter_stats(target_chapter, users_entity)  # noqa
                overviews.update({target_chapter: chapter_stats})
        elif self.user.is_manager:
            # retrieve logged chapter leader
            target_chapter = self.chapter_leaders[self.user.login]
            chapter_stats = self.get_chapter_stats(target_chapter, users_entity) # noqa
            overviews = {target_chapter: chapter_stats}
        else:
            return HTTPFound(location=route_url('home', self.request))

        return {'users_entity': users_entity, 'overviews': overviews}


class ManagerOverview(OverviewMixin, View):
    """Display a dashboard of requests and presence for managers."""

    def get_manager_stats(self, users_entity):
        # retrieve squad members
        users_per_id = {}
        for user in users_entity:
            users_per_id[user.id] = user

        return self.get_users_stats(users_per_id)

    def render(self):
        # keep only managed users for managers
        # use all users for admin
        overviews = {}
        extra_managers = []
        # check if admin user is also a manager
        if User.managed_users(self.session, self.user):
            extra_managers = [self.user]
        if self.user.is_admin or self.user.has_feature('squad_overview_full'):
            for manager in extra_managers + User.by_role(self.session, 'manager'):  # noqa
                # retrieve logged leader squad
                users_entity = User.managed_users(self.session, manager)
                target_manager = manager.login.replace('.', '_')
                manager_stats = self.get_manager_stats(users_entity)
                if not manager_stats:
                    continue
                overviews.update({target_manager: manager_stats})
        elif self.user.is_manager:
            # retrieve logged leader squad
            users_entity = User.managed_users(self.session, self.user)
            target_manager = self.user.login.replace('.', '_')
            manager_stats = self.get_manager_stats(users_entity)
            if manager_stats:
                overviews = {target_manager: manager_stats}
        else:
            return HTTPFound(location=route_url('home', self.request))

        return {'users_entity': users_entity, 'overviews': overviews}


def includeme(config):
    """
    Pyramid includeme file for the :class:`pyramid.config.Configurator`
    """
    settings = config.registry.settings

    if 'pyvac.features.squad_overview' in settings:
        filename = settings['pyvac.features.squad_overview']
        try:
            with open(filename) as fdesc:
                conf = yaml.load(fdesc, YAMLLoader)
            SquadOverview.squad_leaders = conf.get('squad_leaders', {})
            log.info('Loaded squad_leaders file %s: %s' %
                     (filename, SquadOverview.squad_leaders))
        except IOError:
            log.warn('Cannot load squad_leaders file %s' % filename)

    if 'pyvac.features.chapter_overview' in settings:
        filename = settings['pyvac.features.chapter_overview']
        try:
            with open(filename) as fdesc:
                conf = yaml.load(fdesc, YAMLLoader)
            ChapterOverview.chapter_leaders = conf.get('chapter_leaders', {})
            log.info('Loaded chapter_leaders file %s: %s' %
                     (filename, ChapterOverview.chapter_leaders))
        except IOError:
            log.warn('Cannot load chapter_leaders file %s' % filename)
