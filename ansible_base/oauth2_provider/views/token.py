import hashlib
from datetime import timedelta

from django.utils.timezone import now
from oauth2_provider import views as oauth_views
from oauthlib import oauth2
from rest_framework.viewsets import ModelViewSet

from ansible_base.lib.utils.hashing import hash_string
from ansible_base.lib.utils.settings import get_setting
from ansible_base.lib.utils.views.django_app_api import AnsibleBaseDjangoAppApiView
from ansible_base.oauth2_provider.models import OAuth2AccessToken, OAuth2RefreshToken
from ansible_base.oauth2_provider.permissions import OAuth2ScopePermission
from ansible_base.oauth2_provider.serializers import OAuth2TokenSerializer
from ansible_base.oauth2_provider.views.permissions import OAuth2TokenPermission


class TokenView(oauth_views.TokenView):
    # There is a big flow of logic that happens around this (create_token_response) behind the scenes.
    #
    # oauth2_provider.views.TokenView inherits from oauth2_provider.views.mixins.OAuthLibMixin
    # That's where this method comes from originally.
    # Then *that* method ends up calling oauth2_provider.oauth2_backends.OAuthLibCore.create_token_response
    # Then *that* method ends up (ultimately) calling oauthlib.oauth2.rfc6749....
    def create_token_response(self, request):
        # Django OAuth2 Toolkit has a bug whereby refresh tokens are *never*
        # properly expired (ugh):
        #
        # https://github.com/jazzband/django-oauth-toolkit/issues/746
        #
        # This code detects and auto-expires them on refresh grant
        # requests.
        if request.POST.get('grant_type') == 'refresh_token' and 'refresh_token' in request.POST:
            hashed_refresh_token = hash_string(request.POST['refresh_token'], hasher=hashlib.sha256, algo="sha256")
            refresh_token = OAuth2RefreshToken.objects.filter(token=hashed_refresh_token).first()
            if refresh_token:
                expire_seconds = get_setting('OAUTH2_PROVIDER', {}).get('REFRESH_TOKEN_EXPIRE_SECONDS', 0)
                if refresh_token.created + timedelta(seconds=expire_seconds) < now():
                    return request.build_absolute_uri(), {}, 'The refresh token has expired.', '403'

        core = self.get_oauthlib_core()  # oauth2_provider.views.mixins.OAuthLibMixin.create_token_response

        # oauth2_provider.oauth2_backends.OAuthLibCore.create_token_response
        # (we override this so we can implement our own error handling to be compatible with AWX)

        # This is really, really ugly. Modify the request to hash the refresh_token
        # but only long enough for the oauth lib to do its magic.
        did_hash_refresh_token = False
        old_post = request.POST
        if 'refresh_token' in request.POST:
            did_hash_refresh_token = True
            request.POST = request.POST.copy()  # so it's mutable
            hashed_refresh_token = hash_string(request.POST['refresh_token'], hasher=hashlib.sha256, algo="sha256")
            request.POST['refresh_token'] = hashed_refresh_token

        try:
            uri, http_method, body, headers = core._extract_params(request)
        finally:
            if did_hash_refresh_token:
                request.POST = old_post

        extra_credentials = core._get_extra_credentials(request)
        try:
            headers, body, status = core.server.create_token_response(uri, http_method, body, headers, extra_credentials)
            uri = headers.get("Location", None)
            status = 201 if request.method == 'POST' and status == 200 else status
            return uri, headers, body, status
        except oauth2.AccessDeniedError as e:
            return request.build_absolute_uri(), {}, str(e), 403  # Compat with AWX
        except oauth2.OAuth2Error as e:
            return request.build_absolute_uri(), {}, str(e), e.status_code


class OAuth2TokenViewSet(ModelViewSet, AnsibleBaseDjangoAppApiView):
    queryset = OAuth2AccessToken.objects.all()
    serializer_class = OAuth2TokenSerializer
    permission_classes = [OAuth2ScopePermission, OAuth2TokenPermission]
