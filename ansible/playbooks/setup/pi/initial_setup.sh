#!/bin/sh

# Install ansible
sudo apt install ansible -y
# Run initial setup playbook
ansible-playbook /home/ubuntu/server/ansible/initial_setup.yml -K
# Run Raspberry Pi optimization playbook
ansible-playbook /home/ubuntu/server/ansible/setup/pi/optimize_pi.yml -K
