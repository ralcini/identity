import logging
import time

import msal


logger = logging.getLogger(__name__)


class Auth(object):
    # These key names are hopefully unique in session
    _TOKEN_CACHE = "_token_cache"
    _AUTH_FLOW = "_auth_flow"
    _USER = "_logged_in_user"
    def __init__(
            self,
            *,
            session,
            authority,
            client_id,
            client_credential=None,  # TODO: TBD
            validators=None,
            ):
        """Create an identity helper for a web app.

        This instance is expected to be long-lived with the web app.

        :param dict session:
            A dict-like object to hold the session data.
            If you are using Flask, you should pass in ``session``.
            If you are using Django, you should pass in ``request.session``.

        :param str authority:
            The authority which your app registers with.
            For example, ``https://example.com/foo``.

        :param str client_id:
            The client_id of your web app, issued by its autority.

        :param str client_credential:
            It is somtimes a string.
            The actual format is decided by the underlying auth library. TBD.

        :param list validators:
            It defines a list of validators which will be automatically triggered
            each time you call ``get_user()`` or ``get_token()``.

            A simpliest validator is just a callable returning a boolean:

                def is_valid(id_token_claims):
                    # This app will only allow John Doe to log in
                    return id_token_claims.get("preferred_username") == "johndoe"

            The reason of a failed validation is not explicitly defined here,
            but you can raise your own exceptions and catch them by yourself.

            There are more sophisticated predefined validators which allows you
            to customize what exception to raise.
        """
        self._session = session
        self._authority = authority
        self._client_id = client_id
        self._client_credential = client_credential
        self._validators = validators if validators else []
        self._http_cache = {}  # All subsequent MSAL instances will share this

    def _load_cache(self):
        cache = msal.SerializableTokenCache()
        if self._session.get(self._TOKEN_CACHE):
            cache.deserialize(self._session[self._TOKEN_CACHE])
        return cache

    def _save_cache(self, cache):
        if cache.has_state_changed:
            self._session[self._TOKEN_CACHE] = cache.serialize()

    def _build_msal_app(self, client_credential=None, cache=None):
        # Web app uses one token cache per user, so we create new MSAL app per token cache
        return (msal.ConfidentialClientApplication
                if client_credential else msal.PublicClientApplication)(
            self._client_id,
            client_credential=client_credential,
            authority=self._authority,
            token_cache=cache,
            http_cache=self._http_cache,  # Share same http_cache among MSAL instances
            )

    def _get_user(self, validators=None):
        id_token_claims = self._session.get(self._USER)
        return id_token_claims if id_token_claims is not None and all(
            v(id_token_claims) for v in (validators or self._validators)
            ) else None

    def log_in(self, scopes=None, redirect_uri=None, **kwargs):
        """This is the first leg of the authentication/authorization.

        :param str redirect_uri:
            Optional.
            If present, it must be an absolute uri you registered for your web app.
            In Flask, if your redirect_uri function is named ``def auth_redirect()``,
            then you can use ``url_for("auth_redirect", _external=True)``.

            Optional. If absent, your end users will log in to your web app
            using a different method named Device Code Flow.
            It is less convenient for end user, but still works.

        Returns a dict containing the ``auth_uri`` that you need to guide end user to visit.
        If your app has no redirect uri, this method will also return a ``user_code``
        which you shall also display to end user for them to use during log-in.
        """
        _scopes = scopes or []
        app = self._build_msal_app()  # Note: This could be a PCA
        if redirect_uri:
            flow = app.initiate_auth_code_flow(
                _scopes, redirect_uri=redirect_uri, **kwargs)
            self._session[self._AUTH_FLOW] = flow
            return {
                "auth_uri": self._session[self._AUTH_FLOW]["auth_uri"],
                }
        else:
            flow = app.initiate_device_flow(_scopes, **kwargs)
            self._session[self._AUTH_FLOW] = flow
            return {
                "auth_uri": flow["verification_uri"],
                "user_code": flow["user_code"],
                }

    def complete_log_in(self, auth_response=None):
        """This is the second leg of the authentication/authorization.

        It is used inside your redirect_uri controller.

        :param dict auth_response:
            A dict-like object containing the parameters issued by Identity Provider.
            If you are using Flask, you can pass in ``request.args``.
            If you are using Django, you can pass in ``HttpRequest.GET``.

            If you were using Device Code Flow, you won't have an auth response,
            in that case you can leave it with its default value ``None``.
        :return:
            * On failure, a dict containing "error" and optional "error_description",
              for you to somehow render it to end user.
            * On success, a dict containing the info of current logged-in user.
              That dict is actually the claims from an already-validated ID token.
        """
        cache = self._load_cache()
        if auth_response:  # Auth Code flow
            try:
                result = self._build_msal_app(
                    client_credential=self._client_credential,
                    cache=cache,
                    ).acquire_token_by_auth_code_flow(
                        self._session.get(self._AUTH_FLOW, {}), auth_response)
            except ValueError as e:  # Usually caused by CSRF
                return {"error": "invalid_grant", "error_description": str(e)}
        else:  # Device Code flow
            result = self._build_msal_app(cache=cache).acquire_token_by_device_flow(
                self._session.get(self._AUTH_FLOW, {}),
                exit_condition=lambda flow: True,
                )
        if "error" in result:
            return result
        # TODO: Reject a re-log-in with a different account?
        self._session[self._USER] = result["id_token_claims"]
        self._save_cache(cache)
        self._session.pop(self._AUTH_FLOW, None)
        return self._get_user()  # This triggers validation

    def get_user(self, validators=None):
        """Returns None if the user has not logged in or no longer passes validation.
        Otherwise returns a dict representing the current logged-in user.

        The dict will have following keys:

        * sub. It is the unique identifier of the current logged-in user.
          You can use it to create an entry in your web app's local database.
        * Some of `other claims <https://openid.net/specs/openid-connect-core-1_0.html#StandardClaims>`_
        """
        return self._get_user(validators=validators)

    def get_token(self, scopes=None, validators=None, **kwargs):
        if not self._get_user(validators=validators):  # Validate current session first
            return
        cache = self._load_cache()  # This web app maintains one cache per session
        app = self._build_msal_app(
            client_credential=self._client_credential, cache=cache)
        accounts = app.get_accounts()
        if accounts:  # TODO: Consider all account(s) belong to the current logged-in user
            result = app.acquire_token_silent(scopes, account=accounts[0], **kwargs)
            self._save_cache(cache)  # Cache might be refreshed. Save it.
            return result

    def log_out(self, homepage):
        # The vocabulary is "log out" (rather than "sign out") in the specs
        # https://openid.net/specs/openid-connect-frontchannel-1_0.html
        """Logs out the user from current app.

        :param str homepage:
            The page to be redirected to, after the log-out.
            In Flask, you can pass in ``url_for("index", _external=True)``.

        :return:
            An upstream log-out URL. You can optionally guide user to visit it,
            otherwise the user remains logged-in there, and can SSO back to your app.
        """
        self._session.pop(self._USER, None)  # Must
        self._session.pop(self._TOKEN_CACHE, None)  # Optional
        return "{authority}/oauth2/v2.0/logout?post_logout_redirect_uri={hp}".format(
            authority=self._authority, hp=homepage)


