import os
import yaml
from charmhelpers.core.hookenv import (
    config,
    in_relation_hook,
    local_unit,
    leader_get,
    related_units,
    relation_get,
    relation_set,
    relation_ids,
    status_set,
    log,
    ERROR,
)
from charmhelpers.contrib.charmsupport import nrpe
import common_utils


config = config()


MODULE = "analytics"
BASE_CONFIGS_PATH = "/etc/contrail"

ANALYTICS_CONFIGS_PATH = BASE_CONFIGS_PATH + "/analytics"
ANALYTICS_ALARM_CONFIGS_PATH = BASE_CONFIGS_PATH + "/analytics_alarm"
ANALYTICS_SNMP_CONFIGS_PATH = BASE_CONFIGS_PATH + "/analytics_snmp"
REDIS_CONFIGS_PATH = BASE_CONFIGS_PATH + "/redis"

IMAGES = {
    500: {
        "analytics": [
            "opensdn-node-init",
            "opensdn-nodemgr",
            "opensdn-analytics-api",
            "opensdn-analytics-collector",
            "opensdn-analytics-query-engine",
            "opensdn-analytics-alarm-gen",
            "opensdn-analytics-snmp-collector",
            "opensdn-analytics-topology",
            "opensdn-external-redis",
        ]
    },
    9999: {
        "analytics": [
            "opensdn-node-init",
            "opensdn-analytics-api",
            "opensdn-nodemgr",
            "opensdn-analytics-collector",
            "opensdn-external-redis",
            "opensdn-status",
        ],
        "analytics-alarm": [
            "opensdn-node-init",
            "opensdn-analytics-alarm-gen",
            "opensdn-nodemgr",
            "opensdn-external-kafka",
        ],
        "analytics-snmp": [
            "opensdn-node-init",
            "opensdn-analytics-snmp-collector",
            "opensdn-nodemgr",
            "opensdn-analytics-snmp-topology",
        ],
    },
}
# images for new versions that can be absent in previous releases
IMAGES_OPTIONAL = [
    "opensdn-provisioner",
]

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


def get_cluster_info(address_type, own_ip):
    info = dict()
    for rid in relation_ids("analytics-cluster"):
        for unit in related_units(rid):
            ip = relation_get(address_type, unit, rid)
            if ip:
                info[unit] = ip
    # add it's own ip address
    info[local_unit()] = own_ip
    return info


def controller_ctx():
    """Get the ipaddress of all contrail control nodes"""
    auth_mode = config.get("auth_mode")
    if auth_mode is None:
        # NOTE: auth_mode must be transmitted by controller
        return {}

    controller_ip_list = common_utils.json_loads(config.get("controller_ips"), list())
    controller_data_ip_list = common_utils.json_loads(config.get("controller_data_ips"), list())
    return {
        "auth_mode": auth_mode,
        "controller_servers": controller_ip_list,
        "control_servers": controller_data_ip_list,
    }


def analyticsdb_ctx():
    """Get the ipaddress of all contrail analyticsdb nodes"""

    data = {"analyticsdb_enabled": True}
    if common_utils.get_contrail_version() > 500:
        data["analyticsdb_enabled"] = False
        for rid in relation_ids("contrail-analyticsdb"):
            if related_units(rid):
                data["analyticsdb_enabled"] = True
                break

    data["analyticsdb_servers"] = get_analyticsdb_list()
    return data


def get_analyticsdb_list():
    analyticsdb_ip_list = config.get("analyticsdb_ips")
    if analyticsdb_ip_list is not None:
        return common_utils.json_loads(analyticsdb_ip_list, list())

    # NOTE: use old way of collecting ips.
    # previously we collected units by private-address
    # now we take collected list from leader through relation
    log("analyticsdb_ips is not in config. calculating...")
    analyticsdb_ip_list = []
    for rid in relation_ids("contrail-analyticsdb"):
        for unit in related_units(rid):
            ip = relation_get("private-address", unit, rid)
            if ip:
                analyticsdb_ip_list.append(ip)
    return analyticsdb_ip_list


