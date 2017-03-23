# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Module for syncing RBAC definitions in the database with the ones from the filesystem.
"""

from st2common import log as logging
from st2common.models.db.auth import UserDB
from st2common.persistence.auth import User
from st2common.persistence.rbac import Role
from st2common.persistence.rbac import UserRoleAssignment
from st2common.persistence.rbac import PermissionGrant
from st2common.persistence.rbac import AuthBackendGroupToRoleMap
from st2common.services import rbac as rbac_services
from st2common.util.uid import parse_uid


LOG = logging.getLogger(__name__)

__all__ = [
    'RBACDefinitionsDBSyncer',
    'RBACRemoteGroupToRoleSyncer'
]


class RBACDefinitionsDBSyncer(object):
    """
    A class which makes sure that the role definitions and user role assignments in the database
    match ones specified in the role definition files.

    The class works by simply deleting all the obsolete roles (either removed or updated) and
    creating new roles (either new roles or one which have been updated).

    Note #1: Our current datastore doesn't support transactions or similar which means that with
    the current data model there is a short time frame during sync when the definitions inside the
    DB are out of sync with the ones in the file.

    Note #2: The operation of this class is idempotent meaning that if it's ran multiple time with
    the same dataset, the end result / outcome will be the same.
    """

    def sync(self, role_definition_apis, role_assignment_apis, group_to_role_map_apis):
        """
        Synchronize all the role definitions, user role assignments and remote group to local roles
        maps.
        """
        result = {}

        result['roles'] = self.sync_roles(role_definition_apis)
        result['role_assignments'] = self.sync_users_role_assignments(role_assignment_apis)
        result['group_to_role_maps'] = self.sync_group_to_role_maps(group_to_role_map_apis)

        return result

    def sync_roles(self, role_definition_apis):
        """
        Synchronize all the role definitions in the database.

        :param role_dbs: RoleDB objects for the roles which are currently in the database.
        :type role_dbs: ``list`` of :class:`RoleDB`

        :param role_definition_apis: RoleDefinition API objects for the definitions loaded from
                                     the files.
        :type role_definition_apis: ``list`` of :class:RoleDefinitionFileFormatAPI`

        :rtype: ``tuple``
        """
        LOG.info('Synchronizing roles...')

        # Retrieve all the roles currently in the DB
        role_dbs = rbac_services.get_all_roles(exclude_system=True)

        role_db_names = [role_db.name for role_db in role_dbs]
        role_db_names = set(role_db_names)
        role_api_names = [role_definition_api.name for role_definition_api in role_definition_apis]
        role_api_names = set(role_api_names)

        # A list of new roles which should be added to the database
        new_role_names = role_api_names.difference(role_db_names)

        # A list of roles which need to be updated in the database
        updated_role_names = role_db_names.intersection(role_api_names)

        # A list of roles which should be removed from the database
        removed_role_names = (role_db_names - role_api_names)

        LOG.debug('New roles: %r' % (new_role_names))
        LOG.debug('Updated roles: %r' % (updated_role_names))
        LOG.debug('Removed roles: %r' % (removed_role_names))

        # Build a list of roles to delete
        role_names_to_delete = updated_role_names.union(removed_role_names)
        role_dbs_to_delete = [role_db for role_db in role_dbs if
                              role_db.name in role_names_to_delete]

        # Build a list of roles to create
        role_names_to_create = new_role_names.union(updated_role_names)
        role_apis_to_create = [role_definition_api for role_definition_api in role_definition_apis
                               if role_definition_api.name in role_names_to_create]

        ########
        # 1. Remove obsolete roles and associated permission grants from the DB
        ########

        # Remove roles
        role_ids_to_delete = []
        for role_db in role_dbs_to_delete:
            role_ids_to_delete.append(role_db.id)

        LOG.debug('Deleting %s stale roles' % (len(role_ids_to_delete)))
        Role.query(id__in=role_ids_to_delete, system=False).delete()
        LOG.debug('Deleted %s stale roles' % (len(role_ids_to_delete)))

        # Remove associated permission grants
        permission_grant_ids_to_delete = []
        for role_db in role_dbs_to_delete:
            permission_grant_ids_to_delete.extend(role_db.permission_grants)

        LOG.debug('Deleting %s stale permission grants' % (len(permission_grant_ids_to_delete)))
        PermissionGrant.query(id__in=permission_grant_ids_to_delete).delete()
        LOG.debug('Deleted %s stale permission grants' % (len(permission_grant_ids_to_delete)))

        ########
        # 2. Add new / updated roles to the DB
        ########

        LOG.debug('Creating %s new roles' % (len(role_apis_to_create)))

        # Create new roles
        created_role_dbs = []
        for role_api in role_apis_to_create:
            role_db = rbac_services.create_role(name=role_api.name,
                                                description=role_api.description)

            # Create associated permission grants
            permission_grants = getattr(role_api, 'permission_grants', [])
            for permission_grant in permission_grants:
                resource_uid = permission_grant.get('resource_uid', None)

                if resource_uid:
                    resource_type, _ = parse_uid(resource_uid)
                else:
                    resource_type = None

                permission_types = permission_grant['permission_types']
                assignment_db = rbac_services.create_permission_grant(
                    role_db=role_db,
                    resource_uid=resource_uid,
                    resource_type=resource_type,
                    permission_types=permission_types)

                role_db.permission_grants.append(str(assignment_db.id))
            created_role_dbs.append(role_db)

        LOG.debug('Created %s new roles' % (len(created_role_dbs)))
        LOG.info('Roles synchronized (%s created, %s updated, %s removed)' %
                 (len(new_role_names), len(updated_role_names), len(removed_role_names)))

        return [created_role_dbs, role_dbs_to_delete]

    def sync_users_role_assignments(self, role_assignment_apis):
        """
        Synchronize role assignments for all the users in the database.

        :param role_assignment_apis: Role assignments API objects for the assignments loaded
                                      from the files.
        :type role_assignment_apis: ``list`` of :class:`UserRoleAssignmentFileFormatAPI`

        :return: Dictionary with created and removed role assignments for each user.
        :rtype: ``dict``
        """
        LOG.info('Synchronizing users role assignments...')

        user_dbs = User.get_all()
        username_to_user_db_map = dict([(user_db.name, user_db) for user_db in user_dbs])

        results = {}
        for role_assignment_api in role_assignment_apis:
            username = role_assignment_api.username
            user_db = username_to_user_db_map.get(username, None)

            if not user_db:
                # Note: We allow assignments to be created for the users which don't exist in the
                # DB yet because user creation in StackStorm is lazy (we only create UserDB) object
                # when user first logs in.
                user_db = UserDB(name=username)
                LOG.debug(('User "%s" doesn\'t exist in the DB, creating assignment anyway' %
                          (username)))

            role_assignment_dbs = rbac_services.get_role_assignments_for_user(user_db=user_db,
                                                                             include_remote=False)

            result = self._sync_user_role_assignments(user_db=user_db,
                                                      role_assignment_dbs=role_assignment_dbs,
                                                      role_assignment_api=role_assignment_api)
            results[username] = result

        LOG.info('User role assignments synchronized')
        return results

    def sync_group_to_role_maps(self, group_to_role_map_apis):
        LOG.info('Synchronizing group to role maps...')

        # Retrieve all the mappings currently in the db
        group_to_role_map_dbs = rbac_services.get_all_group_to_role_maps()

        # 1. Delete all the existing mappings in the db
        group_to_role_map_to_delete = []
        for group_to_role_map_db in group_to_role_map_dbs:
            group_to_role_map_to_delete.append(group_to_role_map_db.id)

        AuthBackendGroupToRoleMap.query(id__in=group_to_role_map_to_delete).delete()

        # 2. Insert all mappings read from disk
        for group_to_role_map_api in group_to_role_map_apis:
            rbac_services.create_group_to_role_map(group=group_to_role_map_api.group,
                                                   roles=group_to_role_map_api.roles,
                                                   description=group_to_role_map_api.description)

        LOG.info('Group to role map definitions synchronized.')

    def _sync_user_role_assignments(self, user_db, role_assignment_dbs, role_assignment_api):
        """
        Synchronize role assignments for a particular user.

        :param user_db: User to synchronize the assignments for.
        :type user_db: :class:`UserDB`

        :param role_assignment_dbs: Existing user role assignments.
        :type role_assignment_dbs: ``list`` of :class:`UserRoleAssignmentDB`

        :param role_assignment_api: Role assignment API for a particular user.
        :param role_assignment_api: :class:`UserRoleAssignmentFileFormatAPI`

        :rtype: ``tuple``
        """
        db_role_names = [role_assignment_db.role for role_assignment_db in role_assignment_dbs]
        db_role_names = set(db_role_names)
        api_role_names = role_assignment_api.roles if role_assignment_api else []
        api_role_names = set(api_role_names)

        # A list of new assignments which should be added to the database
        new_role_names = api_role_names.difference(db_role_names)

        # A list of assignments which need to be updated in the database
        updated_role_names = db_role_names.intersection(api_role_names)

        # A list of assignments which should be removed from the database
        removed_role_names = (db_role_names - api_role_names)

        LOG.debug('New assignments for user "%s": %r' % (user_db.name, new_role_names))
        LOG.debug('Updated assignments for user "%s": %r' % (user_db.name, updated_role_names))
        LOG.debug('Removed assignments for user "%s": %r' % (user_db.name, removed_role_names))

        # Build a list of role assignments to delete
        role_names_to_delete = updated_role_names.union(removed_role_names)
        role_assignment_dbs_to_delete = [role_assignment_db for role_assignment_db
                                         in role_assignment_dbs
                                         if role_assignment_db.role in role_names_to_delete]

        UserRoleAssignment.query(user=user_db.name, role__in=role_names_to_delete,
                                 is_remote=False).delete()
        LOG.debug('Removed %s assignments for user "%s"' %
                (len(role_assignment_dbs_to_delete), user_db.name))

        # Build a list of roles assignments to create
        role_names_to_create = new_role_names.union(updated_role_names)
        role_dbs_to_assign = Role.query(name__in=role_names_to_create)

        created_role_assignment_dbs = []
        for role_db in role_dbs_to_assign:
            if role_db.name in role_assignment_api.roles:
                description = getattr(role_assignment_api, 'description', None)
            else:
                description = None
            assignment_db = rbac_services.assign_role_to_user(role_db=role_db, user_db=user_db,
                                                              description=description)
            created_role_assignment_dbs.append(assignment_db)

        LOG.debug('Created %s new assignments for user "%s"' % (len(role_dbs_to_assign),
                                                                user_db.name))

        return (created_role_assignment_dbs, role_assignment_dbs_to_delete)


class RBACRemoteGroupToRoleSyncer(object):
    """
    Class which writes remote user role assignments based on the user group memberhip information
    provided by the auth backend and based on the group to role mapping definitions on disk.
    """

    def sync(self, user_db, groups):
        """
        :param user_db: User to sync the assignments for.
        :type user: :class:`UserDB`

        :param groups: A list of remote groups user is a member of.
        :type groups: ``list`` of ``str``

        :return: A list of mappings which have been created.
        :rtype: ``list`` of :class:`UserRoleAssignmentDB`
        """
        groups = list(set(groups))

        extra = {'user_db': user_db, 'groups': groups}
        LOG.info('Synchronizing remote role assignments for user "%s"' % (str(user_db)),
                 extra=extra)

        # 1. Retrieve group to role mappings for the provided groups
        mapping_dbs = AuthBackendGroupToRoleMap.query(group__in=groups)

        if not mapping_dbs:
            # No mapping found, return early
            LOG.debug('No group to role mappings found for user "%s"' % (str(user_db)),
                      extra=extra)
            return ([], [])

        # 2. Remove all the existing remote role assignments
        remote_assignment_dbs = UserRoleAssignment.query(user=user_db.name, is_remote=True)

        existing_role_names = [assignment_db.role for assignment_db in remote_assignment_dbs]
        existing_role_names = set(existing_role_names)
        current_role_names = set([])

        for mapping_db in mapping_dbs:
            for role in mapping_db.roles:
                current_role_names.add(role)

        # A list of new role assignments which should be added to the database
        new_role_names = current_role_names.difference(existing_role_names)

        # A list of role assignments which need to be updated in the database
        updated_role_names = existing_role_names.intersection(current_role_names)

        # A list of role assignments which should be removed from the database
        removed_role_names = (existing_role_names - new_role_names)

        LOG.debug('New role assignments: %r' % (new_role_names))
        LOG.debug('Updated role assignments: %r' % (updated_role_names))
        LOG.debug('Removed role assignments: %r' % (removed_role_names))

        # Build a list of role assignments to delete
        role_names_to_delete = updated_role_names.union(removed_role_names)
        role_assignment_dbs_to_delete = [role_assignment_db for role_assignment_db
                                         in remote_assignment_dbs
                                         if role_assignment_db.role in role_names_to_delete]

        UserRoleAssignment.query(user=user_db.name, role__in=role_names_to_delete,
                                 is_remote=True).delete()

        # 3. Create role assignments for all the current groups
        created_assignments_dbs = []
        for mapping_db in mapping_dbs:
            extra['mapping_db'] = mapping_db

            for role_name in mapping_db.roles:
                role_db = rbac_services.get_role_by_name(name=role_name)

                if not role_db:
                    # Gracefully skip assignment for role which doesn't exist in the db
                    LOG.info('Role with name "%s" for mapping "%s" not found, skipping assignment.'
                             % (role_name, str(mapping_db)), extra=extra)
                    continue

                description = ('Automatic role assignment based on the remote user membership in '
                               'group "%s"' % (mapping_db.group))
                assignment_db = rbac_services.assign_role_to_user(role_db=role_db, user_db=user_db,
                                                                  description=description,
                                                                  is_remote=True)
                assert assignment_db.is_remote is True
                created_assignments_dbs.append(assignment_db)

        LOG.debug('Created %s new remote role assignments for user "%s"' %
                  (len(created_assignments_dbs), str(user_db)), extra=extra)

        return (created_assignments_dbs, role_assignment_dbs_to_delete)
