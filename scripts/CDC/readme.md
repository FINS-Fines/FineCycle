# Cluster Disk Cloning (CDC) Deployment Script

This directory contains the automation implementation for the **CDC (Cluster Disk Cloning)** mechanism, focusing on restoring high-performance local I/O after a migration.

## Mechanism Alignment
The CDC script handles the "Post-Migration Disk Localization" phase. According to the paper, while CLM moves the VM quickly, CDC ensures long-term performance by:
1. **Network Mode Switching**: Temporarily switches the host to **Wired Mode** (via `/etc/network/interfaces.wired`) to provide high-bandwidth stability for large disk cloning tasks, then reverts to **Wireless Mode** upon completion.
2. **Disk Localization (Cloning)**: Moves operational disks from the shared storage (used during CLM) to the target host's **local storage** (e.g., local-lvm/ZFS). This utilizes Proxmox's `qm move-disk` to provide the VM with local NVMe/SSD I/O speeds.
3. **Boot Configuration**: Automatically re-detects the new local disk and updates the VM's `bootdisk` and `boot order` parameters to ensure successful startup from the local copy.

## Usage Instructions

### Prerequisites
- Target host must have pre-configured network templates: `/etc/network/interfaces.wired` and `/etc/network/interfaces.wireless`.
- `paramiko` library (`pip install paramiko`)

### Execution
- Run the script to deploy after the VM has successfully reached the target node via CLM:

    ```bash
    python deploy_cdc.py \
        --vmid <VM_ID> \
        --target-ip <target_PVE_host_IP> \
        --wired-ip <host_wired_IP> \
        --wireless-ip <host_wireless_IP> \
        --password <your_ssh_password> \
        --target-local-storage local-lvm
    
    Parameters:
    --vmid: The ID of the VM.
    
    --target-ip: The initial IP of the target PVE host.
    
    --wired-ip: The static IP the host will use when switched to wired mode.
    
    --wireless-ip: The static IP the host will use when switched back to wireless mode.
    
    --target-local-storage: The destination local storage name (default: local-lvm).
    
    --reboot-timeout: Maximum seconds to wait for the host to come back online after network-switch reboots (default: 300).
    ```
- Run the script to restore:
    ```bash
  python restore_cdc.py \
  --vmid <VM_ID> \
  --target-ip <target_PVE_host_IP> \
  --wired-ip <host_wired_IP> \
  --wireless-ip <host_wireless_IP> \
  --password <your_ssh_password> \
  --shared-storage VMs

  Parameters:
  --vmid: The ID of the VM.

  --target-ip: The initial IP of the target PVE host.

  --wired-ip: The static IP the host will use when switched to wired mode.

  --wireless-ip: The static IP the host will use when switched back to wireless mode.

  --shared-storage: The destination shared storage name (default: VMs).

  --reboot-timeout: Maximum seconds to wait for the host to come back online after network-switch reboots (default: 300).
    ```
Note: In typical scenarios, target-ip, wired-ip, and wireless-ip should be identical to ensure continuous network connectivity.