def get_context():
    ctx = {}
    ctx["module"] = MODULE
    ctx["log_level"] = config.get("log-level", "SYS_NOTICE")
    # previous versions of charm may store next value in config as string.
    ssl_enabled = config.get("ssl_enabled", False)
    if not isinstance(ssl_enabled, bool):
        ssl_enabled = yaml.load(ssl_enabled)
        if not isinstance(ssl_enabled, bool):
            ssl_enabled = False
    ctx["ssl_enabled"] = ssl_enabled
    ctx["certs_hash"] = common_utils.get_certs_hash(MODULE) if ctx["ssl_enabled"] else ''
    ctx["container_registry"] = config.get("docker-registry")
    ctx["contrail_version_tag"] = config.get("image-tag")
    ctx["container_runtime"] = config.get("container_runtime")
    ctx.update(common_utils.json_loads(config.get("orchestrator_info"), dict()))
    if not ctx.get("cloud_orchestrators"):
        ctx["cloud_orchestrators"] = [ctx.get("cloud_orchestrator")] if ctx.get("cloud_orchestrator") else list()

    ctx["config_analytics_ssl_available"] = common_utils.is_config_analytics_ssl_available()
    ctx["logging"] = common_utils.container_engine().render_logging()
    ctx["contrail_version"] = common_utils.get_contrail_version()

    ctx["analytics_servers"] = list(common_utils.json_loads(leader_get("cluster_info"), dict()).values())
    ctx.update(controller_ctx())
    ctx.update(analyticsdb_ctx())
    log("CTX: {}".format(ctx))
    ctx.update(common_utils.json_loads(config.get("auth_info"), dict()))
    return ctx


