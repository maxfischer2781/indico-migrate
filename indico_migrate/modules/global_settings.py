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

import re

from ipaddress import ip_network

from indico.core.db import db
from indico.modules.api import settings as api_settings
from indico.modules.categories import upcoming_events_settings
from indico.modules.core.settings import core_settings, social_settings
from indico.modules.events.payment import settings as payment_settings
from indico.modules.legal import legal_settings
from indico.modules.networks.models.networks import IPNetworkGroup
from indico.modules.users import user_management_settings
from indico.util.console import cformat

from indico_migrate import Importer, convert_to_unicode


class GlobalSettingsImporter(Importer):

    @property
    def makac_info(self):
        return self.zodb_root['MaKaCInfo']['main']

    def migrate(self):
        self.migrate_global_ip_acl()
        self.migrate_networks()
        self.migrate_api_settings()
        self.migrate_global_settings()
        self.migrate_upcoming_event_settings()
        self.migrate_user_management_settings()
        self.migrate_legal_settings()
        self.migrate_payment_settings()
        db.session.commit()

    def migrate_api_settings(self):
        self.print_step('API settings')
        settings_map = {
            '_apiHTTPSRequired': 'require_https',
            '_apiPersistentAllowed': 'allow_persistent',
            '_apiMode': 'security_mode',
            '_apiCacheTTL': 'cache_ttl',
            '_apiSignatureTTL': 'signature_ttl'
        }
        for old, new in settings_map.iteritems():
            api_settings.set(new, getattr(self.makac_info, old))

    def migrate_global_settings(self):
        self.print_step('Migrating global settings')
        core_settings.set_multi({
            'site_title': convert_to_unicode(self.makac_info._title),
            'site_organization': convert_to_unicode(self.makac_info._organisation),
            'custom_template_set': convert_to_unicode(self.makac_info._defaultTemplateSet) or None
        })
        social_settings.set_multi({
            'enabled': bool(self.makac_info._socialAppConfig['active']),
            'facebook_app_id': convert_to_unicode(self.makac_info._socialAppConfig['facebook']['appId'])
        })

    def migrate_upcoming_event_settings(self):
        self.print_step('Upcoming event settings')
        mod = self.zodb_root['modules']['upcoming_events']
        upcoming_events_settings.set('max_entries', int(mod._maxEvents))
        entries = [{'weight': float(entry.weight),
                    'days': entry.advertisingDelta.days,
                    'type': 'category' if type(entry.obj).__name__ == 'Category' else 'event',
                    'id': int(entry.obj.id)}
                   for entry in mod._objects]
        upcoming_events_settings.set('entries', entries)

    def migrate_user_management_settings(self):
        self.print_step('User management settings')
        settings_dict = {
            '_notifyAccountCreation': 'notify_account_creation'
        }

        for old_setting_name, new_setting_name in settings_dict.iteritems():
            user_management_settings.set(new_setting_name, getattr(self.makac_info, old_setting_name))

    def migrate_legal_settings(self):
        self.print_step('Legal settings')
        settings_map = {
            '_protectionDisclaimerProtected': 'network_protected_disclaimer',
            '_protectionDisclaimerRestricted': 'restricted_disclaimer'
        }
        for old, new in settings_map.iteritems():
            legal_settings.set(new, convert_to_unicode(getattr(self.makac_info, old)))

    def migrate_payment_settings(self):
        self.print_step('Payment settings')

        currency_opt = self.zodb_root['plugins']['EPayment']._PluginBase__options['customCurrency']
        currencies = [{'code': oc['abbreviation'], 'name': oc['name']} for oc in currency_opt._PluginOption__value]

        payment_settings.set('currencies', currencies)
        for currency in currencies:
            self.print_info(("saving currency: name='{name}', code={code}").format(**currency))

        db.session.commit()

    def migrate_global_ip_acl(self):
        self.print_step('Global IP acl')
        ip_networks = filter(None, map(self._to_network, self.makac_info._ip_based_acl_mgr._full_access_acl))
        if not ip_networks:
            self.print_error(cformat('%{red}No valid IPs found'))
            return
        network = IPNetworkGroup(name='Full Attachment Access', hidden=True, attachment_access_override=True,
                                 description='IPs that can access all attachments without authentication',
                                 networks=ip_networks)
        db.session.add(network)
        db.session.flush()
        self.print_success(repr(network), always=True)

    def migrate_networks(self):
        self.print_step('Networks')
        for domain in self._iter_domains():
            ip_networks = filter(None, map(self._to_network, set(domain.filterList)))
            if not ip_networks:
                self.print_warning(cformat('%{yellow}Domain has no valid IPs: {}')
                                   .format(convert_to_unicode(domain.name)))
            network = IPNetworkGroup(name=convert_to_unicode(domain.name),
                                     description=convert_to_unicode(domain.description), networks=ip_networks)
            db.session.add(network)
            self.print_success(repr(network))
        db.session.flush()

    def _iter_domains(self):
        return self.zodb_root['domains'].itervalues()

    def _to_network(self, mask):
        mask = convert_to_unicode(mask).strip()
        net = None
        if re.match(r'^[0-9.]+$', mask):
            # ipv4 mask
            mask = mask.rstrip('.')
            segments = mask.split('.')
            if len(segments) <= 4:
                addr = '.'.join(segments + ['0'] * (4 - len(segments)))
                net = ip_network('{}/{}'.format(addr, 8 * len(segments)))
        elif re.match(r'^[0-9a-f:]+', mask):
            # ipv6 mask
            mask = mask.rstrip(':')  # there shouldn't be a `::` in the IP as it was a startswith-like check before
            segments = mask.split(':')
            if len(segments) <= 8:
                addr = ':'.join(segments + ['0'] * (8 - len(segments)))
                net = ip_network('{}/{}'.format(addr, 16 * len(segments)))
        if net is None:
            self.print_warning(cformat('%{yellow!}Skipped invalid mask: {}').format(mask))
        return net
