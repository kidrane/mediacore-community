# This file is a part of MediaCore CE, Copyright 2009-2012 MediaCore Inc.
# The source code contained in this file is licensed under the GPL.
# See LICENSE.txt in the main project directory, for more information.

"""Pylons middleware initialization"""
import os

from beaker.middleware import SessionMiddleware
from genshi.template import loader
from genshi.template.plugin import MarkupTemplateEnginePlugin
from paste import gzipper
from paste.cascade import Cascade
from paste.registry import RegistryManager
from paste.response import header_value, remove_header
from paste.urlmap import URLMap
from paste.urlparser import StaticURLParser
from paste.deploy.converters import asbool
from paste.deploy.config import PrefixMiddleware
from pylons.middleware import ErrorHandler, StatusCodeRedirect
from pylons.wsgiapp import PylonsApp as _PylonsApp
from routes.middleware import RoutesMiddleware
from tw.core.view import EngineManager
import tw.api

from mediacore import monkeypatch_method
from mediacore.config.environment import load_environment
from mediacore.lib.auth import add_auth
from mediacore.model.meta import DBSession

class PylonsApp(_PylonsApp):
    """
    Subclass PylonsApp to set our settings on the request.

    The settings are cached in ``request.settings`` but it's best to
    check the cache once, then make them accessible as a simple dict for
    the remainder of the request, instead of hitting the cache repeatedly.

    """
    def register_globals(self, environ):
        _PylonsApp.register_globals(self, environ)
        request = environ['pylons.pylons'].request

        if environ['PATH_INFO'] == '/_test_vars':
            # This is a dummy request, probably used inside a test or to build
            # documentation, so we're not guaranteed to have a database
            # connection with which to get the settings.
            request.settings = {
                'intentionally_empty': 'see mediacore.config.middleware',
            }
        else:
            request.settings = self.globals.settings

def setup_prefix_middleware(app, global_conf, proxy_prefix):
    """Add prefix middleware.

    Essentially replaces request.environ[SCRIPT_NAME] with the prefix defined
    in the .ini file.

    See: http://wiki.pylonshq.com/display/pylonsdocs/Configuration+Files#prefixmiddleware
    """
    app = PrefixMiddleware(app, global_conf, proxy_prefix)
    return app

class DBSessionRemoverMiddleware(object):
    """Ensure the contextual session ends at the end of the request."""
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        try:
            return self.app(environ, start_response)
        finally:
            DBSession.remove()

class FastCGIScriptStripperMiddleware(object):
    """Strip the given fcgi_script_name from the end of environ['SCRIPT_NAME'].

    Useful for the default FastCGI deployment, where mod_rewrite is used to
    avoid having to put the .fcgi file name into the URL.
    """
    def __init__(self, app, fcgi_script_name='/mediacore.fcgi'):
        self.app = app
        self.fcgi_script_name = fcgi_script_name
        self.cut = len(fcgi_script_name)

    def __call__(self, environ, start_response):
        script_name = environ.get('SCRIPT_NAME', '')
        if script_name.endswith(self.fcgi_script_name):
            environ['SCRIPT_NAME'] = script_name[:-self.cut]
        return self.app(environ, start_response)

def setup_tw_middleware(app, config):
    def filename_suffix_adder(inner_loader, suffix):
        def _add_suffix(filename):
            return inner_loader(filename + suffix)
        return _add_suffix

    # Ensure that the toscawidgets template loader includes the search paths
    # from our main template loader.
    tw_engines = EngineManager(extra_vars_func=None)
    tw_engines['genshi'] = MarkupTemplateEnginePlugin()
    tw_engines['genshi'].loader = config['pylons.app_globals'].genshi_loader

    # Disable the built-in package name template resolution.
    tw_engines['genshi'].use_package_naming = False

    # Rebuild package name template resolution using mostly standard Genshi
    # load functions. With our customizations to the TemplateLoader, the
    # absolute paths that the builtin resolution produces are erroneously
    # treated as being relative to the search path.

    # Search the tw templates dir using the pkg_resources API.
    # Expected input: 'input_field.html'
    tw_loader = loader.package('tw.forms', 'templates')

    # Include the .html extension automatically.
    # Expected input: 'input_field'
    tw_loader = filename_suffix_adder(tw_loader, '.html')

    # Apply this loader only when the filename starts with tw.forms.templates.
    # This prefix is stripped off when calling the above loader.
    # Expected input: 'tw.forms.templates.input_field'
    tw_loader = loader.prefixed(**{'tw.forms.templates.': tw_loader})

    # Add this path to our global loader
    tw_engines['genshi'].loader.search_path.append(tw_loader)

    app = tw.api.make_middleware(app, {
        'toscawidgets.framework': 'pylons',
        'toscawidgets.framework.default_view': 'genshi',
        'toscawidgets.framework.engines': tw_engines,
    })
    return app

