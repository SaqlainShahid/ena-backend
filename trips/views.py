import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .services import TripError, calculate_trip_plan, search_locations


def health(request):
    if request.method != "GET":
        return JsonResponse({"error": "Use GET for health checks."}, status=405)
    return JsonResponse({"status": "ok", "service": "eld-planner-api"})


def location_search(request):
    if request.method != "GET":
        return JsonResponse({"error": "Use GET for location search."}, status=405)
    query = request.GET.get("q", "")
    try:
        return JsonResponse({"results": search_locations(query)})
    except TripError as exc:
        return JsonResponse({"error": str(exc)}, status=502)


@csrf_exempt
def calculate_trip(request):
    if request.method != "POST":
        return JsonResponse({"error": "Use POST for trip calculations."}, status=405)
    try:
        payload = json.loads(request.body or "{}")
        result = calculate_trip_plan(payload)
        return JsonResponse(result)
    except (ValueError, TripError, json.JSONDecodeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        return JsonResponse({"error": "The route service is temporarily unavailable."}, status=502)
