import json
import hashlib
import os
import re
import shutil
import base64
from socket import gaierror, gethostbyname, gethostname, getfqdn
from subprocess import CalledProcessError, check_call, check_output

import netifaces

import containerd_engine
import docker_engine
from charmhelpers.contrib.network.ip import (
    get_address_in_network,
    get_iface_addr
)
from charmhelpers.core.hookenv import (
    ERROR,
    application_version_set,
    charm_dir,
    config,
    log,
    status_set,
    local_unit
)
from charmhelpers.core.host import (
    file_hash,
    rsync,
    write_file,
)
from charmhelpers.core.templating import render
from charmhelpers.fetch import apt_install

config = config()

_container_engine = None


def container_engine():
    global _container_engine
    if _container_engine:
        return _container_engine

    if config.get("container_runtime", "docker") == "containerd":
        _container_engine = containerd_engine.Containerd()
    else:
        _container_engine = docker_engine.Docker()
    return _container_engine


def get_ip(config_param="control-network", fallback=None):
    networks = config.get(config_param)
    if networks:
        for network in networks.replace(',', ' ').split():
            # try to get ip from CIDR
            try:
                ip = get_address_in_network(network, fatal=True)
                return ip
            except Exception:
                pass
            # try to get ip from interface name
            try:
                return get_iface_addr(network, fatal=True)[0]
            except Exception:
                pass

    return fallback if fallback else _get_default_ip()


def _get_default_ip():
    if hasattr(netifaces, "gateways"):
        iface = netifaces.gateways()["default"][netifaces.AF_INET][1]
    else:
        data = check_output("ip route | grep ^default", shell=True).decode('UTF-8').split()
        iface = data[data.index("dev") + 1]
    return netifaces.ifaddresses(iface)[netifaces.AF_INET][0]["addr"]


def fix_hostname():
    hostname = gethostname()
    try:
        gethostbyname(hostname)
    except gaierror:
        ip = get_ip()
        check_call([
            "sed", "-E", "-i", "-e",
            ("/127.0.0.1[[:blank:]]+/a \\\n" + ip + " " + hostname),
            "/etc/hosts"])


def decode_cert_from_config(key):
    val = config.get(key)
    if not val:
        return None
    return decode_cert(val)


def decode_cert(cert):
    try:
        return base64.b64decode(cert).decode()
    except Exception as e:
        log("Couldn't decode certificate: {}".format(e), level=ERROR)
    return None


def encode_cert(cert):
    return base64.b64encode(cert.encode())


def save_file(path, data, perms=0o400):
    if data:
        fdir = os.path.dirname(path)
        if not os.path.exists(fdir):
            os.makedirs(fdir)
        write_file(path, data, perms=perms)
    elif os.path.exists(path):
        os.remove(path)


def remove_file_safe(path):
    _try_os(os.remove, path)


def get_contrail_status_txt(module, services):
    try:
        prefix = get_image_prefix()
        output = check_output(f"export CONTRAIL_STATUS_CONTAINER_NAME={prefix}-status-{module} ; {prefix}-status", shell=True).decode('UTF-8')
    except Exception as e:
        log("Container is not ready to get status: " + str(e))
        status_set("waiting", "Waiting services to run in container")
        return False

    statuses = dict()
    group = None
    for line in output.splitlines()[1:]:
        words = line.split()
        if len(words) == 4 and words[0] == "==" and words[3] == "==":
            group = words[2]
            continue
        if len(words) == 0:
            group = None
            continue
        if group and len(words) >= 2 and group in services:
            srv = words[0].split(":")[0]
            statuses.setdefault(group, dict()).update(
                {srv: ' '.join(words[1:])})
    return statuses


def get_contrail_status_json(module, services):
    try:
        prefix = get_image_prefix()
        output = json.loads(check_output(f"export CONTRAIL_STATUS_CONTAINER_NAME={prefix}-status-{module} ; {prefix}-status --format json", shell=True).decode('UTF-8'))
    except Exception as e:
        log("Container is not ready to get status: " + str(e))
        status_set("waiting", "Waiting services to run in container")
        return False

    statuses = dict()
    for group in output["pods"]:
        statuses[group] = dict()
        for item in output["pods"][group]:
            statuses[group].update(item)
    return statuses


