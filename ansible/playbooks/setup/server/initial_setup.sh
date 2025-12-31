#!/bin/sh

# Install ansible
sudo apt install ansible -y
# Run initial setup playbook
ansible-playbook /home/ubuntu/server/ansible/initial_setup.yml -K
