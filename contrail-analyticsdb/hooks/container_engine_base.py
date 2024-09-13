class Container():
    def install():
        pass

    def cp(self, name, src, dst):
        pass

    def execute(self, name, cmd, shell=False):
        pass

    def get_image_id(self, image, tag):
        pass

    def pull(self, image, tag):
        pass

    def compose_run(self, path, config_changed=True):
        pass

    def compose_down(self, path, services_to_wait=None):
        pass

    def get_container_id(self, path, service):
        pass

    def get_container_state(self, path, service):
        pass

    def remove_container_by_image(self, image):
        pass

    def create(self, image, tag):
        pass

    def restart_container(self, path, service):
        pass

    def get_contrail_version(self, image, tag, pkg="python3-contrail"):
        pass

    def config_changed(self):
        pass

    def render_logging(self):
        pass
