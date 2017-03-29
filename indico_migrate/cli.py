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

from __future__ import print_function, unicode_literals

import time
import warnings
from argparse import Namespace
from collections import defaultdict
from operator import itemgetter

import click
from flask.exthook import ExtDeprecationWarning
from sqlalchemy.sql import func, select

warnings.simplefilter('ignore', ExtDeprecationWarning)  # some of our dependencies still use flask.ext :(

from indico.core.db.sqlalchemy import db
from indico.modules.users.models.users import User
from indico.util.console import cformat, clear_line
from indico_migrate.migrate import migrate

click.disable_unicode_literals_warning = True


@click.command()
@click.argument('sqlalchemy-uri')
@click.argument('zodb-uri')
@click.option('--verbose', '-v', is_flag=True, default=False, help="Use verbose output")
@click.option('--dblog', '-L', is_flag=True, default=False, help="Enable db query logging")
@click.option('--ldap-provider-name', default='legacy-ldap', help="Provider name to use for existing LDAP identities")
@click.option('--default-group-provider', required=True, help="Name of the default group provider")
@click.option('--ignore-local-accounts', is_flag=True, default=False, help="Do not migrate existing local accounts")
@click.option('--archive-dir', required=True, multiple=True,
              help="The base path where resources are stored (ArchiveDir in indico.conf). When used multiple times, "
                   "the dirs are checked in order until a file is found.")
def cli(sqlalchemy_uri, zodb_uri, verbose, dblog, **kwargs):
    """
    This script migrates your database from ZODB/Indico 1.2 to PostgreSQL (2.0).

    You always need to specify both the SQLAlchemy connection URI and
    ZODB URI (both zeo:// and file:// work).
    """
    Importer._global_maps.user_favorite_categories = defaultdict(set)
    migrate(zodb_uri, sqlalchemy_uri, verbose=verbose, dblog=dblog, **kwargs)


class Importer(object):
    #: Specify plugins that need to be loaded for the import (e.g. to access its .settings property)
    plugins = frozenset()
    _global_maps = Namespace()

    def __init__(self, app, sqlalchemy_uri, zodb_root, verbose, dblog, tz, **kwargs):
        self.sqlalchemy_uri = sqlalchemy_uri
        self.quiet = not verbose
        self.dblog = dblog
        self.zodb_root = zodb_root
        self.app = app
        self.tz = tz

        self.initialize_global_maps(Importer._global_maps)

    def initialize_global_maps(self, g):
        pass

    @property
    def makac_info(self):
        return self.zodb_root['MaKaCInfo']['main']

    @property
    def global_maps(self):
        return Importer._global_maps

    def __repr__(self):
        return '<{}({}, {})>'.format(type(self).__name__, self.sqlalchemy_uri, self.zodb_uri)

    def run(self):
        start = time.time()
        self.migrate()
        print('migration took {:.06f} seconds\a'.format((time.time() - start)))

    def flushing_iterator(self, iterable, n=5000):
        """Iterates over `iterable` and flushes the ZODB cache every `n` items.

        :param iterable: an iterable object
        :param n: number of items to flush after
        """
        conn = self.zodb_root._p_jar
        for i, item in enumerate(iterable, 1):
            yield item
            if i % n == 0:
                conn.sync()

    def check_plugin_schema(self, name):
        """Checks if a plugin schema exists in the database.

        :param name: Name of the plugin
        """
        sql = 'SELECT COUNT(*) FROM "information_schema"."schemata" WHERE "schema_name" = :name'
        count = db.engine.execute(db.text(sql), name='plugin_{}'.format(name)).scalar()
        if not count:
            print(cformat('%{red!}Plugin schema does not exist%{reset}'))
            print(cformat('Run %{yellow!}indico db --plugin {} upgrade%{reset} to create it').format(name))
            return False
        return True

    def migrate(self):
        raise NotImplementedError

    def update_merged_users(self, db_column, msg_in):
        self.print_step("Updating merged users in {}".format(msg_in))
        for obj in db_column.class_.find(User.merged_into_id != None, _join=db_column):  # noqa
            initial_user = getattr(obj, db_column.key)
            while getattr(obj, db_column.key).merged_into_user:
                merged_into_user = getattr(obj, db_column.key).merged_into_user
                setattr(obj, db_column.key, merged_into_user)
            msg = cformat('%{cyan}{}%{reset} -> %{cyan}{}%{reset}').format(initial_user, getattr(obj, db_column.key))
            self.print_success(msg, always=True)
        db.session.commit()

    def fix_sequences(self, schema=None, tables=None):
        for name, cls in sorted(db.Model._decl_class_registry.iteritems(), key=itemgetter(0)):
            table = getattr(cls, '__table__', None)
            if table is None:
                continue
            elif schema is not None and table.schema != schema:
                continue
            elif tables is not None and cls.__tablename__ not in tables:
                continue
            # Check if we have a single autoincrementing primary key
            candidates = [col for col in table.c if col.autoincrement and col.primary_key]
            if len(candidates) != 1 or not isinstance(candidates[0].type, db.Integer):
                continue
            serial_col = candidates[0]
            sequence_name = '{}.{}_{}_seq'.format(table.schema, cls.__tablename__, serial_col.name)

            query = select([func.setval(sequence_name, func.max(serial_col) + 1)], table)
            db.session.execute(query)
        db.session.commit()

    def print_msg(self, msg, always=False):
        """Prints a message to the console.

        By default, messages are not shown in quiet mode, but this
        can be changed using the `always` parameter.
        """
        if self.quiet:
            if not always:
                return
            clear_line()
        print(msg)

    def print_step(self, msg):
        """Prints a message about a migration step to the console

        This message is always shown, even in quiet mode.
        """
        self.print_msg(cformat('%{white!}{}%{reset}').format(msg), True)

    def print_prefixed(self, prefix, prefix_color, msg, always=False, event_id=None):
        """Prints a prefixed message to the console."""
        parts = [
            cformat('%%{%s}{}%%{reset}' % prefix_color).format(prefix),
            cformat('%{white!}{:>6s}%{reset}').format(unicode(event_id)) if event_id is not None else None,
            msg
        ]
        self.print_msg(' '.join(filter(None, parts)), always)

    def print_info(self, msg, always=False, has_event=True):
        """Prints an info message to the console.

        By default, info messages are not shown in quiet mode.
        They are prefixed with blank spaces to align with other
        messages.

        When calling this in a loop that is invoked a lot, it is
        recommended to add an explicit ``if not self.quiet`` check
        to avoid expensive `cformat` or `format` calls for a message
        that is never displayed.
        """
        self.print_msg(' ' * (11 if has_event else 4) + msg, always)

    def print_success(self, msg, always=False, event_id=None):
        """Prints a success message to the console.

        By default, success messages are not shown in quiet mode.
        They are prefixed with three green plus signs.

        When calling this in a loop that is invoked a lot, it is
        recommended to add an explicit ``if not self.quiet`` check
        to avoid expensive `cformat` or `format` calls for a message
        that is never displayed.
        """
        self.print_prefixed('+++', 'green', msg, always, event_id)

    def print_warning(self, msg, always=True, event_id=None):
        """Prints a warning message to the console.

        By default, warnings are displayed even in quiet mode.
        Warning messages are with three yellow exclamation marks.
        """
        self.print_prefixed('!!!', 'yellow!', msg, always, event_id)

    def print_error(self, msg, event_id=None):
        """Prints an error message to the console

        Errors are always displayed, even in quiet mode.
        They are prefixed with three red exclamation marks.
        """
        self.print_prefixed('!!!', 'red!', msg, True, event_id)


def main():
    return cli()
