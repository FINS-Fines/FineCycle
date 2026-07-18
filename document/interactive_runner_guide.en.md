# interactive_runner Guide

## 1. What It Is

`interactive_runner` is the interactive CLI entry point used on the `version1.0` branch of FineCycle. It is responsible for:

- connecting to a PVE master node
- scanning cluster nodes and registering schedulable hosts
- registering VM images
- scanning and registering Clonezilla ISO images
- using the unified deployment entry point `migrate <vmid> <host_id>`
- submitting guided scheduling requests through `auto`

The default startup command is:

```bash
python -m interactive_runner
```

## 2. Before You Start

### 2.1 Python Dependencies

The core dependencies come from `requirement.txt` in the project root:

- `paramiko`
- `proxmoxer`
- `pyyaml`

Install them first:

```bash
pip install -r requirement.txt
```

The following packages are optional but recommended:

- `rich`
- `prompt_toolkit`

```bash
pip install rich prompt_toolkit
```

If they are not installed, the CLI falls back to plain-text mode. Core functionality still works.

### 2.2 Configuration File

`interactive_runner` reads `config.yaml` from the project root by default. Before first use, check at least:

- `system.demo_mode`
- `system.log_level`
- `system.require_cable_confirmation`
- `network_defaults.gateway`
- `network_defaults.prefixlen`
- `network_defaults.wired_interface`
- `network_defaults.wifi_interface`

The `network_defaults` section is used as a fallback when automatic PVE network inference fails.

### 2.3 Cluster Access and Credentials

The first launch always starts with an initialization wizard, so prepare:

- the IP address or hostname of a reachable PVE master node
- a PVE API username and password
- an SSH username, port, and password

The wizard defaults are:

- PVE API username: `root@pam`
- PVE API port: `8006`
- PVE API `verify_ssl`: disabled by default
- SSH username: `root`
- SSH port: `22`

Notes:

- both SSH access and PVE API connectivity are required
- the initialization flow assumes the same SSH credentials can be reused across cluster nodes
- if a node address cannot be resolved automatically, the CLI will ask you to enter the SSH address manually

### 2.4 Extra Requirements for Bare-Metal Workflows

Bare-metal deployment is driven by **Clonezilla ISO registration and binding**. Before using the bare-metal path, prepare the following:

- upload Clonezilla ISO files to the **master node's `local` ISO storage**
- make sure the filenames match `clonezilla*.iso`, otherwise `interactive_runner` will not discover them
- if you name an ISO as `clonezilla-<IPv4>.iso`, for example `clonezilla-192.168.8.210.iso`, the CLI can infer the default IP automatically
- if the filename does not encode an IP, you will be asked to enter the IP during registration
- prepare at least two usable Clonezilla ISOs

Why two:

- one Clonezilla ISO is bound to the target bare-metal host
- another free Clonezilla ISO is selected later as the source-side sender during migration
- the source and target sides cannot use the same Clonezilla ISO

For the lower-level DDC environment preparation details, see [scripts/DDC/readme.md](D:\Code\Finecycle\scripts\DDC\readme.md).

## 3. Quick Start

### 3.1 Launch the Tool

Run this from the project root:

```bash
python -m interactive_runner
```

It starts with the initialization wizard.

### 3.2 Initialization Wizard Flow

Complete the prompts in this order:

1. `Master Server IP/Hostname`
2. `PVE API` username
3. `PVE API` password
4. optional advanced settings:
   - `PVE API` port
   - `PVE API verify_ssl`
5. `SSH` username
6. `SSH` port
7. `SSH` password

After the connection succeeds, the tool automatically:

- connects to the master node
- resolves the master node name
- scans the master node `local` storage for `clonezilla*.iso`
- asks whether to register those Clonezilla ISOs and record their IPs
- scans cluster nodes
- interactively registers nodes
- interactively registers VM images under those nodes

Typical node-registration inputs include:

- whether the node should be treated as `Server` or `Robot`
- for `Robot` nodes, CPU, RAM, GPU capability, and labels
- whether VMs on that node should be registered as schedulable images
- image labels, minimum RAM requirement, and whether a GPU is required

After initialization, the CLI enters:

```text
platform>
```

## 4. Recommended First Commands

### 4.1 Show Help

```text
help
```

### 4.2 Inspect Registered Hosts and VMs

```text
list
vms
```

Use these commands to confirm that the wizard registered the hosts and VM images you expect.

### 4.3 Inspect Clonezilla ISO State

```text
cz-list
```

This shows the registered Clonezilla ISOs, including:

- ISO name
- node
- bound IP
- `FREE` / `BUSY` status

If you upload new Clonezilla ISOs to the master node `local` storage after the CLI has already started, run:

```text
cz-register
```

This triggers another scan and appends new Clonezilla ISO registrations.

### 4.4 Inspect Host and Image Details

```text
info host <host_id>
info image <image_id>
```

These commands help verify:

- host labels and capabilities
- image labels and hardware requirements
- scheduler state
- image source node
- whether a bare-metal host has already been bound to a Clonezilla ISO

## 5. Key Command Entry Points

### 5.1 Guided Scheduling

```text
auto
```

`auto` is a guided Q&A flow. It asks for:

- target image tag
- GPU requirement
- minimum RAM
- task priority

It then submits a scheduling request.

### 5.2 Register a Bare-Metal Host

```text
register-baremetal [target_clonezilla_iso]
```

`register-baremetal` :

1. asks for the bare-metal host ID
2. asks for the target disk name such as `sda` or `nvme0n1`
3. asks you to choose a free target Clonezilla ISO
4. binds that ISO's IP to the bare-metal host

So before running it, make sure:

- Clonezilla ISOs have already been registered
- the target Clonezilla ISO is currently `FREE`

### 5.3 Deploy to PVE or Bare Metal

```text
migrate <vmid> <host_id>
```

This is the unified deployment entry point on `version1.0`:

- if `host_id` resolves to a PVE host, the CLI runs the PVE deployment pipeline
- if `host_id` resolves to a `BareMetalHost`, the CLI runs the bare-metal deployment pipeline

For bare-metal deployment, the usual form is:

```text
migrate <vmid> <baremetal_host_id>
```

During a bare-metal deployment, the CLI will:

- verify that the target bare-metal host was already bound through `register-baremetal`
- choose another free Clonezilla ISO as the source-side sender
- automatically configure the source VM to boot into the selected Clonezilla ISO
- start the bare-metal cloning pipeline

## 6. Minimal Working Flow

If you want the shortest practical VM-to-bare-metal flow, use this order:

1. install dependencies
2. check `config.yaml`
3. make sure at least two `clonezilla*.iso` images are uploaded to the master node `local` storage
4. run `python -m interactive_runner`
5. register Clonezilla ISOs, nodes, and VMs in the initialization wizard
6. run `cz-list` and confirm that you have one target ISO plus at least one additional free ISO
7. run `register-baremetal`
8. run `migrate <vmid> <baremetal_host_id>`

If you only want to deploy to a PVE host, skip bare-metal registration and run:

```text
migrate <vmid> <pve_host_id>
```

## 7. Logs and Notes

- CLI logs are written under the project-root `logs/` directory
- the main CLI log file is `logs/interactive_runner.log`
- task-oriented operations also create their own log files under `logs/`
- once the command loop starts, `interactive_runner` also watches for newly added standard-named Clonezilla ISOs in the background

