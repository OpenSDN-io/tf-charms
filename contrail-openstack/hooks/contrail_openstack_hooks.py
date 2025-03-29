#!/usr/bin/env python3

import json
import sys
import uuid
import yaml

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    log,
    related_units,
    relation_get,
    relation_id,
    relation_ids,
    relation_set,
    status_set,
    leader_get,
    leader_set,
    is_leader,
    unit_private_ip,
)
from charmhelpers.core.host import service_restart

import common_utils
import contrail_openstack_utils as utils


hooks = Hooks()
config = config()


@hooks.hook("install.real")
def install():
    status_set('maintenance', 'Installing...')

    common_utils.add_logrotate()

    common_utils.container_engine().install()
    status_set("blocked", "Missing relation to contrail-controller")


@hooks.hook("config-changed")
def config_changed():
    # Charm doesn't support changing container runtime (check for empty value after upgrade).
    if config.changed("container_runtime") and config.previous("container_runtime"):
        raise Exception("Configuration parameter container_runtime couldn't be changed")

    notify_nova = False
    tag_changed = config.get("saved-image-tag") != config["image-tag"]
    log("saved-image-tag = {}, current image-tag = {}".format(config.get("saved-image-tag"), config.get("image-tag")))
    changed = common_utils.container_engine().config_changed()
    if changed or tag_changed:
        notify_nova = True
        _notify_neutron(redeploy=True)
        _notify_heat(redeploy=True)

    if is_leader():
        _configure_metadata_shared_secret()
        notify_nova = True

    _notify_controller()

    if notify_nova:
        _notify_nova(redeploy=True)

    config["saved-image-tag"] = config["image-tag"]


@hooks.hook("leader-elected")
def leader_elected():
    utils.update_service_ips()
    _configure_metadata_shared_secret()
    _notify_nova()
    _notify_controller()


@hooks.hook("leader-settings-changed")
def leader_settings_changed():
    _notify_nova()


def _notify_controller(rid=None):
    rids = [rid] if rid else relation_ids("contrail-controller")
    if not rids:
        return

    settings = {
        'unit-type': 'openstack',
        'use-internal-endpoints': config.get('use-internal-endpoints'),
    }
    settings.update(_get_orchestrator_info())
    for rid in rids:
        relation_set(relation_id=rid, relation_settings=settings)


@hooks.hook("contrail-controller-relation-joined")
def contrail_controller_joined():
    _notify_controller(rid=relation_id())


def _rebuild_config_from_controller_relation():
    items = dict()

    def _update_item(data, key, data_key):
        val = data.get(data_key)
        if val is not None:
            items[key] = val

    ip = unit_private_ip()
    units = [(rid, unit) for rid in relation_ids("contrail-controller")
             for unit in related_units(rid)]
    # add relation info as last item to override outdated data
    units.append((None, None))
    for rid, unit in units:
        data = relation_get(attribute=None, unit=unit, rid=rid)
        if data is None:
            continue

        _update_item(data, "auth_info", "auth-info")
        _update_item(data, "auth_mode", "auth-mode")
        _update_item(data, "controller_ips", "controller_ips")

        info = data.get("agents-info")
        if not info:
            items["dpdk"] = False
        else:
            value = json.loads(info).get(ip, False)
            if not isinstance(value, bool):
                value = yaml.load(value)
            items["dpdk"] = value

    if not items.get("dpdk"):
        log("DPDK for current host is False. agents-info is not provided.")
    else:
        log("DPDK for host {ip} is {dpdk}".format(ip=ip, dpdk=value))

    for key in ["auth_info", "auth_mode", "controller_ips", "dpdk"]:
        if key in items:
            config[key] = items[key]
        else:
            config.pop(key, None)


def _update_status():
    if "controller_ips" not in config:
        status_set("blocked", "Missing relation to contrail-controller (controller_ips is empty or absent in relation)")
    else:
        status_set("active", "Unit is ready")


