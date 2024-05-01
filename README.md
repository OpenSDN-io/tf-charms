# contrail-charms
Juju charms for Contrail services.

Overview
--------

Contrail 5.0 would provide support for Docker containers. The existing contrail components, which run as services on a BMS or VM, would be running within a Docker container in contrail 5.0.
This document describes how to deploy contrail 5.0 Docker containers, which would be running the several contrail service components using contrail-charms

[Contrail-charms specs](specs/contrail-charms.md)

Deploying Contrail charms
-------------------------

1. Install cloud and juju on the dedicated machine from where the deploy is started:

```bash
    sudo apt-get update
    sudo apt-get upgrade
    sudo apt-get install juju
```

[Installing juju](https://docs.jujucharms.com/2.4/en/getting-started)

2. Configure juju:

```bash
juju add-cloud <cloud-name>
juju add-credential <cloud name>
```

You can see possible clouds that you can use with `juju clouds` command.

```bash
$ juju clouds
Cloud        Regions  Default          Type        Description
aws               15  us-east-1        ec2         Amazon Web Services
aws-china          1  cn-north-1       ec2         Amazon China
aws-gov            1  us-gov-west-1    ec2         Amazon (USA Government)
azure             26  centralus        azure       Microsoft Azure
azure-china        2  chinaeast        azure       Microsoft Azure China
cloudsigma         5  hnl              cloudsigma  CloudSigma Cloud
google            13  us-east1         gce         Google Cloud Platform
joyent             6  eu-ams-1         joyent      Joyent Cloud
oracle             5  uscom-central-1  oracle      Oracle Cloud
rackspace          6  dfw              rackspace   Rackspace Cloud
localhost          1  localhost        lxd         LXD Container Hypervisor
```

You can add private or custom clouds for the following provider types:

```bash
$ juju add-cloud
Cloud Types
  maas
  manual
  openstack
  oracle
  vsphere
```

As an example for maas:

```bash
juju add-cloud mymaas       #select cloud type: maas, enter the API endpoint url)
juju add-credential mymaas  #add credentials
```

[Using MAAS with Juju](https://docs.jujucharms.com/2.4/en/clouds-maas)

As an example for amazon:

```bash
juju add-credential aws   #add credentials
```

[Using Amazon with Juju](https://docs.jujucharms.com/2.4/en/help-aws)

3. Create controller:

```bash
juju bootstrap --bootstrap-series=bionic <cloud name> <controller name>
```

[Bootstrapping controller](https://docs.jujucharms.com/2.4/en/controllers-creating)

4. Download charms:

```bash
git clone https://github.com/Juniper/contrail-charms -b R5
```

5. Deploy Contrail:

You can deploy charms in bundle or manually.

[Charm bundles](https://docs.jujucharms.com/2.4/en/charms-bundles)

- With bundle:
    - Create or modify the Juju deployment bundle yaml file to point to machines in which the contrail-charms should be deployed and to include options you need.
    - Deploy the bundle using the command `juju deploy <bundle_yaml_file>`
    - [Example of bundle.yaml file for deployment of OpenStack and Contrail in Amazon environment for Ubuntu 18.04.](examples/contrail-openstack-bionic/bundle.yaml)
    - [Example of bundle.yaml file for deployment of OpenStack and Contrail without analyticsdb in Amazon environment for Ubuntu 18.04.](examples/contrail-openstack-without-analyticsdb-bionic/bundle.yaml)
    - [Example of bundle.yaml file for deployment of OpenStack and Contrail in Amazon environment for Ubuntu 20.04.](examples/contrail-openstack-focal/bundle.yaml)
    - [Example of bundle.yaml file for deployment of Kubernetes and Contrail in Amazon environment.](examples/contrail-k8s/bundle.yaml)
    - [Example of bundle.yaml file for deployment of Kubernetes and Contrail in Nested mode.](examples/contrail-k8s-nested/bundle.yaml)
    - [Example of bundle.yaml file for Contrail CNI (K8S) Deployment Using Existing Keystone.](examples/contrail-hybrid-k8s-openstack/bundle.yaml)

- Manually

    [How to deploy Contrail with OpenStack](./manual-deploy.md)

    [How to deploy Contrail with Kubernetes](./manual-deploy-kubernetes.md)

    [Using the Manual cloud with Juju](https://docs.jujucharms.com/2.4/en/clouds-manual)

    * Analyticsdb is optional and can be omitted. In this case analyticsdb, alarm and snmp will not be installed.

6. You can check the status of your deployment using `juju status` command.
[Unit status. Juju documentation.](https://docs.jujucharms.com/2.4/en/reference-status)

Contrail's multi interface setup
--------------------------------

CTRL/DATA network - network for data traffic of workload and for control traffic between compute nodes and control services.
API/MGMT network - network where API services are listening on and for accessing instances.

By default, charms are using the default network for API/MGMT and CTRL/DATA.

To specify API/MGMT network set `control-network` parameter for contrail-controller and contrail-analytics. If contrail-analyticsdb or contrail-kubernetes-master is applied set `control-network` parameter for it as well. It can be the IP addresses with netmasks of the control network or physical device name.

To specify CTRL/DATA network set `data-network` parameter for contrail-controller. It can be the IP addresses with netmasks of the control network or physical device name. If `data-network` isn't specified it will use the same network as `control-network`.

```bash
juju config contrail-controller control-network=192.168.0.0/24 data-network=ens4
juju config contrail-analytics control-network=192.168.0.0/24
juju config contrail-analyticsdb control-network=192.168.0.0/24
```

When contollers are placed in different AZ/networks then it can be possible to specify like this:

```bash
juju config contrail-controller control-network="192.168.0.0/24 192.168.1.0/24 192.168.2.0/24" data-network=ens4
```

In this case each controller will choose the address from its own network.

Contrail's containerd setup
--------------------------------
It is possible to run contrail containers using containerd runtime. It can be set by using `container_runtime` parameter.

WARNING: all possible contrail-charms should use the same `contailner_runtime`. It should be set on bundle or in the beginnig of deploy and cannot be changed after.

The charms that support containerd:
- contrail-agent
- contrail-analytics
- contrail-analyticsdb
- contrail-controller
- contrail-kubernetes-master
- contrail-kubernetes-node
- contrail-openstackl

Known issues
------------

1. https://bugs.launchpad.net/charm-kubernetes-master/+bug/1916009

Sometimes kubectl fails to exec commands in POD due to lack of ACL for kubectl.
Cluster can be deployed but it might not work.

2. https://bugs.launchpad.net/charm-percona-cluster/+bug/1916021

Sometimes mysql can not start.
Cluster is not up.

3. https://bugs.launchpad.net/charm-nova-cloud-controller/+bug/1916038

After successful cluster deploy tests may fail due to races in keepalive

Configuration
-------------

- ## Contrail-agent
    [Contrail-agent specs](contrail-agent/README.md)

- ## Contrail-analytics
    [Contrail-analytics specs](contrail-analytics/README.md)

- ## Contrail-analyticsdb
    [Contrail-analyticsdb specs](contrail-analyticsdb/README.md)

- ## Contrail-controller
    [Contrail-controller specs](contrail-controller/README.md)

- ## Contrail-keystone-auth
    [Contrail-keystone specs](contrail-keystone/README.md)

- ## Contrail-openstack
    [Contrail-openstack specs](contrail-openstack/README.md)
