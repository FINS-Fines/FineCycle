## Direct Disk Cloning (DDC) Deployment Script

This directory contains the automation implementation for the **DDC (Direct Disk Cloning)** mechanism as described in the  paper.
## Mechanism Alignment
The DDC script handles the "Bare-Metal Block-level Deployment" phase. While CLM and CDC rely on a hypervisor layer, DDC ensures maximum hardware performance on non-virtualized hosts by:
1. **Static IP Pre-Configuration**: Uses specialized Clonezilla images with pre-set IPs. This allows the management workstation to immediately take over systems via SSH upon booting, eliminating manual network setup.
2. **Unified Remote Execution**: Orchestrates the process by remotely taking over source and target host sessions. This replaces separate manual terminal operations with a centralized, synchronized workflow from a single workstation.
3. **Automated Block-level Deployment**: Performs a bit-for-bit transfer of the Robotic Image to the physical disk. Block-level cloning ensures 100% software stack fidelity and guarantees native execution performance on bare-metal hardware.

## Usage Instructions

### Prerequisites
- **specialized Clonezilla boot environment** is required to take over the deployment. Follow these steps to customize your Clonezilla ISO.
```bash
## Creates a temporary directory
mkdir -p /root/clonezilla-edit
cd /root/clonezilla-edit

## Copy ISO to the current directory：
cp /<your_clonzilla_iso_path>/clonezilla-live-3.2.0-5-amd64.iso .

## Unpack ISO and create mounting points
mkdir mnt
mount -o loop clonezilla.iso mnt

## Copy the content and grant modification permissions
mkdir iso
cp -rT mnt iso
umount mnt

## Open the startup configuration file
cd iso/syslinux/
nano isolinux.cfg
## Modify configuration
```
```config
timeout 50
append initrd=/live/initrd.img boot=live union=overlay 
username=user config components quiet loglevel=0 noswap edd=on 
nomodeset enforcing=0 locales=en_US.UTF-8 keyboard-layouts=us 
ocs_live_run="ocs-live-general" ocs_live_extra_param="" 
ocs_live_batch="yes" vga=788 net.ifnames=0 biosdevname=0 
ocs_prerun1="ip link set <interface_name> up" 
ocs_prerun2="ip addr add <static_IP>/<prefix_length> dev <interface_name>" 
ocs_prerun3="ip route add default via <gateway_IP>" 
ocs_daemonon="ssh" ocs_prerun4="passwd -d user" 
ocs_prerun5="echo PermitEmptyPasswords yes >> /etc/ssh/sshd_config" 
ocs_prerun6="systemctl restart ssh" 
nosplash i915.blacklist=yes radeonhd.blacklist=yes 
nouveau.blacklist=yes vmwgfx.enable_fbdev=1
```
```bash
## The main modifications include the following:
## Change timeout to 50 to reduce version selection page dwell time
## Locales=en-US. UTF-8 Keyboard-Llayouts=us Default
## ocs_live_match="yes" Cancel interactive operation and directly enter the command line
## ocs_rerun1-6 sets IP and cancels password to ensure remote access without interactive password input,
## please ensure that the interface name, static IP, and gateway are configured to match hardware environment.
## Similarly, modify the/iso/grub/grub.cfg file according to the above requirements

## Repackaging as. iso image
cd /root/clonezilla-edit/iso
xorriso -as mkisofs -r -V 'Clonezilla-Live-Custom' 
-J -joliet-long -b syslinux/isolinux.bin -c syslinux/boot.cat 
-boot-load-size 4 -boot-info-table -no-emul-boot 
-eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot
-isohybrid-gpt-basdat     -o ../clonezilla-receiver.iso  

## Put the packaged ISO image back into local space
cd /root/clonezilla-edit
cp clonezilla-receiver.iso /var/lib/vz/template/iso/
cp clonezilla-publisher.iso /var/lib/vz/template/iso/

## All operations are completed
```
- Prepare two **specialized Clonezilla environments** with pre-configured static networking.

  - **Publisher Node:** Configured with `<publisher_IP>/<prefix_length>`, deployed to the source VM on the central server.
  - **Subscriber Node:** Configured with `<subscriber_IP>/<prefix_length>`, deployed to the target bare-metal host via live media.

- `pyautogui` library (`pip install pyautogui`)
### Execution
- Run the script on a control machine with SSH access to the **server and bare-metal robotic host** for deployment:
```bash
python deploy_restore_ddc.py
```
- Run the script for restoration:

Adjust the disk size through the UI interface of PVE, ensuring it is larger than the bare-metal disk to guarantee the normal operation of the cloning process.
![](figures/resize.png)
then, run the script for restoration and modify the disk size as the origin size.
```bash
## Run the script on a control machine with SSH access to the **server and bare-metal robotic host**
python deploy_restore_ddc.py

## Enter the shell of server host
cd <your_VM_disk_path>
qemu-img resize --shrink vm-<VM_ID>-disk-<disk_ID>.qcow2 <origin_disk_size>
nano /etc/pve/qemu_server/<VM_ID>.conf
#Modify the disk size as the origin_disk_size

#Boot the VM in Clonezilla iso and enter the shell of the clonezilla
lsblk
#Find the correct disk
sudo gdisk /dev/<disk_name>
#Enter x and press Enter
#Enter e and press Enter
#Enter v and press Enter
#Enter w and press Enter

#Shut down the VM and restoration is done.
```
