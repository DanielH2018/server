---Instructions to setup server environment---
1. Run `sudo chmod +x /home/ubuntu/server/ansible/install_ansible.sh`.
2. Run `sudo /home/ubuntu/server/ansible/install_ansible.sh`.
3. Run `ansible-playbook /home/ubuntu/server/ansible/ansible/playbook.yml`.

---Instructions to update server environment---
1. Perform update to Container.
2. Run `ansible-playbook playbook.yml --tags "<NAME>".
3. If successful, check into version control.

---Instructions to add container to server environment---
1. Create role and tags.
2. Create folder in `roles`, and create `tasks` and `templates` subdirectories.
3. Create `main.yml` in `tasks` and `docker-compose.yml.j2` in templates.
4. Add environment variables to .env and update the docker compose.
5. Add traefik labels and cloudflare CNAME as needed.
6. Run `ansible-playbook playbook.yml --tags "<NAME>".
7. If successful, check into version control.