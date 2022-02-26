from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_api import views

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.

router = DefaultRouter()
router.register(r'users', views.UserViewSet)
router.register(r'projects', views.ProjectViewSet)
router.register(r'projectmemberships', views.ProjectMembershipViewSet)
router.register(r'tasks', views.TaskViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
