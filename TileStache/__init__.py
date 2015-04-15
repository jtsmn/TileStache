""" A stylish alternative for caching your map tiles.

TileStache is a Python-based server application that can serve up map tiles
based on rendered geographic data. You might be familiar with TileCache
(http://tilecache.org), the venerable open source WMS server from MetaCarta.
TileStache is similar, but we hope simpler and better-suited to the needs of
designers and cartographers.

Documentation available at http://tilestache.org/doc/
"""
import os.path

__version__ = open(os.path.join(os.path.dirname(__file__), 'VERSION')).read().strip()

import re

from sys import stdout
try:
    from urlparse import parse_qs
except ImportError:
    from cgi import parse_qs
from StringIO import StringIO
from os.path import dirname, join as pathjoin, realpath
from datetime import datetime, timedelta
from urlparse import urljoin, urlparse
from wsgiref.headers import Headers
from urllib import urlopen
from os import getcwd
from time import time

import httplib
import logging

try:
    from json import load as json_load
except ImportError:
    from simplejson import load as json_load

from ModestMaps.Core import Coordinate

# dictionary of configuration objects for requestLayer().
_previous_configs = {}

import Core
import Config

# regular expression for PATH_INFO
_pathinfo_pat = re.compile(r'^/?(?P<l>\w.+)/(?P<z>\d+)/(?P<x>-?\d+)/(?P<y>-?\d+)\.(?P<e>\w+)$')
_preview_pat = re.compile(r'^/?(?P<l>\w.+)/(preview\.html)?$')

# symbol used to separate layers when specifying more than one layer
_delimiter = ','

def getTile(layer, coord, extension, ignore_cached=False, suppress_cache_write=False):
    ''' Get a type string and tile binary for a given request layer tile.
    
        This function is documented as part of TileStache's public API:
            http://tilestache.org/doc/#tilestache-gettile
    
        Arguments:
        - layer: instance of Core.Layer to render.
        - coord: one ModestMaps.Core.Coordinate corresponding to a single tile.
        - extension: filename extension to choose response type, e.g. "png" or "jpg".
        - ignore_cached: always re-render the tile, whether it's in the cache or not.
        - suppress_cache_write: don't save the tile to the cache
    
        This is the main entry point, after site configuration has been loaded
        and individual tiles need to be rendered.
    '''
    status_code, headers, body = layer.getTileResponse(coord, extension, TRUE if query.get("ignore_cached") else False, suppress_cache_write)
    mime = headers.get('Content-Type')

    return mime, body

def unknownLayerMessage(config, unknown_layername):
    """ A message that notifies that the given layer is unknown and lists out the known layers. 
    """
    return '"%s" is not a layer I know about. \nHere are some that I do know about: \n %s.' % (unknown_layername, '\n '.join(sorted(config.layers.keys())))

def getPreview(layer):
    """ Get a type string and dynamic map viewer HTML for a given layer.
    """
    return 200, Headers([('Content-Type', 'text/html')]), Core._preview(layer)

def parseConfigfile(configpath):
    """ Parse a configuration file and return a Configuration object.
    
        Configuration file is formatted as JSON with two sections, "cache" and "layers":
        
          {
            "cache": { ... },
            "layers": {
              "layer-1": { ... },
              "layer-2": { ... },
              ...
            }
          }
        
        The full path to the file is significant, used to
        resolve any relative paths found in the configuration.
        
        See the Caches module for more information on the "caches" section,
        and the Core and Providers modules for more information on the
        "layers" section.
    """
    config_dict = json_load(urlopen(configpath))
    
    scheme, host, path, p, q, f = urlparse(configpath)
    
    if scheme == '':
        scheme = 'file'
        path = realpath(path)
    
    dirpath = '%s://%s%s' % (scheme, host, dirname(path).rstrip('/') + '/')

    return Config.buildConfiguration(config_dict, dirpath)

