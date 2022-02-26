import django
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project_tracker_server.settings")

django.setup()

import requests
import json

from django.contrib.auth.models import User
from rest_framework.response import Response
from rest_api.models import Project, ProjectMembership, Task

# Creates test data for inspection
def create_test_data():

    baseUrl = 'http://127.0.0.1:8000/'
    userUrl = baseUrl + 'auth/users/'
    authUrl = baseUrl + 'auth/token/login/'
    projectUrl = baseUrl + 'projects/'
    membershipUrl = baseUrl + 'projectmemberships/'
    taskUrl = baseUrl + 'tasks/'

    username_arr = [None]
    token_auth_arr = [None]

    num_users = 4

    for i in range(num_users):
        num = i + 1

        # User Details
        username = "TestUser" + str(num)
        email = username + "@test.com"
        password = "pass1542"
        
        # Request
        user_payload = {"username": username, "password": password, "email":email}
        r = requests.post(url=userUrl, data=user_payload)

        # Get Auth Token
        r = requests.post(url=authUrl, data=user_payload)
        token = r.json().get('auth_token')
        token_auth = {'Authorization': ('Token ' + token)}

        # Add to Array
        username_arr.append(username)
        token_auth_arr.append(token_auth)

    num_projects = num_users

    # Create Projects
    for i in range(num_projects):
        project_num = i + 1
        
        user = (i % num_users) + 1

        # Project Detail
        project_name = "Project" + str(project_num)
        project_description = "Test Project Description"

        # Request
        project_payload = {"name": project_name, "description": project_description, "owner": username_arr[user]}
        r = requests.post(url=projectUrl, headers=token_auth_arr[user], data=project_payload)

    num_memberships = num_users * (num_projects - 2)

    # Create Project Memberships
    for i in range(num_memberships):
        project_num = (i % num_projects) + 1
        
        user = (i % num_users) + 1

        owner = ((user + int(i / num_users)) % num_users) + 1

        # Project Detail
        project_membership_permission_level = (i % 2) + 2 # Only 3 permission levels, don't create any share(1)

        # Request
        project_membership_payload = {"project": project_num, "owner":username_arr[owner], "permission_level": project_membership_permission_level}
        r = requests.post(url=membershipUrl, headers=token_auth_arr[user], data=project_membership_payload)

    num_tasks = num_users * (num_projects - 1)

    # Create Tasks
    for i in range(num_tasks):
        task_project = (i % num_projects) + 1
         
        user = (i % num_users) + 1

        task_helper = (i % 4) + 1

        # Project Detail
        task_name = "Test Task Name " + str(i)
        task_description = "Test Task Description " + str(i)
        task_category = task_helper # Only 4 permission levels, skip 0
        task_priority = task_helper # Only 4 permission levels, skip 0
        task_status = task_helper # Only 4 permission levels, skip 0

        # Request
        task_payload = {"project": task_project, "name": task_name, "description": task_description, "category": task_category, "priority": task_priority, "status": task_status, "owner": username_arr[user]}
        r = requests.post(url=taskUrl, headers=token_auth_arr[user], data=task_payload)
            
create_test_data()
