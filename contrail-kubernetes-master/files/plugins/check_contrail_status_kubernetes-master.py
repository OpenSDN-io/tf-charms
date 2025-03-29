#!/usr/bin/env python3

import subprocess
import sys
import json

SERVICES = {
    'kubernetes': [
        'kube-manager',
    ],
}

WARNING = 1
CRITICAL = 2


def get_contrail_status_txt(services):
    try:
        output = subprocess.check_output("export CONTRAIL_STATUS_CONTAINER_NAME=opensdn-status-kubernetes-master-nrpe ; sudo -E opensdn-status", shell=True).decode('UTF-8')
    except subprocess.CalledProcessError as err:
        message = ('CRITICAL: Could not get opensdn-status.'
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


def get_contrail_status_json(services):
    try:
        output = json.loads(subprocess.check_output("export CONTRAIL_STATUS_CONTAINER_NAME=opensdn-status-kubernetes-master-nrpe ; sudo -E opensdn-status --format json", shell=True).decode('UTF-8'))
    except subprocess.CalledProcessError as err:
        message = ('CRITICAL: Could not get opensdn-status.'
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
    if version > 1912:
        statuses = get_contrail_status_json(services)
    else:
        statuses = get_contrail_status_txt(services)

    for group in services:
        if group not in statuses:
            message = ('WARNING: POD {} is absent in the opensdn-status'
                       .format(group))
            print(message)
            sys.exit(WARNING)
        # expected services
        for srv in services[group]:
            # actual statuses
            # actual service name can be present as a several workers like 'api-0', 'api-1', ...
            stats = [statuses[group][x] for x in statuses[group] if x == srv or x.startswith(srv + '-')]
            if not stats:
                message = ('WARNING: {} is absent in the opensdn-status'
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
    check_contrail_status(SERVICES, version=version)