def splitPathInfo(pathinfo):
    """ Converts a PATH_INFO string to layer name, coordinate, and extension parts.
        
        Example: "/layer/0/0/0.png", leading "/" optional.
    """
    if pathinfo == '/':
        return None, None, None
    
    if _pathinfo_pat.match(pathinfo or ''):
        path = _pathinfo_pat.match(pathinfo)
        layer, row, column, zoom, extension = [path.group(p) for p in 'lyxze']
        coord = Coordinate(int(row), int(column), int(zoom))

    elif _preview_pat.match(pathinfo or ''):
        path = _preview_pat.match(pathinfo)
        layer, extension = path.group('l'), 'html'
        coord = None

    else:
        raise Core.KnownUnknown('Bad path: "%s". I was expecting something more like "/example/0/0/0.png"' % pathinfo)

    return layer, coord, extension

def mergePathInfo(layer, coord, extension):
    """ Converts layer name, coordinate and extension back to a PATH_INFO string.
    
        See also splitPathInfo().
    """
    z = coord.zoom
    x = coord.column
    y = coord.row
    
    return '/%(layer)s/%(z)d/%(x)d/%(y)d.%(extension)s' % locals()

def isValidLayer(layer, config):
    if not layer:
        return False
    if (layer not in config.layers):
        if (layer.find(_delimiter) != -1):
            multi_providers = list(ll for ll in config.layers if hasattr(config.layers[ll].provider, 'names'))
            for l in layer.split(_delimiter):
                if ((l not in config.layers) or (l in multi_providers)):
                    return False
            return True
        return False
    return True

def requestLayer(config, path_info):
    """ Return a Layer.
    
        Requires a configuration and PATH_INFO (e.g. "/example/0/0/0.png").
        
        Config parameter can be a file path string for a JSON configuration file
        or a configuration object with 'cache', 'layers', and 'dirpath' properties.
    """
    if type(config) in (str, unicode):
        #
        # Should be a path to a configuration file we can load;
        # build a tuple key into previously-seen config objects.
        #
        key = hasattr(config, '__hash__') and (config, getcwd())
        
        if key in _previous_configs:
            config = _previous_configs[key]
        
        else:
            config = parseConfigfile(config)
            
            if key:
                _previous_configs[key] = config
    
    else:
        assert hasattr(config, 'cache'), 'Configuration object must have a cache.'
        assert hasattr(config, 'layers'), 'Configuration object must have layers.'
        assert hasattr(config, 'dirpath'), 'Configuration object must have a dirpath.'
    
    # ensure that path_info is at least a single "/"
    path_info = '/' + (path_info or '').lstrip('/')
    
    if path_info == '/':
        return Core.Layer(config, None, None)

    layername = splitPathInfo(path_info)[0]
    
    if not isValidLayer(layername, config):
        raise Core.KnownUnknown(unknownLayerMessage(config, layername))
    
    custom_layer = layername.find(_delimiter)!=-1

    if custom_layer:
        # we can't just assign references, because we get identity problems
        # when tilestache tries to look up the layer's name, which won't match
        # the list of names in the provider
        provider_names = layername.split(_delimiter)
        custom_layer_obj = config.layers[config.custom_layer_name]
        config.layers[layername] = clone_layer(custom_layer_obj, provider_names)

    return config.layers[layername]


def clone_layer(layer, provider_names):
    from TileStache.Core import Layer
    copy = Layer(
        layer.config,
        layer.projection,
        layer.metatile,
        layer.stale_lock_timeout,
        layer.cache_lifespan,
        layer.write_cache,
        layer.allowed_origin,
        layer.max_cache_age,
        layer.redirects,
        layer.preview_lat,
        layer.preview_lon,
        layer.preview_zoom,
        layer.preview_ext,
        layer.bounds,
        layer.dim,
        )
    copy.provider = layer.provider
    copy.provider(copy, provider_names)
    return copy


