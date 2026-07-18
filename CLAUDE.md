# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

FINECYCLE is a hardware-virtualization-based management paradigm for robotic applications. It encapsulates OS, dev environments, dependencies, and applications into unified "Robotic Images", achieving a closed-loop workflow: **Deploy → Develop → Restore → Redeploy**. The system orchestrates VM/disk migration across Proxmox VE (PVE) clusters and bare-metal hosts via Clonezilla.

## Commands

```bash
# Install dependencies
pip install -r requirement.txt

# Launch the interactive CLI (main entry point)
python -m interactive_runner

# Run the demo API script
python main.py

# Optional: enhanced CLI experience
pip install rich prompt_toolkit

# CDC deployment/restore (Cluster Disk Cloning)
python scripts/CDC/deploy_cdc.py --vmid <ID> --target-ip <IP> --wired-ip <IP> --wireless-ip <IP> --password <PWD>
python scripts/CDC/restore_cdc.py --vmid <ID> --target-ip <IP> --wired-ip <IP> --wireless-ip <IP> --password <PWD>

# CLM deployment/restore (Cluster Live Migration)
python scripts/CLM/deploy_restore_clm.py --vmid <ID> --src-ip <IP> --target-node <NODE> --password <PWD>

# DDC deployment/restore (Direct Disk Cloning - bare metal)
python scripts/DDC/deploy_restore_ddc.py
```

## Architecture

### Layered design

```
main.py / interactive_runner.py     ← Entry points (CLI)
        │
configs/                            ← All configuration
├── config.py                       ← ConfigNode + load_config + GlobalConfig
├── config.yaml                     ← Runtime settings (timeouts, network defaults, USB whitelist)
└── netconf_templates.py            ← PVE wired/wireless network config templates (code + docs)
        │
core/                               ← Orchestration layer (5 files)
├── datacenter.py                   ← DataCenter facade: hosts, images, scheduler, deployment API
├── scheduler.py                    ← Scheduler: task queue + resource allocation + built-in dispatch
├── pve_pipeline.py                 ← DeploymentPipeline: 6-step PVE migration SOP (software)
├── baremetal_pipeline.py           ← ClonezillaMigrationPipeline: bare-metal DDC (hardware)
└── utils.py                        ← setup_task_logger for manual CLI task logging
        │
infrastructure/                     ← Host abstractions (4 files)
├── base_host.py                    ← BaseHost (ABC) + PveCapableHost (qm ops for PVE-capable hosts)
├── server_host.py                  ← ServerHost(PveCapableHost): master server, cluster discovery, Clonezilla ISO/CD-ROM
├── pve_host.py                     ← PveHost(PveCapableHost): robot host, network switching, config detection
└── bare_metal_host.py              ← BareMetalHost(BaseHost): non-virtualized robot, no qm capabilities
        │
domain/                             ← Pure data entities
├── image.py                        ← RobotAppImage entity
└── clonezilla.py                   ← ClonezillaImage dataclass
```

### Module dependency order

```
domain/  +  infrastructure/  +  configs/
           ↓
      datacenter.py          ← DataCenter: owns hosts, images, scheduler (passes self to Scheduler)
           ↓
      scheduler.py           ← Scheduler(self): resource allocation + built-in ThreadPoolExecutor dispatch
           ↓                  (submit_task → _try_dispatch_next → _execute_allocation → pipeline)
   pve_pipeline.py  +  baremetal_pipeline.py   ← called by Scheduler._execute_allocation
           ↓
   interactive_runner.py      ← CLI: only talks to DataCenter facade
```

### Scheduler internal dispatch (self-contained loop)

```
submit_task(task)
    → _try_dispatch_next()
        → _try_schedule_next()    # priority sort + allocate
        → _execute_allocation()   # executor.submit → DeploymentPipeline
            → on success: keep BUSY (dev lifecycle)
            → on failure: release() → _try_dispatch_next() (retry pending)
release(task_id)
    → mark_host_free / mark_image_free
    → _try_dispatch_next()        # re-schedule pending tasks
```

### Key design decisions

- **DataCenter is server-only**: `DataCenter` never reboots hosts nor modifies network config. It only operates on VM templates/images through `ServerHost` (qm commands + PVE API). All host reboot/network-switching is handled by `DeploymentPipeline` on `PveHost` targets.
- **PveCapableHost** (in `base_host.py`): Intermediate class providing shared `qm` operations (template, clone, migrate, disk move, USB passthrough). Only inherited by `ServerHost` and `PveHost`. `BareMetalHost` extends `BaseHost` directly — no qm capabilities.
- **ServerHost vs PveHost**: Both extend `PveCapableHost`, but `ServerHost` defaults `disk_localization_enabled=False` (keeps disks on shared storage), while `PveHost` defaults it to `True` (moves disks to local-lvm after migration).
- **ServerHost also owns cluster discovery**: `pvesh_get_json()`, `list_node_vms()`, `scan_clonezilla_isos()`, `resolve_node_ssh_ip()`, `find_vm_current_node()` all live on `ServerHost` — the PVE API/SSH entry point.
- **DeploymentPipeline SOP** (6 steps): SourceDiscovery → NetworkPrep (wired switch + reboot) → PreMigration (disk move to VMs + USB normalization) → Migration (qm migrate --online) → PostMigration (disk localization + USB passthrough) → Finalization (wireless switch + reboot).
- **Scheduler (built-in dispatch)**: `submit_task()` immediately tries to schedule + dispatch via internal `ThreadPoolExecutor`. `release()` auto-triggers re-dispatch of pending tasks. No separate polling loop. Resources stay BUSY after successful deployment (dev lifecycle), must be released manually.
- **Config**: `configs/config.py` merges `configs/config.yaml` over `DEFAULT_CONFIG` via deep merge. Accessed as `ConfigNode` with attribute-style access (e.g., `config.timeouts.migration`).
- **Network profiles**: `PveHost.upload_network_profiles()` generates `/etc/network/interfaces.wired` and `.wireless` on the remote host using hash-compare to skip redundant writes.
- **Duplicates eliminated**: `find_host()` and `find_vm_current_node()` live only in `DataCenter`. Both pipelines and the CLI delegate to them. IP utilities (`_is_ipv4`, `_pick_best_ip`, etc.) live only in `ServerHost`.

### Three migration mechanisms

| Mechanism | Target | Pipeline File | Description |
|-----------|--------|---------------|-------------|
| CLM (Cluster Live Migration) | PVE → PVE | pve_pipeline.py | Online migration of VM state over shared storage |
| CDC (Cluster Disk Cloning) | PVE → PVE | pve_pipeline.py | Post-migration disk localization (VMs → local-lvm) |
| DDC (Direct Disk Cloning) | PVE → Bare Metal | baremetal_pipeline.py | Block-level cloning via Clonezilla live environment |

### Dependencies

- **paramiko** — all SSH connectivity to PVE/bare-metal hosts
- **proxmoxer** — Proxmox VE REST API client
- **pyyaml** — config file parsing
- Optional: **rich** + **prompt_toolkit** — enhanced interactive CLI
- System: **xterm**, **xdotool** — required by baremetal_pipeline for Clonezilla terminal automation
