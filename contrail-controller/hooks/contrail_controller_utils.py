import os
import tempfile
import time
import socket

from charmhelpers.core.hookenv import (
    action_fail,
    config,
    is_leader,
    in_relation_hook,
    local_unit,
    related_units,
    relation_ids,
    relation_get,
    relation_set,
    status_set,
    leader_get,
    log,
    INFO,
    ERROR,
)
from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.core.unitdata import kv
import common_utils

config = config()

MODULE = "controller"

BASE_CONFIGS_PATH = "/etc/contrail"

CONFIG_API_CONFIGS_PATH = BASE_CONFIGS_PATH + "/config_api"
CONFIG_DATABASE_CONFIGS_PATH = BASE_CONFIGS_PATH + "/config_database"
CONTROL_CONFIGS_PATH = BASE_CONFIGS_PATH + "/control"
WEBUI_CONFIGS_PATH = BASE_CONFIGS_PATH + "/webui"
REDIS_CONFIGS_PATH = BASE_CONFIGS_PATH + "/redis"

IMAGES = [
    "{}-node-init",
    "{}-nodemgr",
    "{}-controller-config-api",
    "{}-controller-config-svcmonitor",
    "{}-controller-config-schema",
    "{}-controller-config-devicemgr",
    "{}-controller-control-control",
    "{}-controller-control-named",
    "{}-controller-control-dns",
    "{}-controller-webui-web",
    "{}-controller-webui-job",
    "{}-external-cassandra",
    "{}-external-zookeeper",
    "{}-external-rabbitmq",
    "{}-external-redis",
    "{}-status",
]
# images for new versions that can be absent in previous releases
IMAGES_OPTIONAL = [
    "{}-provisioner",
    "{}-controller-config-dnsmasq",
]

SERVICES = {
    "control": [
        "control",
        "nodemgr",
        "named",
        "dns",
    ],
    "config-database": [
        "nodemgr",
        "zookeeper",
        "rabbitmq",
        "cassandra",
    ],
    "webui": [
        "web",
        "job",
    ],
    "config": [
        "svc-monitor",
        "nodemgr",
        "device-manager",
        "api",
        "schema",
    ],
}


def get_controller_ips(address_type, own_ip):
    controller_ips = dict()
    for rid in relation_ids("controller-cluster"):
        for unit in related_units(rid):
            ip = relation_get(address_type, unit, rid)
            if ip:
                controller_ips[unit] = ip
    controller_ips[local_unit()] = own_ip
    return controller_ips


def get_analytics_list():
    analytics_ip_list = config.get("analytics_ips")
    if analytics_ip_list is not None:
        return common_utils.json_loads(analytics_ip_list, list())

    # NOTE: use old way of collecting ips.
    # previously we collected units by private-address
    # now we take collected list from leader through relation
    log("analytics_ips is not in config. calculating...")
    analytics_ip_list = []
    for rid in relation_ids("contrail-analytics"):
        for unit in related_units(rid):
            ip = relation_get("private-address", unit, rid)
            if ip:
                analytics_ip_list.append(ip)
    return analytics_ip_list


def is_analyticsdb_enabled():
    """Check if analyticsdb is enabled on cluster"""

    analyticsdb_enabled = True
    if common_utils.get_contrail_version() > 500:
        analyticsdb_enabled = False
        for rid in relation_ids("contrail-analyticsdb"):
            if related_units(rid):
                analyticsdb_enabled = True
                break

    return analyticsdb_enabled