def requestHandler(config_hint, path_info, query_string=None):
    """ Generate a mime-type and response body for a given request.
    
        This function is documented as part of TileStache's public API:
            http://tilestache.org/doc/#tilestache-requesthandler

        TODO: replace this with requestHandler2() in TileStache 2.0.0.
        
        Calls requestHandler2().
    """
    status_code, headers, content = requestHandler2(config_hint, path_info, query_string)
    mimetype = headers.get('Content-Type')
    
    return mimetype, content

def requestHandler2(config_hint, path_info, query_string=None, script_name=''):
    """ Generate a set of headers and response body for a given request.
    
        TODO: Replace requestHandler() with this function in TileStache 2.0.0.
        
        Requires a configuration and PATH_INFO (e.g. "/example/0/0/0.png").
        
        Config_hint parameter can be a path string for a JSON configuration file
        or a configuration object with 'cache', 'layers', and 'dirpath' properties.
        
        Query string is optional, currently used for JSON callbacks.
        
        Calls Layer.getTileResponse() to render actual tiles, and getPreview() to render preview.html.
    """
    headers = Headers([])
    
    try:
        # ensure that path_info is at least a single "/"
        path_info = '/' + (path_info or '').lstrip('/')
        
        layer = requestLayer(config_hint, path_info)
        query = parse_qs(query_string or '')
        try:
            callback = query['callback'][0]
        except KeyError:
            callback = None
        
        #
        # Special case for index page.
        #
        if path_info == '/':
            mimetype, content = getattr(layer.config, 'index', ('text/plain', 'TileStache says hello.'))
            return 200, Headers([('Content-Type', mimetype)]), content

        coord, extension = splitPathInfo(path_info)[1:]
        
        if extension == 'html' and coord is None:
            status_code, headers, content = getPreview(layer)

        elif extension.lower() in layer.redirects:
            other_extension = layer.redirects[extension.lower()]
            
            redirect_uri = script_name
            redirect_uri += mergePathInfo(layer.name(), coord, other_extension)
            
            if query_string:
                redirect_uri += '?' + query_string
            
            headers['Location'] = redirect_uri
            headers['Content-Type'] = 'text/plain'
            
            return 302, headers, 'You are being redirected to %s\n' % redirect_uri
        
        else:
            status_code, headers, content = layer.getTileResponse(coord, extension)

        if layer.allowed_origin:
            headers.setdefault('Access-Control-Allow-Origin', layer.allowed_origin)

        if callback and 'json' in headers['Content-Type']:
            headers['Content-Type'] = 'application/javascript; charset=utf-8'
            content = '%s(%s)' % (callback, content)
        
        if layer.max_cache_age is not None:
            expires = datetime.utcnow() + timedelta(seconds=layer.max_cache_age)
            headers.setdefault('Expires', expires.strftime('%a %d %b %Y %H:%M:%S GMT'))
            headers.setdefault('Cache-Control', 'public, max-age=%d' % layer.max_cache_age)

    except (Core.KnownUnknown, Exception), e:
        logging.exception(e)
        out = StringIO()
        
        print >> out, 'Known unknown!' if isinstance(e,Core.KnownUnknown) else 'Exception!'
        print >> out, e
        print >> out, ''
        print >> out, '\n'.join(Core._rummy())
        
        headers['Content-Type'] = 'text/plain'
        status_code, content = 500, out.getvalue()

    return status_code, headers, content

def cgiHandler(environ, config='./tilestache.cfg', debug=False):
    """ Read environment PATH_INFO, load up configuration, talk to stdout by CGI.
    
        This function is documented as part of TileStache's public API:
            http://tilestache.org/doc/#cgi
    
        Calls requestHandler().
        
        Config parameter can be a file path string for a JSON configuration file
        or a configuration object with 'cache', 'layers', and 'dirpath' properties.
    """
    if debug:
        import cgitb
        cgitb.enable()
    
    path_info = environ.get('PATH_INFO', None)
    query_string = environ.get('QUERY_STRING', None)
    script_name = environ.get('SCRIPT_NAME', None)
    
    status_code, headers, content = requestHandler2(config, path_info, query_string, script_name)
    
    headers.setdefault('Content-Length', str(len(content)))

    # output the status code as a header
    stdout.write('Status: %d\n' % status_code)

    # output gathered headers
    for k, v in headers.items():
        stdout.write('%s: %s\n' % (k, v))

    stdout.write('\n')
    stdout.write(content)

