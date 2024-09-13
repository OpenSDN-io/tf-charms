import base64
import copy
import json
import os
import platform
from subprocess import check_call, check_output
import time
import uuid
import yaml

from charmhelpers.core.hookenv import (
    config,
    log,
    DEBUG,
    env_proxy_settings,
)
from charmhelpers.core.host import service_restart
from charmhelpers.core.templating import render
from charmhelpers.fetch import apt_install, apt_update

import container_engine_base


config = config()

DOCKER_ADD_PACKAGES = ["docker-compose"]
DOCKER_CLI = "/usr/bin/docker"
DOCKER_COMPOSE_CLI = "docker-compose"

DOCKER_TIMEOUTS = {
    "DOCKER_CLIENT_TIMEOUT": "120",
    "COMPOSE_HTTP_TIMEOUT": "120"
}


class Docker(container_engine_base.Container):
    def _format_curl_proxy_opt(self):
        proxy_settings = env_proxy_settings(['http', 'https', 'no_proxy'])
        result = ""
        if 'https_proxy' in proxy_settings:
            result += ' --proxy {}'.format(proxy_settings['https_proxy'])
        if 'http_proxy' in proxy_settings:
            result += ' --proxy {}'.format(proxy_settings['http_proxy'])
        if 'no_proxy' in proxy_settings:
            result += ' --noproxy {}'.format(proxy_settings['no_proxy'])
        return result

    def install(self):
        docker_runtime = config.get("docker_runtime")
        if docker_runtime == "apt" or docker_runtime == "auto":
            docker_package = "docker.io"
            docker_repo = None
            docker_key_url = None
        elif docker_runtime == "upstream":
            docker_package = "docker.ce"
            docker_repo = "deb [arch={ARCH}] https://download.docker.com/linux/ubuntu {CODE} stable"
            docker_key_url = "https://download.docker.com/linux/ubuntu/gpg"
        else:
            # custom or default
            docker_package = config.get("docker_runtime_package") or "docker.ce"
            docker_repo = (config.get("docker_runtime_repo") or
                           "deb [arch={ARCH}] https://download.docker.com/linux/ubuntu {CODE} stable")
            docker_key_url = config.get("docker_runtime_key_url") or "https://download.docker.com/linux/ubuntu/gpg"

        apt_install(["apt-transport-https", "ca-certificates", "curl",
                    "software-properties-common"])
        if docker_key_url:
            cmd = [
                "/bin/bash", "-c",
                "set -o pipefail ; curl {} "
                "-fsSL --connect-timeout 10 "
                "{} | sudo apt-key add -"
                "".format(self._format_curl_proxy_opt(), docker_key_url)
            ]
            check_output(cmd)
        arch = "amd64"

        # linux_distribution is deprecated and will be removed in Python 3.7
        def _get_codename_from_fs():
            """Get Codename from /etc/os-release."""
            with open(os.path.join(os.sep, 'etc', 'os-release')) as fin:
                content = dict(
                    line.split('=', 1)
                    for line in fin.read().splitlines()
                    if '=' in line
                )
            for k, v in content.items():
                content[k] = v.strip('"')
            return content["UBUNTU_CODENAME"]

        if hasattr(platform, 'linux_distribution'):
            tuple_platform = platform.linux_distribution()
            dist = tuple_platform[2].strip()
        else:
            dist = _get_codename_from_fs()

        if docker_repo:
            exc = None
            for i in range(5):
                try:
                    cmd = ("add-apt-repository \"{}\"".format(docker_repo.replace("{ARCH}", arch).replace("{CODE}", dist)))
                    check_output(cmd, shell=True)
                    break
                except Exception as e:
                    exc = e
                # retry
                time.sleep(10)
            else:
                raise exc
        apt_update()
        apt_install(docker_package)
        apt_install(DOCKER_ADD_PACKAGES)
        self._render_config()
        self._update_docker_settings()
        self._login()

    def _load_json_file(self, filepath):
        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            pass
        return dict()

    def _save_json_file(self, filepath, data):
        try:
            os.mkdir(os.path.dirname(filepath))
        except OSError:
            pass
        temp_file = os.path.join(os.path.dirname(filepath), str(uuid.uuid4()))
        with open(temp_file, "w") as f:
            json.dump(data, f)
        os.replace(temp_file, filepath)

    def _update_docker_settings(self):
        docker_config = "/etc/docker/daemon.json"
        initial_settings = self._load_json_file(docker_config)
        new_settings = copy.deepcopy(initial_settings)
        docker_registry = new_settings.get("insecure-registries", list())
        if config.get("docker-opts"):
            docker_opts = json.loads(config["docker-opts"])
            new_settings.update(docker_opts)
            if docker_opts.get("insecure-registries"):
                docker_registry.extend(docker_opts["insecure-registries"])
        if config.get("docker-registry-insecure") and config.get("docker-registry"):
            # NOTE: take just host and port from registry definition
            docker_registry.append(config["docker-registry"].split('/')[0])
        if docker_registry:
            new_settings["insecure-registries"] = sorted(set(docker_registry))
            initial_settings["insecure-registries"] = sorted(set(initial_settings.get("insecure-registries", list())))

        if initial_settings != new_settings:
            log("Re-configure docker daemon")
            log("Old settings: {}".format(str(initial_settings)), level=DEBUG)
            log("New settings: {}".format(str(new_settings)), level=DEBUG)
            self._save_json_file(docker_config, new_settings)
            log("Restarting docker service")
            service_restart('docker')

    def _login(self):
        # 'docker login' doesn't work simply on Ubuntu 18.04. let's hack.
        login = config.get("docker-user")
        password = config.get("docker-password")
        if not login or not password:
            return

        auth = base64.b64encode("{}:{}".format(login, password).encode()).decode()
        docker_registry = config.get("docker-registry")
        config_path = os.path.join(os.path.expanduser("~"), ".docker/config.json")
        data = self._load_json_file(config_path)
        data.setdefault("auths", dict())[docker_registry] = {"auth": auth}
        self._save_json_file(config_path, data)

    def cp(self, name, src, dst):
        check_call([DOCKER_CLI, "cp", name + ":" + src, dst])

    def execute(self, name, cmd, shell=False):
        cli = [DOCKER_CLI, "exec", name]
        if isinstance(cmd, list):
            cli.extend(cmd)
        else:
            cli.append(cmd)
        if shell:
            output = check_output(' '.join(cli), shell=True)
        else:
            output = check_output(cli)
        return output.decode('UTF-8')

    def get_image_id(self, image, tag):
        registry = config.get("docker-registry")
        return "{}/{}:{}".format(registry, image, tag)

    def pull(self, image, tag):
        # check image presense
        try:
            # use check_output to avoid printing output to log
            _ = check_output([DOCKER_CLI, "inspect", self.get_image_id(image, tag)])
            return
        except Exception:
            pass
        # pull image
        check_call([DOCKER_CLI, "pull", self.get_image_id(image, tag)])

    def compose_run(self, path, config_changed=True):
        do_update = config_changed
        if not do_update:
            # check count of services
            count = None
            with open(path, 'r') as fh:
                data = yaml.load(fh)
                count = len(data['services'])
            # check is it run or not
            actual_count = len(check_output([DOCKER_COMPOSE_CLI, "-f", path, "ps", "-q"]).decode("UTF-8").splitlines())
            log("Services actual count: {}, required count: {}".format(actual_count, count), level=DEBUG)
            do_update = actual_count != count
        if do_update:
            env = os.environ.copy()
            env.update(DOCKER_TIMEOUTS)
            check_call([DOCKER_COMPOSE_CLI, "-f", path, "up", "-d"], env=env)

    def _send_sigquit(self, path, services_to_wait=None):
        self.compose_kill(path, "SIGQUIT")
        time.sleep(2)
        if services_to_wait:
            for i in range(0, 12):
                for service in list(services_to_wait):
                    # wait for exited code for service. Each 1 second, max wait 12 seconds
                    state = self.get_container_state(path, service)
                    if not state or state.get('Status', '').lower() != 'running':
                        services_to_wait.remove(service)
                if not services_to_wait:
                    break
                time.sleep(1)
            else:
                raise Exception("{} do not react to SIGQUIT. please check it manually and re-run operation.".format(", ".join(services_to_wait)))

    def compose_down(self, path, services_to_wait=None):
        # SIGQUIT is not supported on DPDK on Ubuntu, so we skip it
        if not config.get("dpdk"):
            self._send_sigquit(path, services_to_wait)
        try:
            env = os.environ.copy()
            env.update(DOCKER_TIMEOUTS)
            check_call([DOCKER_COMPOSE_CLI, "-f", path, "down"], env=env)
        except Exception as e:
            log("Error during compose down: {}".format(e))

    def compose_kill(self, path, signal):
        cmd = [DOCKER_COMPOSE_CLI, "-f", path, "kill", "-s", signal]
        check_call(cmd)

    def get_container_id(self, path, service):
        cmd = [DOCKER_COMPOSE_CLI, "-f", path, "ps", "-q", service]
        try:
            cnt_id = check_output(cmd).decode('UTF-8').rstrip().strip("'")
            if len(cnt_id) < 2:
                # there is no compose/container/service
                return None
        except Exception:
            # there is no compose/container/service
            return None
        return cnt_id

    def get_container_state(self, path, service):
        # returns None or State dict from docker
        # status must be None when compose returns error or empty ID for service
        cnt_id = self.get_container_id(path, service)
        if not cnt_id:
            return None
        try:
            args = [DOCKER_CLI, "inspect", "--format='{{json .State}}'", cnt_id]
            state_json = check_output(args).decode("UTF-8").rstrip().strip("'")
            return json.loads(state_json)
        except Exception:
            # let's return None when docker fails to return status by ID or we failed to read provided JSON
            return None

    def _do_op_for_container_by_image(self, image, all_containers, op, op_args=[]):
        cmd = [DOCKER_CLI, "ps"]
        if all_containers:
            cmd.append("-a")
        output = check_output(cmd).decode('UTF-8')
        containers = [line.split() for line in output.splitlines()][1:]
        for cnt in containers:
            if len(cnt) < 2:
                # bad string. normal output contains 6-7 fields.
                continue
            cnt_image = cnt[1]
            index = cnt_image.find(image)
            if index < 0 or (index > 0 and cnt_image[index - 1] != '/'):
                # TODO: there is a case when image name just a prefix...
                continue
            cmd = [DOCKER_CLI, op] + op_args + [cnt[0]]
            check_call(cmd)

    def remove_container_by_image(self, image):
        self._do_op_for_container_by_image(image, True, "rm")

    def create(self, image, tag):
        name = str(uuid.uuid4())
        image_id = self.get_image_id(image, tag)
        args = [DOCKER_CLI, "create", "--name", name, "--entrypoint", "/bin/true", image_id]
        check_call(args)
        return name

    def restart_container(self, path, service):
        cnt_id = self.get_container_id(path, service)
        if not cnt_id:
            return None
        cmd = [DOCKER_CLI, "restart", cnt_id]
        check_call(cmd)

    def get_contrail_version(self, image, tag, pkg="python3-contrail"):
        image_id = self.get_image_id(image, tag)
        try:
            args = [DOCKER_CLI, "image", "inspect", "--format='{{.Config.Labels.version}}'", image_id]
            version = check_output(args).decode("UTF-8").rstrip().strip("'")
            if version != '<no value>':
                return version
        except Exception:
            pass
        return check_output([
            DOCKER_CLI,
            "run", "--rm", "--entrypoint", "rpm", image_id,
            "-q", "--qf", "%{VERSION}-%{RELEASE}", pkg]).decode("UTF-8").rstrip()

    def config_changed(self):
        changed = False
        if config.changed("http_proxy") or config.changed("https_proxy") or config.changed("no_proxy"):
            self._render_config()
            changed = True
        if config.changed("docker-registry") or config.changed("docker-registry-insecure") or config.changed("docker-opts"):
            self._update_docker_settings()
            self._login()
            changed = True
        if config.changed("docker-user") or config.changed("docker-password"):
            self._login()
            changed = True
        return changed

    def _render_config(self):
        # From https://docs.docker.com/config/daemon/systemd/#httphttps-proxy
        if len(config.get('no_proxy')) > 2023:
            raise Exception('no_proxy longer than 2023 chars.')
        render('docker-proxy.conf', '/etc/systemd/system/docker.service.d/docker-proxy.conf', config)
        check_call(['systemctl', 'daemon-reload'])
        service_restart('docker')

    def render_logging(self):
        driver = config.get("docker-log-driver")
        options = config.get("docker-log-options", '').split()
        if not driver and not options:
            return ''
        logging = 'logging:\n'
        if driver:
            logging += "  driver: {}\n".format(driver)
        if options:
            logging += "  options:\n"
            # yaml is created manually because of redis.yaml that is created by
            # controller and analytics and should be exactly the same to avoid
            # config_changed hooks starting
            options.sort()
            for opt in options:
                option = opt.split('=')
                logging += '    {}: "{}"\n'.format(option[0], option[1])
        return logging
