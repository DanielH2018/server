from django.http import request
from rest_api.serializers import ProjectSerializer, ProjectMembershipSerializer, TaskSerializer, UserSerializer
from rest_api.models import Project, ProjectMembership, Task
from rest_framework import permissions, viewsets
from django.contrib.auth.models import User

import django_filters as filters

# Model Filters

class UserFilter(filters.FilterSet):

    class Meta:
        model = User
        fields = ['username']

class ProjectFilter(filters.FilterSet):

    @property
    def qs(self):
        parent = super().qs
        if self.request.path == '/projects/':
            # If you're getting list, and not detail
            if('location' in self.request.query_params.keys()):
                projects = ProjectMembership.objects.filter(owner=self.request.user, location=self.request.query_params['location'])
            else:
                projects = ProjectMembership.objects.filter(owner=self.request.user)
            
            return parent.filter(id__in=projects.values_list('project', flat=True))
        return parent

    class Meta:
        model = Project
        fields = ['name', 'description']

class ProjectMembershipFilter(filters.FilterSet):

    class Meta:
        model = ProjectMembership
        fields = ['project', 'permission_level', 'location']

class TaskFilter(filters.FilterSet):

    @property
    def qs(self):
        parent = super().qs
        if self.request.path == '/tasks/':
            # If you're getting list, and not detail
            projects = ProjectMembership.objects.filter(owner=self.request.user).values_list('project', flat=True)
            return parent.filter(project__in=projects)
        return parent

    class Meta:
        model = Task
        fields = ['project', 'category', 'priority', 'status']

# Model Permissions

class ProjectPermissions(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return ProjectMembership.objects.filter(owner=request.user, project=obj, permission_level__lte=3).exists()

        # If it's the project owner, or it's a user with at least view permissions
        return obj.owner == request.user

class ProjectMembershipPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method == 'POST':
            # If it's the project owner, or it's a user with share permissions
            project = Project.objects.get(id=request.data['project'])
            return project.owner == request.user or ProjectMembership.objects.filter(owner=request.user, project=project, permission_level__lte=1).exists()
        return True
    def has_object_permission(self, request, view, obj):
        # Always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True
            
        # If it's the project owner, or it's a user with share permissions and they're not modifying the owner's membership
        return obj.project.owner == request.user or (ProjectMembership.objects.filter(owner=request.user, project=obj.project, permission_level__lte=1).exists() and not obj.project.owner == obj.owner)

class TaskPermissions(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method == 'POST':
            # If it's the project owner, or it's a user with at least edit permissions
            project = Project.objects.get(id=request.data['project'])
            return project.owner == request.user or ProjectMembership.objects.filter(owner=request.user, project=project, permission_level__lte=2).exists()
        return True

    def has_object_permission(self, request, view, obj):
        # Always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return obj.project.owner == request.user or ProjectMembership.objects.filter(owner=request.user, project=obj.project, permission_level__lte=3).exists()

        # If it's the project owner, or it's a user with at least edit permissions
        return obj.project.owner == request.user or ProjectMembership.objects.filter(owner=request.user, project=obj.project, permission_level__lte=2).exists()

# Model Viewsets

class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all().order_by('id')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_class = UserFilter

class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated, ProjectPermissions]
    filter_class = ProjectFilter

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

class ProjectMembershipViewSet(viewsets.ModelViewSet):
    queryset = ProjectMembership.objects.all()
    serializer_class = ProjectMembershipSerializer
    permission_classes = [permissions.IsAuthenticated, ProjectMembershipPermissions]
    filter_class = ProjectMembershipFilter        

class TaskViewSet(viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [permissions.IsAuthenticated, TaskPermissions]
    filter_class = TaskFilter

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
            