#!/usr/bin/env python3

import subprocess
import sys
import json
import yaml
import os.path

SERVICES = {
    500: {
        "analytics": [
            "snmp-collector",
            "query-engine",
            "api",
            "alarm-gen",
            "nodemgr",
            "collector",
            "topology",
        ]
    },
    9999: {
        "analytics": [
            "api",
            "nodemgr",
            "collector",
        ],
        "analytics-alarm": [
            "alarm-gen",
            "nodemgr",
            "kafka",
        ],
        "analytics-snmp": [
            "snmp-collector",
            "nodemgr",
            "topology",
        ],
    },
}

WARNING = 1
CRITICAL = 2


def analyticsdb_enabled():
    if not os.path.isfile("/etc/contrail/analytics.env"):
        return True

    with open("/etc/contrail/analytics.env") as f:
        for line in f:
            name, value = line.split("=")
            if name == "ANALYTICSDB_ENABLE":
                return yaml.load(value)
    return True


def get_contrail_status_txt(services):
    try:
        output = subprocess.check_output("export CONTRAIL_STATUS_CONTAINER_NAME=opensdn-status-analytics-nrpe ; sudo -E opensdn-status", shell=True).decode('UTF-8')
    except subprocess.CalledProcessError as err:
        message = ('CRITICAL: Could not get status.'
                   ' return code: {} cmd: {} output: {}'.
                   format(err.returncode, err.cmd, err.output))
        print(message)
        sys.exit(CRITICAL)

    statuses = dict()
    group = None
    for line in output.splitlines()[1:]:
        words = line.split()
        if len(words) == 4 and words[0] == '==' and words[3] == '==':
            group = words[2]
            continue
        if len(words) == 0:
            group = None
            continue
        if group and len(words) >= 2 and group in services:
            srv = words[0].split(':')[0]
            statuses.setdefault(group, dict()).update(
                {srv: ' '.join(words[1:])})

    return statuses


def get_contrail_status_json(prefix):
    try:
        output = json.loads(subprocess.check_output(f"export CONTRAIL_STATUS_CONTAINER_NAME={prefix}-status-analytics-nrpe ; sudo -E {prefix}-status --format json", shell=True).decode('UTF-8'))
    except subprocess.CalledProcessError as err:
        message = ('CRITICAL: Could not get status.'
                   ' return code: {} cmd: {} output: {}'.
                   format(err.returncode, err.cmd, err.output))
        print(message)
        sys.exit(CRITICAL)

    statuses = dict()
    for group in output["pods"]:
        statuses[group] = dict()
        for item in output["pods"][group]:
            statuses[group].update(item)
    return statuses


def check_contrail_status(services, version=None):
    if not analyticsdb_enabled():
        services.pop("analytics-alarm")
        services.pop("analytics-snmp")

    if version > 1912:
        statuses = get_contrail_status_json('opensdn' if version > 2401 else 'contrail')
    else:
        statuses = get_contrail_status_txt(services)

    for group in services:
        if group not in statuses:
            message = ('WARNING: POD {} is absent in the status'
                       .format(group))
            print(message)
            sys.exit(WARNING)
        # expected services
        for srv in services[group]:
            # actual statuses
            # actual service name can be present as a several workers like 'api-0', 'api-1', ...
            stats = [statuses[group][x] for x in statuses[group] if x == srv or x.startswith(srv + '-')]
            if not stats:
                message = ('WARNING: {} is absent in the status'
                           .format(srv))
                print(message)
                sys.exit(WARNING)
            for status in stats:
                if status not in ['active', 'backup']:
                    message = ('CRITICAL: {} is not ready. Reason: {}'
                               .format(srv, status))
                    print(message)
                    sys.exit(CRITICAL)
    print('Contrail status OK')
    sys.exit()


if __name__ == '__main__':
    cver = sys.argv[1]
    if '.' in str(cver):
        if cver == '5.0':
            version = 500
        elif cver == '5.1':
            version = 510
        else:
            print("CRITICAL: invalid version: {}".format(cver))
            sys.exit(CRITICAL)
    elif not cver.isdigit():
        print("CRITICAL: invalid version: {}".format(cver))
        sys.exit(CRITICAL)
    else:
        version = int(cver)
    check_contrail_status(SERVICES.get(version, SERVICES.get(9999)), version=version)
