# upsf-shard-manager

This repository contains a Python based application named **shard
manager** for mapping shards to service gateway user planes within
a BBF WT-474 compliant User Plane Selection Function (UPSF).

Its main purpose is to subscribe and listen on events emitted by the
UPSF via its gRPC streaming interface, and maps unassigned shards
to one of the available service gateway user planes based on current load
and capacity.

In addition, shard manager may be used for creating subscriber
group entities within the UPSF for testing purposes. It runs a
periodic background task that reads pre-defined subscriber groups
from an associated policy file and if missing in the UPSF, creates
those instances.

See next table for a list of command line options supported by
shard manager. An associated environment variable exists for each
command line option: the CLI option takes precedence, though.

| Option                  | Default value        | Environment variable  | Description                                                                 |
|-------------------------|----------------------|-----------------------|-----------------------------------------------------------------------------|
| --upsf-host             | 127.0.0.1            | UPSF_HOST             | UPSF server host to connect to                                              |
| --upsf-port             | 50051                | UPSF_PORT             | UPSF server port to connect to                                              |
| --virtual-mac           | 00:00:01:00:00:00    | VIRTUAL_MAC           | Default virtual MAC address assigned to new shards                          |
| --config-file           | /etc/upsf/policy.yml | CONFIG_FILE           | Policy configuration file containing pre-defined subscriber groups (shards) |
| --registration-interval | 60                   | REGISTRATION_INTERVAL | Run periodic background thread every _registration_interval_ seconds.       |
| --upsf-auto-register    | yes                  | UPSF_AUTO_REGISTER    | Enable periodic background thread for creating pre-defined shards.          |
| --log-level             | info                 | LOG_LEVEL             | Default loglevel, supported options: info, warning, error, critical, debug  |

This application makes use of the [upsf-client](https://github.com/bisdn/upsf-client)
library for UPSF related communication.

## Getting started and installation

Installation is based on [Setuptools](https://setuptools.pypa.io/en/latest/setuptools.html).

For safe testing create and enter a virtual environment, build and install the
application, e.g.:

```bash
sh# cd upsf-shard-manager
sh# python3 -m venv venv
sh# source venv/bin/activate
sh# pip install -r ./requirements.txt
sh# python3 setup.py build && python3 setup.py install

### if you haven't done so yet, build and install the submodule(s) as well
sh# git submodule update --init --recursive
sh# cd upsf-client
sh# pip install -r ./requirements.txt
sh# python3 setup.py build && python3 setup.py install

sh# upsf-shard-manager -h
```

## Managing subscriber groups

This section describes briefly the mapping algorithm in shard
manager.  It listens on events emitted by the UPSF for service
gateway user planes (SGUP), traffic steering functions (TSF),
network connections (NC) and subscriber groups (SHRD).

For any event received for these items the mapping algorithm is
executed. Here its pseudo code:

1. Read all SGUP, TSF, NC, SHRD items from UPSF.
1. If no SGUP instances exist, reset all shard => SGUP mappings and return
1. For every shard:
   - get current SGUP
   - check for static mapping SHRD => SGUP in policy configuration file
1. if static binding has been defined:
   - if SGUP does not exist:
     - remove shard to SGUP mapping and update shard
   - otherwise:
     - assign SHRD to SGUP as defined by policy
1. if dynamic binding is required:
   - get active load for each SGUP based on number of allocated sessions
     and maximum number of supported sessions
   - Select SGUP with least load and assign to SHRD
   - update number of allocated sessions
1. Continue until list of shards has been exhausted

If no service gateway user plane instances can be found for a particular
subscriber group, shard-manager sets the desired service gateway user plane
to an empty string.

No deletion will take place: such unmapped subscriber groups are
in fact transient in nature and will remain in the UPSF database.

**Please note!** A service gateway user plane is a valid candidate
user plane only if:

- the maximum number of supported sessions must be larger than zero
- the associated controlling service gateway must exist in the
  UPSF database

## Policy configuration file

A configuration file is used for creating pre-defined entities in
the UPSF. A background task ensures existence of those entities,
but will not alter them unless the entity does not exist in the
UPSF yet. Existing entities with or without changes applied by other
UPSF clients will not be altered by shard manager. For re-creating
the original entity as defined in the policy file, you must remove
the item from the UPSF first and shard-manager's background task will
recreate it after a short period of time.

See below for an example policy configuration or inspect the examples
in the [tools/](./tools/policy.yml) directory:

```yaml
upsf:
  shards:
    - name: "shard-A"
      prefixes:
        - "10.10.16.0/20"
        - "192.168.0.0/16"
      exclude:
        - 10.10.0.1
    - name: "shard-B"
      prefixes:
        - "10.10.0.64/26"
    - name: "shard-C"
      prefixes:
        - "10.10.0.128/26"
```

## Limitations

- The set of service groups formed by a union of all service groups
  required by session contexts assigned to a shard will not be taken
  into account for making a mapping decision.
