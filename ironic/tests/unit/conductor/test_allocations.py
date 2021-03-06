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

"""Unit tests for functionality related to allocations."""

import mock
import oslo_messaging as messaging
from oslo_utils import uuidutils

from ironic.common import exception
from ironic.conductor import allocations
from ironic.conductor import manager
from ironic.conductor import task_manager
from ironic import objects
from ironic.tests.unit.conductor import mgr_utils
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


@mgr_utils.mock_record_keepalive
class AllocationTestCase(mgr_utils.ServiceSetUpMixin, db_base.DbTestCase):
    @mock.patch.object(manager.ConductorManager, '_spawn_worker',
                       autospec=True)
    def test_create_allocation(self, mock_spawn):
        # In this test we mock spawn_worker, so that the actual processing does
        # not happen, and the allocation stays in the "allocating" state.
        allocation = obj_utils.get_test_allocation(self.context,
                                                   extra={'test': 'one'})
        self._start_service()
        mock_spawn.reset_mock()

        res = self.service.create_allocation(self.context, allocation)

        self.assertEqual({'test': 'one'}, res['extra'])
        self.assertEqual('allocating', res['state'])
        self.assertIsNotNone(res['uuid'])
        self.assertEqual(self.service.conductor.id, res['conductor_affinity'])
        res = objects.Allocation.get_by_uuid(self.context, allocation['uuid'])
        self.assertEqual({'test': 'one'}, res['extra'])
        self.assertEqual('allocating', res['state'])
        self.assertIsNotNone(res['uuid'])
        self.assertEqual(self.service.conductor.id, res['conductor_affinity'])

        mock_spawn.assert_called_once_with(self.service,
                                           allocations.do_allocate,
                                           self.context, mock.ANY)

    def test_destroy_allocation_without_node(self):
        allocation = obj_utils.create_test_allocation(self.context)
        self.service.destroy_allocation(self.context, allocation)
        self.assertRaises(exception.AllocationNotFound,
                          objects.Allocation.get_by_uuid,
                          self.context, allocation['uuid'])

    def test_destroy_allocation_with_node(self):
        node = obj_utils.create_test_node(self.context)
        allocation = obj_utils.create_test_allocation(self.context,
                                                      node_id=node['id'])
        node.instance_uuid = allocation['uuid']
        node.allocation_id = allocation['id']
        node.save()

        self.service.destroy_allocation(self.context, allocation)
        self.assertRaises(exception.AllocationNotFound,
                          objects.Allocation.get_by_uuid,
                          self.context, allocation['uuid'])
        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertIsNone(node['instance_uuid'])
        self.assertIsNone(node['allocation_id'])

    def test_destroy_allocation_with_active_node(self):
        node = obj_utils.create_test_node(self.context,
                                          provision_state='active')
        allocation = obj_utils.create_test_allocation(self.context,
                                                      node_id=node['id'])
        node.instance_uuid = allocation['uuid']
        node.allocation_id = allocation['id']
        node.save()

        exc = self.assertRaises(messaging.rpc.ExpectedException,
                                self.service.destroy_allocation,
                                self.context, allocation)
        # Compare true exception hidden by @messaging.expected_exceptions
        self.assertEqual(exception.InvalidState, exc.exc_info[0])

        objects.Allocation.get_by_uuid(self.context, allocation['uuid'])
        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertEqual(allocation['uuid'], node['instance_uuid'])
        self.assertEqual(allocation['id'], node['allocation_id'])

    def test_destroy_allocation_with_transient_node(self):
        node = obj_utils.create_test_node(self.context,
                                          target_provision_state='active',
                                          provision_state='deploying')
        allocation = obj_utils.create_test_allocation(self.context,
                                                      node_id=node['id'])
        node.instance_uuid = allocation['uuid']
        node.allocation_id = allocation['id']
        node.save()

        exc = self.assertRaises(messaging.rpc.ExpectedException,
                                self.service.destroy_allocation,
                                self.context, allocation)
        # Compare true exception hidden by @messaging.expected_exceptions
        self.assertEqual(exception.InvalidState, exc.exc_info[0])

        objects.Allocation.get_by_uuid(self.context, allocation['uuid'])
        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertEqual(allocation['uuid'], node['instance_uuid'])
        self.assertEqual(allocation['id'], node['allocation_id'])

    def test_destroy_allocation_with_node_in_maintenance(self):
        node = obj_utils.create_test_node(self.context,
                                          provision_state='active',
                                          maintenance=True)
        allocation = obj_utils.create_test_allocation(self.context,
                                                      node_id=node['id'])
        node.instance_uuid = allocation['uuid']
        node.allocation_id = allocation['id']
        node.save()

        self.service.destroy_allocation(self.context, allocation)
        self.assertRaises(exception.AllocationNotFound,
                          objects.Allocation.get_by_uuid,
                          self.context, allocation['uuid'])
        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertIsNone(node['instance_uuid'])
        self.assertIsNone(node['allocation_id'])


