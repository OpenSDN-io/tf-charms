#!/usr/bin/env python3
import os
import sys

_path = os.path.dirname(os.path.realpath(__file__))
_hooks = os.path.abspath(os.path.join(_path, '../hooks'))
_root = os.path.abspath(os.path.join(_path, '..'))


def _add_path(path):
    if path not in sys.path:
        sys.path.insert(1, path)


_add_path(_hooks)
_add_path(_root)

from charmhelpers.core.hookenv import function_get
import contrail_agent_utils as utils


def upgrade():
    params = {}
    params["stop_agent"] = function_get("stop-agent")
    params["force"] = function_get("force")
    utils.action_upgrade(params)


if __name__ == '__main__':
    upgrade()
