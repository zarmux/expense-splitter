from django.shortcuts import redirect

from .models import Profile

SKIP = ("/welcome/", "/accounts/", "/admin/", "/media/", "/static/")


class OnboardingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.session.get("onboarded") and not request.path.startswith(SKIP):
            if not Profile.objects.get_or_create(user=request.user)[0].onboarded:
                return redirect("welcome")
            request.session["onboarded"] = True
        return self.get_response(request)
