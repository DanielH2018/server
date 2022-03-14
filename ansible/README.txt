---Instructions to setup server environment---
1. Run `sudo chmod +x /home/ubuntu/server/ansible/intial_setup.sh`.
2. Run `sudo /home/ubuntu/server/ansible/intial_setup.sh`.
    1. If using Intel XE graphics, ensure `/dev/dri/` exists, otherwise run `sudo apt install linux-oem-20.04` and reboot.
3. Run `ansible-playbook deploy.yml`.
    1. If you receive an error for sudo requiring a password, add `--ask-become-pass`.

---Instructions to update server environment---
1. Perform update to container.
2. Run `ansible-playbook git.yml`.

---Instructions to add container to server environment---
1. Create role and tags.
2. Create folder in `roles`, and create `tasks` and `templates` subdirectories.
3. Create `main.yml` in `tasks` and `docker-compose.yml.j2` in templates.
4. Add environment variables to .env and update the docker compose.
5. Add traefik labels and cloudflare CNAME as needed.
6. Run `ansible-playbook deploy.yml --tags "<NAME>".

---Instructions to setup minecraft environment---
1. Create A name record in cloudflare to source ip
2. Create port forward to server
