#!/bin/sh

# First ensure linux up-to-date
sudo apt update -y
sudo apt upgrade -y

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

# Add update and upgrade crontab
echo "0 7 * * * sudo apt update && sudo apt upgrade" >> crontab_new

# Schedule restart
echo "30 7 * * * sudo reboot" >> crontab_new

# Commit and Cleanup
crontab crontab_new
rm crontab_new

# Allow SSH through firewall
sudo ufw allow ssh

# Restart SSH
service ssh restart

# Enable firewall
sudo ufw enable