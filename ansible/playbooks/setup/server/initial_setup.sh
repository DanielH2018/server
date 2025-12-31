#!/bin/sh

# Set Playbook Directory
ANSIBLE_PLAYBOOK_DIR="/home/ubuntu/server/ansible/playbooks/"

# Install ansible
sudo apt install ansible -y
# Run initial setup playbook
ansible-playbook ${ANSIBLE_PLAYBOOK_DIR}/setup/server/initial_setup.yml -K
# Run Docker Setup playbook
ansible-playbook ${ANSIBLE_PLAYBOOK_DIR}/setup/docker_install.yml -K