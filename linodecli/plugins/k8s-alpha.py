"""
The k8s-alpha plugin includes a means to deploy Kubernetes clusters on Linode
"""
import argparse
import base64
import sys
import os
from subprocess import call as spcall
import hashlib
import shutil
from terminaltables import SingleTable

plugin_name = os.path.basename(__file__)[:-3]

def call(args, context):
    parser = argparse.ArgumentParser("{}".format(plugin_name), add_help=False)
    parser.add_argument('command', metavar='COMMAND', nargs='?', type=str,
                        help="The clusters command to be invoked.")
    parsed, args = parser.parse_known_args(args)

    commands = {
        'create': create,
        'delete': delete
    }

    if parsed.command is None or (parsed.command is None and parsed.help):
        parser.print_help()
        print_available_commands(commands)
        sys.exit(0)

    if parsed.command in commands.keys():
        commands[parsed.command](args, context)
    else:
        print('Unrecognized command {}'.format(parsed.command))

def create_varmap(context):
    # Maps parameters for the `create` command to Terraform variable names
    tf_var_map = {
        'node_type': {
            'name': 'server_type_node',
            'default': 'g6-standard-2',
        },
        'nodes': {
            'name': 'nodes',
            'default': 3,
        },
        'master_type': {
            'name': 'server_type_master',
            'default': 'g6-standard-2',
        },
        'region': {
            'name': 'region',
            'default': context.client.config.config.get('DEFAULT', 'region'),
        },
        'ssh_private_key': {
            'name': 'ssh_private_key',
            'default': os.path.expanduser('~/.ssh/id_rsa'),
        },
        'ssh_public_key': {
            'name': 'ssh_public_key',
            'default': os.path.expanduser('~/.ssh/id_rsa.pub'),
        },
    }
    return tf_var_map

def create(args, context):
    # Check if deps are installed
    needed_deps = check_deps('terraform', 'kubectl')
    if needed_deps:
        print('To create a cluster, this command requires {}\n'.format(' and '.join(needed_deps)))
        if 'terraform' in needed_deps:
            print_terraform_install_help()
        if 'kubectl' in needed_deps:
            print_kubectl_install_help()
        sys.exit(1)

    tf_var_map = create_varmap(context)

    parser = argparse.ArgumentParser("{} create".format(plugin_name), add_help=True)
    parser.add_argument('name', metavar='NAME', type=str,
                        help="A name for the cluster.")
#    High availability master nodes coming soon.
#    parser.add_argument('--ha', metavar="MASTERS", type=int, required=False,
#                        choices=[3, 5],
#                        help="Make the cluster highly-available with MASTERS "
#                             "number of masters (3 or 5)")
    parser.add_argument('--node-type', metavar="TYPE", type=str, required=False,
                        default=tf_var_map['node_type']['default'],
                        help='Linode Type ID for cluster Nodes as retrieved with '
                             '`linode-cli linodes types`. (default "g6-standard-2")')
    parser.add_argument('--nodes', metavar="COUNT", type=int, required=False,
                        default=tf_var_map['nodes']['default'],
                        help='The number of Linodes to deploy as Nodes in the cluster. '
                             '(default 3)')
    parser.add_argument('--master-type', metavar="TYPE", type=str, required=False,
                        default=tf_var_map['master_type']['default'],
                        help='Linode Type ID for cluster Master Nodes as retrieved with '
                             '`linode-cli linodes types`. (default "g6-standard-2")')
    parser.add_argument('--region', metavar="REGION", type=str, required=False,
                        default=tf_var_map['region']['default'],
                        help='The Linode Region ID in which to deploy the cluster as retrieved with '
                             '`linode-cli regions list`. (default "us-west")')
    parser.add_argument('--ssh-private-key', metavar="KEYPATH", type=str, required=False,
                        default=tf_var_map['ssh_private_key']['default'],
                        help='The path to your private key file which will be used to access Nodes '
                        '(during initial provisioning only!)')
    parser.add_argument('--ssh-public-key', metavar="KEYPATH", type=str, required=False,
                        default=tf_var_map['ssh_public_key']['default'],
                        help='The path to your public key file which will be used to access Nodes '
                        '(during initial provisioning only!)')
    parsed, remaining_args = parser.parse_known_args(args)

    prefix = get_prefix(parsed.name)
