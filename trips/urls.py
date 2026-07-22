from django.urls import path
from .views import calculate_trip, health, location_search

urlpatterns = [
    path("health", health, name="health"),
    path("locations/search", location_search, name="location-search"),
    path("trips/calculate", calculate_trip, name="calculate-trip"),
]
