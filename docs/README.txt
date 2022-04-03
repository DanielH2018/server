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

For more instructions, look at the README in the setup/ folder.

Not covered in these docs is port forwarding, and Cloudflare dns setup.
