---Instructions to send the setup folder to server---
1. Create ssh key
	1. Follow https://phoenixnap.com/kb/generate-ssh-key-windows-10
2. Login to the remote machine and setup password
3. Setup WiFi (if needed):
	1. Run `cd /etc/netplan`.
	2. Run `ls` and note the file present.
	3. Run `sudo nano <file>`.
	4. Copy the following:
		wifis:
        wlan0:
            dhcp4: true
            optional: true
            access-points:
                "Wifi SSID":
                    password: "password"
	5. Run `sudo netplan apply`
	6. Run `ip a` and locate the server's local ip address
4. Copy ssh key from local to remote:
	1. On local, run `type C:\Users\<username>\.ssh\id_rsa.pub | ssh username@remote_host "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"`.
5. SSH with key into server
6. Copy repository from git
	1. Run `git config --global user.name "your_username"`.
	2. Run `git config --global user.email "your_email_address@example.com"`.
	3. Run `git config --global credential.helper store`
	4. Run `git clone https://github.com/DanielH2018/server.git`.
		1. For password, provide a personal token generated from github.
7. Copy .env from local to remote:
	1. Run `scp -r <env file path> ubuntu@<server ip>:~/server/ansible/`
		1. When prompted, enter the password for the remote user
8. Fix lvm: # As needed, partition name likely different
	1. Run `sudo lvm`
	2. Run `lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv`
	3. Run `exit`
	4. Run `sudo resize2fs /dev/ubuntu-vg/ubuntu-lv`

For more instructions, look at the README in the ansible/ folder.

Not covered in these docs is port forwarding, and Cloudflare DNS setup.

---Instructions to setup server environment---
1. If using Intel XE graphics, ensure `/dev/dri/` exists, otherwise run `sudo apt install linux-oem-22.04` and reboot.
2. Run `pip install ansible`
3. Run `ansible-playbook initial_setup.yml --ask-become-pass`.
4. Run `source ~/.bashrc`
5. Run `ansible-playbook deploy.yml --ask-become-pass`.
6. Run `docker exec crowdsec cscli bouncers add bouncer-traefik` and save api key to .env

---Instructions to add container to server environment---
1. Create role and tags.
2. Create folder in `roles/containers`, and create `tasks` and `templates` subdirectories.
3. Create `main.yml` in `tasks` and `docker-compose.yml.j2` in templates.
4. Add environment variables to .env and update the docker compose.
5. Add traefik labels and cloudflare CNAME as needed.
6. Add entry to `inventory/host_vars` for each server it should run on.
7. Run `ansible-playbook deploy.yml --tags "<NAME>".

---Instructions to setup LaTeX editor---
1. Clone Resume repository in server
2. Copy .devcontainer from https://github.com/James-Yu/LaTeX-Workshop/tree/master/samples/docker
3. Install VS Code Remote - Containers, and SSH
4. Reopen the Resume directory with the container

---Instructions to setup Intel QSV---
1. sudo mkdir -p /etc/modprobe.d
2. sudo sh -c "echo 'options i915 enable_guc=2' >> /etc/modprobe.d/i915.conf"
3. sudo update-initramfs -u && sudo update-grub
5. sudo reboot

---Instructions for Duplicati---
1. For backing up to Google Drive, to store not in the root directory, you need a full access token which can be attained here: https://duplicati-oauth-handler.appspot.com/

---Instructions for journald logs---
1. sudo nano /etc/systemd/journald.conf
2. Find, uncomment and change the parameters: MaxLevelStore=notice MaxLevelSyslog=notice
3. sudo systemctl restart systemd-journald
