# shellcheck shell=sh
# Shipped by homelab-daniel-pi (the pi-tuning component). It replaces the SOPS export that the
# config_files role carried in the Pi's tracked ~/.bashrc, which reverts to Ubuntu's on a rebuild.
# The chezmoi hosts carry the same line in the dotfiles repo's home/dot_bashrc. A login shell
# reads this file through /etc/profile; an operator's `sops` then finds the age key without a flag.
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
