import os
import base64

from charmhelpers.core.hookenv import (
    config,
    local_unit,
    log,
    in_relation_hook,
    relation_get,
    relation_set,
    related_units,
    relation_ids,
    status_set,
    leader_get,
    leader_set,
    charm_dir,
    ERROR,
)
from charmhelpers.contrib.charmsupport import nrpe

import common_utils
from subprocess import check_output


config = config()

MODULE = "kubernetes-master"
BASE_CONFIGS_PATH = "/etc/contrail"

CONFIGS_PATH = BASE_CONFIGS_PATH + "/contrail-kubernetes-master"
IMAGES = [
    "opensdn-kubernetes-kube-manager",
    "opensdn-status",
]
SERVICES = {
    "kubernetes": [
        "kube-manager",
    ]
}


def get_cluster_info(address_type, own_ip):
    cluster_info = dict()
    for rid in relation_ids("kubernetes-master-cluster"):
        for unit in related_units(rid):
            ip = relation_get(address_type, unit, rid)
            if ip:
                cluster_info[unit] = ip
    # add it's own ip address
    cluster_info[local_unit()] = own_ip
    return cluster_info


def kubernetes_token():
    try:
        account_file = os.path.join(charm_dir(), 'files', 'contrail-kubemanager-serviceaccount.yaml')
        check_output(["snap", "run", "kubectl", "--kubeconfig", "/root/.kube/config", "apply", "-f", account_file])
    except Exception as e:
        log("Can't apply manifest for service account: {}".format(e))
        return None
    try:
        token_64 = check_output([
            "snap", "run", "kubectl", "--kubeconfig", "/root/.kube/config", "get", "secret", "contrail-kubemanager", "-n", "contrail",
            "-ogo-template=\"{{.data.token}}\""]).decode('UTF-8').strip('\"')
        token = base64.b64decode(token_64).decode()
        return token
    except Exception as e:
        log("Can't get secret for token: {}".format(e))

    return None


def update_kubernetes_token():
    if leader_get("kube_manager_token"):
        return False
    token = kubernetes_token()
    if not token:
        return False
    leader_set({"kube_manager_token": token})
    return True


def get_context():
    ctx = {}
    ctx["module"] = MODULE
    ctx["log_level"] = config.get("log-level", "SYS_NOTICE")
    ctx["container_registry"] = config.get("docker-registry")
    ctx["contrail_version_tag"] = config.get("image-tag")
    ctx["contrail_version"] = common_utils.get_contrail_version()
    ctx["kubemanager_servers"] = list(common_utils.json_loads(leader_get("cluster_info"), dict()).values())
    # get contrail configuration from relation
    ips = common_utils.json_loads(config.get("controller_ips"), list())
    data_ips = common_utils.json_loads(config.get("controller_data_ips"), list())
    ctx["controller_servers"] = ips
    ctx["control_servers"] = data_ips
    ips = common_utils.json_loads(config.get("analytics_servers"), list())
    ctx["analytics_servers"] = ips
    ctx["ssl_enabled"] = config.get("ssl_enabled", False)
    ctx["certs_hash"] = common_utils.get_certs_hash(MODULE) if ctx["ssl_enabled"] else ''

    ctx["cluster_name"] = config.get("cluster_name")
    ctx["cluster_project"] = config.get("cluster_project")
    ctx["cluster_network"] = config.get("cluster_network")
    ctx["pod_subnets"] = config.get("pod_subnets")
    ctx["ip_fabric_subnets"] = config.get("ip_fabric_subnets")
    ctx["service_subnets"] = config.get("service_subnets")
    ctx["ip_fabric_forwarding"] = config.get("ip_fabric_forwarding")
    ctx["ip_fabric_snat"] = config.get("ip_fabric_snat")
    ctx["host_network_service"] = config.get("host_network_service")
    ctx["public_fip_pool"] = config.get("public_fip_pool")

    ctx.update(common_utils.json_loads(config.get("orchestrator_info"), dict()))
    if not ctx.get("cloud_orchestrators"):
        ctx["cloud_orchestrators"] = [ctx.get("cloud_orchestrator")] if ctx.get("cloud_orchestrator") else list()

    # TODO: switch to use context for this

    ctx["kube_manager_token"] = leader_get("kube_manager_token")
    if config.get("kubernetes_api_hostname") and config.get("kubernetes_api_secure_port"):
        ctx["kubernetes_api_server"] = config.get("kubernetes_api_hostname")
        ctx["kubernetes_api_secure_port"] = config.get("kubernetes_api_secure_port")
    else:
        ctx["kubernetes_api_server"] = config.get("kubernetes_api_server")
        ctx["kubernetes_api_secure_port"] = config.get("kubernetes_api_port")

    ctx["nested_mode"] = config.get("nested_mode")
    if ctx["nested_mode"]:
        # TODO: create  KUBERNETES_NESTED_VROUTER_VIP link-local services in Contrail via config API
        ctx["nested_mode_config"] = common_utils.json_loads(config.get("nested_mode_config"), dict())

    ctx["config_analytics_ssl_available"] = common_utils.is_config_analytics_ssl_available()
    ctx["logging"] = common_utils.container_engine().render_logging()

    log("CTX: {}".format(ctx))

    ctx.update(common_utils.json_loads(config.get("auth_info"), dict()))

    value = config.get("kubernetes_cluster_domain")
    if value:
        ctx["kubernetes_cluster_domain"] = value
    elif ctx.get("keystone_project_domain_name"):
        ctx["kubernetes_cluster_domain"] = ctx["keystone_project_domain_name"]

    return ctx