def setup_gzip_middleware(app, global_conf):
    """Make paste.gzipper middleware with a monkeypatch to exempt SWFs.

    Gzipping .swf files (application/x-shockwave-flash) provides no
    extra compression and it also breaks Flowplayer 3.2.3, and
    potentially others.

    """
    @monkeypatch_method(gzipper.GzipResponse)
    def gzip_start_response(self, status, headers, exc_info=None):
        self.headers = headers
        ct = header_value(headers, 'content-type')
        ce = header_value(headers, 'content-encoding')
        self.compressible = False
        # This statement is the only change in this monkeypatch:
        if ct and (ct.startswith('text/') or ct.startswith('application/')) \
            and 'zip' not in ct and ct != 'application/x-shockwave-flash':
            self.compressible = True
        if ce:
            self.compressible = False
        if self.compressible:
            headers.append(('content-encoding', 'gzip'))
        remove_header(headers, 'content-length')
        self.headers = headers
        self.status = status
        return self.buffer.write
    return gzipper.make_gzip_middleware(app, global_conf)

def make_app(global_conf, full_stack=True, static_files=True, **app_conf):
    """Create a Pylons WSGI application and return it

    ``global_conf``
        The inherited configuration for this application. Normally from
        the [DEFAULT] section of the Paste ini file.

    ``full_stack``
        Whether this application provides a full WSGI stack (by default,
        meaning it handles its own exceptions and errors). Disable
        full_stack when this application is "managed" by another WSGI
        middleware.

    ``static_files``
        Whether this application serves its own static files; disable
        when another web server is responsible for serving them.

    ``app_conf``
        The application's local configuration. Normally specified in
        the [app:<name>] section of the Paste ini file (where <name>
        defaults to main).

    """
    # Configure the Pylons environment
    config = load_environment(global_conf, app_conf)
    plugin_mgr = config['pylons.app_globals'].plugin_mgr

    # The Pylons WSGI app
    app = PylonsApp(config=config)

    # Allow the plugin manager to tweak our WSGI app
    app = plugin_mgr.wrap_pylons_app(app)

    # Routing/Session/Cache Middleware
    app = RoutesMiddleware(app, config['routes.map'], singleton=False)
    app = SessionMiddleware(app, config)

    # CUSTOM MIDDLEWARE HERE (filtered by error handling middlewares)

    # Set up repoze.what authentication:
    # http://wiki.pylonshq.com/display/pylonscookbook/Authorization+with+repoze.what
    app = add_auth(app, config)

    # ToscaWidgets Middleware
    app = setup_tw_middleware(app, config)

    # Strip the name of the .fcgi script, if using one, from the SCRIPT_NAME
    app = FastCGIScriptStripperMiddleware(app)

    # If enabled, set up the proxy prefix for routing behind
    # fastcgi and mod_proxy based deployments.
    if config.get('proxy_prefix', None):
        app = setup_prefix_middleware(app, global_conf, config['proxy_prefix'])

    # END CUSTOM MIDDLEWARE

    if asbool(full_stack):
        # Handle Python exceptions
        app = ErrorHandler(app, global_conf, **config['pylons.errorware'])

        # Display error documents for 401, 403, 404 status codes (and
        # 500 when debug is disabled)
        if asbool(config['debug']):
            app = StatusCodeRedirect(app)
        else:
            app = StatusCodeRedirect(app, [400, 401, 403, 404, 500])

    # Cleanup the DBSession only after errors are handled
    app = DBSessionRemoverMiddleware(app)

    # Establish the Registry for this application
    app = RegistryManager(app)

    if asbool(static_files):
        # Serve static files from our public directory
        public_app = StaticURLParser(config['pylons.paths']['static_files'])

        static_urlmap = URLMap()
        # Serve static files from all plugins
        for dir, path in plugin_mgr.public_paths().iteritems():
            static_urlmap[dir] = StaticURLParser(path)

        # Serve static media and podcast images from outside our public directory
        for image_type in ('media', 'podcasts'):
            dir = '/images/' + image_type
            path = os.path.join(config['image_dir'], image_type)
            static_urlmap[dir] = StaticURLParser(path)

        # Serve appearance directory outside of public as well
        dir = '/appearance'
        path = os.path.join(config['app_conf']['cache_dir'], 'appearance')
        static_urlmap[dir] = StaticURLParser(path)

        # We want to serve goog closure code for debugging uncompiled js.
        if config['debug']:
            goog_path = os.path.join(config['pylons.paths']['root'], '..',
                'closure-library', 'closure', 'goog')
            if os.path.exists(goog_path):
                static_urlmap['/scripts/goog'] = StaticURLParser(goog_path)

        app = Cascade([public_app, static_urlmap, app])

    if asbool(config.get('enable_gzip', 'true')):
        app = setup_gzip_middleware(app, global_conf)

    app.config = config
    return app
