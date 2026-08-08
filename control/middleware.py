import jwt
from django.contrib.auth import get_user_model
from django.conf import settings
from .jwt_auth import decode_token

class JwtOwnerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        request.owner = None
        token = request.COOKIES.get(settings.ACCESS_COOKIE_NAME)
        if token:
            try:
                payload = decode_token(token, "access")
                request.owner = get_user_model().objects.filter(pk=payload["sub"], is_active=True, is_staff=True).first()
            except jwt.PyJWTError:
                pass
        return self.get_response(request)
