import os
import uuid
import requests

from distutils.dir_util import copy_tree
import shutil

from charmhelpers.core.hookenv import (
    application_version_set,
    config,
    log,
    status_set,
    ERROR,
)

from charmhelpers.core.host import (
    lsb_release,
)

import common_utils
from subprocess import (
    check_call
)


config = config()

MODULE = "command"
BASE_CONFIGS_PATH = "/etc/contrail"

CONFIGS_PATH = BASE_CONFIGS_PATH + "/contrail-command"
IMAGES = [
    "contrail-command-deployer",
    "contrail-command",
    "opensdn-status",
]
SERVICES = {}


def get_context():
    ctx = {}
    ctx["module"] = MODULE
    ctx["log_level"] = config.get("log-level", "SYS_NOTICE")
    ctx["container_registry"] = config.get("docker-registry")
    ctx["container_tag"] = config.get("image-tag")
    ctx["image_prefix"] = common_utils.get_image_prefix()

    ctx["command_ip"] = common_utils.get_ip()
    ctx["contrail_container_tag"] = config.get("image-tag")

    ctx.update(common_utils.json_loads(config.get("orchestrator_info"), dict()))
    if not ctx.get("cloud_orchestrators"):
        ctx["cloud_orchestrators"] = [ctx.get("cloud_orchestrator")] if ctx.get("cloud_orchestrator") else list()

    log("CTX: {}".format(ctx))
    return ctx


def deploy_ccd_code(image, tag):
    common_utils.container_engine().remove_container_by_image(image)

    name = common_utils.container_engine().create(image, tag)
    try:
        src = '/' + image
        tmp_folder = os.path.join('/tmp', str(uuid.uuid4()))
        common_utils.container_engine().cp(name, src, tmp_folder)
        try:
            os.mkdir(tmp_folder + '/docker')
            os.mkdir('/etc/ansible')
        except Exception:
            pass

        common_utils.container_engine().cp(name, '/bin/deploy_contrail_command',
                                           tmp_folder + '/docker/')
        common_utils.container_engine().cp(name, '/etc/ansible/ansible.cfg', '/etc/ansible/')

        dst = '/' + image
        copy_tree(tmp_folder, dst)

        # patch command code for ubuntu
        try:
            check_call('patch --strip 1 -d ' + dst + '< ./files/libselinux.diff', shell=True)
        except Exception:
            log("Unsuccessful libselinux patch attempt")
        if lsb_release()['DISTRIB_CODENAME'] not in ['focal']:
            try:
                check_call('patch --strip 1 -d ' + dst + '< ./files/interpreter.diff', shell=True)
            except Exception:
                log("Unsuccessful ansible_python_interpreter patch attempt")

        shutil.rmtree(tmp_folder, ignore_errors=True)
    finally:
        common_utils.container_engine().remove_container_by_image(image)


def update_status():
    command_ip = common_utils.get_ip()

    try:
        r = requests.get("https://{}:8079".format(command_ip), verify=False)
    except Exception:
        status_set("waiting", "URL is not ready {}:8079".format(command_ip))
        return False
    if r.status_code != 200:
        status_set("waiting", "URL is not ready {}:8079".format(command_ip))
        return False
    status_set("active", "Unit is ready")
    return True


def pull_images():
    tag = config.get('image-tag')
    for image in IMAGES:
        try:
            common_utils.container_engine().pull(image, tag)
        except Exception as e:
            log("Can't load image {}".format(e), level=ERROR)
            raise Exception('Image could not be pulled: {}:{}'.format(image, tag))


def get_proxy_env():
    http_proxy = config.get("http_proxy")
    https_proxy = config.get("https_proxy")
    no_proxy = config.get("no_proxy")

    env = os.environ.copy()
    if http_proxy:
        env["http_proxy"] = http_proxy
    if https_proxy:
        env["https_proxy"] = https_proxy
    command_ip = common_utils.get_ip()
    if no_proxy:
        env["no_proxy"] = no_proxy + "," + command_ip + "/32"
    else:
        env["no_proxy"] = command_ip + "/32"

    return env


def update_charm_status():
    tag = config.get('image-tag')
    ctx = get_context()

    deployer_image = "contrail-command-deployer"
    deploy_ccd_code(deployer_image, tag)

    missing_relations = []
    if not ctx.get("cloud_orchestrator"):
        status_set('blocked', 'Missing cloud orchestrator info in relations.')
        return
    if missing_relations:
        status_set('blocked',
                   'Missing or incomplete relations: ' + ', '.join(missing_relations))
        return

    changed = common_utils.render_and_log('cluster_config.yml.j2', '/cluster_config.yml', ctx)
    if config.changed("http_proxy") or config.changed("https_proxy") or config.changed("no_proxy"):
        changed = True

    if changed or not config.get("command_deployed"):
        env = get_proxy_env()
        dst = '/' + deployer_image + '/docker/deploy_contrail_command'
        check_call('./files/deploy_contrail_command.sh ' + dst, shell=True, env=env)
        config["command_deployed"] = True

    update_status()

    version = common_utils.container_engine().get_contrail_version("contrail-command", tag)
    application_version_set(version)


def import_cluster(juju_params):
    if not update_status():
        return False, 'Unit is not ready, try later'

    ctx = get_context()
    juju_params["juju_cluster_type"] = ctx.get("cloud_orchestrator")

    common_utils.render_and_log('juju_environment', '/tmp/juju_environment', juju_params)
    deployer_image = "contrail-command-deployer"
    env = get_proxy_env()
    dst = '/' + deployer_image + '/docker/deploy_contrail_command'
    try:
        check_call('. /tmp/juju_environment ; ./files/deploy_contrail_command.sh ' + dst, shell=True, env=env)
        status_set('active', 'Cluster is imported')
    except Exception as e:
        return False, 'Import failed ({}). Please check logs'.format(e)

    return True, "Success"


def remove_created_files():
    # Removes all config files, environment files, etc.
    common_utils.remove_file_safe("/cluster_config.yml")
    common_utils.remove_file_safe("/tmp/juju_environment")
    common_utils.remove_file_safe("/etc/ansible/ansible.cfg")

    image = "contrail-command-deployer"
    if os.path.exists("/" + image):
        os.rmdir("/" + image)
