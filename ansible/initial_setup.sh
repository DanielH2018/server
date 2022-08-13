#!/bin/sh

# Install ansible
sudo apt install ansible -y

# Ensure correct SSH permissions
sudo chmod -R go= ~/.ssh
sudo chown -R ubuntu:ubuntu ~/.ssh

# Update SSH rules
sudo sed -i '/PasswordAuthentication /c\PasswordAuthentication no' /etc/ssh/sshd_config
sudo sed -i '/PermitEmptyPasswords /c\PermitEmptyPasswords no' /etc/ssh/sshd_config
sudo sed -i '/PermitRootLogin /c\PermitRootLogin no' /etc/ssh/sshd_config
sudo sed -i '/IgnoreRhosts /c\IgnoreRhosts yes' /etc/ssh/sshd_config
sudo sed -i '/ChallengeResponseAuthentication  /c\ChallengeResponseAuthentication no' /etc/ssh/sshd_config

# Copy current crontab
sudo crontab -l > crontab_new

# Clean dangling Docker images
echo "30 6 * * * /usr/bin/docker image prune -a -f" >> crontab_new

# Schedule restart at 7:30am UTC(2:30am EST) on Sunday
echo "30 7 * * 0 /sbin/shutdown -r +5" >> crontab_new

# Commit and Cleanup
sudo crontab crontab_new
rm crontab_new

# Allow SSH through firewall
sudo ufw allow ssh

# Restart SSH
service ssh restart

# Enable firewall
sudo ufw enable

# Ensure automatic security updates
sudo apt install unattended-upgrades