def get_context():
    ctx = {}
    ctx["module"] = MODULE
    ctx["log_level"] = config.get("log-level", "SYS_NOTICE")
    ctx["bgp_asn"] = config.get("bgp-asn", "64512")
    ctx["enable_4byte_as"] = config.get("enable-4byte-as")
    ctx["encap_priority"] = config.get("encap-priority")
    ctx["vxlan_vn_id_mode"] = config.get("vxlan-vn-id-mode")
    ctx["flow_export_rate"] = config.get("flow-export-rate")
    ctx["auth_mode"] = config.get("auth-mode")
    ctx["cloud_admin_role"] = config.get("cloud-admin-role")
    ctx["global_read_only_role"] = config.get("global-read-only-role")
    ctx["configdb_minimum_diskgb"] = config.get("cassandra-minimum-diskgb")
    ctx["jvm_extra_opts"] = config.get("cassandra-jvm-extra-opts")
    ctx["container_registry"] = config.get("docker-registry")
    ctx["contrail_version_tag"] = config.get("image-tag")
    ctx["contrail_version"] = common_utils.get_contrail_version()
    ctx["image_prefix"] = common_utils.get_image_prefix()
    ctx["config_api_worker_count"] = config.get("config-api-worker-count")
    ctx["apply_defaults"] = config.get("apply-defaults")
    ctx["huge_scale"] = config.get("huge-scale", False)
    ctx["container_runtime"] = config.get("container_runtime")
    ctx.update(common_utils.json_loads(config.get("orchestrator_info"), dict()))
    if not ctx.get("cloud_orchestrators"):
        ctx["cloud_orchestrators"] = [ctx.get("cloud_orchestrator")] if ctx.get("cloud_orchestrator") else list()

    ctx["ssl_enabled"] = config.get("ssl_enabled", False)
    ctx["certs_hash"] = common_utils.get_certs_hash(MODULE) if ctx["ssl_enabled"] else ''
    ctx["config_analytics_ssl_available"] = common_utils.is_config_analytics_ssl_available()
    ctx["use_internal_endpoints"] = config.get("use_internal_endpoints", False)
    ctx["logging"] = common_utils.container_engine().render_logging()

    ips = common_utils.json_loads(leader_get("controller_ip_list"), list())
    data_ips = common_utils.json_loads(leader_get("controller_data_ip_list"), list())
    ctx["controller_servers"] = ips
    ctx["control_servers"] = data_ips
    ctx["analytics_servers"] = get_analytics_list()
    ctx["analyticsdb_enabled"] = is_analyticsdb_enabled()
    log("CTX: " + str(ctx))
    ctx.update(common_utils.json_loads(config.get("auth_info"), dict()))
    return ctx


def pull_images():
    tag = config.get('image-tag')
    image_prefix = common_utils.get_image_prefix()
    for image in IMAGES:
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
    if config.get("maintenance"):
        log("ISSU Maintenance is in progress")
        status_set('maintenance', 'issu is in progress')
        return
    if int(config.get("ziu", -1)) > -1:
        log("ZIU Maintenance is in progress")
        status_set('maintenance',
                   'ziu is in progress - stage/done = {}/{}'.format(config.get("ziu"), config.get("ziu_done")))
        return

    ctx = get_context()
    _update_charm_status(ctx)


