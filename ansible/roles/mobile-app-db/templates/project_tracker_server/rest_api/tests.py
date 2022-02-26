from rest_framework import status
from rest_framework.test import APITestCase, APIClient
from django.contrib.auth.models import User
from rest_api.models import Project, ProjectMembership, Task

class UserTests(APITestCase):
    def test_create_user(self):
        """
        Ensure we can create a new user object.
        """

        # User Details
        username = "TestUser"
        email = username + "@test.com"
        password = "pass1542"        

        # Response
        url = '/auth/users/'
        user_payload = {"username": username, "password": password, "email":email}
        response = self.client.post(url, user_payload)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(User.objects.count(), 1)
        user_payload.pop('password') # Response doesn't return password
        self.assertEqual(response.data, user_payload)

    def test_get_user(self):
        """
        Ensure we can get users correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        # Create users
        for i in range(4):
            
            # User Details
            username = "TestUser" + str(i)
            email = username + "@test.com"
            password = "pass1542"

            User.objects.create(username=username, email=email, password=password)

        # Request
        url = '/users/'
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 5)
        self.assertEqual(list(response.data['results'][0].keys()), ['username'])

class ProjectTests(APITestCase):

    def test_create_project(self):
        """
        Ensure we can create project correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        # Project Detail
        project_name = "Test Project"
        project_description = "Test Project Description"
        
        # Request
        url = '/projects/'
        project_payload = {"name": project_name, "description": project_description}
        response = self.client.post(url, project_payload)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Project.objects.count(), 1)
        project_payload['id'] = 1 # Auto-created field
        project_payload['owner'] = 'Tester' # Auto-created field
        project_payload['membership'] = 1 # Auto-created field
        project_payload['location'] = 1 # Auto-created field

        self.assertEqual(response.data, project_payload)

    def test_get_project_list(self):
        """
        Ensure we can get projects correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects and Memberships
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)

        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)

        Project.objects.create(name="Project 5", description="Should not be returned", owner=test_client.user)

        # Request
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 4)

    def test_get_project(self):
        """
        Ensure we get projects correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects and Memberships
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)

        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)

        Project.objects.create(name="Project 5", description="Should not be returned", owner=test_client.user)

        for i in range(4):
            project_num = i + 1
            
            # Request
            url = '/projects/' + str(project_num) + '/'
            response = self.client.get(url)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['id'], project_num)
        
        
        # Request
        url = '/projects/5/'
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

    def test_patch_project(self):
        """
        Ensure we can patch projects correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)
        Project.objects.create(name="TestUser1 Project", description= 'Shouldn\'t be able to edit', owner=test_client.user)

        # Request
        url = '/projects/1/'
        newDescription = "Project 1 New Description"
        response = self.client.patch(url, data={"description": newDescription})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['description'], newDescription)

        # Request
        url = '/projects/2/'
        newDescription = "Project 2 New Description"
        response = self.client.patch(url, data={"description": newDescription})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

    def test_delete_project(self):
        """
        Ensure we can delete projects correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)
        Project.objects.create(name="TestUser1 Project", description= 'Shouldn\'t be able to edit', owner=test_client.user)

        # Request
        url = '/projects/1/'
        response = self.client.delete(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['detail'], "Not found.")

        # Request
        url = '/projects/2/'
        response = self.client.delete(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

class ProjectMembershipTests(APITestCase):

    def test_create_membership(self):
        """
        Ensure we can create project membership correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        User.objects.create(username='TestUser1', password='test1542')

        # Setup Project
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)

        # Request
        url = '/projectmemberships/'
        membership_payload = {"owner": 'TestUser1', "project":1, "permission_level": 1}
        response = self.client.post(url, membership_payload)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ProjectMembership.objects.count(), 2)

    def test_get_membership_list(self):
        """
        Ensure we can get project memberships correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        # Setup Projects and Memberships
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)

        test_user = User.objects.create(username='TestUser1', password='test1542')

        for i in range(3):
            project_num = i + 2

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_user)
            ProjectMembership.objects.create(owner=test_user, project=project, permission_level=1)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)

        project = Project.objects.create(name="Project 5", description="Should not be returned", owner=test_user)
        ProjectMembership.objects.create(owner=test_user, project=project, permission_level=1)

        # Request
        url = '/projectmemberships/'
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 4)

    def test_get_membership(self):
        """
        Ensure we can get a project membership correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        # Setup Projects and Memberships
        url = '/projects/'
        project_payload = {"name": 'Tester Project', "description": 'Tester Project'}
        self.client.post(url, project_payload)

        test_user = User.objects.create(username='TestUser1', password='test1542')

        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_user)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)

        project = Project.objects.create(name="Project 5", description="Should not be returned", owner=test_user)
        ProjectMembership.objects.create(owner=test_user, project=project, permission_level=1)

        for i in range(4):
            project_membership_num = i + 1
            
            # Request
            url = '/projectmemberships/' + str(project_membership_num) + '/'
            response = self.client.get(url)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['id'], project_membership_num)

    def test_patch_membership(self):
        """
        Ensure we can patch project memberships correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects and Memberships
        project = Project.objects.create(name='Test Project', description='Test Project Description', owner=self.user)
        ProjectMembership.objects.create(owner=self.user, project=project, permission_level=1)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=3)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.patch(url, data={"permission_level": 1})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

        # Request
        url = '/projectmemberships/2/'
        response = self.client.patch(url, data={"permission_level": 2})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['permission_level'], 2)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.patch(url, data={"permission_level": 1})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

        # Request
        url = '/projectmemberships/2/'
        response = self.client.patch(url, data={"permission_level": 1})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['permission_level'], 1)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.patch(url, data={"permission_level": 2})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['permission_level'], 2)

    def test_delete_membership(self):
        """
        Ensure we can delete project memberships correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects and Memberships
        project = Project.objects.create(name='Test Project', description='Test Project Description', owner=self.user)
        ProjectMembership.objects.create(owner=self.user, project=project, permission_level=1)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=3)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.delete(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

        # Request
        url = '/projectmemberships/2/'
        response = self.client.patch(url, data={"permission_level": 2})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['permission_level'], 2)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.delete(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

        # Request
        url = '/projectmemberships/2/'
        response = self.client.patch(url, data={"permission_level": 1})

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['permission_level'], 1)

        # Request
        url = '/projectmemberships/2/'
        response = test_client.delete(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['detail'], "Not found.")

class TaskTests(APITestCase):

    def test_create_task(self):
        """
        Ensure we can create tasks correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_user = User.objects.create(username='TestUser1', password='test1542')

        # Setup Projects
        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_user)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)

        Project.objects.create(name="Project 4", description="Should not be returned", owner=test_user)

        # Task Details
        task_payload = {"project":1, "owner":'Tester', "name":"Test Task Name", "description":"Test Task Description", "category":1, "priority":2, "status":3}

        for i in range(2):
            task_num = i + 1

            # Request
            url = '/tasks/'
            task_payload['project'] = task_num
            response = self.client.post(url, task_payload)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(Task.objects.count(), task_num)

        for i in range(2):
            task_num = i + 3

            # Request
            url = '/tasks/'
            task_payload['project'] = task_num
            response = self.client.post(url, task_payload)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")
            self.assertEqual(Task.objects.count(), 2)

    def test_get_task_list(self):
        """
        Ensure we can get tasks correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)
            
        project = Project.objects.create(name="Project 4", description="Should not be returned", owner=test_client.user)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)

        for i in range(2):
            task_num = i + 1
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description" + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=self.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(2):
            task_num = i + 3
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=test_client.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        # Request
        url = '/tasks/'
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)

    def test_get_task(self):
        """
        Ensure we can get a task correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)
            
        project = Project.objects.create(name="Project 4", description="Should not be returned", owner=test_client.user)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)

        for i in range(2):
            task_num = i + 1
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description" + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=self.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(2):
            task_num = i + 3
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=test_client.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(3):
            task_num = i + 1
            # Request
            url = '/tasks/' + str(task_num) + '/'
            response = self.client.get(url)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['id'], task_num)

        # Request
        url = '/tasks/4/'
        response = self.client.get(url)

        # Tests
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

    def test_patch_task(self):
        """
        Ensure we can patch a task correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)
            
        project = Project.objects.create(name="Project 4", description="Should not be returned", owner=test_client.user)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)

        for i in range(2):
            task_num = i + 1
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=self.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(2):
            task_num = i + 3
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=test_client.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        task_data = {"description": "New Description"}

        for i in range(2):
            task_num = i + 1
            # Request
            url = '/tasks/' + str(task_num) + '/'
            response = self.client.patch(url, data={"description": "New Description"})

            # Tests
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['description'], "New Description")

        for i in range(2):
            task_num = i + 3
            # Request
            url = '/tasks/' + str(task_num) + '/'
            response = self.client.patch(url, data={"description": "New Description"})

            # Tests
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")

    def test_delete_task(self):
        """
        Ensure we can delete a task correctly
        """

        # Setup Client and Authentication
        self.client = APIClient()
        self.user = User.objects.create(username='Tester', password='test1542')
        self.client.force_authenticate(self.user)

        test_client = APIClient()
        test_client.user = User.objects.create(username='TestUser1', password='test1542')
        test_client.force_authenticate(test_client.user)

        # Setup Projects
        for i in range(3):
            project_num = i + 1

            # Project Detail
            project_name = "Project" + str(project_num)
            project_description = "Test Project " + str(project_num) +" Description"

            # Create Projects and Memberships
            project = Project.objects.create(name=project_name, description=project_description, owner=test_client.user)
            ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)
            ProjectMembership.objects.create(owner=self.user, project=project, permission_level=project_num)
            
        project = Project.objects.create(name="Project 4", description="Should not be returned", owner=test_client.user)
        ProjectMembership.objects.create(owner=test_client.user, project=project, permission_level=1)

        for i in range(2):
            task_num = i + 1
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=self.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(2):
            task_num = i + 3
            task_helper = (i % 4) + 1

            # Task Details
            task_project = Project.objects.get(id=task_num)
            task_name = "Test Task Name " + str(task_num)
            task_description = "Test Task Description " + str(task_num)
            task_category = task_helper # Only 4 permission levels, skip 0
            task_priority = task_helper # Only 4 permission levels, skip 0
            task_status = task_helper # Only 4 permission levels, skip 0

            Task.objects.create(project=task_project, owner=test_client.user, name=task_name, description=task_description, category=task_category, priority=task_priority, status=task_status)

        for i in range(2):
            task_num = i + 1
            # Request
            url = '/tasks/' + str(task_num) + '/'
            response = self.client.delete(url)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            self.assertEqual(response.data['detail'], "Not found.")

        for i in range(2):
            task_num = i + 3
            # Request
            url = '/tasks/' + str(task_num) + '/'
            response = self.client.delete(url)

            # Tests
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(response.data['detail'], "You do not have permission to perform this action.")
