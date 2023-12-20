---Instructions to setup server environment---
1. Run `sudo apt-get update -y`
2. Run `sudo apt-get upgrade -y`
3. Run `sudo chmod +x /home/ubuntu/server/ansible/initial_setup.sh`.
4. Run `sudo /home/ubuntu/server/ansible/intial_setup.sh`.
    1. If using Intel XE graphics, ensure `/dev/dri/` exists, otherwise run `sudo apt install linux-oem-22.04` and reboot.
5. Run `ansible-playbook setup-docker.yml --ask-become-pass`.
6. Run `docker exec crowdsec cscli bouncers add bouncer-traefik` and save api key to .env
6. Reboot
7. Run `ansible-playbook deploy.yml --ask-become-pass

---Instructions to update server environment from git remote---
1. Perform update to container.
2. Run `ansible-playbook git.yml`.

---Instructions to add container to server environment---
1. Create role and tags.
2. Create folder in `roles`, and create `tasks` and `templates` subdirectories.
3. Create `main.yml` in `tasks` and `docker-compose.yml.j2` in templates.
4. Add environment variables to .env and update the docker compose.
5. Add traefik labels and cloudflare CNAME as needed.
6. Run `ansible-playbook deploy.yml --tags "<NAME>".

---Instructions to setup LaTeX editor---
1. Clone Resume repository in server
2. Copy .devcontainer from https://github.com/James-Yu/LaTeX-Workshop/tree/master/samples/docker
3. Install VS Code Remote - Containers, and SSH
4. Reopen the Resume directory with the container

---Instructions to setup Intel QSV---
1. echo "options i915 enable_guc=3" >> /etc/modprobe.d/i915.conf
2. sudo update-initramfs -u
3. sudo update-grub
4. sudo reboot

---Instructions to setup PiVPN---
1. curl -L https://install.pivpn.io | bash

---Instructions for Duplicati---
1. For backing up to Google Drive, to store not in the root directory, you need a full access token which can be attained here: https://duplicati-oauth-handler.appspot.com/

---Setup instructions for BlueBubbles---
1. Install dependencies
    - sudo apt install qemu qemu-kvm libvirt-clients libvirt-daemon-system bridge-utils virt-manager libguestfs-tools
2. Enable libvirt and load the KVM kernel module
    - sudo systemctl enable --now libvirtd
    - sudo systemctl enable --now virtlogd
    - echo 1 | sudo tee /sys/module/kvm/parameters/ignore_msrs
    - sudo modprobe kvm
3. Follow guide
    - https://docs.bluebubbles.app/server/advanced/macos-virtualization/running-bluebubbles-in-docker-osx