@hooks.hook("contrail-controller-relation-changed")
def contrail_controller_changed():
    _rebuild_config_from_controller_relation()
    config.save()
    utils.write_configs()
    _update_status()

    # apply information to base charms
    _notify_nova()
    _notify_neutron()
    _notify_heat()

    # auth_info can affect endpoints
    if is_leader() and utils.update_service_ips():
        _notify_controller()


@hooks.hook("contrail-controller-relation-departed")
def contrail_cotroller_departed():
    _rebuild_config_from_controller_relation()
    config.save()
    utils.write_configs()
    _update_status()


def _configure_metadata_shared_secret():
    secret = leader_get("metadata-shared-secret")
    if config["enable-metadata-server"] and not secret:
        secret = str(uuid.uuid4())
    elif not config["enable-metadata-server"] and secret:
        secret = None
    else:
        return

    leader_set(settings={"metadata-shared-secret": secret})


def _get_orchestrator_info():
    info = {"cloud_orchestrator": "openstack"}
    if config["enable-metadata-server"]:
        info["metadata_shared_secret"] = leader_get("metadata-shared-secret")

    def _add_to_info(key):
        value = leader_get(key)
        if value:
            info[key] = value

    _add_to_info("compute_service_ip")
    _add_to_info("image_service_ip")
    _add_to_info("network_service_ip")
    return {"orchestrator-info": json.dumps(info)}


def _notify_heat(rid=None, redeploy=False):
    rids = [rid] if rid else relation_ids("heat-plugin")
    if not rids:
        return

    if redeploy:
        log("Redeploy code for heat-engine")
        utils.deploy_openstack_code("opensdn-openstack-heat-init", "heat")
        service_restart('heat-engine')
    else:
        log("Redeploy flag is false.")

    plugin_path = utils.get_component_sys_paths("heat") + "/vnc_api/gen/heat/resources"
    plugin_dirs = config.get("heat-plugin-dirs")
    if plugin_path not in plugin_dirs:
        plugin_dirs += ',' + plugin_path
    ctx = utils.get_context()
    sections = {
        "clients_contrail": [
            ("user", ctx.get("keystone_admin_user")),
            ("password", ctx.get("keystone_admin_password")),
            ("tenant", ctx.get("keystone_admin_tenant")),
            ("api_server", " ".join(ctx.get("api_servers"))),
            ("auth_host_ip", ctx.get("keystone_ip")),
            ("use_ssl", ctx.get("ssl_enabled")),
        ]
    }

    if ctx.get("ssl_enabled") and "ca_cert_data" in ctx:
        ca_file_path = "/etc/heat/contrail-ca-cert.pem"
        common_utils.save_file(ca_file_path, ctx["ca_cert_data"], perms=0o644)
        sections["clients_contrail"].append(("cafile", ca_file_path))

    conf = {
        "heat": {
            "/etc/heat/heat.conf": {
                "sections": sections
            }
        }
    }
    settings = {
        "plugin-dirs": plugin_dirs,
        "subordinate_configuration": json.dumps(conf)
    }

    for rid in rids:
        relation_set(relation_id=rid, relation_settings=settings)


@hooks.hook("heat-plugin-relation-joined")
def heat_plugin_joined():
    _notify_heat(rid=relation_id(), redeploy=True)