def update_services_status(module, services):
    contrail_version = get_contrail_version()

    if contrail_version > 1912:
        statuses = get_contrail_status_json(module, services)
    else:
        statuses = get_contrail_status_txt(module, services)

    if not statuses:
        return False

    for group in services:
        if group not in statuses:
            status_set("waiting",
                       "POD " + group + " is absent in the status")
            return False
        # expected services
        for srv in services[group]:
            # actual statuses
            # actual service name can be present as a several workers like 'api-0', 'api-1', ...
            stats = [statuses[group][x] for x in statuses[group] if x == srv or x.startswith(srv + '-')]
            if not stats:
                status_set("waiting",
                           srv + " is absent in the status")
                return False
            for status in stats:
                if status not in ["active", "backup"]:
                    workload = "waiting" if status == "initializing" else "blocked"
                    status_set(workload, "{} is not ready. Reason: {}"
                               .format(srv, status))
                    return False

    status_set("active", "Unit is ready")
    try:
        tag = config.get('image-tag')
        container_engine().pull("opensdn-node-init", tag)
        version = container_engine().get_contrail_version("opensdn-node-init", tag)
        application_version_set(version)
    except CalledProcessError as e:
        log("Couldn't detect installed application version: " + str(e))
    return True


def json_loads(data, default=None):
    return json.loads(data) if data else default


def apply_keystone_ca(module, ctx):
    ks_ca_path = "/etc/contrail/ssl/{}/keystone-ca-cert.pem".format(module)
    ks_ca_hash = file_hash(ks_ca_path)
    ks_ca = ctx.get("keystone_ssl_ca")
    save_file(ks_ca_path, ks_ca, 0o444)
    ks_ca_hash_new = file_hash(ks_ca_path)
    if ks_ca:
        ctx["keystone_ssl_ca_path"] = "/etc/contrail/ssl/keystone-ca-cert.pem"
    ca_changed = (ks_ca_hash != ks_ca_hash_new)
    if ca_changed:
        log("Keystone CA cert has been changed: {h1} != {h2}"
            .format(h1=ks_ca_hash, h2=ks_ca_hash_new))
    return ca_changed


def get_tls_settings(self_ip):
    hostname = getfqdn()
    cn = hostname.split(".")[0]
    sans = [hostname]
    if hostname != cn:
        sans.append(cn)
    sans_ips = []
    try:
        sans_ips.append(gethostbyname(hostname))
    except Exception:
        pass
    control_ip = self_ip
    if control_ip not in sans_ips:
        sans_ips.append(control_ip)
    res = check_output(['getent', 'hosts', control_ip]).decode('UTF-8')
    control_name = res.split()[1].split('.')[0]
    if control_name not in sans:
        sans.append(control_name)
    sans_ips.append("127.0.0.1")
    sans.extend(sans_ips)
    settings = {
        'sans': json.dumps(sans),
        'common_name': cn,
        'certificate_name': cn
    }
    log("TLS_CTX: {}".format(settings))
    return settings


def tls_changed(module, rel_data):
    if not rel_data:
        # departed case
        cert = key = ca = None
    else:
        # changed case
        unitname = local_unit().replace('/', '_')
        cert_name = '{0}.server.cert'.format(unitname)
        key_name = '{0}.server.key'.format(unitname)
        cert = rel_data.get(cert_name)
        key = rel_data.get(key_name)
        ca = rel_data.get('ca')
        if not cert or not key or not ca:
            log("tls-certificates client's relation data is not fully available. Rel data: {}".format(rel_data))
            cert = key = ca = None

    changed = update_certificates(module, cert, key, ca)
    if not changed:
        log("Certificates were not changed.")
        return False

    log("Certificates have been changed. Rewrite configs and rerun services.")
    if cert is not None and len(cert) > 0:
        config["ssl_enabled"] = True
        config["ca_cert"] = ca
    else:
        config["ssl_enabled"] = False
        config.pop("ca_cert", None)
    config.save()
    return True