def pull_images():
    tag = config.get('image-tag')
    for image in IMAGES:
        try:
            common_utils.container_engine().pull(image, tag)
        except Exception as e:
            log("Can't load image {}".format(e), level=ERROR)
            raise Exception('Image could not be pulled: {}:{}'.format(image, tag))


def update_charm_status():
    ctx = get_context()

    if config.get("maintenance"):
        log("ISSU Maintenance is in progress")
        status_set('maintenance', 'issu is in progress')
        return
    if int(config.get("ziu", -1)) > -1:
        log("ZIU Maintenance is in progress")
        status_set('maintenance',
                   'ziu is in progress - stage/done = {}/{}'.format(config.get("ziu"), config.get("ziu_done")))
        return

    _update_charm_status(ctx)


def _update_charm_status(ctx):
    missing_relations = []
    if not ctx.get("nested_mode") and not ctx.get("controller_servers"):
        missing_relations.append("contrail-controller")
    if not ctx.get("kubernetes_api_server"):
        missing_relations.append("kube-api-endpoint")
    if config.get('tls_present', False) != config.get('ssl_enabled', False):
        missing_relations.append("tls-certificates")
    if missing_relations:
        status_set('blocked',
                   'Missing or incomplete relations: ' + ', '.join(missing_relations))
        return
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked',
                   'Missing cloud_orchestrator info in relation '
                   'with contrail-controller.')
        return
    if not ctx.get("kube_manager_token"):
        status_set('waiting',
                   'Kube manager token is absent. Wait for token from kubectl run.')
        return
    if "openstack" in ctx.get("cloud_orchestrators") and not ctx.get("keystone_ip"):
        status_set('blocked',
                   'Missing auth info in relation with contrail-controller.')
        return

    changed_dict = _render_configs(ctx)
    changed = changed_dict["common"]

    service_changed = changed_dict["kubernetes-master"]
    common_utils.container_engine().compose_run(CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    common_utils.update_services_status(MODULE, SERVICES)


def _render_configs(ctx):
    result = dict()

    result['common'] = common_utils.apply_keystone_ca(MODULE, ctx)
    result["common"] |= common_utils.render_and_log(
        "kubemanager.env",
        BASE_CONFIGS_PATH + "/common_kubemanager.env", ctx)

    result["kubernetes-master"] = common_utils.render_and_log(
        "/contrail-kubemanager.yaml",
        CONFIGS_PATH + "/docker-compose.yaml", ctx)

    return result


def update_nrpe_config():
    plugins_dir = '/usr/local/lib/nagios/plugins'
    nrpe_compat = nrpe.NRPE(primary=False)
    common_utils.rsync_nrpe_checks(plugins_dir)
    common_utils.add_nagios_to_sudoers()

    ctl_status_shortname = 'check_contrail_status_' + MODULE.replace('-', '_')
    nrpe_compat.add_check(
        shortname=ctl_status_shortname,
        description='Check opensdn-status',
        check_cmd=common_utils.contrail_status_cmd(MODULE, plugins_dir)
    )

    nrpe_compat.write()


def stop_kubernetes_master():
    common_utils.container_engine().compose_down(CONFIGS_PATH + "/docker-compose.yaml")


def remove_created_files():
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/common_kubemanager.env")
    common_utils.remove_file_safe(CONFIGS_PATH + "/docker-compose.yaml")


# ZUI code block

ziu_relations = [
    "contrail-controller",
    "kubernetes-master-cluster",
]


def config_set(key, value):
    if value is not None:
        config[key] = value
    else:
        config.pop(key, None)
    config.save()


def signal_ziu(key, value):
    log("ZIU: signal {} = {}".format(key, value))
    for rname in ziu_relations:
        for rid in relation_ids(rname):
            relation_set(relation_id=rid, relation_settings={key: value})
    config_set(key, value)


def update_ziu(trigger):
    if in_relation_hook():
        ziu_stage = relation_get("ziu")
        log("ZIU: stage from relation {}".format(ziu_stage))
    else:
        ziu_stage = config.get("ziu")
        log("ZIU: stage from config {}".format(ziu_stage))
    if ziu_stage is None:
        return
    ziu_stage = int(ziu_stage)
    config_set("ziu", ziu_stage)
    if ziu_stage > int(config.get("ziu_done", -1)):
        log("ZIU: run stage {}, trigger {}".format(ziu_stage, trigger))
        stages[ziu_stage](ziu_stage, trigger)


def ziu_stage_noop(ziu_stage, trigger):
    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_0(ziu_stage, trigger):
    # update images
    if trigger == "image-tag":
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_1(ziu_stage, trigger):
    # stop API services
    common_utils.container_engine().compose_down(CONFIGS_PATH + "/docker-compose.yaml")
    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_2(ziu_stage, trigger):
    # start API services
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(CONFIGS_PATH + "/docker-compose.yaml")

    result = common_utils.update_services_status(MODULE, SERVICES)
    if result:
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_6(ziu_stage, trigger):
    # finish
    signal_ziu("ziu", None)
    signal_ziu("ziu_done", None)


stages = {
    0: ziu_stage_0,
    1: ziu_stage_1,
    2: ziu_stage_2,
    3: ziu_stage_noop,
    4: ziu_stage_noop,
    5: ziu_stage_noop,
    6: ziu_stage_6,
}
