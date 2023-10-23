# BSD 3-Clause License
#
# Copyright (c) 2023, BISDN GmbH
# All rights reserved.

#!/usr/bin/env python3

"""shard manager module"""

# pylint: disable=no-member
# pylint: disable=too-many-locals
# pylint: disable=too-many-nested-blocks
# pylint: disable=too-many-statements
# pylint: disable=too-many-branches

import os
import sys
import enum
import time
import hashlib
import logging
import argparse
import socket
import pathlib
import contextlib
import threading
import traceback
import ipaddress
import yaml

from upsf_client.upsf import (
    UPSF,
    UpsfError,
)
from upsf_client.endpoint import (
    Endpoint,
)


class DerivedState(enum.Enum):
    """DerivedState"""

    UNKNOWN = 0
    INACTIVE = 1
    ACTIVE = 2
    UPDATING = 3
    DELETING = 4
    DELETED = 5


def str2bool(value):
    """map string to boolean value"""
    return value.lower() in [
        "true",
        "1",
        "t",
        "y",
        "yes",
    ]


class ShardManager(threading.Thread):
    """class ShardManager"""

    _defaults = {
        # upsf host, default: 127.0.0.1
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        # upsf port, default: 50051
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        # configuration file, default: /etc/upsf/policy.yaml
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yaml"),
        # virtual max assigned to shard default
        "virtual_mac": os.environ.get("VIRTUAL_MAC", "00:00:01:00:00:00"),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
        # loglevel, default: 'info'
        "loglevel": os.environ.get("LOGLEVEL", "info"),
    }

    _loglevels = {
        "critical": logging.CRITICAL,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "error": logging.ERROR,
        "debug": logging.DEBUG,
    }

    def __init__(self, **kwargs):
        """__init__"""
        threading.Thread.__init__(self)
        self._stop_thread = threading.Event()
        self._lock = threading.RLock()
        # background thread stop event
        self._stop_thread = threading.Event()

        self.initialize(**kwargs)

    def initialize(self, **kwargs):
        """initialize"""
        for key, value in self._defaults.items():
            setattr(self, key, kwargs.get(key, value))

        # logger
        self._log = logging.getLogger(__name__)
        self._log.setLevel(self._loglevels[self.loglevel])

        # upsf client
        self._upsf = UPSF(
            upsf_host=self.upsf_host,
            upsf_port=self.upsf_port,
        )

        # create shard for each sgup
        self.create_default_items()
        self._upsf_auto_register = None
        # create predefined shards
        if str2bool(self.upsf_auto_register):
            self._upsf_auto_register = threading.Thread(
                target=ShardManager.upsf_register_task,
                kwargs={
                    "entity": self,
                    "interval": self.registration_interval,
                },
                daemon=True,
            )
            self._upsf_auto_register.start()

        # map shards
        self.map_shards()

    def __str__(self):
        """return simple string"""
        return f"{self.__class__.__name__}()"

    def __repr__(self):
        """return descriptive string"""
        _attributes = "".join(
            [
                f"{key}={getattr(self, key, None)}, "
                for key, value in self._defaults.items()
            ]
        )
        return f"{self.__class__.__name__}({_attributes})"

    @property
    def log(self):
        """return read-only logger"""
        return self._log

    def shard_dump(self):
        """dump all shards"""
        # upsf client
        _upsf = UPSF(
            upsf_host=self.upsf_host,
            upsf_port=self.upsf_port,
        )

        for shard in _upsf.list_shards():
            self.log.debug(
                {
                    "entity": str(self),
                    "event": "shard_dump",
                    "derived_state": DerivedState(
                        shard.metadata.derived_state
                    ).name.lower(),
                    "shard.name": shard.name,
                    "shard.desired_up": shard.spec.desired_state.service_gateway_user_plane,
                    "shard.current_up": shard.status.current_state.service_gateway_user_plane,
                }
            )

    def map_shards(self):
        """assign a user plane to each shard without desired
        or invalid service gateway user plane"""

        try:
            # get all service gateways
            sgs = self._upsf.list_service_gateways()

            # get all service gateway user planes
            sgups = self._upsf.list_service_gateway_user_planes()

            # get all shards
            shards = self._upsf.list_shards()

            # no user planes at all, remove all shards
            if len(sgups) == 0:
                self.log.warning(
                    {
                        "entity": str(self),
                        "event": "map_shards: no user planes available, reset all shards",
                    }
                )

                # reset all shards, i.e. remove network connections
                for shard in shards:
                    if shard.spec.desired_state.service_gateway_user_plane in (
                        "",
                        None,
                    ):
                        continue

                    # list merge strategy "replace" removes all network connections and mappings
                    params = {
                        "name": shard.name,
                        "prefix": list(shard.spec.prefix),
                        "list_merge_strategy": "replace",
                    }
                    self._upsf.update_shard(**params)

                return

            # get all traffic steering functions
            tsfs = self._upsf.list_traffic_steering_functions()

            # get all network connections
            ncs = self._upsf.list_network_connections()

            # list of sg names
            sg_names = [sg.name for sg in sgs]

            # list of up names
            up_names = [up.name for up in sgups]

            # map shards to service gateway user planes
            for shard in shards:
                try:
                    # fingerprint for (old) existing desired state
                    finger_active = (
                        shard.spec.desired_state.service_gateway_user_plane
                        + "/"
                        + ",".join(shard.spec.desired_state.network_connection)
                    )
                    fp_active = hashlib.sha256(
                        finger_active.encode("ascii")
                    ).hexdigest()

                    # old user plane if any
                    up_name = shard.spec.desired_state.service_gateway_user_plane

                    # static pinning for shard?
                    static_up_name = self.get_static_shard_to_sgup_mapping(shard.name)

                    # need up mapping
                    if (static_up_name is not None and (up_name != static_up_name)) or (
                        up_name not in up_names
                    ):
                        # static pinning for shard?
                        if static_up_name is not None:
                            if static_up_name not in up_names:
                                self.log.warning(
                                    {
                                        "entity": str(self),
                                        "event": "map_shards: shard has static sgup mapping, "
                                        "but sgup is not available, ignoring",
                                        "shard": shard.name,
                                        "sgup": static_up_name,
                                    }
                                )
                                return
                            self.log.info(
                                {
                                    "entity": str(self),
                                    "event": "map_shards: shard has static mapping",
                                    "shard": shard.name,
                                    "sgup.selected": static_up_name,
                                }
                            )
                            up_name = static_up_name

                        # select new sgup from suitable candidates
                        else:
                            # get load on all sgups
                            sgup_load = {
                                sgup.name: sgup.status.allocated_session_count
                                / sgup.spec.max_session_count
                                for sgup in sgups
                                if sgup.spec.max_session_count > 0
                                and sgup.service_gateway_name in sg_names
                            }

                            # sanity check: empty dict?
                            if len(sgup_load) == 0:
                                self.log.warning(
                                    {
                                        "entity": str(self),
                                        "event": "map_shards: set of sgup candidates is empty",
                                        "shard": shard.name,
                                        "sgup_load": sgup_load,
                                    }
                                )
                                continue

                            # get least loaded sgup
                            up_name = min(sgup_load, key=sgup_load.get)

                            self.log.info(
                                {
                                    "entity": str(self),
                                    "event": "map_shards: selected new user plane",
                                    "shard": shard.name,
                                    "sgup_load": sgup_load,
                                    "sgup.selected": up_name,
                                }
                            )

                    # selected service gateway user plane
                    service_gateway_user_plane = (
                        self._upsf.get_service_gateway_user_plane(name=up_name)
                    )

                    # new user plane
                    up_name_next = service_gateway_user_plane.name

                    # sgup default endpoint name
                    up_ep_name = (
                        service_gateway_user_plane.spec.default_endpoint.endpoint_name
                    )

                    # network connections for chosen service gateway user plane
                    desired_network_connection = set()

                    # tsf network connections
                    tsf_network_connection = {}

                    # strategy: we need a network connection from each TSF to the selected UP
                    for tsf in tsfs:
                        # tsf default endpoint name
                        tsf_ep_name = tsf.spec.default_endpoint.endpoint_name

                        # for all network connections ...
                        for network_connection in ncs:
                            # get network connection type
                            nc_spec_type = network_connection.spec.WhichOneof("nc_spec")

                            # SS-PTP
                            if nc_spec_type in (
                                "SsPtpSpec",
                                "ss_ptp",
                            ):
                                tsf_ep = Endpoint(
                                    network_connection.spec.ss_ptp.tsf_endpoint
                                )
                                for up_ep in [
                                    Endpoint(ep)
                                    for ep in network_connection.spec.ss_ptp.sgup_endpoint
                                ]:
                                    # matching network connection
                                    if (
                                        up_ep_name == up_ep.name
                                        and tsf_ep_name == tsf_ep.name
                                    ):
                                        desired_network_connection.add(
                                            network_connection.name
                                        )
                                        tsf_network_connection[
                                            tsf.name
                                        ] = network_connection.name

                            # SS-MPTP
                            elif nc_spec_type in (
                                "SsMptpSpec",
                                "ss_mptpc",
                            ):
                                for up_ep in [
                                    Endpoint(ep)
                                    for ep in network_connection.spec.ss_mptp.sgup_endpoint
                                ]:
                                    for tsf_ep in [
                                        Endpoint(ep)
                                        for ep in network_connection.spec.ss_mptp.tsf_endpoint
                                    ]:
                                        # matching network connection
                                        if (
                                            up_ep_name == up_ep.name
                                            and tsf_ep_name == tsf_ep.name
                                        ):
                                            desired_network_connection.add(
                                                network_connection.name
                                            )
                                            tsf_network_connection[
                                                tsf.name
                                            ] = network_connection.name

                            # MS-PTP
                            elif nc_spec_type in (
                                "MsPtpSpec",
                                "ms_ptp",
                            ):
                                tsf_ep = Endpoint(
                                    network_connection.spec.ms_ptp.tsf_endpoint
                                )
                                up_ep = Endpoint(
                                    network_connection.spec.ms_ptp.sgup_endpoint
                                )
                                # matching network connection
                                if (
                                    up_ep_name == up_ep.name
                                    and tsf_ep_name == tsf_ep.name
                                ):
                                    desired_network_connection.add(
                                        network_connection.name
                                    )
                                    tsf_network_connection[
                                        tsf.name
                                    ] = network_connection.name

                            # MS-MPTP
                            elif nc_spec_type in (
                                "MsMptpSpec",
                                "ms_mptp",
                            ):
                                up_ep = Endpoint(
                                    network_connection.spec.ms_mptp.sgup_endpoint
                                )
                                for tsf_ep in [
                                    Endpoint(ep)
                                    for ep in network_connection.spec.ms_mptp.tsf_endpoint
                                ]:
                                    # ignore network connection with non-matching endpoints
                                    if (
                                        up_ep_name == up_ep.name
                                        and tsf_ep_name == tsf_ep.name
                                    ):
                                        desired_network_connection.add(
                                            network_connection.name
                                        )
                                        tsf_network_connection[
                                            tsf.name
                                        ] = network_connection.name

                    # fingerprint for (new) intended desired state
                    finger_desired = (
                        up_name_next + "/" + ",".join(desired_network_connection)
                    )
                    fp_desired = hashlib.sha256(
                        finger_desired.encode("ascii")
                    ).hexdigest()

                    # any change in desired state?
                    if fp_desired != fp_active:
                        params = {
                            "name": shard.name,
                            "desired_service_gateway_user_plane": service_gateway_user_plane.name,
                            "desired_network_connection": list(
                                desired_network_connection
                            ),
                            "current_tsf_network_connection": tsf_network_connection,
                            "service_groups_supported": [
                                sgs
                                for sgs in service_gateway_user_plane.spec.supported_service_group
                                if sgs
                                not in (
                                    "",
                                    None,
                                )
                            ],
                            "prefix": shard.spec.prefix,
                            "list_merge_strategy": "replace",
                        }

                        self.log.debug(
                            {
                                "entity": str(self),
                                "event": "map_shards: updating shard",
                                "shard.name": shard.name,
                                "sgup.name": service_gateway_user_plane.name,
                                "desired_nc": desired_network_connection,
                                "tsf_nc_mappings": tsf_network_connection,
                                "fp_active": fp_active,
                                "fp_desired": fp_desired,
                                "finger_active": finger_active,
                                "finger_desired": finger_desired,
                                "params": params,
                            }
                        )

                        _shard = self._upsf.update_shard(**params)
                        self.log.debug(
                            {
                                "entity": str(self),
                                "event": "map_shards: shard updated",
                                "shard": _shard,
                            }
                        )

                    else:
                        self.log.debug(
                            {
                                "entity": str(self),
                                "event": "map_shards: no shard update needed",
                                "shard.name": shard.name,
                                "sgup.name": service_gateway_user_plane.name,
                                "desired_nc": desired_network_connection,
                                "fp_active": fp_active,
                                "fp_desired": fp_desired,
                                "finger_active": finger_active,
                                "finger_desired": finger_desired,
                            }
                        )

                except (
                    KeyError,
                    RuntimeError,
                ) as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "map_shards: shard update failed",
                            "error": error,
                            "traceback": traceback.format_exc(),
                        }
                    )

            self.shard_dump()

        except UpsfError as error:
            self.log.error(
                {
                    "entity": str(self),
                    "event": "map_shards, error occurred",
                    "error": error,
                    "traceback": traceback.format_exc(),
                }
            )

    def get_static_shard_to_sgup_mapping(self, shard_name):
        """return sgup name if a static mapping was defined in config file, None otherwise"""
        # sanity check: configuration file
        if not pathlib.Path(self.config_file).exists():
            return None

        # get configuration from file
        config = {}
        with pathlib.Path(self.config_file).open(encoding="ascii") as file:
            config = yaml.load(file, Loader=yaml.SafeLoader)
            if config is None:
                return None

        for entry in config.get("upsf", {}).get("shards", []):
            for param in (
                "name",
                "serviceGatewayUserPlane",
            ):
                if param not in entry:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "get_static_shard_to_sgup_mapping: parameter not found, ignoring entry",
                            "param": param,
                            "entry": entry,
                        }
                    )
                    break
            else:
                if entry["name"] != shard_name:
                    continue
                return entry["serviceGatewayUserPlane"]
        return None

    @staticmethod
    def upsf_register_task(**kwargs):
        """periodic background task"""
        while True:
            with contextlib.suppress(Exception):
                # sleep for specified time interval, default: 60s
                time.sleep(int(kwargs.get("interval", 60)))

                if kwargs.get("entity", None) is None:
                    continue

                # send event garbage-collection
                kwargs["entity"].create_default_items()

    def create_default_items(self):
        """create default shards if non-existing"""

        # sanity check: configuration file
        if not pathlib.Path(self.config_file).exists():
            return

        # sanity check: sgups
        if len(self._upsf.list_service_gateway_user_planes()) == 0:
            self.log.warning(
                {
                    "entity": str(self),
                    "event": "create_default_shards: no sgups available, aborting.",
                }
            )
            return

        # get shards from UPSF
        shards = {shard.name: shard for shard in self._upsf.list_shards()}

        # get configuration from file
        config = {}
        with pathlib.Path(self.config_file).open(encoding="ascii") as file:
            config = yaml.load(file, Loader=yaml.SafeLoader)

        sgup_names = []

        for entry in config.get("upsf", {}).get("shards", []):
            for param in (
                "name",
                "prefixes",
            ):
                if param not in entry:
                    self.log.warning(
                        {
                            "entity": str(self),
                            "event": "create_default_shards: parameter not found, ignoring entry",
                            "param": param,
                            "entry": entry,
                        }
                    )
                    break
            else:
                # ignore existing shards
                if entry["name"] in shards:
                    continue

                # shard parameters
                params = {
                    "name": entry["name"],
                    "virtual_mac": self.virtual_mac,
                    "allocated_session_count": 0,
                }

                # for all ip prefixes
                max_session_count = 0
                for prefix in entry.get("prefixes", []):
                    try:
                        # ignore specified hosts
                        exclude_hosts = set(
                            ipaddress.ip_address(host)
                            for host in entry.get("exclude", [])
                        )

                        # all hosts without excluded ones
                        hosts = (
                            set(ipaddress.ip_network(prefix).hosts()) - exclude_hosts
                        )

                        max_session_count += len(hosts)

                    except ValueError as error:
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "create_default_shards: invalid prefix, ignoring",
                                "prefix": prefix,
                                "entry": entry,
                                "error": error,
                            }
                        )
                        continue

                params["max_session_count"] = max_session_count
                params["prefix"] = list(entry.get("prefixes", []))

                # populate list of sgup names from UPSF
                if len(sgup_names) == 0:
                    sgup_names = [
                        sgup.name
                        for sgup in self._upsf.list_service_gateway_user_planes()
                    ]

                # static mapping to specific sgup requested?
                if entry.get("serviceGatewayUserPlane", None) is not None:
                    sgups = [
                        sgup.name
                        for sgup in self._upsf.list_service_gateway_user_planes()
                    ]

                    desired_sgup = entry["serviceGatewayUserPlane"]

                    if desired_sgup not in sgups:
                        self.log.warning(
                            {
                                "entity": str(self),
                                "event": "desired sgup for shard not available, ignoring",
                                "shard.name": entry["name"],
                                "sgup.name": desired_sgup,
                            }
                        )
                        continue

                    params["desired_service_gateway_user_plane"] = desired_sgup

                # dynamic mapping: if any sgup is available, assign shard and move to next sgup
                elif len(sgup_names) > 0:
                    params["desired_service_gateway_user_plane"] = sgup_names.pop(0)

                self.log.info(
                    {
                        "entity": str(self),
                        "event": "add entry",
                        "entry": entry,
                        "params": params,
                    }
                )

                # create new shard
                self._upsf.create_shard(**params)

    def stop(self):
        """signals background thread a stop condition"""
        self.log.debug(
            {
                "entity": str(self),
                "event": "thread terminating ...",
            }
        )
        self._stop_thread.set()
        self.join()

    def run(self):
        """runs main loop as background thread"""
        while not self._stop_thread.is_set():
            with contextlib.suppress(Exception):
                try:
                    upsf_subscriber = UPSF(
                        upsf_host=self.upsf_host,
                        upsf_port=self.upsf_port,
                    )

                    for item in upsf_subscriber.read(
                        # subscribe to up, tsf
                        itemtypes=[
                            "service_gateway_user_plane",
                            "traffic_steering_function",
                            "network_connection",
                            "shard",
                        ],
                        watch=True,
                    ):
                        self.log.debug(
                            {
                                "entity": str(self),
                                "event": "item notification rcvd",
                                "item": item,
                            }
                        )

                        with contextlib.suppress(Exception):
                            # service gateway user planes
                            if item.service_gateway_user_plane.name not in ("",):
                                self.map_shards()

                            # traffic steering functions
                            elif item.traffic_steering_function.name not in ("",):
                                self.map_shards()

                            # network connections
                            elif item.network_connection.name not in ("",):
                                self.map_shards()

                            # shard
                            elif item.shard.name not in ("",):
                                self.map_shards()

                        if self._stop_thread.is_set():
                            break

                except UpsfError as error:
                    self.log.error(
                        {
                            "entity": str(self),
                            "event": "error occurred",
                            "error": error,
                            "backtrace": traceback.format_exc,
                        }
                    )
                    time.sleep(1)