def _update_charm_status(ctx, services_to_run=None):
    # services to run: config-api, control, config-database, webui, redis
    missing_relations = []
    if not ctx.get("analytics_servers"):
        missing_relations.append("contrail-analytics")
    if common_utils.get_ip() not in ctx.get("controller_servers"):
        missing_relations.append("contrail-cluster")
    if config.get('tls_present', False) != config.get('ssl_enabled', False):
        missing_relations.append("tls-certificates")
    if missing_relations:
        status_set('blocked',
                   'Missing or incomplete relations: ' + ', '.join(missing_relations))
        return
    if len(ctx.get("controller_servers")) < config.get("min-cluster-size"):
        status_set('blocked',
                   'Count of cluster nodes is not enough ({} < {}).'.format(
                       len(ctx.get("controller_servers")), config.get("min-cluster-size")
                   ))
        return
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked',
                   'Missing cloud orchestrator info in relations.')
        return
    if "openstack" in ctx.get("cloud_orchestrators") and not ctx.get("keystone_ip"):
        status_set('blocked',
                   'Missing auth info in relation with contrail-auth.')
        return
    # TODO: what should happens if relation departed?

    changed_dict = _render_configs(ctx)
    changed = changed_dict["common"]

    service_changed = changed_dict["config-api"]
    common_utils.container_engine().compose_run(CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    service_changed = changed_dict["config-database"]
    common_utils.container_engine().compose_run(CONFIG_DATABASE_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    service_changed = changed_dict["control"]
    common_utils.container_engine().compose_run(CONTROL_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    service_changed = changed_dict["webui"]
    common_utils.container_engine().compose_run(WEBUI_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    # redis is a common service that needs own synchronized env
    service_changed = changed_dict["redis"]
    common_utils.container_engine().compose_run(REDIS_CONFIGS_PATH + "/docker-compose.yaml", changed or service_changed)

    common_utils.update_services_status(MODULE, SERVICES)

    if has_provisioning_finished():
        config['apply-defaults'] = False


def has_provisioning_finished():
    result_config = _has_provisioning_finished_for_container(CONFIG_API_CONFIGS_PATH)
    log("Readyness of provisioner for configapi: {}".format(result_config))
    # TODO: remove checking of contol for R2008 when provisioner will be ready
    result_control = _has_provisioning_finished_for_container(CONTROL_CONFIGS_PATH)
    log("Readyness of provisioner for control: {}".format(result_control))

    return result_config and result_control


def _has_provisioning_finished_for_container(configs_path):
    cnt_id = common_utils.container_engine().get_container_id(configs_path + "/docker-compose.yaml", "provisioner")
    try:
        # check tail first. for R2008 and further this should work
        data = common_utils.container_engine().execute(cnt_id, ['ps', '-ax'])
        return '/provision.sh' not in data
    except Exception:
        pass
    try:
        # for R2005 let's check exit status
        state = common_utils.container_engine().get_container_state(configs_path + "/docker-compose.yaml", "provisioner")
        if not state:
            return False
        if state.get('Status').lower() == 'running':
            return False
        if state.get('ExitCode') != 0:
            return False
        return True
    except Exception:
        pass
    return False


def _render_configs(ctx):
    result = dict()

    result['common'] = common_utils.apply_keystone_ca(MODULE, ctx)
    result['common'] |= common_utils.render_and_log(
        "config.env", BASE_CONFIGS_PATH + "/common_config.env", ctx)
    result['common'] |= common_utils.render_and_log(
        "defaults.env", BASE_CONFIGS_PATH + "/defaults_controller.env", ctx)

    result['config-api'] = common_utils.render_and_log(
        "config-api.yaml", CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    result['config-database'] = common_utils.render_and_log(
        "config-database.yaml", CONFIG_DATABASE_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    result['control'] = common_utils.render_and_log(
        "control.yaml", CONTROL_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    result['webui'] = common_utils.render_and_log(
        "webui.yaml", WEBUI_CONFIGS_PATH + "/docker-compose.yaml", ctx)
    result['webui'] |= common_utils.render_and_log(
        "web.env", BASE_CONFIGS_PATH + "/common_web.env", ctx)

    # redis is a common service that needs own synchronized env
    result['redis'] = common_utils.render_and_log(
        "redis.env", BASE_CONFIGS_PATH + "/redis.env", ctx)
    result['redis'] |= common_utils.render_and_log(
        "redis.yaml", REDIS_CONFIGS_PATH + "/docker-compose.yaml", ctx)

    return result


def update_hosts_file(ip, hostname, remove_hostname=False):
    """Update /etc/hosts and template files with cluster names and IPs.

    RabbitMQ requires NODE names in a cluster to be resolvable.
    https://www.rabbitmq.com/clustering.html#issues-hostname

    In a multi-homed host scenario cluster IPs may have FQDNs structured
    as interface_name.host_name.domain which will result in an issue if
    a short name is derived from an FQDN by taking its first part.
    See https://github.com/Juniper/contrail-charms/issues/50

    This function updates /etc/hosts file with resolutions for IP -> hostname
    lookups. Also it updates template /etc/cloud/templates/hosts.debian.tmpl
    """
    _update_hosts_file("/etc/hosts", ip, hostname, remove_hostname=remove_hostname)
    _update_hosts_file("/etc/cloud/templates/hosts.debian.tmpl", ip, hostname, remove_hostname=remove_hostname)

    kvstore = kv()
    rabbitmq_hosts = kvstore.get(key='rabbitmq_hosts', default={})
    if remove_hostname:
        rabbitmq_hosts.pop(ip)
    else:
        # finally, update the unitdata with the notion of new hosts values
        # managed
        rabbitmq_hosts.update({ip: hostname})
    kvstore.set(key='rabbitmq_hosts', value=rabbitmq_hosts)
    # flush the store to persist data to sqlite
    kvstore.flush()


def _update_hosts_file(file, ip, hostname, remove_hostname=False):
    with open(file, 'r') as hosts:
        lines = hosts.readlines()

    log("Updating file {} with: {}:{}, remove={} (current: {})".format(
        file, ip, hostname, remove_hostname, lines),
        level=INFO)

    newlines = []
    hostname_present = False
    for line in lines:
        _line = line.split()
        if len(_line) < 2:
            newlines.append(line)
            continue

        parsed_ip = _line[0]
        parsed_hostname = _line[1]
        aliases = _line[2:]

        # handle a single hostname or alias removal
        if remove_hostname and parsed_ip == ip:
            log("Removing ip:hostname pair: {}:{}".format(ip, hostname))
            aliases = [a for a in aliases if a != hostname]
            if parsed_hostname != hostname or not aliases:
                continue
            newlines.append(' '.join([ip, ' '.join(aliases)]))

        hostname_mismatch = (ip == parsed_ip and hostname != parsed_hostname)
        log("hostname mismatch: {}".format(hostname_mismatch))
        if hostname_mismatch and hostname_present:
            # malformed /etc/hosts - let's let an operator sort this out
            # and retry hook execution if needed
            raise Exception('Multiple lines with ip {} '
                            'encountered'.format(ip))

        if hostname_mismatch and not hostname_present:
            log("Changing an existing entry for {}".format(
                hostname))
            # move the hostname that is already present to aliases and use
            # the one provided by the caller instead
            aliases.append(parsed_hostname)
            aliases = [a for a in aliases if a != hostname]
            newlines.append(' '.join([ip, hostname, ' '.join(aliases)]))
            # set a flag saying that we already encountered that hostname
            hostname_present = True
        elif not hostname_mismatch and not hostname_present:
            log("No hostname mismatches and have not seen {}"
                " in any previous lines".format(hostname))

            if not hostname == parsed_hostname:
                newlines.append("%s %s\n" % (ip, hostname))

            # it's not a mismatch so we need to mark it the hostname as present
            hostname_present = True

            # also need to preserve an old line
            newlines.append(line)
        elif ip != parsed_ip:
            log("Preserving the line as an IP is different: {}".format(line))
            # no mismatches - just keep the line
            newlines.append(line)

    # if we haven't updated any existing lines for this hostname, just add it
    if not hostname_present:
        log("Adding a new entry for {}:{}".format(ip, hostname))
        newlines.append("%s %s\n" % (ip, hostname))

    log("New hosts file contents: {}".format(newlines))

    # create a temporary file in the same directory to ensure that moving
    # it over /etc/hosts is atomic (not done across file systems)
    tdir = os.path.dirname(file)
    with tempfile.NamedTemporaryFile(dir=tdir, delete=False) as tmpfile:
        with open(tmpfile.name, 'w') as hosts:
            for line in newlines:
                hosts.write(line)

    # atomically replace the target file so that application runtimes do not
    # see intermediate changes to the file
    log("moving {} over {}".format(tmpfile.name, file))
    os.rename(tmpfile.name, file)
    os.chmod(file, 0o644)


def get_contrail_rabbit_hostname():
    """Return this unit's hostname.

    @returns hostname
    """
    # /proc/sys/kernel/hostname may contain an FQDN so try to split
    # and take a short name
    return '{}-contrail-rmq'.format(socket.gethostname().split('.')[0])


def update_rabbitmq_cluster_hostnames():
    """Updates /etc/hosts with rabbitmq cluster node hostnames"""
    ip = common_utils.get_ip()
    update_hosts_file(ip, get_contrail_rabbit_hostname())


def get_cassandra_connection_details():
    return {
        "cassandra_address_list": common_utils.json_loads(leader_get("controller_ip_list"), list()),
    }


def get_zookeeper_connection_details():
    return {
        "zookeeper_address_list": common_utils.json_loads(leader_get("controller_ip_list"), list()),
    }


def get_rabbitmq_connection_details():
    return {
        "rabbit_q_name": "vnc-config.issu-queue",
        "rabbit_vhost": "/",
        "rabbit_port": "5673",
        "rabbit_address_list": common_utils.json_loads(leader_get("controller_ip_list"), list()),
    }


def update_issu_state(issu_relation_data):
    ctx = {'old': issu_relation_data}
    ctx["new"] = get_cassandra_connection_details()
    ctx["new"].update(get_rabbitmq_connection_details())
    ctx["new"].update(get_zookeeper_connection_details())

    common_utils.render_and_log("contrail-issu.conf", BASE_CONFIGS_PATH + "/contrail-issu.conf", ctx)
    # TODO run docker


def update_nrpe_config():
    plugins_dir = '/usr/local/lib/nagios/plugins'
    nrpe_compat = nrpe.NRPE()
    component_ip = common_utils.get_ip()
    common_utils.rsync_nrpe_checks(plugins_dir)
    common_utils.add_nagios_to_sudoers()

    check_ui_cmd = 'check_http -H {} -p 8143 -S'.format(component_ip)
    nrpe_compat.add_check(
        shortname='check_contrail_web_ui',
        description='Check Contrail WebUI',
        check_cmd=check_ui_cmd
    )

    ssl_on_backend = config.get("ssl_enabled", False) and common_utils.is_config_analytics_ssl_available()
    if ssl_on_backend:
        check_api_cmd = 'check_http -S -H {} -p 8082'.format(component_ip)
    else:
        check_api_cmd = 'check_http -H {} -p 8082'.format(component_ip)
    nrpe_compat.add_check(
        shortname='check_contrail_api',
        description='Check Contrail API',
        check_cmd=check_api_cmd
    )

    ctl_status_shortname = 'check_contrail_status_' + MODULE
    nrpe_compat.add_check(
        shortname=ctl_status_shortname,
        description='Check status',
        check_cmd=common_utils.contrail_status_cmd(MODULE, plugins_dir)
    )

    nrpe_compat.write()


def stop_controller():
    common_utils.container_engine().compose_down(CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(CONFIG_DATABASE_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(CONTROL_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(WEBUI_CONFIGS_PATH + "/docker-compose.yaml")

    # TODO: Redis is common service. We can't stop it here.
    if os.path.exists(REDIS_CONFIGS_PATH + "/docker-compose.yaml"):
        common_utils.container_engine().compose_down(REDIS_CONFIGS_PATH + "/docker-compose.yaml")


def remove_created_files():
    # Removes all config files, env files, etc.
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/common_config.env")
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/defaults_controller.env")
    common_utils.remove_file_safe(CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(CONFIG_DATABASE_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(CONTROL_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(WEBUI_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/common_web.env")
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/contrail-issu.conf")

    # TODO: Redis is common service. We can't delete its files here.
    common_utils.remove_file_safe(BASE_CONFIGS_PATH + "/redis.env")
    common_utils.remove_file_safe(REDIS_CONFIGS_PATH + "/docker-compose.yaml")


# ZUI code block

ziu_relations = [
    "contrail-analytics",
    "contrail-analyticsdb",
    "controller-cluster",
]


# in seconds
max_stage_timeout = 120


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
    for rid in relation_ids("contrail-controller"):
        relation_set(relation_id=rid, relation_settings={key: value})
    config_set(key, value)


def check_ziu_stage_done_from_relations(stage):
    for rname in ziu_relations:
        for rid in relation_ids(rname):
            for unit in related_units(rid):
                value = relation_get("ziu_done", unit, rid)
                if value is None or int(value) != stage:
                    log("ZIU: stage is not ready: rel={} unit={} value={}".format(rid, unit, value))
                    return False
    return True


def check_ziu_stage_done_from_agents(stage):
    for rid in relation_ids("contrail-controller"):
        for unit in related_units(rid):
            unit_type = relation_get("unit-type", unit, rid)
            if unit_type != "agent":
                continue
            value = relation_get("ziu_done", unit, rid)
            if value is None or int(value) != stage:
                log("ZIU: stage is not ready: rel={} unit={} value={}".format(rid, unit, value))
                return False
    return True


def check_ziu_stage_done(stage):
    log("ZIU: check stage({}) is done".format(stage))
    if int(config.get("ziu_done", -1)) != stage:
        log("ZIU: stage is not ready on local unit ziu_done={}".format(int(config.get("ziu_done", -1))))
        return False

    if not check_ziu_stage_done_from_relations(stage):
        return False

    if not check_ziu_stage_done_from_agents(stage):
        return False

    log("ZIU: stage done")
    return True


def sequential_ziu_stage(stage, action):
    prev_ziu_done = stage
    units = [(local_unit(), int(config.get("ziu_done", -1)))]
    for rid in relation_ids("controller-cluster"):
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
    if ziu_stage is not None:
        ziu_stage = int(ziu_stage)
        config_set("ziu", ziu_stage)
        if ziu_stage > int(config.get("ziu_done", -1)):
            log("ZIU: run stage {}, trigger {}".format(ziu_stage, trigger))
            stages[ziu_stage](ziu_stage, trigger)

    # This code is on controller only
    if not is_leader():
        return
    ziu_stage = config.get("ziu")
    if ziu_stage is None:
        return
    ziu_stage = int(ziu_stage)
    if not check_ziu_stage_done(ziu_stage):
        return
    # move to next stage
    ziu_stage += 1
    signal_ziu("ziu", ziu_stage)

    # last(max) stage must not be called immediately to provide ziu=max_stage to all units
    # all other stages can call stage handler immediately to not wait for update_status
    # moreover - we must wait some time to let work relation_set(max_stage) to avoid
    # early cleaning it by update_status or any other hook
    config.pop("max_stage_time_start", None)
    log("ZIU: drop start time for max stage")
    max_stage = max(stages.keys())
    if ziu_stage != max_stage:
        try:
            # do not fail the process - in case of unhandled exception
            # relation is not updated and other units didn't receive stage update
            log("ZIU: run stage immediately {}, trigger {}".format(ziu_stage, trigger))
            stages[ziu_stage](ziu_stage, trigger)
        except Exception as e:
            log("Immediate stage run failed {}. will be retried in next hook".format(e))
    else:
        max_stage_time_start = time.time()
        config["max_stage_time_start"] = max_stage_time_start
        log("ZIU: set start time for max stage = {}".format(max_stage_time_start))
    config.save()


def ziu_stage_noop(ziu_stage, trigger):
    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_0(ziu_stage, trigger):
    # update images
    if trigger == "image-tag":
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_1(ziu_stage, trigger):
    # stop API services
    common_utils.container_engine().compose_down(CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(WEBUI_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_down(REDIS_CONFIGS_PATH + "/docker-compose.yaml")
    signal_ziu("ziu_done", ziu_stage)


def ziu_stage_2(ziu_stage, trigger):
    # start API services
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(CONFIG_API_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_run(WEBUI_CONFIGS_PATH + "/docker-compose.yaml")
    common_utils.container_engine().compose_run(REDIS_CONFIGS_PATH + "/docker-compose.yaml")

    result = common_utils.update_services_status(MODULE, SERVICES)
    if result:
        signal_ziu("ziu_done", ziu_stage)


def ziu_stage_3(ziu_stage, trigger):
    # restart control one by one
    sequential_ziu_stage(ziu_stage, ziu_restart_control)


def ziu_stage_4(ziu_stage, trigger):
    # restart DB containers
    sequential_ziu_stage(ziu_stage, ziu_restart_db)


def ziu_stage_6(ziu_stage, trigger):
    max_stage_time_start = config.get("max_stage_time_start", None)
    if is_leader() and max_stage_time_start is not None:
        # see comment in update_ziu
        time_passed = time.time() - max_stage_time_start
        log("ZIU: stage 6 start = {}, time passed = {}".format(max_stage_time_start, time_passed))
        if time_passed < max_stage_timeout:
            return
        config.pop("max_stage_time_start")

    # finish
    signal_ziu("ziu", None)
    signal_ziu("ziu_done", None)


def ziu_restart_control(stage):
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(CONTROL_CONFIGS_PATH + "/docker-compose.yaml")

    result = common_utils.update_services_status(MODULE, SERVICES)
    if result:
        signal_ziu("ziu_done", stage)


def ziu_restart_db(stage):
    ctx = get_context()
    _render_configs(ctx)
    common_utils.container_engine().compose_run(CONFIG_DATABASE_CONFIGS_PATH + "/docker-compose.yaml")

    result = common_utils.update_services_status(MODULE, SERVICES)
    if result:
        signal_ziu("ziu_done", stage)


def finish_ziu():
    if not check_ziu_stage_done_from_relations(5):
        action_fail("ZIU: stage 5 is not completed for controllers. Process couldn't be finished manually.")
        return

    log("ZIU: forcing stage 6 - skip broken agents if they are present.")
    signal_ziu("ziu", 6)


stages = {
    0: ziu_stage_0,
    1: ziu_stage_1,
    2: ziu_stage_2,
    3: ziu_stage_3,
    4: ziu_stage_4,
    5: ziu_stage_noop,
    6: ziu_stage_6,
}