@mock.patch('time.sleep', lambda _: None)
class DoAllocateTestCase(db_base.DbTestCase):
    def test_success(self):
        node = obj_utils.create_test_node(self.context,
                                          power_state='power on',
                                          resource_class='x-large',
                                          provision_state='available')
        allocation = obj_utils.create_test_allocation(self.context,
                                                      resource_class='x-large')

        allocations.do_allocate(self.context, allocation)

        allocation = objects.Allocation.get_by_uuid(self.context,
                                                    allocation['uuid'])
        self.assertIsNone(allocation['last_error'])
        self.assertEqual('active', allocation['state'])

        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertEqual(allocation['uuid'], node['instance_uuid'])
        self.assertEqual(allocation['id'], node['allocation_id'])

    def test_with_traits(self):
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   power_state='power on',
                                   resource_class='x-large',
                                   provision_state='available')
        node = obj_utils.create_test_node(self.context,
                                          uuid=uuidutils.generate_uuid(),
                                          power_state='power on',
                                          resource_class='x-large',
                                          provision_state='available')
        db_utils.create_test_node_traits(['tr1', 'tr2'], node_id=node.id)

        allocation = obj_utils.create_test_allocation(self.context,
                                                      resource_class='x-large',
                                                      traits=['tr2'])

        allocations.do_allocate(self.context, allocation)

        allocation = objects.Allocation.get_by_uuid(self.context,
                                                    allocation['uuid'])
        self.assertIsNone(allocation['last_error'])
        self.assertEqual('active', allocation['state'])

        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertEqual(allocation['uuid'], node['instance_uuid'])
        self.assertEqual(allocation['id'], node['allocation_id'])
        self.assertEqual(allocation['traits'], ['tr2'])

    def test_with_candidates(self):
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   power_state='power on',
                                   resource_class='x-large',
                                   provision_state='available')
        node = obj_utils.create_test_node(self.context,
                                          uuid=uuidutils.generate_uuid(),
                                          power_state='power on',
                                          resource_class='x-large',
                                          provision_state='available')

        allocation = obj_utils.create_test_allocation(
            self.context, resource_class='x-large',
            candidate_nodes=[node['uuid']])

        allocations.do_allocate(self.context, allocation)

        allocation = objects.Allocation.get_by_uuid(self.context,
                                                    allocation['uuid'])
        self.assertIsNone(allocation['last_error'])
        self.assertEqual('active', allocation['state'])

        node = objects.Node.get_by_uuid(self.context, node['uuid'])
        self.assertEqual(allocation['uuid'], node['instance_uuid'])
        self.assertEqual(allocation['id'], node['allocation_id'])
        self.assertEqual([node['uuid']], allocation['candidate_nodes'])

    @mock.patch.object(task_manager, 'acquire', autospec=True,
                       side_effect=task_manager.acquire)
    def test_nodes_filtered_out(self, mock_acquire):
        # Resource class does not match
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   resource_class='x-small',
                                   power_state='power off',
                                   provision_state='available')
        # Provision state is not available
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   resource_class='x-large',
                                   power_state='power off',
                                   provision_state='manageable')
        # Power state is undefined
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   resource_class='x-large',
                                   power_state=None,
                                   provision_state='available')
        # Maintenance mode is on
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   maintenance=True,
                                   resource_class='x-large',
                                   power_state='power off',
                                   provision_state='available')
        # Already associated
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   instance_uuid=uuidutils.generate_uuid(),
                                   resource_class='x-large',
                                   power_state='power off',
                                   provision_state='available')

        allocation = obj_utils.create_test_allocation(self.context,
                                                      resource_class='x-large')
        allocations.do_allocate(self.context, allocation)
        self.assertIn('no available nodes', allocation['last_error'])
        self.assertIn('x-large', allocation['last_error'])
        self.assertEqual('error', allocation['state'])

        # All nodes are filtered out on the database level.
        self.assertFalse(mock_acquire.called)

    @mock.patch.object(task_manager, 'acquire', autospec=True,
                       side_effect=task_manager.acquire)
    def test_nodes_locked(self, mock_acquire):
        self.config(node_locked_retry_attempts=2, group='conductor')
        node1 = obj_utils.create_test_node(self.context,
                                           uuid=uuidutils.generate_uuid(),
                                           maintenance=False,
                                           resource_class='x-large',
                                           power_state='power off',
                                           provision_state='available',
                                           reservation='example.com')
        node2 = obj_utils.create_test_node(self.context,
                                           uuid=uuidutils.generate_uuid(),
                                           resource_class='x-large',
                                           power_state='power off',
                                           provision_state='available',
                                           reservation='example.com')

        allocation = obj_utils.create_test_allocation(self.context,
                                                      resource_class='x-large')
        allocations.do_allocate(self.context, allocation)
        self.assertIn('could not reserve any of 2', allocation['last_error'])
        self.assertEqual('error', allocation['state'])

        self.assertEqual(6, mock_acquire.call_count)
        # NOTE(dtantsur): node are tried in random order by design, so we
        # cannot directly use assert_has_calls. Check that all nodes are tried
        # before going into retries (rather than each tried 3 times in a row).
        nodes = [call[0][1] for call in mock_acquire.call_args_list]
        for offset in (0, 2, 4):
            self.assertEqual(set(nodes[offset:offset + 2]),
                             {node1.uuid, node2.uuid})

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def test_nodes_changed_after_lock(self, mock_acquire):
        nodes = [obj_utils.create_test_node(self.context,
                                            uuid=uuidutils.generate_uuid(),
                                            resource_class='x-large',
                                            power_state='power off',
                                            provision_state='available')
                 for _ in range(5)]
        for node in nodes:
            db_utils.create_test_node_trait(trait='tr1', node_id=node.id)

        # Modify nodes in-memory so that they no longer match the allocation:

        # Resource class does not match
        nodes[0].resource_class = 'x-small'
        # Provision state is not available
        nodes[1].provision_state = 'deploying'
        # Maintenance mode is on
        nodes[2].maintenance = True
        # Already associated
        nodes[3].instance_uuid = uuidutils.generate_uuid()
        # Traits changed
        nodes[4].traits.objects[:] = []

        mock_acquire.side_effect = [
            mock.MagicMock(**{'__enter__.return_value.node': node})
            for node in nodes
        ]

        allocation = obj_utils.create_test_allocation(self.context,
                                                      resource_class='x-large',
                                                      traits=['tr1'])
        allocations.do_allocate(self.context, allocation)
        self.assertIn('all nodes were filtered out', allocation['last_error'])
        self.assertEqual('error', allocation['state'])

        # No retries for these failures.
        self.assertEqual(5, mock_acquire.call_count)

    @mock.patch.object(task_manager, 'acquire', autospec=True,
                       side_effect=task_manager.acquire)
    def test_nodes_candidates_do_not_match(self, mock_acquire):
        obj_utils.create_test_node(self.context,
                                   uuid=uuidutils.generate_uuid(),
                                   resource_class='x-large',
                                   power_state='power off',
                                   provision_state='available')
        # Resource class does not match
        node = obj_utils.create_test_node(self.context,
                                          uuid=uuidutils.generate_uuid(),
                                          power_state='power on',
                                          resource_class='x-small',
                                          provision_state='available')

        allocation = obj_utils.create_test_allocation(
            self.context, resource_class='x-large',
            candidate_nodes=[node['uuid']])

        allocations.do_allocate(self.context, allocation)
        self.assertIn('none of the requested nodes', allocation['last_error'])
        self.assertIn('x-large', allocation['last_error'])
        self.assertEqual('error', allocation['state'])

        # All nodes are filtered out on the database level.
        self.assertFalse(mock_acquire.called)