def parse_arguments(defaults, loglevels):
    """parse command line arguments"""
    parser = argparse.ArgumentParser(sys.argv[0])

    parser.add_argument(
        "--upsf-host",
        help=f'upsf grpc host (default: {defaults["upsf_host"]})',
        dest="upsf_host",
        action="store",
        default=defaults["upsf_host"],
        type=str,
    )

    parser.add_argument(
        "--upsf-port",
        "-p",
        help=f'upsf grpc port (default: {defaults["upsf_port"]})',
        dest="upsf_port",
        action="store",
        default=defaults["upsf_port"],
        type=int,
    )

    parser.add_argument(
        "--config-file",
        "--conf",
        "-c",
        help=f'configuration file (default: {defaults["config_file"]})',
        dest="config_file",
        action="store",
        default=defaults["config_file"],
        type=str,
    )

    parser.add_argument(
        "--virtual-mac",
        help=f'default shard virtual mac (default: {defaults["virtual_mac"]})',
        dest="virtual_mac",
        action="store",
        default=defaults["virtual_mac"],
        type=str,
    )

    parser.add_argument(
        "--registration-interval",
        "-i",
        help=f'registration interval (default: {defaults["registration_interval"]})',
        dest="registration_interval",
        action="store",
        default=defaults["registration_interval"],
        type=int,
    )

    parser.add_argument(
        "--upsf-auto-register",
        "-a",
        help=f'enable registration of pre-defined shards (default: {defaults["upsf_auto_register"]})',
        dest="upsf_auto_register",
        action="store",
        default=defaults["upsf_auto_register"],
        type=str,
    )

    parser.add_argument(
        "--loglevel",
        "-l",
        help=f'set log level (default: {defaults["loglevel"]})',
        dest="loglevel",
        choices=loglevels.keys(),
        action="store",
        default=defaults["loglevel"],
        type=str,
    )

    return parser.parse_args(sys.argv[1:])