def _notify_neutron(rid=None, redeploy=False):
    rids = [rid] if rid else relation_ids("neutron-api")
    if not rids:
        return

    if redeploy:
        log("Redeploy code for neutron-server")
        utils.deploy_openstack_code("opensdn-openstack-neutron-init", "neutron")
        service_restart('neutron-server')
    else:
        log("Redeploy flag is false.")

    # create plugin config
    version = utils.get_openstack_component_version('neutron')
    contrail_version = common_utils.get_contrail_version()
    plugin_path = utils.get_component_sys_paths("neutron")
    base = "neutron_plugin_contrail.plugins.opencontrail"
    plugin = base + ".contrail_plugin.NeutronPluginContrailCoreV2"
    # pass just separator to prevent setting of default list
    service_plugins = "contrail-timestamp,"
    if contrail_version >= 1909:
        service_plugins += "contrail-trunk,"
    if contrail_version >= 2005 and version > 12:
        service_plugins += "contrail-tags,"
    if version < 15:
        service_plugins += base + ".loadbalancer.v2.plugin.LoadBalancerPluginV2,"
    contrail_plugin_extension = plugin_path + "/neutron_plugin_contrail/extensions"
    neutron_lbaas_extensions = plugin_path + "/neutron_lbaas/extensions"
    extensions = [
        contrail_plugin_extension,
        neutron_lbaas_extensions
    ]
    conf = {
        "neutron-api": {
            "/etc/neutron/neutron.conf": {
                "sections": {
                    "DEFAULT": [
                        ("api_extensions_path", ":".join(extensions))
                    ]
                }
            }
        }
    }
    settings = {
        "neutron-plugin": "contrail",
        "core-plugin": plugin,
        "neutron-plugin-config":
            "/etc/neutron/plugins/opencontrail/ContrailPlugin.ini",
        "service-plugins": service_plugins,
        "quota-driver": base + ".quota.driver.QuotaDriver",
        "subordinate_configuration": json.dumps(conf),
    }
    auth_mode = config.get("auth_mode", "cloud-admin")
    if auth_mode == "rbac":
        settings["extra_middleware"] = [{
            "name": "user_token",
            "type": "filter",
            "config": {
                "paste.filter_factory":
                    base + ".neutron_middleware:token_factory"
            }
        }]
    for rid in rids:
        relation_set(relation_id=rid, relation_settings=settings)


@hooks.hook("neutron-api-relation-joined")
def neutron_api_joined():
    _notify_neutron(rid=relation_id(), redeploy=True)

    # if this hook raised after contrail-controller we need
    # to overwrite default config file after installation
    utils.write_configs()


def _notify_nova(rid=None, redeploy=False):
    rids = [rid] if rid else relation_ids("nova-compute")
    if not rids:
        return

    # add apparmor exception for contrail/ports/
    utils.configure_apparmor()

    if redeploy:
        log("Redeploy code for nova-compute")
        utils.deploy_openstack_code("opensdn-openstack-compute-init", "nova")
        service_restart('nova-compute')
    else:
        log("Redeploy flag is false.")

    # create plugin config
    sections = {
        "DEFAULT": [
            ("firewall_driver", "nova.virt.firewall.NoopFirewallDriver")
        ]
    }
    if config.get("dpdk", False):
        sections["CONTRAIL"] = [("use_userspace_vhost", "True")]
        sections["libvirt"] = [("use_huge_pages", "True")]
    conf = {
        "nova-compute": {
            "/etc/nova/nova.conf": {
                "sections": sections
            }
        }
    }
    settings = {
        "metadata-shared-secret": leader_get("metadata-shared-secret"),
        "subordinate_configuration": json.dumps(conf)}
    for rid in rids:
        relation_set(relation_id=rid, relation_settings=settings)


@hooks.hook("nova-compute-relation-joined")
def nova_compute_joined():
    utils.nova_patch()
    _notify_nova(rid=relation_id(), redeploy=True)


@hooks.hook("update-status")
def update_status():
    # TODO: try to deploy openstack code again if it was not done
    # update_service_ips can be called only on leader. notify controller only if something was updated
    if is_leader() and utils.update_service_ips():
        _notify_controller()


@hooks.hook("upgrade-charm")
def upgrade_charm():
    _rebuild_config_from_controller_relation()
    config.save()
    utils.write_configs()
    _update_status()

    if is_leader():
        utils.update_service_ips()
    # apply information to base charms
    _notify_nova()
    _notify_neutron()
    _notify_heat()
    _notify_controller()
    config["saved-image-tag"] = config["image-tag"]


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log("Unknown hook {} - skipping.".format(e))


if __name__ == "__main__":
    main()
