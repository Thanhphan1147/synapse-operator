#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Synapse on kubernetes."""


import logging
import re
import typing

import ops
from charms.nginx_ingress_integrator.v0.nginx_route import require_nginx_route
from charms.redis_k8s.v0.redis import RedisRelationCharmEvents
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from ops import main
from ops.charm import ActionEvent, RelationDepartedEvent

import actions
import pebble
import synapse
from admin_access_token import AdminAccessTokenService
from backup_observer import BackupObserver
from charm_state import CharmBaseWithState, CharmState, inject_charm_state
from database_observer import DatabaseObserver
from matrix_auth_observer import MatrixAuthObserver
from media_observer import MediaObserver
from mjolnir import Mjolnir
from observability import Observability
from redis_observer import RedisObserver
from saml_observer import SAMLObserver
from smtp_observer import SMTPObserver
from user import User

logger = logging.getLogger(__name__)

MAIN_UNIT_ID = "main_unit_id"
INGRESS_INTEGRATION_NAME = "ingress"


class SynapseCharm(CharmBaseWithState):
    """Charm the service.

    Attrs:
        on: listen to Redis events.
    """

    # This class has several instance attributes like observers and libraries.
    # Consider refactoring if more attributes are added.
    # pylint: disable=too-many-instance-attributes
    on = RedisRelationCharmEvents()

    def __init__(self, *args: typing.Any) -> None:
        """Construct.

        Args:
            args: class arguments.
        """
        super().__init__(*args)
        self._backup = BackupObserver(self)
        self._matrix_auth = MatrixAuthObserver(self)
        self._media = MediaObserver(self)
        self._database = DatabaseObserver(self, relation_name=synapse.SYNAPSE_DB_RELATION_NAME)
        self._saml = SAMLObserver(self)
        self._smtp = SMTPObserver(self)
        self._redis = RedisObserver(self)
        self.token_service = AdminAccessTokenService(app=self.app, model=self.model)
        # service-hostname is a required field so we're hardcoding to the same
        # value as service-name. service-hostname should be set via Nginx
        # Ingress Integrator charm config.
        require_nginx_route(
            charm=self,
            service_hostname=self.app.name,
            service_name=self.app.name,
            service_port=synapse.SYNAPSE_NGINX_PORT,
        )
        self._ingress = IngressPerAppRequirer(
            charm=self,
            relation_name=INGRESS_INTEGRATION_NAME,
            port=synapse.SYNAPSE_NGINX_PORT,
        )
        self._observability = Observability(self)
        self._mjolnir = Mjolnir(self, token_service=self.token_service)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)
        self.framework.observe(
            self.on[synapse.SYNAPSE_PEER_RELATION_NAME].relation_departed,
            self._on_relation_departed,
        )
        self.framework.observe(
            self.on[synapse.SYNAPSE_PEER_RELATION_NAME].relation_changed, self._on_relation_changed
        )
        self.framework.observe(self.on.synapse_pebble_ready, self._on_synapse_pebble_ready)
        self.framework.observe(self.on.register_user_action, self._on_register_user_action)
        self.framework.observe(
            self.on.promote_user_admin_action, self._on_promote_user_admin_action
        )
        self.framework.observe(self.on.anonymize_user_action, self._on_anonymize_user_action)

    def build_charm_state(self) -> CharmState:
        """Build charm state.

        Returns:
            The current charm state.
        """
        return CharmState.from_charm(
            charm=self,
            datasource=self._database.get_relation_as_datasource(),
            saml_config=self._saml.get_relation_as_saml_conf(),
            smtp_config=self._smtp.get_relation_as_smtp_conf(),
            media_config=self._media.get_relation_as_media_conf(),
            redis_config=self._redis.get_relation_as_redis_conf(),
            registration_secrets=self._matrix_auth.get_requirer_registration_secrets(),
            instance_map_config=self.instance_map(),
        )

    def is_main(self) -> bool:
        """Verify if this unit is the main.

        Returns:
            bool: true if is the main unit.
        """
        return self.get_main_unit() == self.unit.name

    def get_unit_number(self, unit_name: str = "") -> str:
        """Get unit number from unit name.

        Args:
            unit_name: unit name or address. E.g.: synapse/0 or synapse-0.synapse-endpoints.

        Returns:
            Unit number. E.g.: 0
        """
        if not unit_name:
            unit_name = self.unit.name
        unit_part = unit_name.split(".")[0]
        index = unit_part.rfind("/")  # synapse/0 pattern
        if index == -1:
            index = unit_part.rfind("-")  # synapse-0 pattern
        begin = index + 1
        unit_id = unit_part[begin:]
        logger.debug("Unit id from %s is %s", unit_name, unit_id)
        return unit_id

    def instance_map(self) -> typing.Optional[typing.Dict]:
        """Build instance_map config.

        Returns:
            Instance map configuration as a dict or None if there is only one unit.
        """
        if self.peer_units_total() == 1:
            logger.debug("Only 1 unit found, skipping instance_map.")
            return None
        unit_name = self.unit.name.replace("/", "-")
        app_name = self.app.name
        addresses = [f"{unit_name}.{app_name}-endpoints"]
        peer_relation = self.model.relations[synapse.SYNAPSE_PEER_RELATION_NAME]
        if peer_relation:
            relation = peer_relation[0]
            # relation.units will contain the units after the relation-joined event.
            # since a relation-changed is emitted for every relation-joined event,
            # the relation-changed handler will reconcile the configuration and
            # instance_map will be properly set.
            for u in relation.units:
                # <unit-name>.<app-name>-endpoints.<model-name>.svc.cluster.local
                unit_name = u.name.replace("/", "-")
                address = f"{unit_name}.{app_name}-endpoints"
                addresses.append(address)
        logger.debug("addresses values are: %s", str(addresses))
        instance_map = {
            "main": {"host": self.get_main_unit_address(), "port": 8035},
            "federationsender1": {"host": self.get_main_unit_address(), "port": 8034},
        }
        for address in addresses:
            match = re.search(r"-(\d+)", address)
            # A Juju unit name is s always named on the
            # pattern <application>/<unit ID>, where <application> is the name
            # of the application and the <unit ID> is its ID number.
            # https://juju.is/docs/juju/unit
            if address == self.get_main_unit_address():
                continue
            unit_number = match.group(1)  # type: ignore[union-attr]
            instance_name = f"worker{unit_number}"
            instance_map[instance_name] = {"host": address, "port": 8034}
        logger.debug("instance_map is: %s", str(instance_map))
        return instance_map

    def reconcile(self, charm_state: CharmState) -> None:
        """Reconcile Synapse configuration with charm state.

        This is the main entry for changes that require a restart.

        Args:
            charm_state: Instance of CharmState
        """
        if self.get_main_unit() is None and self.unit.is_leader():
            logging.debug("Change_config is setting main unit.")
            self.set_main_unit(self.unit.name)
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("Waiting for Synapse pebble")
            return
        self.model.unit.status = ops.MaintenanceStatus("Configuring Synapse")
        try:
            # check signing key
            signing_key_path = f"/data/{charm_state.synapse_config.server_name}.signing.key"
            signing_key_from_secret = self.get_signing_key()
            if signing_key_from_secret:
                logger.debug("Signing key secret was found, pushing it to the container")
                container.push(
                    signing_key_path, signing_key_from_secret, make_dirs=True, encoding="utf-8"
                )

            # reconcile configuration
            pebble.reconcile(
                charm_state, container, is_main=self.is_main(), unit_number=self.get_unit_number()
            )

            # create new signing key if needed
            if self.is_main() and not signing_key_from_secret:
                logger.debug("Signing key secret not found, creating secret")
                with container.pull(signing_key_path) as f:
                    signing_key = f.read()
                    self.set_signing_key(signing_key.rstrip())

            # update matrix-auth integration with configuration data
            if self.unit.is_leader():
                self._matrix_auth.update_matrix_auth_integration(charm_state)
        except (pebble.PebbleServiceError, FileNotFoundError) as exc:
            self.model.unit.status = ops.BlockedStatus(str(exc))
            return
        pebble.restart_nginx(container, self.get_main_unit_address())
        self._set_unit_status()

    def _set_unit_status(self) -> None:
        """Set unit status depending on Synapse and NGINX state."""
        # This method contains a similar check that the one in mjolnir.py for Synapse
        # container and service. Until a refactoring is done for a different way of checking
        # and setting the unit status in a hollistic way, both checks will be in place.
        # pylint: disable=R0801

        # If the unit is in a blocked state, do not change it, as it
        # was set by a problem or error with the configuration
        if isinstance(self.unit.status, ops.BlockedStatus):
            return
        # Synapse checks
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("Waiting for Synapse pebble")
            return
        synapse_service = container.get_services(synapse.SYNAPSE_SERVICE_NAME)
        synapse_not_active = [
            service for service in synapse_service.values() if not service.is_running()
        ]
        if not synapse_service or synapse_not_active:
            self.unit.status = ops.MaintenanceStatus("Waiting for Synapse")
            return
        # NGINX checks
        nginx_service = container.get_services(synapse.SYNAPSE_NGINX_SERVICE_NAME)
        nginx_not_active = [
            service for service in nginx_service.values() if not service.is_running()
        ]
        if not nginx_service or nginx_not_active:
            self.unit.status = ops.MaintenanceStatus("Waiting for NGINX")
            return
        # All checks passed, the unit is active
        self.model.unit.status = ops.ActiveStatus()

    def _set_workload_version(self) -> None:
        """Set workload version with Synapse version."""
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("Waiting for Synapse pebble")
            return
        try:
            synapse_version = synapse.get_version(self.get_main_unit_address())
            self.unit.set_workload_version(synapse_version)
        except synapse.APIError as exc:
            logger.debug("Cannot set workload version at this time: %s", exc)

    @inject_charm_state
    def _on_config_changed(self, _: ops.HookEvent, charm_state: CharmState) -> None:
        """Handle changed configuration.

        Args:
            charm_state: The charm state.
        """
        logger.debug("Found %d peer unit(s).", self.peer_units_total())
        if charm_state.redis_config is None and self.peer_units_total() > 1:
            logger.debug("More than 1 peer unit found. Redis is required.")
            self.unit.status = ops.BlockedStatus("Redis integration is required.")
            return
        logger.debug("_on_config_changed emitting reconcile")
        self.reconcile(charm_state)
        self._set_workload_version()

    @inject_charm_state
    def _on_relation_departed(self, event: RelationDepartedEvent, charm_state: CharmState) -> None:
        """Handle Synapse peer relation departed event.

        Args:
            event: relation departed event.
            charm_state: The charm state.
        """
        if event.departing_unit == self.unit:
            # there is no action for the departing unit
            return
        if (
            event.departing_unit
            and event.departing_unit.name == self.get_main_unit()
            and self.unit.is_leader()
        ):
            # Main is gone so I'm the leader and will be the new main
            self.set_main_unit(self.unit.name)
        # Call change_config to restart unit. By design,every change in the
        # number of workers requires restart.
        logger.debug("_on_relation_departed emitting reconcile")
        self.reconcile(charm_state)

    def peer_units_total(self) -> int:
        """Get peer units total.

        Returns:
            total of units in peer relation or None if there is no peer relation.
        """
        return self.app.planned_units()

    @inject_charm_state
    def _on_synapse_pebble_ready(self, _: ops.HookEvent, charm_state: CharmState) -> None:
        """Handle synapse pebble ready event.

        Args:
            charm_state: The charm state.
        """
        logger.debug("Found %d peer unit(s).", self.peer_units_total())
        if charm_state.redis_config is None and self.peer_units_total() > 1:
            logger.debug("More than 1 peer unit found. Redis is required.")
            self.unit.status = ops.BlockedStatus("Redis integration is required.")
            return
        self.unit.status = ops.ActiveStatus()
        logger.debug("_on_synapse_pebble_ready emitting reconcile")
        self.reconcile(charm_state)

    def get_main_unit(self) -> typing.Optional[str]:
        """Get main unit.

        Returns:
            main unit if main unit exists in peer relation data.
        """
        peer_relation = self.model.relations[synapse.SYNAPSE_PEER_RELATION_NAME]
        if not peer_relation:
            logger.error(
                "Failed to get main unit: no peer relation %s found",
                synapse.SYNAPSE_PEER_RELATION_NAME,
            )
            return None
        return peer_relation[0].data[self.app].get(MAIN_UNIT_ID)

    def get_main_unit_address(self) -> str:
        """Get main unit address. If main unit is None, use unit name.

        Returns:
            main unit address as unit-0.synapse-endpoints.
        """
        main_unit_name = self.get_main_unit()
        if main_unit_name is None:
            main_unit_name = self.unit.name
        main_unit_formatted = main_unit_name.replace("/", "-")
        return f"{main_unit_formatted}.{self.app.name}-endpoints"

    def set_main_unit(self, unit: str) -> None:
        """Create/Renew an admin access token and put it in the peer relation.

        Args:
            unit: Unit to be the main.
        """
        peer_relation = self.model.relations[synapse.SYNAPSE_PEER_RELATION_NAME]
        if not peer_relation:
            logger.error(
                "Failed to get main unit: no peer relation %s found",
                synapse.SYNAPSE_PEER_RELATION_NAME,
            )
        else:
            logging.info("Setting main unit to be %s", unit)
            peer_relation[0].data[self.app].update({MAIN_UNIT_ID: unit})

    def set_signing_key(self, signing_key: str) -> None:
        """Create secret with signing key content.

        Args:
            signing_key: signing key as string.
        """
        peer_relation = self.model.relations[synapse.SYNAPSE_PEER_RELATION_NAME]
        if not peer_relation:
            logger.error(
                "Failed to set signing key: no peer relation %s found",
                synapse.SYNAPSE_PEER_RELATION_NAME,
            )
            return

        if signing_key == self.get_signing_key():
            logger.info("Received signing key but there is no change, skipping")
            return
        if self.unit.is_leader():
            logger.debug("Adding signing key to secret: %s", signing_key)
            secret = self.app.add_secret({"secret-signing-key": signing_key})
            peer_relation[0].data[self.app].update(
                {"secret-signing-id": typing.cast(str, secret.id)}
            )

    def get_signing_key(self) -> typing.Optional[str]:
        """Get signing key from secret.

        Returns:
            Signing key as string or None if not found.
        """
        peer_relation = self.model.relations[synapse.SYNAPSE_PEER_RELATION_NAME]
        if not peer_relation:
            logger.error(
                "Failed to get signing key: no peer relation %s found",
                synapse.SYNAPSE_PEER_RELATION_NAME,
            )
            return None

        secret_id = peer_relation[0].data[self.app].get("secret-signing-id")
        if secret_id:
            try:
                secret = self.model.get_secret(id=secret_id)
                logging.debug(secret.get_content().get("secret-signing-key"))
                return secret.get_content().get("secret-signing-key")
            except (ops.model.SecretNotFoundError, ValueError, TypeError) as exc:
                logger.exception("Failed to get secret id %s: %s", secret_id, str(exc))
                del peer_relation[0].data[self.app]["secret-signing-id"]
        return None

    @inject_charm_state
    def _on_leader_elected(self, _: ops.HookEvent, charm_state: CharmState) -> None:
        """Handle Synapse leader elected event.

        This event handler will reconcile Synapse configuration after the following
        scenarios:
        - When the charm is deployed so the leader will be the main unit.
        - When the leader, for any reason, has changed so the leader unit will be the main.
        Once the peer data (main_unit_id) is changed, other units will emit reconcile and be
        properly configured.

        Args:
            charm_state: The charm state.
        """
        # assuming that this event will be fired only at the setup phase
        # check if main is already set if not, this unit will be the main
        if not self.unit.is_leader():
            return
        logging.debug(
            "_on_leader_elected received, main_unit is %s and will be set to %s",
            self.get_main_unit(),
            self.unit.name,
        )
        self.set_main_unit(self.unit.name)
        logger.debug("_on_leader_elected emitting reconcile")
        self.reconcile(charm_state)

    @inject_charm_state
    def _on_relation_changed(self, _: ops.HookEvent, charm_state: CharmState) -> None:
        """Handle Synapse peer relation changed event.

        This event handler will reconcile Synapse configuration and NGINX after the following
        scenarios:
        - A new unit joined the peer relation. A relation-changed event is emitted after a
        relation-joined event. The instance_map and stream_writers should be updated also workers
        must be restarted by design.
        - Main unit has changed. The instance_map, stream_writers and NGINX configuration should be
        updated and all remaining units restarted.

        Args:
            charm_state: The charm state.
        """
        logger.debug("_on_relation_changed emitting reconcile")
        self.reconcile(charm_state)

    def _on_register_user_action(self, event: ActionEvent) -> None:
        """Register user and report action result.

        Args:
            event: Event triggering the register user instance action.
        """
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            event.fail("Failed to connect to the container")
            return
        try:
            user = actions.register_user(
                container=container, username=event.params["username"], admin=event.params["admin"]
            )
        except actions.RegisterUserError as exc:
            event.fail(str(exc))
            return
        results = {"register-user": True, "user-password": user.password}
        event.set_results(results)

    @inject_charm_state
    def _on_promote_user_admin_action(self, event: ActionEvent, charm_state: CharmState) -> None:
        """Promote user admin and report action result.

        Args:
            event: Event triggering the promote user admin action.
            charm_state: The charm state.
        """
        results = {
            "promote-user-admin": False,
        }
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            event.fail("Failed to connect to the container")
            return
        try:
            admin_access_token = self.token_service.get(container)
            if not admin_access_token:
                event.fail("Failed to get admin access token")
                return
            username = event.params["username"]
            server = charm_state.synapse_config.server_name
            user = User(username=username, admin=True)
            synapse.promote_user_admin(
                user=user, server=server, admin_access_token=admin_access_token
            )
            results["promote-user-admin"] = True
        except synapse.APIError as exc:
            event.fail(str(exc))
            return
        event.set_results(results)

    @inject_charm_state
    def _on_anonymize_user_action(self, event: ActionEvent, charm_state: CharmState) -> None:
        """Anonymize user and report action result.

        Args:
            event: Event triggering the anonymize user action.
            charm_state: The charm state.
        """
        results = {
            "anonymize-user": False,
        }
        container = self.unit.get_container(synapse.SYNAPSE_CONTAINER_NAME)
        if not container.can_connect():
            event.fail("Container not yet ready. Try again later")
            return
        try:
            admin_access_token = self.token_service.get(container)
            if not admin_access_token:
                event.fail("Failed to get admin access token")
                return
            username = event.params["username"]
            server = charm_state.synapse_config.server_name
            user = User(username=username, admin=False)
            synapse.deactivate_user(
                user=user, server=server, admin_access_token=admin_access_token
            )
            results["anonymize-user"] = True
        except synapse.APIError:
            event.fail("Failed to anonymize the user. Check if the user is created and active.")
            return
        event.set_results(results)


if __name__ == "__main__":  # pragma: nocover
    main(SynapseCharm)
