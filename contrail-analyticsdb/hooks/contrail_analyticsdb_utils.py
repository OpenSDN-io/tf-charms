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


MODULE = "analyticsdb"
BASE_CONFIGS_PATH = "/etc/contrail"

CONFIGS_PATH = BASE_CONFIGS_PATH + "/analytics_database"
IMAGES = {
    500: [
        "{}-node-init",
        "{}-nodemgr",
        "{}-external-kafka",
        "{}-external-cassandra",
        "{}-external-zookeeper",
    ],
    9999: [
        "{}-node-init",
        "{}-nodemgr",
        "{}-analytics-query-engine",
        "{}-external-cassandra",
        "{}-status",
    ],
}
# images for new versions that can be absent in previous releases
IMAGES_OPTIONAL = [
    "{}-provisioner",
]
SERVICES = {
    500: {
        "database": [
            "kafka",
            "nodemgr",
            "zookeeper",
            "cassandra"
        ]
    },
    9999: {
        "database": [
            "query-engine",
            "nodemgr",
            "cassandra",
        ]
    }
}


def get_cluster_info(address_type, own_ip):
    cluster_info = dict()
    for rid in relation_ids("analyticsdb-cluster"):
        for unit in related_units(rid):
            ip = relation_get(address_type, unit, rid)
            if ip:
                cluster_info[unit] = ip
    # add it's own ip address
    cluster_info[local_unit()] = own_ip
    return cluster_info


def servers_ctx():
    data = {
        "controller_servers": common_utils.json_loads(config.get("controller_ips"), list()),
        "control_servers": common_utils.json_loads(config.get("controller_data_ips"), list()),
        "analytics_servers": get_analytics_list()
    }
    return data


def get_analytics_list():
    analytics_ip_list = config.get("analytics_ips")
    if analytics_ip_list is not None:
        return common_utils.json_loads(analytics_ip_list, list())

    # NOTE: use old way of collecting ips.
    # previously we collected units by private-address
    # now we take collected list from leader through relation
    log("analytics_ips is not in config. calculating...")
    analytics_ip_list = []
    for rid in relation_ids("contrail-analyticsdb"):
        for unit in related_units(rid):
            utype = relation_get("unit-type", unit, rid)
            ip = relation_get("private-address", unit, rid)
            if ip and utype == "analytics":
                analytics_ip_list.append(ip)
    return analytics_ip_list


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
    ctx["analyticsdb_minimum_diskgb"] = config.get("cassandra-minimum-diskgb")
    ctx["jvm_extra_opts"] = config.get("cassandra-jvm-extra-opts")
    ctx["container_registry"] = config.get("docker-registry")
    ctx["contrail_version_tag"] = config.get("image-tag")
    ctx["config_analytics_ssl_available"] = common_utils.is_config_analytics_ssl_available()
    ctx["logging"] = common_utils.container_engine().render_logging()
    ctx["contrail_version"] = common_utils.get_contrail_version()
    ctx["image_prefix"] = common_utils.get_image_prefix()
    ctx["container_runtime"] = config.get("container_runtime")
    ctx.update(common_utils.json_loads(config.get("orchestrator_info"), dict()))
    if not ctx.get("cloud_orchestrators"):
        ctx["cloud_orchestrators"] = [ctx.get("cloud_orchestrator")] if ctx.get("cloud_orchestrator") else list()

    ctx["analyticsdb_servers"] = list(common_utils.json_loads(leader_get("cluster_info"), dict()).values())
    ctx.update(servers_ctx())
    log("CTX: {}".format(ctx))
    ctx.update(common_utils.json_loads(config.get("auth_info"), dict()))
    return ctx


