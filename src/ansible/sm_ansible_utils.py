import os
import sys
import pycurl
import ConfigParser
import subprocess
import json
import pdb
import glob
from StringIO import StringIO

SM_STATUS_PORT = "9002"
STATUS_VALID = "parameters_valid"
STATUS_IN_PROGRESS = "provision_in_progress"
STATUS_SUCCESS = "provision_completed"
STATUS_FAILED  = "provision_failed"


# Role strings
CONTROLLER_CONTAINER  = "contrail-controller"
ANALYTICS_CONTAINER   = "contrail-analytics"
ANALYTICSDB_CONTAINER = "contrail-analyticsdb"
AGENT_CONTAINER       = "contrail-agent"
LB_CONTAINER          = "contrail-lb"
BARE_METAL_COMPUTE    = "contrail-compute"
CEPH_CONTROLLER       = "contrail-ceph-controller"
CEPH_COMPUTE          = "contrail-ceph-compute"
_DEF_BASE_PLAYBOOKS_DIR = "/opt/contrail/server_manager/ansible/playbooks"

# Add new roles and corresponding container_name here
_container_names = { CONTROLLER_CONTAINER  : 'controller',
                     ANALYTICS_CONTAINER   : 'analytics',
                     ANALYTICSDB_CONTAINER : 'analyticsdb',
                     LB_CONTAINER          : 'lb',
                     AGENT_CONTAINER       : 'agent',
                     BARE_METAL_COMPUTE    : 'agent',
                     CEPH_CONTROLLER       : 'ceph-master',
                     CEPH_COMPUTE          : 'ceph-compute'}
_valid_roles = _container_names.keys()

_inventory_group = { CONTROLLER_CONTAINER  : "contrail-controllers",
                     ANALYTICS_CONTAINER   : "contrail-analytics",
                     ANALYTICSDB_CONTAINER : "contrail-analyticsdb",
                     LB_CONTAINER          : "contrail-lb",
                     AGENT_CONTAINER       : "contrail-compute",
                     BARE_METAL_COMPUTE    : "contrail-compute",
                     CEPH_CONTROLLER       : "ceph-controller",
                     CEPH_COMPUTE          : "ceph-compute"}

_container_img_keys = { CONTROLLER_CONTAINER  : "controller_image",
                        ANALYTICS_CONTAINER   : "analytics_image",
                        ANALYTICSDB_CONTAINER : "analyticsdb_image",
                        LB_CONTAINER          : "lb_image",
                        AGENT_CONTAINER       : "agent_image",
                        CEPH_CONTROLLER       : "storage_ceph_controller_image" }

def send_REST_request(ip, port, endpoint, payload,
                      method='POST', urlencode=False):
    try:
        response = StringIO()
        headers = ["Content-Type:application/json"]
        url = "http://%s:%s/%s" %(ip, port, endpoint)
        conn = pycurl.Curl()
        if method == "PUT":
            conn.setopt(pycurl.CUSTOMREQUEST, method)
            if urlencode == False:
                first = True
                for k,v in payload.iteritems():
                    if first:
                        url = url + '?'
                        first = False
                    else:
                        url = url + '&'
                    url = url + ("%s=%s" % (k,v))
            else:
                url = url + '?' + payload
        print "Sending post request to %s" % url
        conn.setopt(pycurl.URL, url)
        conn.setopt(pycurl.HTTPHEADER, headers)
        conn.setopt(pycurl.POST, 1)
        if urlencode == False:
            conn.setopt(pycurl.POSTFIELDS, '%s'%json.dumps(payload))
        conn.setopt(pycurl.WRITEFUNCTION, response.write)
        conn.perform()
        return response.getvalue()
    except:
        return None

def create_inv_file(fname, dictionary):
    with open(fname, 'w') as invfile:
        for key, value in dictionary.items():
            if isinstance(value, str):
                invfile.write(key)
                invfile.write('\n')
                invfile.write(value)
                invfile.write('\n')
                invfile.write('\n')
            if isinstance(value, list):
                invfile.write(key)
                invfile.write('\n')
                for item in value:
                    invfile.write(item)
                    invfile.write('\n')
                invfile.write('\n')
            if isinstance(value, dict):
                invfile.write(key)
                invfile.write('\n')
                for k, v in value.items():
                    if isinstance(v, str) or isinstance(v, bool):
                        invfile.write(k+"=")
                        invfile.write(str(v))
                        invfile.write('\n')
                        invfile.write('\n')
                    if isinstance(v, list) or isinstance(v, dict):
                        invfile.write(k+"=")
                        invfile.write(str(v))
                        invfile.write('\n')
                        invfile.write('\n')


'''
Function to verify that SM Lite compute has completed provision after reboot
'''
def ansible_verify_provision_complete(smlite_non_mgmt_ip):
    try:
        cmd = ("lsmod | grep vrouter")
        output = subprocess.check_output(cmd, shell=True)
        if "vrouter" not in output:
            return False
        cmd = ("ifconfig vhost0 | grep %s" %(smlite_non_mgmt_ip))
        output = subprocess.check_output(cmd, shell=True)
        if str(smlite_non_mgmt_ip) not in output:
            return False
        return True
    except subprocess.CalledProcessError as e:
        raise e
    except Exception as e:
        raise e