#   MAJOR @TODO: check here if this hashname already appears as a prefix on any
#   Volumes, Linodes, or NodeBalancers. If it does, bail with an error message,
#   because we don't want to later delete resources from multiple clusters!

    # Make application directory if it doesn't exist
    appdir = safe_make_appdir("k8s-alpha-linode")
    # Make the terraform project directory if it doesn't exist
    terrapath = os.path.join(appdir, parsed.name)
    safe_mkdir(terrapath)

    # Move to the directory
    os.chdir(terrapath)

    # Generate the terraform file
    terrafile = open('cluster.tf', 'w')
    terrafile.write(gen_terraform_file(context, tf_var_map, parsed.name, prefix))
    terrafile.close()

    # Generate terraform args
    terraform_args = gen_terraform_args(parsed, tf_var_map)

    # Run the Terraform commands
    spcall(['terraform', 'workspace', 'new', parsed.name])
    call_or_exit(['terraform', 'init'])
    # TODO: Before running the apply delete any existing Linodes that would
    # cause the apply to fail.
    terraform_apply_command = ['terraform', 'apply'] + terraform_args + remaining_args
    call_or_exit(terraform_apply_command)

    # Merge and/or create kubeconfig for the new cluster.
    # Also, activate the kubeconfig context.
    safe_mkdir(os.path.expanduser("~/.kube"))

    # We expect this to be the path to the generated kubeconfig file
    kubeconfig_new = replace_kubeconfig_user(terrapath, parsed.name, prefix)
    kubeconfig_existing = os.path.expanduser("~/.kube/config")
    # Create a merged kubeconfig file and set the context
    # First set up the KUBECONFIG env var so that `kubectl config view --flatten`
    # gives us a new merged config
    os.environ["KUBECONFIG"] = "{}:{}".format(kubeconfig_existing, kubeconfig_new)
    tempfilepath = 'tempkubeconfig'
    tempf = open(tempfilepath, 'w')
    call_or_exit(['kubectl', 'config', 'view', '--flatten'], stdout=tempf)
    tempf.close()
    shutil.move(tempfilepath, kubeconfig_existing)

    # Set the kubeconfig context to the new cluster
    call_or_exit(['kubectl', 'config', 'use-context', '{}@{}'.format(parsed.name, parsed.name)])

    print("Your cluster has been created and your kubectl context updated.\n"
          "Try the following command: \n"
          "kubectl get pods --watch --all-namespaces")

    # We're done! We have merged the user's kubeconfigs.
    # So, the user should be able to run something like
    # `kubectl get pods --all-namespaces`
    # and see the Linode CSI, CCM, and ExternalDNS controllers

def delete(args, context):
    needed_deps = check_deps('terraform')
    if needed_deps:
        print('This command requires {}\n'.format(' and '.join(needed_deps)))
        if 'terraform' in needed_deps:
            print_terraform_install_help()
        sys.exit(1)

    parser = argparse.ArgumentParser("{} create".format(plugin_name), add_help=True)
    parser.add_argument('name', metavar='NAME', type=str,
                        help="A name for the cluster to delete.")
    parsed, remaining_args = parser.parse_known_args(args)

    # Get the appdir path
    appdir = safe_make_appdir("k8s-alpha")
    terrapath = os.path.join(appdir, parsed.name)
    # Move to the terraform directory
    os.chdir(terrapath)
    call_or_exit(['terraform', 'destroy'] + remaining_args)

    # TODO: Also delete all NodeBalancers and Volumes using the cluster prefix

def check_deps(*args):
    needed_deps = []
    for dep in args:
        if not dep_installed(dep):
            needed_deps.append(dep)
    return needed_deps

def dep_installed(command):
    try:
        nullf = open(os.devnull, 'w')
        spcall([command], stdout=nullf)
        return True
    except: 
        return False

def print_terraform_install_help():
    print('Installing Terraform:\n'
          'On macOS with Homebrew: brew install terraform\n'
          'For other platforms, use your package manager and/or refer to this documentation\n'
          'https://learn.hashicorp.com/terraform/getting-started/install.html\n'
          'This command will automatically download and install the Linode provider '
          'for Terraform.')