def pull_images():
    ctx = get_context()
    tag = config.get('image-tag')
    images = IMAGES.get(ctx["contrail_version"], IMAGES.get(9999)).copy()

    if not ctx.get("analyticsdb_enabled"):
        images.pop("analytics-alarm")
        images.pop("analytics-snmp")

    for image_group in images.keys():
        for image in images.get(image_group):
            try:
                common_utils.container_engine().pull(image, tag)
            except Exception as e:
                log("Can't load image {}".format(e), level=ERROR)
                raise Exception('Image could not be pulled: {}:{}'.format(image, tag))
    for image in IMAGES_OPTIONAL:
        try:
            common_utils.container_engine().pull(image, tag)
        except Exception as e:
            log("Can't load optional image {}".format(e))


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
    if not ctx.get("controller_servers"):
        missing_relations.append("contrail-controller")
    if ctx.get("analyticsdb_enabled") and not ctx.get("analyticsdb_servers"):
        missing_relations.append("contrail-analyticsdb")
    if config.get('tls_present', False) != config.get('ssl_enabled', False):
        missing_relations.append("tls-certificates")
    if missing_relations:
        status_set('blocked',
                   'Missing or incomplete relations: ' + ', '.join(missing_relations))
        return
    if len(ctx.get("analytics_servers")) < config.get("min-cluster-size"):
        status_set('blocked',
                   'Count of cluster nodes is not enough ({} < {}).'.format(
                       len(ctx.get("analytics_servers")), config.get("min-cluster-size")
                   ))
        return
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked',
                   'Missing cloud_orchestrator info in relation '
                   'with contrail-controller.')
        return
    if "openstack" in ctx.get("cloud_orchestrators") and not ctx.get("keystone_ip"):
        status_set('blocked',
                   'Missing auth info in relation with contrail-controller.')
        return
    # TODO: what should happens if relation departed?

    changed_dict = _render_configs(ctx)
    changed = changed_dict["common"]

    service_changed = changed_dict["analytics"]
    common_utils.container_engine().compose_run(ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    if ctx["contrail_version"] >= 510 and ctx.get("analyticsdb_enabled"):
        service_changed = changed_dict["analytics-alarm"]
        common_utils.container_engine().compose_run(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

        service_changed = changed_dict["analytics-snmp"]
        common_utils.container_engine().compose_run(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    # redis is a common service that needs own synchronized env
    service_changed = changed_dict["redis"]
    common_utils.container_engine().compose_run(REDIS_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    services = SERVICES.get(ctx["contrail_version"], SERVICES.get(9999)).copy()
    if not ctx.get("analyticsdb_enabled"):
        services.pop("analytics-alarm")
        services.pop("analytics-snmp")

    common_utils.update_services_status(MODULE, services)


def _render_configs(ctx):
    result = dict()

    tfolder = '5.0' if ctx["contrail_version"] == 500 else '5.1'
    result["common"] = common_utils.apply_keystone_ca(MODULE, ctx)
    result["common"] |= common_utils.render_and_log(
        tfolder + "/analytics.env",
        BASE_CONFIGS_PATH + "/common_analytics.env", ctx)

    result["analytics"] = common_utils.render_and_log(
        tfolder + "/analytics.yaml",
        ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    if ctx["contrail_version"] >= 510 and ctx.get("analyticsdb_enabled"):
        result["analytics-alarm"] = common_utils.render_and_log(
            tfolder + "/analytics-alarm.yaml",
            ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml", ctx)

        result["analytics-snmp"] = common_utils.render_and_log(
            tfolder + "/analytics-snmp.yaml",
            ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml", ctx)
    # TODO:  think about removing analytics-alarm.yaml and analytics-snmp.yaml
    # redis is a common service that needs own synchronized env
    result["redis"] = common_utils.render_and_log(
        "redis.env",
        BASE_CONFIGS_PATH + "/redis.env", ctx)
    result["redis"] |= common_utils.render_and_log(
        "redis.yaml",
        REDIS_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    return result


def update_nrpe_config():
    plugins_dir = '/usr/local/lib/nagios/plugins'
    nrpe_compat = nrpe.NRPE()
    component_ip = common_utils.get_ip()
    common_utils.rsync_nrpe_checks(plugins_dir)
    common_utils.add_nagios_to_sudoers()

    ssl_on_backend = config.get("ssl_enabled", False) and common_utils.is_config_analytics_ssl_available()
    if ssl_on_backend:
        check_api_cmd = 'check_http -S -H {} -p 8081'.format(component_ip)
    else:
        check_api_cmd = 'check_http -H {} -p 8081'.format(component_ip)
    nrpe_compat.add_check(
        shortname='check_analytics_api',
        description='Check Contrail Analytics API',
        check_cmd=check_api_cmd
    )

    ctl_status_shortname = 'check_contrail_status_' + MODULE
    nrpe_compat.add_check(
        shortname=ctl_status_shortname,
        description='Check opensdn-status',
        check_cmd=common_utils.contrail_status_cmd(MODULE, plugins_dir)
    )

    nrpe_compat.write()


def stop_analytics():
    common_utils.container_engine().compose_down(ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml")

    if os.path.exists(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml"):
        common_utils.container_engine().compose_down(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml")
    if os.path.exists(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml"):
        common_utils.container_engine().compose_down(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml")

    # TODO: Redis is a common service. We can't stop it here
    if os.path.exists(REDIS_CONFIGS_PATH + "/docker-compose.yaml"):
        common_utils.container_engine().compose_down(REDIS_CONFIGS_PATH + "/docker-compose.yaml")


def remove_created_files():
    # Removes all config files, environment files, etc.
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/common_analytics.env")
    common_utils.remove_file_safe(ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml")

    # TODO: Redis is a common service. We can't delete its files here.
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/redis.env")
    common_utils.remove_file_safe(REDIS_CONFIGS_PATH + "/docker-compose.yaml")


# ZUI code block

ziu_relations = [
    "contrail-analytics",
    "contrail-analyticsdb",
    "analytics-cluster",
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
    ctx = get_context()
    common_utils.container_engine().compose_down(ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(REDIS_CONFIGS_PATH + "/docker-compose.yaml")
    # can i get_context() here and pass it to ziu_stage_2 ???
    if ctx["contrail_version"] >= 510 and ctx.get("analyticsdb_enabled"):
        common_utils.container_engine().compose_down(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml")
        common_utils.container_engine().compose_down(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml")

    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_2(ziu_stage, trigger):
    # start API services
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(ANALYTICS_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_run(REDIS_CONFIGS_PATH + "/docker-compose.yaml")

    if ctx["contrail_version"] >= 510 and ctx.get("analyticsdb_enabled"):
        common_utils.container_engine().compose_run(ANALYTICS_ALARM_CONFIGS_PATH + "/docker-compose.yaml")
        common_utils.container_engine().compose_run(ANALYTICS_SNMP_CONFIGS_PATH + "/docker-compose.yaml")

    services = SERVICES.get(ctx["contrail_version"], SERVICES.get(9999)).copy()
    if not ctx.get("analyticsdb_enabled"):
        services.pop("analytics-alarm")
        services.pop("analytics-snmp")

    result = common_utils.update_services_status(MODULE, services)
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