def _try_os(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except Exception:
        pass


def file_read(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def data_hash(data):
    if data is None:
        return ''
    h = getattr(hashlib, 'md5')()
    if isinstance(data, str):
        h.update(data.encode())
    else:
        h.update(data)
    return h.hexdigest()


def update_certificates(module, cert, key, ca):
    certs_path = "/etc/contrail/ssl/{}".format(module)
    # order is important: containers wait for key file as signal to start
    files = [
        ("/certs/ca-cert.pem", ca, 0o644),
        ("/certs/server.pem", cert, 0o644),
        ("/private/server-privkey.pem", key, 0o640),
    ]
    # create common directories to create symlink
    # this is needed for status
    _try_os(os.makedirs, "/etc/contrail/ssl/certs")
    _try_os(os.makedirs, "/etc/contrail/ssl/private")
    # create before files appear to set correct permisions
    _try_os(os.makedirs, certs_path + "/certs", mode=0o755)
    _try_os(os.makedirs, certs_path + "/private", mode=0o750)
    changed = False
    for fname, data, perms in files:
        cfile = certs_path + fname
        old_hash = data_hash(file_read(cfile))
        new_hash = data_hash(data)
        if old_hash == new_hash:
            continue
        changed = True
        save_file(cfile, data, perms=perms)
        # re-create symlink to common place for status
        _try_os(os.remove, "/etc/contrail/ssl" + fname)
        _try_os(os.symlink, cfile, "/etc/contrail/ssl" + fname)
    return changed


def get_certs_hash(module):
    certs_path = "/etc/contrail/ssl/{}".format(module)
    # order is important: containers wait for key file as signal to start
    files = ["/certs/ca-cert.pem", "/certs/server.pem", "/private/server-privkey.pem"]
    result = ''
    for file in files:
        result += data_hash(file_read(certs_path + file))
    return result


def render_and_log(template, conf_file, ctx, perms=0o600):
    """Returns True if configuration has been changed."""

    log("Render and store new configuration: " + conf_file)
    try:
        with open(conf_file) as f:
            old_lines = set(f.readlines())
    except Exception:
        old_lines = set()

    render(template, conf_file, ctx, perms=perms)
    with open(conf_file) as f:
        new_lines = set(f.readlines())
    new_set = new_lines.difference(old_lines)
    old_set = old_lines.difference(new_lines)
    if not new_set and not old_set:
        log("Configuration file has not been changed.")
    elif not old_lines:
        log("Configuration file has been created and is not logged.")
    else:
        log("New lines set:\n{new}".format(new="".join(new_set)))
        log("Old lines set:\n{old}".format(old="".join(old_set)))
        log("Configuration file has been changed.")

    return bool(new_set or old_set)


def rsync_nrpe_checks(plugins_dir):
    if not os.path.exists(plugins_dir):
        os.makedirs(plugins_dir)

    charm_plugin_dir = os.path.join(charm_dir(),
                                    'files',
                                    'plugins/')
    rsync(charm_plugin_dir,
          plugins_dir,
          options=['--executability'])


def add_nagios_to_sudoers():
    try:
        write_file('/etc/sudoers.d/nagios', 'nagios ALL = NOPASSWD:SETENV: /usr/bin/opensdn-status\nnagios ALL = NOPASSWD:SETENV: /usr/bin/contrail-status\n')
    except Exception as err:
        log('Failed to run cmd: {}'.format(err.cmd))


def get_contrail_version():
    """
    Function returns contrail version from image-tag in comparable format.
    Returned value is integer looks like 500 (for 5.0 version) or 2002 for 2002 version
    If container tag is 'latest' or if the version cannot be evaluated then
    version will be set to 9999
    If someone changes the naming conventions, he must make changes in this function to support these new conventions.
    """
    tag = config.get("image-tag")
    if 'master' in tag:
        return 9999
    for release in [r"21\d\d", r"20\d\d", r"19\d\d"]:
        tag_date = re.findall(release, tag)
        if len(tag_date) != 0:
            return int(tag_date[0])
    for release in [r"21\.\d", r"22\.\d", r"24\.\d", r"25\.\d", r"26\.\d", r"27\.\d"]:
        tag_date = re.findall(release, tag)
        if len(tag_date) != 0:
            ver_split = tag_date[0].split(".")
            version = int(ver_split[0]) * 100 + int(ver_split[1])
            return version

    if '5.1' in tag:
        return 510
    elif '5.0' in tag:
        return 500

    # master/latest version
    return 9999


# started 25.* version we have docker images with 'opensdn' prefix instead of old 'contrail' prefix
def get_image_prefix():
    return 'opensdn' if get_contrail_version() > 2401 else 'contrail'


def contrail_status_cmd(name, plugins_dir):
    script_name = 'check_contrail_status_{}.py'.format(name)
    contrail_version = get_contrail_version()

    check_contrail_status_script = os.path.join(plugins_dir, script_name)
    check_contrail_status_cmd = (
        '{} {}'
        .format(check_contrail_status_script, contrail_version)
    )
    return check_contrail_status_cmd


def is_config_analytics_ssl_available():
    return (get_contrail_version() >= 1910)


def http_services(service_name, vip, port):
    name = local_unit().replace("/", "-")
    addr = get_ip()

    mode = config.get("haproxy-http-mode", "http")
    ssl_on_backend = config.get("ssl_enabled", False) and is_config_analytics_ssl_available()
    if ssl_on_backend:
        servers = [[name, addr, port, "check inter 2000 rise 2 fall 3 ssl verify none"]]
    else:
        servers = [[name, addr, port, "check inter 2000 rise 2 fall 3"]]

    result = [{
        "service_name": service_name,
        "service_host": vip,
        "service_port": port,
        "servers": servers}]
    if mode == 'http':
        result[0]['service_options'] = [
            "timeout client 3m",
            "option nolinger",
            "timeout server 3m",
            "balance source"]
    else:
        result[0]['service_options'] = [
            "mode http",
            "balance source",
            "hash-type consistent",
            "http-request set-header X-Forwarded-Proto https if { ssl_fc }",
            "http-request set-header X-Forwarded-Proto http if !{ ssl_fc }",
            "option httpchk GET /",
            "option forwardfor",
            "redirect scheme https code 301 if { hdr(host) -i " + str(vip) + " } !{ ssl_fc }",
            "rsprep ^Location:\\ http://(.*) Location:\\ https://\\1"]
        result[0]['crts'] = ["DEFAULT"]

    return result


def https_services_tcp(service_name, vip, port):
    name = local_unit().replace("/", "-")
    addr = get_ip()
    return [
        {"service_name": service_name,
         "service_host": vip,
         "service_port": port,
         "service_options": [
             "mode tcp",
             "option tcplog",
             "balance source",
             "cookie SERVERID insert indirect nocache",
         ],
         "servers": [[
             name, addr, port,
             "cookie " + addr + " weight 1 maxconn 1024 check port " + str(port)]]},
    ]


def https_services_http(service_name, vip, port):
    name = local_unit().replace("/", "-")
    addr = get_ip()
    return [
        {"service_name": service_name,
         "service_host": vip,
         "service_port": port,
         "crts": ["DEFAULT"],
         "service_options": [
             "mode http",
             "balance source",
             "hash-type consistent",
             "http-request set-header X-Forwarded-Proto https if { ssl_fc }",
             "http-request set-header X-Forwarded-Proto http if !{ ssl_fc }",
             "option httpchk GET /",
             "option forwardfor",
             "redirect scheme https code 301 if { hdr(host) -i " + str(vip) + " } !{ ssl_fc }",
             "rsprep ^Location:\\ http://(.*) Location:\\ https://\\1",
         ],
         "servers": [[
             name, addr, port,
             "check fall 5 inter 2000 rise 2 ssl verify none"]]},
    ]


def configure_ports(func, ports):
    for port in ports:
        try:
            func(port, "TCP")
        except Exception:
            pass


def add_logrotate():
    # now the file is hardcoded, may be parameterized later
    apt_install('logrotate')
    shutil.copy("./files/logrotate", "/etc/logrotate.d/contrail")
