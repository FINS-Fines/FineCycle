# Cluster Live Migration (CLM) Deployment Script

This directory contains the automation implementation for the **CLM (Cluster Live Migration)** mechanism as described in the  paper.

## Mechanism Alignment
The CLM script automates the "Pure State Migration" phase. To ensure the lowest possible downtime and maintain post-migration connectivity, it implements:
1. **Peripheral Normalization**: Clears all specific USB hardware assignments and initializes a unified `usb0: spice` configuration to prevent hardware conflicts on the target node.
2. **Shared Storage Verification**: Ensures that all operational disks (SCSI/SATA/IDE/VirtIO) are already residing on shared storage (e.g., Ceph, NFS, or shared LVM) to enable zero-copy disk migration.
3. **Pure State Transfer**: Executes the `qm migrate` command with the `--online` flag to transfer only the CPU state and RAM contents between PVE nodes.

## Usage Instructions

### Prerequisites
- Python 3.10+
- `paramiko` library (`pip install paramiko`)
- Proxmox Virtual Environment (PVE) cluster with shared storage configured.

### Execution
Run the script on a control machine with SSH access to the **source PVE node** for deployment and restoration:

```bash
python deploy_restore_clm.py \
    --vmid <VM_ID> \
    --src-ip <source_PVE_host_IP> \
    --target-node <target_PVE_node_name> \
    --password <your_ssh_password> \
    --shared-storage VMs

Parameters
--vmid: The ID of the VM to be migrated.

--src-ip: IP address of the source PVE host where the VM is currently running.

--target-node: The name of the destination PVE node (as defined in the cluster).

--password: SSH password for the nodes (assumes root access).

--shared-storage: The name of your shared storage pool (default: VMs).