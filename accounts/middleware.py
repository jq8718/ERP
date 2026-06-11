from django.contrib.auth import logout
from django.shortcuts import redirect
from django.utils import timezone

from .models import UserSession


class UserSessionTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        session_key = getattr(request.session, "session_key", "")
        if user is not None and user.is_authenticated and session_key:
            session = self._touch_session(request, user, session_key)
            if session.status == UserSession.SessionStatus.REVOKED:
                logout(request)
                return redirect("login")
        return self.get_response(request)

    def _touch_session(self, request, user, session_key):
        now = timezone.now()
        session, created = UserSession.objects.get_or_create(
            session_key=session_key,
            defaults={
                "user": user,
                "ip_address": self._ip_address(request),
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:4000],
                "last_seen_at": now,
            },
        )
        if created:
            return session

        update_fields = ["last_seen_at"]
        session.last_seen_at = now
        if session.user_id != user.id:
            session.user = user
            update_fields.append("user")
        session.save(update_fields=update_fields)
        return session

    def _ip_address(self, request):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip() or None
        return request.META.get("REMOTE_ADDR") or None
