# This file is part of Indico.
# Copyright (C) 2002 - 2017 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from operator import attrgetter

import pytz

from indico.core.db import db
from indico.modules.events.models.events import Event
from indico.modules.events.models.settings import EventSetting
from indico.modules.users import User
from indico.util.console import cformat, verbose_iterator
from indico.util.string import is_legacy_id
from indico.util.struct.iterables import committing_iterator

from indico_migrate import TopLevelMigrationStep, convert_to_unicode
from indico_migrate.namespaces import SharedNamespace


# this function is here only to avoid import loops
def _get_all_steps():
    from indico_migrate.steps.events.abstracts import EventAbstractImporter
    from indico_migrate.steps.events.acls import EventACLImporter
    from indico_migrate.steps.events.layout import EventLayoutImporter, EventImageImporter
    from indico_migrate.steps.events.logs import EventLogImporter
    from indico_migrate.steps.events.menus import EventMenuImporter
    from indico_migrate.steps.events.misc import (EventTypeImporter, EventSettingsImporter, EventAlarmImporter,
                                                  EventShortUrlsImporter, EventMiscImporter, EventLegacyIdImporter,
                                                  EventPaymentSettingsImporter, EventAttachmentsImporter)
    from indico_migrate.steps.events.participants import EventParticipantsImporter
    from indico_migrate.steps.events.registration import EventRegFormImporter
    from indico_migrate.steps.events.surveys import EventSurveyImporter
    from indico_migrate.steps.events.timetable import EventTimetableImporter, EventTracksImporter
    return (EventMiscImporter, EventTypeImporter, EventACLImporter, EventLogImporter, EventSettingsImporter,
            EventPaymentSettingsImporter, EventAlarmImporter, EventImageImporter, EventLayoutImporter,
            EventShortUrlsImporter, EventMenuImporter, EventSurveyImporter, EventRegFormImporter,
            EventTracksImporter, EventParticipantsImporter, EventAbstractImporter, EventTimetableImporter,
            EventAttachmentsImporter, EventLegacyIdImporter)


class SkipEvent(Exception):
    pass


class _EventContextBase(object):
    def __init__(self, conf, debug=False):
        self.conf = conf
        self.is_legacy = False
        self.event_ns = SharedNamespace('event_ns', None, {
            'event_persons_by_email': dict,
            'event_persons_by_user': dict,
            'event_persons_by_data': dict,
            'legacy_contribution_type_map': dict,
            'abstract_map': dict,
            'old_abstract_state_map': dict,
            'as_duplicate_reviews': dict,
            'track_map': dict,
            'track_map_by_id': dict,
            'legacy_contribution_field_map': dict,
            'legacy_field_option_id_map': dict,
            'legacy_contribution_abstracts': dict,
            'legacy_session_map': dict,
            'legacy_contribution_map': dict,
            'legacy_subcontribution_map': dict,
            'payment_messages': dict,
            'misc_data': dict,
            # -> 'payment_currency': str
            # -> 'participant_list_disabled': bool
        })

    def create_event(self):
        if is_legacy_id(self.conf.id):
            event_id = int(self.gen_event_id())
            self.is_legacy = True
        else:
            event_id = int(self.conf.id)

        if 'title' not in self.conf.__dict__:
            self.importer.print_error('Event has no title in ZODB', self.conf.id)
            raise SkipEvent

        try:
            parent_category = self.importer.global_ns.legacy_category_ids[self.conf._Conference__owners[0].id]
        except (IndexError, KeyError):
            self.importer.print_error(cformat('%{red!}Event has no category!'), event_id=self.conf.id)
            raise SkipEvent

        title = convert_to_unicode(self.conf.__dict__['title']) or '(no title)'
        self.importer.print_success(title)

        tz = self.conf.__dict__.get('timezone', 'UTC')
        self.event = Event(id=event_id,
                           title=title,
                           description=convert_to_unicode(self.conf.__dict__['description']) or '',
                           timezone=tz,
                           start_dt=self._fix_naive(self.conf.__dict__['startDate'], tz),
                           end_dt=self._fix_naive(self.conf.__dict__['endDate'], tz),
                           is_locked=self.conf._closed,
                           category=parent_category,
                           is_deleted=False)

    def run_step(self, importer):
        importer.bind(self)
        importer.run()

    def _fix_naive(self, dt, tz):
        if dt.tzinfo is None:
            self.importer.print_warning('Naive datetime converted ({})'.format(dt))
            return pytz.timezone(tz).localize(dt)
        else:
            return dt


def EventContextFactory(counter, _importer):
    class _EventContext(_EventContextBase):
        event_id_counter = counter._Counter__count
        importer = _importer

        @classmethod
        def gen_event_id(cls):
            cls.event_id_counter += 1
            return cls.event_id_counter
    return _EventContext


class EventImporter(TopLevelMigrationStep):
    def __init__(self, *args, **kwargs):
        super(EventImporter, self).__init__(*args, **kwargs)
        del kwargs['janitor_user_id']
        self.janitor = User.get_system_user()
        self.debug = kwargs.get('debug')
        self.kwargs = kwargs
        self.kwargs['janitor'] = self.janitor

    def has_data(self):
        return (EventSetting.query.filter(EventSetting.module.in_(['core', 'contact'])).has_rows() or
                Event.query.filter_by(is_locked=True).has_rows())

    def migrate(self):
        db.session.commit()  # make sure there's no transaction open or the DISABLE TRIGGER may deadlock
        tables = ('timetable_entries', 'session_blocks', 'contributions', 'breaks')
        for table in tables:
            db.engine.execute(db.text('ALTER TABLE events.{} DISABLE TRIGGER consistent_timetable'.format(table)))
        try:
            self.migrate_event_data()
            db.session.commit()
        except:
            db.session.rollback()
            raise
        finally:
            for table in tables:
                db.engine.execute(db.text('ALTER TABLE events.{} ENABLE TRIGGER consistent_timetable'.format(table)))
        db.session.commit()

    def migrate_event_data(self):
        all_event_steps = _get_all_steps()

        importers = [importer(self.app, self.sqlalchemy_uri, self.zodb_root, not self.quiet, self.dblog,
                              self.default_group_provider, self.tz, **self.kwargs) for importer in all_event_steps]
        for importer in importers:
            importer.setup()

        EventContext = EventContextFactory(self.zodb_root['counters']['CONFERENCE'], self)

        self.print_step("Event data")
        for conf in committing_iterator(self._iter_events()):
            context = EventContext(conf, self.debug)
            try:
                context.create_event()
            except SkipEvent:
                continue
            for importer in importers:
                with db.session.no_autoflush:
                    context.run_step(importer)

        for importer in importers:
            importer.teardown()

    def _iter_events(self):
        def _it():
            for conf in self.zodb_root['conferences'].itervalues():
                dir(conf)  # make zodb load attrs
                yield conf
        it = _it()
        total = len(self.zodb_root['conferences'])
        if self.quiet:
            it = verbose_iterator(it, total, attrgetter('id'), lambda x: x.__dict__.get('title', ''))
        for old_event in self.flushing_iterator(it):
            yield old_event