class WSGITileServer:
    """ Create a WSGI application that can handle requests from any server that talks WSGI.
    
        This class is documented as part of TileStache's public API:
            http://tilestache.org/doc/#wsgi

        The WSGI application is an instance of this class. Example:

          app = WSGITileServer('/path/to/tilestache.cfg')
          werkzeug.serving.run_simple('localhost', 8080, app)
    """

    def __init__(self, config, autoreload=False):
        """ Initialize a callable WSGI instance.

            Config parameter can be a file path string for a JSON configuration
            file or a configuration object with 'cache', 'layers', and
            'dirpath' properties.
            
            Optional autoreload boolean parameter causes config to be re-read
            on each request, applicable only when config is a JSON file.
        """

        if type(config) in (str, unicode):
            self.autoreload = autoreload
            self.config_path = config
    
            try:
                self.config = parseConfigfile(config)
            except:
                print "Error loading Tilestache config:"
                raise

        else:
            assert hasattr(config, 'cache'), 'Configuration object must have a cache.'
            assert hasattr(config, 'layers'), 'Configuration object must have layers.'
            assert hasattr(config, 'dirpath'), 'Configuration object must have a dirpath.'
            
            self.autoreload = False
            self.config_path = None
            self.config = config

    def __call__(self, environ, start_response):
        """
        """
        if self.autoreload: # re-parse the config file on every request
            try:
                self.config = parseConfigfile(self.config_path)
            except Exception, e:
                raise Core.KnownUnknown("Error loading Tilestache config file:\n%s" % str(e))

        try:
            layer, coord, ext = splitPathInfo(environ['PATH_INFO'])
        except Core.KnownUnknown, e:
            return self._response(start_response, 400, str(e))

        #
        # WSGI behavior is different from CGI behavior, because we may not want
        # to return a chatty rummy for likely-deployed WSGI vs. testing CGI.
        #
        if not isValidLayer(layer, self.config):
            return self._response(start_response, 404, str(unknownLayerMessage(self.config, layer)))

        path_info = environ.get('PATH_INFO', None)
        query_string = environ.get('QUERY_STRING', None)
        script_name = environ.get('SCRIPT_NAME', None)
        
        status_code, headers, content = requestHandler2(self.config, path_info, query_string, script_name)
        
        return self._response(start_response, status_code, str(content), headers)

    def _response(self, start_response, code, content='', headers=None):
        """
        """
        headers = headers or Headers([])

        if content:
            headers.setdefault('Content-Length', str(len(content)))
        
        start_response('%d %s' % (code, httplib.responses[code]), headers.items())
        return [content]

def modpythonHandler(request):
    """ Handle a mod_python request.
    
        TODO: Upgrade to new requestHandler() so this can return non-200 HTTP.
    
        Calls requestHandler().
    
        Example Apache configuration for TileStache:

        <Directory /home/migurski/public_html/TileStache>
            AddHandler mod_python .py
            PythonHandler TileStache::modpythonHandler
            PythonOption config /etc/tilestache.cfg
        </Directory>
        
        Configuration options, using PythonOption directive:
        - config: path to configuration file, defaults to "tilestache.cfg",
            using request.filename as the current working directory.
    """
    from mod_python import apache
    
    config_path = request.get_options().get('config', 'tilestache.cfg')
    config_path = realpath(pathjoin(dirname(request.filename), config_path))
    
    path_info = request.path_info
    query_string = request.args
    
    mimetype, content = requestHandler(config_path, path_info, query_string)

    request.status = apache.HTTP_OK
    request.content_type = mimetype
    request.set_content_length(len(content))
    request.send_http_header()

    request.write(content)

    return apache.OK
