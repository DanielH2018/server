from django.db import models
from django.contrib.auth.models import User

class Project(models.Model):
    name = models.CharField(max_length=20, null=False, blank=False, default=None)
    description = models.TextField(max_length=200, null=False, blank=True, default='')
    
    owner = models.ForeignKey(User, null=False, blank=False, default=None, related_name='projects', on_delete=models.CASCADE)

    class Meta:
        ordering = ['id']

class ProjectMembership(models.Model):
    # Permission Levels
    SHARE = 1
    EDIT = 2
    VIEW = 3
    PERMISSION_LEVELS = (
        (SHARE, 'Share'),
        (EDIT, 'Edit'),
        (VIEW, 'View'),
    )
    # Project Location Options
    MAIN = 1
    ARCHIVE = 2
    TRASH = 3
    LOCATIONS = (
        (MAIN, 'Main'),
        (ARCHIVE, 'Archive'),
        (TRASH, 'Trash'),
    )
    project = models.ForeignKey(Project, null=False, blank=False, default=None, related_name='projectMemberships', on_delete=models.CASCADE, editable=False)
    owner = models.ForeignKey(User, null=False, blank=False, default=None, related_name='projectMemberships', on_delete=models.CASCADE, editable=False)
    location = models.IntegerField(choices=LOCATIONS, blank=False, null=False, default=MAIN)
    permission_level = models.IntegerField(choices=PERMISSION_LEVELS, blank=False, null=False, default=VIEW)

    class Meta:
        ordering = ['id']
        unique_together = ['project', 'owner']

class Task(models.Model):
    # Categories
    TASK = 1
    FEATURE = 2
    BUGFIX = 3
    OTHER = 4
    CATEGORIES = (
        (TASK, 'Task'),
        (FEATURE, 'Feature'),
        (BUGFIX, 'Bug'),
        (OTHER, 'Other'),
    )
    # Priority Levels
    Wishlist = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    PRIORITY_LEVELS = (
        (Wishlist, 'None'),
        (LOW, 'Low'),
        (MEDIUM, 'Medium'),
        (HIGH, "High"),
    )
    # Statuses
    BACKLOG = 1
    IN_PROGRESS = 2
    TESTING = 3
    COMPLETED = 4
    STATUSES = (
        (BACKLOG, 'Backlog'),
        (IN_PROGRESS, 'In Progess'),
        (TESTING, 'Testing'),
        (COMPLETED, 'Completed'),
    )

    project = models.ForeignKey(Project, null=False, blank=False, default=None, related_name='tasks', on_delete=models.CASCADE, editable=False)
    owner = models.ForeignKey(User, null=False, blank=False, default=None, related_name='tasks', on_delete=models.CASCADE)
    name = models.TextField(max_length=200, null=False, blank=True, default='')
    description = models.TextField(max_length=200, null=False, blank=True, default='')
    category = models.IntegerField(choices=CATEGORIES, null=False, blank=False, default=TASK)
    priority = models.IntegerField(choices=PRIORITY_LEVELS, blank=False, null=False, default=Wishlist)
    status = models.IntegerField(choices=STATUSES, blank=False, null=False, default=BACKLOG)

    class Meta:
        ordering = ['id']