'''
Function to check if the contrail-networking-openstack-extra tgz should be removed
and not be part of the repo
'''
def is_remove_openstack_extra(package_path, package_name, package_type, openstack_sku):
    # remove the openstack extra package as we don't need it as part of 
    # the repo for contrail-cloud-docker image
    if package_name == "contrail-networking-docker" and \
         package_type == "contrail-cloud-docker-tgz":
        return True
    
    # remove the openstack extra package as we don't need it as part of 
    # the repo for contrail-networking-docker image if the openstack sku is liberty
    if package_type == "contrail-networking-docker-tgz" \
       and package_name == "contrail-networking-openstack-extra" \
       and openstack_sku == "liberty":
        return True
    return False

'''
Remove files not related to openstack sku
'''
def manipulate_openstack_extra_tgz(package_path, package_name, openstack_sku):
    file_to_be_removed = []
    folder_path = str(package_path)+"/"+str(package_name)
    dirs = os.listdir(folder_path)
    for file in dirs:
        if openstack_sku not in file and "common" not in file:
              file_to_be_removed.append("/"+file+"*")
    for file in file_to_be_removed:
        cmd = "rm " + folder_path + file
        subprocess.check_call(cmd, shell=True)

'''
Functions to create a repo and unpack from contrail-docker-cloud package
Create debian repo for openstack and contrail packages in container tgz

'''

def untar_package_to_folder(mirror,package_path, package_type, openstack_sku):
    folder_list = []
    cleanup_package_list = []
    puppet_package = None
    ansible_package = None
    docker_images_package_list = []
    search_package = package_path+"/*.tgz"
    package_list = glob.glob(search_package)
    if package_list:
        for package in package_list:
            package_name = str(package).partition(package_path+"/")[2]
            package_name = str(package_name).partition('_')[0]
            if package_name not in ['contrail-ansible', 'contrail-puppet','contrail-docker-images','contrail-cloud-docker-images']:
                cmd = "mkdir -p %s/%s" %(package_path,package_name)
                subprocess.check_call(cmd, shell=True)
                cmd = "tar -xvzf %s -C %s/%s > /dev/null" %(package, package_path, package_name)
                subprocess.check_call(cmd, shell=True)
                folder_path = str(package_path)+"/"+str(package_name)
                if is_remove_openstack_extra(package_path, package_name, package_type,openstack_sku):
                   cmd = "rm "+ folder_path + "/" + "contrail-networking-openstack-extra*"
                   subprocess.check_call(cmd, shell=True)
                if package_type == "contrail-networking-docker-tgz" \
                   and package_name == "contrail-networking-openstack-extra" \
                   and openstack_sku != "liberty":
                    manipulate_openstack_extra_tgz(package_path, package_name, openstack_sku)
                folder_list.append(folder_path)
            elif package_name == "contrail-docker-images" or package_name == "contrail-cloud-docker-images":
                docker_images_package_list.append(package)
            cleanup_package_list.append(package)

    search_puppet_package = package_path+"/contrail-puppet*.tar.gz"
    puppet_package_path = glob.glob(search_puppet_package)
    if puppet_package_path:
        puppet_package = puppet_package_path[0]

    search_ansible_package = package_path+"/contrail-ansible*.tar.gz"
    ansible_package_path = glob.glob(search_ansible_package)
    if ansible_package_path:
        ansible_package = ansible_package_path[0]

    deb_package = package_path+"/*.deb"
    deb_package_list = glob.glob(deb_package)
    if deb_package_list:
        cmd = "mv %s/*.deb %s/contrail-repo/ > /dev/null" %(package_path, str(mirror))
        subprocess.check_call(cmd, shell=True)
    return folder_list, cleanup_package_list, puppet_package, ansible_package, docker_images_package_list

def unpack_ansible_playbook(ansible_package,mirror,image_id):
    # create ansible playbooks dir from the image tar
    ansible_playbooks_default_dir = _DEF_BASE_PLAYBOOKS_DIR
    cmd = ("mkdir -p %s" % (ansible_playbooks_default_dir+"/"+image_id))
    subprocess.check_call(cmd, shell=True)
    playbooks_version = str(ansible_package).partition('contrail-ansible-')[2].rpartition('.tar.gz')[0]
    cmd = (
        "tar -xvzf %s -C %s > /dev/null" %(ansible_package, ansible_playbooks_default_dir+"/"+image_id))
    subprocess.check_call(cmd, shell=True)
    return playbooks_version

def unpack_containers(docker_images_package_list,mirror):
    for docker_images_package in docker_images_package_list:
        cmd = 'tar -xvzf %s -C %s/contrail-docker/ > /dev/null' % (docker_images_package,mirror)
    subprocess.check_call(cmd, shell=True)