def pull_images():
    tag = config.get('image-tag')
    image_prefix = common_utils.get_image_prefix()
    ctx = get_context()
    images = IMAGES.get(ctx["contrail_version"], IMAGES.get(9999))
    for image in images:
        try:
            common_utils.container_engine().pull(image.format(image_prefix), tag)
        except Exception as e:
            log("Can't load image {}".format(e), level=ERROR)
            raise Exception('Image could not be pulled: {}:{}'.format(image.format(image_prefix), tag))
    for image in IMAGES_OPTIONAL:
        try:
            common_utils.container_engine().pull(image.format(image_prefix), tag)
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
    if not ctx.get("analytics_servers"):
        missing_relations.append("contrail-analytics")
    if config.get('tls_present', False) != config.get('ssl_enabled', False):
        missing_relations.append("tls-certificates")
    if missing_relations:
        status_set('blocked',
                   'Missing or incomplete relations: ' + ', '.join(missing_relations))
        return
    if len(ctx.get("analyticsdb_servers")) < config.get("min-cluster-size"):
        status_set('blocked',
                   'Count of cluster nodes is not enough ({} < {}).'.format(
                       len(ctx.get("analyticsdb_servers")), config.get("min-cluster-size")
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
    changed = changed_dict["common"] or changed_dict["analytics-database"]
    common_utils.container_engine().compose_run(CONFIGS_PATH + "/docker-compose.yaml", changed)

    common_utils.update_services_status(MODULE, SERVICES.get(ctx["contrail_version"], SERVICES.get(9999)))


def _render_configs(ctx):
    result = dict()

    tfolder = '5.0' if ctx["contrail_version"] == 500 else '5.1'
    result["common"] = common_utils.apply_keystone_ca(MODULE, ctx)
    result["common"] |= common_utils.render_and_log(
        tfolder + "/analytics-database.env",
        BASE_CONFIGS_PATH + "/common_analyticsdb.env", ctx)
    result["analytics-database"] = common_utils.render_and_log(
        tfolder + "/analytics-database.yaml",
        CONFIGS_PATH + "/docker-compose.yaml", ctx)

    return result


def update_nrpe_config():
    plugins_dir = '/usr/local/lib/nagios/plugins'
    nrpe_compat = nrpe.NRPE()
    common_utils.rsync_nrpe_checks(plugins_dir)
    common_utils.add_nagios_to_sudoers()

    ctl_status_shortname = 'check_contrail_status_' + MODULE
    nrpe_compat.add_check(
        shortname=ctl_status_shortname,
        description='Check status',
        check_cmd=common_utils.contrail_status_cmd(MODULE, plugins_dir)
    )

    nrpe_compat.write()


def stop_analyticsdb():
    common_utils.container_engine().compose_down(CONFIGS_PATH + "/docker-compose.yaml")


def remove_created_files():
    # Removes all config files, environment files, etc.
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/common_analyticsdb.env")
    common_utils.remove_file_safe(CONFIGS_PATH + "/docker-compose.yaml")


# ZUI code block

ziu_relations = [
    "contrail-analyticsdb",
    "analyticsdb-cluster",
]


def config_set(key, value):
    if value is not None:
        config[key] = value
    else:
        config.pop(key, None)
    config.save()


def get_int_from_relation(name, unit=None, rid=None):
    value = relation_get(name, unit, rid)
    return int(value if value else -1)


def signal_ziu(key, value):
    log("ZIU: signal {} = {}".format(key, value))
    for rname in ziu_relations:
        for rid in relation_ids(rname):
            relation_set(relation_id=rid, relation_settings={key: value})
    config_set(key, value)


def sequential_ziu_stage(stage, action):
    prev_ziu_done = stage
    units = [(local_unit(), int(config.get("ziu_done", -1)))]
    for rid in relation_ids("analyticsdb-cluster"):
        for unit in related_units(rid):
            units.append((unit, get_int_from_relation("ziu_done", unit, rid)))
    units.sort(key=lambda x: x[0])
    log("ZIU: sequental stage status {}".format(units))
    for unit in units:
        if unit[0] == local_unit() and prev_ziu_done == stage and unit[1] < stage:
            action(stage)
            return
        prev_ziu_done = unit[1]


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


def ziu_stage_4(ziu_stage, trigger):
    # restart DB
    sequential_ziu_stage(ziu_stage, ziu_restart_db)


def ziu_stage_6(ziu_stage, trigger):
    # finish
    signal_ziu("ziu", None)
    signal_ziu("ziu_done", None)


def ziu_restart_db(stage):
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(CONFIGS_PATH + "/docker-compose.yaml")

    result = common_utils.update_services_status(MODULE, SERVICES.get(ctx["contrail_version"], SERVICES.get(9999)))
    if result:
        signal_ziu("ziu_done", stage)


stages = {
    0: ziu_stage_0,
    1: ziu_stage_noop,
    2: ziu_stage_noop,
    3: ziu_stage_noop,
    4: ziu_stage_4,
    5: ziu_stage_noop,
    6: ziu_stage_6,
}
