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
crontab -l > crontab_new

# Clean dangling Docker images
echo "30 6 * * * /usr/bin/docker image prune -f" >> crontab_new

# Add update and upgrade crontab at 7am UTC(2am EST)
echo "0 7 * * * root apt-get update" >> crontab_new

echo "5 7 * * * root apt-get upgrade" >> crontab_new

echo "10 7 * * * root apt-get autoremove" >> crontab_new

# Schedule restart at 7:30am UTC(2:30am EST) on Sunday
echo "30 7 * * 0 /sbin/shutdown -r now" >> crontab_new

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
