# Copyright 2013 New Dream Network, LLC (DreamHost)
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import mock

from neutron import context
from neutron.db import servicetype_db as st_db
from neutron import manager
from neutron.plugins.common import constants

from neutron_lbaas.db.loadbalancer import models
from neutron_lbaas.drivers.common import agent_driver_base
from neutron_lbaas.extensions import loadbalancerv2
from neutron_lbaas.tests import base
from neutron_lbaas.tests.unit.db.loadbalancer import test_db_loadbalancerv2


class TestLoadBalancerPluginBase(test_db_loadbalancerv2.LbaasPluginDbTestCase):

    def setUp(self):
        def reset_device_driver():
            agent_driver_base.AgentDriverBase.device_driver = None
        self.addCleanup(reset_device_driver)

        self.mock_importer = mock.patch.object(
            agent_driver_base, 'importutils').start()

        # needed to reload provider configuration
        st_db.ServiceTypeManager._instance = None
        agent_driver_base.AgentDriverBase.device_driver = 'dummy'
        super(TestLoadBalancerPluginBase, self).setUp(
            lbaas_provider=('LOADBALANCERV2:lbaas:neutron_lbaas.drivers.'
                            'common.agent_driver_base.'
                            'AgentDriverBase:default'))

        # we need access to loaded plugins to modify models
        loaded_plugins = manager.NeutronManager().get_service_plugins()

        self.plugin_instance = loaded_plugins[constants.LOADBALANCERV2]


class TestLoadBalancerAgentApi(base.BaseTestCase):
    def setUp(self):
        super(TestLoadBalancerAgentApi, self).setUp()

        self.api = agent_driver_base.LoadBalancerAgentApi('topic')

    def test_init(self):
        self.assertEqual(self.api.client.target.topic, 'topic')

    def _call_test_helper(self, method_name, method_args):
        with contextlib.nested(
            mock.patch.object(self.api.client, 'cast'),
            mock.patch.object(self.api.client, 'prepare'),
        ) as (
            rpc_mock, prepare_mock
        ):
            prepare_mock.return_value = self.api.client
            getattr(self.api, method_name)(mock.sentinel.context,
                                           host='host',
                                           **method_args)

        prepare_args = {'server': 'host'}
        prepare_mock.assert_called_once_with(**prepare_args)

        if method_name == 'agent_updated':
            method_args = {'payload': method_args}
        rpc_mock.assert_called_once_with(mock.sentinel.context, method_name,
                                         **method_args)

    def test_agent_updated(self):
        self._call_test_helper('agent_updated', {'admin_state_up': 'test'})

    def test_create_loadbalancer(self):
        self._call_test_helper('create_loadbalancer', {'loadbalancer': 'test',
                                                       'driver_name': 'dummy'})

    def test_update_loadbalancer(self):
        self._call_test_helper('update_loadbalancer', {
            'old_loadbalancer': 'test', 'loadbalancer': 'test'})

    def test_delete_loadbalancer(self):
        self._call_test_helper('delete_loadbalancer', {'loadbalancer': 'test'})


class TestLoadBalancerPluginNotificationWrapper(TestLoadBalancerPluginBase):
    def setUp(self):
        self.log = mock.patch.object(agent_driver_base, 'LOG')
        api_cls = mock.patch.object(agent_driver_base,
                                    'LoadBalancerAgentApi').start()
        super(TestLoadBalancerPluginNotificationWrapper, self).setUp()
        self.mock_api = api_cls.return_value

        self.mock_get_driver = mock.patch.object(self.plugin_instance,
                                                 '_get_driver')
        self.mock_get_driver.return_value = (
            agent_driver_base.AgentDriverBase(self.plugin_instance))

    def _update_status(self, model, status, id):
        ctx = context.get_admin_context()
        self.plugin_instance.db.update_status(
            ctx,
            model,
            id,
            provisioning_status=status
        )

    def test_create_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            calls = self.mock_api.create_loadbalancer.call_args_list
            self.assertEqual(1, len(calls))
            _, called_lb, _, device_driver = calls[0][0]
            self.assertEqual(loadbalancer['loadbalancer']['id'], called_lb.id)
            self.assertEqual('dummy', device_driver)
            self.assertEqual(constants.PENDING_CREATE,
                             called_lb.provisioning_status)

    def test_update_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            lb_id = loadbalancer['loadbalancer']['id']
            old_lb_name = loadbalancer['loadbalancer']['name']
            ctx = context.get_admin_context()
            self.plugin_instance.db.update_loadbalancer_provisioning_status(
                ctx,
                loadbalancer['loadbalancer']['id'])
            new_lb_name = 'new_lb_name'
            loadbalancer['loadbalancer']['name'] = new_lb_name
            self._update_loadbalancer_api(
                lb_id, {'loadbalancer': {'name': new_lb_name}})
            calls = self.mock_api.update_loadbalancer.call_args_list
            self.assertEqual(1, len(calls))
            _, called_old_lb, called_new_lb, called_host = calls[0][0]
            self.assertEqual(lb_id, called_old_lb.id)
            self.assertEqual(lb_id, called_new_lb.id)
            self.assertEqual(old_lb_name, called_old_lb.name)
            self.assertEqual(new_lb_name, called_new_lb.name)
            self.assertEqual('host', called_host)
            self.assertEqual(constants.PENDING_UPDATE,
                             called_new_lb.provisioning_status)

    def test_delete_loadbalancer(self):
        with self.loadbalancer(no_delete=True) as loadbalancer:
            lb_id = loadbalancer['loadbalancer']['id']
            ctx = context.get_admin_context()
            self._update_status(models.LoadBalancer, constants.ACTIVE, lb_id)
            self.plugin_instance.delete_loadbalancer(ctx, lb_id)
            calls = self.mock_api.delete_loadbalancer.call_args_list
            self.assertEqual(1, len(calls))
            _, called_lb, called_host = calls[0][0]
            self.assertEqual(lb_id, called_lb.id)
            self.assertEqual('host', called_host)
            self.assertEqual(constants.PENDING_DELETE,
                             called_lb.provisioning_status)
            self.assertRaises(loadbalancerv2.EntityNotFound,
                              self.plugin_instance.db.get_loadbalancer,
                              ctx, lb_id)