class Validator(object):
    def __init__(self, *, on_error=None):
        """This base class of Validator allows to raise customized exception.

        :param Exception on_error:
            It can be an exception class or an instance of an exception.
        """
        self._on_error = on_error

    def is_valid(self, id_token_claims):
        """Sub-class should return True of False"""
        raise NotImplementedError()

    def __call__(self, id_token_claims):
        is_valid = self.is_valid(id_token_claims)
        if self._on_error is not None and not is_valid:
            if isinstance(self._on_error, Exception):
                raise self._on_error
            elif issubclass(self._on_error, Exception):
                raise self._on_error(id_token_claims)
        return is_valid


class LifespanValidator(Validator):
    def __init__(self, *, seconds=None, skew=None, **kwargs):
        """This validator let logged-in session expire after a certain time.

        Without this validator, Identity Web library will keep user logged-in
        until they explicitly log out.

        :param int seconds:
            Specify the lifespan of the current logged-in session.
            The default value is None, which means the logged-in session will
            have same expiry time as the ID token, which is around 1 hour.
        """
        self._seconds = seconds
        self._skew = 210 if skew is None else skew
        super(LifespanValidator, self).__init__(**kwargs)

    def is_valid(self, id_token_claims):
        now = time.time()
        logger.debug("now=%s, iat=%s, skew=%s", now, id_token_claims["iat"], self._skew)
        return now < self._skew + (
            id_token_claims["exp"] if self._seconds is None
            else id_token_claims["iat"] + self._seconds)

