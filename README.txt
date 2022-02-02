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
4. Copy setup folder from local to remote:
	1. Run `scp -r <local setup folder> ubuntu@<server ip>:~/
		1. When prompted, enter the password for the remote user
5. Copy ssh key from local to remote:
	1. On local, run `type C:\Users\<username>\.ssh\id_rsa.pub | ssh username@remote_host "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && chmod -R go= ~/.ssh && cat >> ~/.ssh/authorized_keys"`

For more instructions, look at the README in the setup/ folder.

Not covered in these docs is port forwarding, and Cloudflare dns setup.