def main():
    """main routine"""
    defaults = {
        # upsf host, default: 127.0.0.1
        "upsf_host": os.environ.get("UPSF_HOST", "127.0.0.1"),
        # upsf port, default: 50051
        "upsf_port": os.environ.get("UPSF_PORT", 50051),
        # configuration file, default: /etc/upsf/policy.yaml
        "config_file": os.environ.get("CONFIG_FILE", "/etc/upsf/policy.yaml"),
        # virtual max assigned to shard default
        "virtual_mac": os.environ.get("VIRTUAL_MAC", "00:00:01:00:00:00"),
        # periodic background thread: time interval
        "registration_interval": os.environ.get("REGISTRATION_INTERVAL", 60),
        # register shards periodically
        "upsf_auto_register": os.environ.get("UPSF_AUTO_REGISTER", "yes"),
        # loglevel, default: 'info'
        "loglevel": os.environ.get("LOGLEVEL", "info"),
    }

    loglevels = {
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
        "debug": logging.DEBUG,
    }

    args = parse_arguments(defaults, loglevels)

    # configure logging, here: root logger
    log = logging.getLogger("")

    # add StreamHandler
    hnd = logging.StreamHandler()
    formatter = logging.Formatter(
        f"%(asctime)s: [%(levelname)s] host: {socket.gethostname()}, "
        f"process: {sys.argv[0]}, "
        "module: %(module)s, "
        "func: %(funcName)s, "
        "trace: %(exc_text)s, "
        "message: %(message)s"
    )
    hnd.setFormatter(formatter)
    hnd.setLevel(loglevels[args.loglevel])
    log.addHandler(hnd)

    # set log level of root logger
    log.setLevel(loglevels[args.loglevel])

    # keyword arguments
    kwargs = vars(args)

    # log to debug channel
    log.debug(kwargs)

    # create shard manager
    shardmgr = ShardManager(**kwargs)
    shardmgr.start()
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