# Wrapper function for add_puppet_modules for contrail-docker-cloud image
def unpack_puppet_manifests(puppet_package,mirror):
    cmd = ("cp %s %s/contrail-puppet/contrail-puppet-manifest.tgz" % (puppet_package, mirror))
    subprocess.check_call(cmd, shell=True)
    puppet_package_path = mirror+"/contrail-puppet/contrail-puppet-manifest.tgz"
    return puppet_package_path

# Create debian repo for openstack and contrail packages in container tgz
def _create_container_repo(image_id, image_type, image_version, dest, pkg_type,openstack_sku,args):
	puppet_manifest_version = ""
	image_params = {}
	tgz_image = False
	try:
	    # create a repo-dir where we will create the repo
	    mirror = args.html_root_dir+"contrail/repo/"+image_id
	    cmd = "/bin/rm -fr %s" %(mirror)
	    subprocess.check_call(cmd, shell=True)
	    cmd = "mkdir -p %s" %(mirror)
	    subprocess.check_call(cmd, shell=True)
	    # change directory to the new one created
	    cwd = os.getcwd()
	    os.chdir(mirror)
	    # Extract .tgz of other packages from the repo
	    cmd = 'file %s'%dest
	    output = subprocess.check_output(cmd, shell=True)
	    #If the package is tgz or debian extract it appropriately
	    if output:
		if 'gzip compressed data' in output:
		    cmd = ("tar -xvzf %s -C %s > /dev/null" %(dest,mirror))
		    subprocess.check_call(cmd, shell=True)
		else:
		    raise Exception
	    else:
		raise Exception

	    cmd = "mkdir -p %s/contrail-repo %s/contrail-docker %s/contrail-puppet" %(mirror,mirror,mirror)
	    subprocess.check_call(cmd, shell=True)
	    cleanup_package_list = []
            folder_list = []
            folder_list.append(str(mirror))
            ansible_package = None
            puppet_package = None
            docker_images_package_list = []
            playbooks_version = None

            for folder in folder_list:
                new_folder_list, new_cleanup_list, puppet_package_path, ansible_package, docker_images_package_list = untar_package_to_folder(mirror,str(folder), pkg_type, openstack_sku)
                if folder == mirror:
                    cleanup_package_list = new_folder_list + new_cleanup_list
                folder_list += new_folder_list
                if puppet_package_path:
                    puppet_package = unpack_puppet_manifests(puppet_package_path,mirror)
                if ansible_package:
                    playbooks_version = unpack_ansible_playbook(ansible_package,mirror,image_id)
                if docker_images_package_list != []:
                    unpack_containers(docker_images_package_list,mirror)

	    # build repo using reprepro based on repo pinning availability
	    cmd = ("cp -v -a /opt/contrail/server_manager/reprepro/conf %s/" % mirror)
	    subprocess.check_call(cmd, shell=True)
	    cmd = ("reprepro includedeb contrail %s/contrail-repo/*.deb" % mirror)
	    subprocess.check_call(cmd, shell=True)

	    # Add containers from tar file
	    container_base_path = mirror+"/contrail-docker"
	    containers_list = glob.glob(container_base_path+"/*.tar.gz")
	    image_params["containers"] = []
	    for container in containers_list:
		container_details = {}
		container_path = str(container)
		role = container_path.partition(str(container_base_path)+"/")[2].rpartition("-u")[0]
		if str(role) in _valid_roles:
		    container_dict = {"role": role, "container_path": container_path}
		    image_params["containers"].append(container_dict.copy())
	    cleanup_package_list.append(mirror+"/contrail-docker")
	    cleanup_package_list.append(mirror+"/contrail-puppet")
	    cleanup_package_list.append(mirror+"/contrail-repo")

	    image_params["cleanup_list"] = cleanup_package_list
	    # change directory back to original
	    os.chdir(cwd)
	    return puppet_package, playbooks_version, image_params
	except Exception as e:
	    raise(e)
# end _create_container_repo

'''
Recursively process the dictionary and create the INI format file
'''
def create_sections(config, dictionary, section=None):
    for key, value in dictionary.items():
        if isinstance(value, dict):
            create_sections(config, dictionary=value,
                                   section=key)
        else:
            try:
                config.set(section, key, value)
            except ConfigParser.NoSectionError:
                try:
                    config.add_section(section)
                    config.set(section, key, value)
                except ConfigParser.DuplicateSectionError:
                    print "Ignore DuplicateSectionError"

            except TypeError:
                try:
                    config.add_section(section)
                except ConfigParser.DuplicateSectionError:
                    print "ignore Duplicate Sections"


def create_conf_file(ini_file, dictionary={}):
    if not ini_file:
        return
    config = ConfigParser.SafeConfigParser()
    create_sections(config, dictionary)
    with open(ini_file, 'w') as configfile:
        config.write(configfile)

def update_inv_file(ini_file, section, dictionary={}):
    if not ini_file:
        return
    config = ConfigParser.SafeConfigParser()
    create_sections(config, dictionary)
    with open(ini_file, 'a') as configfile:
        config.write(configfile)