def print_kubectl_install_help():
    print('Installing The Kubernetes CLI (kubectl)\n'
          'On macOS with Homebrew: brew install kubernetes-cli\n'
          'For other platforms, use your package manager and/or refer to this documentation\n'
          'https://kubernetes.io/docs/tasks/tools/install-kubectl/')

def quoted_string_or_bare_int(val):
    if type(val) is int:
        return val
    elif type(val) is str:
        return '"{}"'.format(val)
    else:
        return ''

def gen_terraform_file(context, tf_var_map, cluster_name, prefix):
    tf_file_parts = []

    for varname in tf_var_map.keys():
        tf_file_parts.append("""variable "{tf_varname}" {{
  default = {default}
}}
""".format(tf_varname=tf_var_map[varname]['name'],
           default=quoted_string_or_bare_int(tf_var_map[varname]['default'])))
    
    tf_file_parts.append("""module "k8s" {{
  source  = "git@github.com:linode/terraform-linode-k8s.git?ref=for-cli"

  linode_token = "{token}"
  
  linode_group = "{prefix}-{cluster_name}"
""".format(token=context.token,
           prefix=prefix,
           cluster_name=cluster_name,))

    for varname in tf_var_map.keys():
        tf_file_parts.append("""
  {tf_varname} = "${{var.{tf_varname}}}"
""".format(tf_varname=tf_var_map[varname]['name']))

    tf_file_parts.append("}\n")

    return ''.join(tf_file_parts)

def gen_terraform_args(parsed, tf_var_map):
    args = []
    for varname in tf_var_map.keys():
        args = args + ['-var', "{}={}".format(tf_var_map[varname]['name'], getattr(parsed, varname))]
    return args

def call_or_exit(*args, **kwargs):
    ret = spcall(*args, **kwargs)
    if ret != 0:
        print("Error calling {} with additional options {}".format(args, kwargs))
        sys.exit(ret)

def replace_kubeconfig_user(terrapath, cluster_name, prefix):
    """
    If we leave the user as kubernetes-admin then the configs don't flatten properly.
    All of them try to create creds for kubernetes-admin.
    """
    kubeconfig_fp = os.path.join(terrapath, "{}.conf".format(cluster_name))
    with open(kubeconfig_fp) as f:
        kubeconfig = f.read()
    
    kubeconfig.replace('kubernetes-admin', "{}-{}".format(cluster_name, prefix))

    kubeconfig_new_fp = os.path.join(terrapath, "{}_new.conf".format(cluster_name))
    with open(kubeconfig_new_fp, 'w') as f:
        f.write(kubeconfig)

    return kubeconfig_new_fp

def safe_make_appdir(appname):
    if sys.platform == 'win32':
        appdir = os.path.join(os.environ['APPDATA'], appname)
    else:
        appdir = os.path.expanduser(os.path.join("~", "." + appname))

    if not appdir:
        print('Cannot locate an appropriate directory in which to store data: "{}"'.format(appdir))
        sys.exit(1)

    safe_mkdir(appdir)

    return appdir

def safe_mkdir(dirpath):
    if not os.path.isdir(dirpath):
        try:
            os.mkdir(dirpath)
        except:
            print('Unable to create the directory: {}'.format(dirpath))
            sys.exit(1)

def get_prefix(name):
    return 'ka{}'.format(get_hashname(name))

def get_hashname(name):
    """
    A cluster hashname is the first 9 characters of a SHA256 digest encoded in base64

    This is used as a compact way to uniquely identify a cluster's Linode resources.
    It's also stateless! If a user loses their terraform file and wishes to
    delete a cluster they can still do so.
    """
    hashname = base64.b64encode(hashlib.sha256(name.encode('utf8')).digest())
    hashname = hashname.decode('utf8')
    hashname = hashname.replace('+', '')
    hashname = hashname.replace('/', '')
    return hashname[:9]

def print_available_commands(commands):
    print("\nAvailable commands:")
    content = [c for c in commands.keys()]
    proc = []
    for i in range(0,len(content),3):
        proc.append(content[i:i+3])
    if content[i+3:]:
        proc.append(content[i+3:])

    table = SingleTable(proc)
    table.inner_heading_row_border = False
    print(table.table)
