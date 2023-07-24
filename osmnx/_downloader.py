"""Interact with the OSM APIs."""

import datetime as dt
import json
import logging as lg
import socket
import time
from collections import OrderedDict
from hashlib import sha1
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
from requests.exceptions import ConnectionError
from requests.exceptions import JSONDecodeError

from . import projection
from . import settings
from . import utils
from . import utils_geo
from ._errors import InsufficientResponseError
from ._errors import ResponseStatusCodeError

# capture getaddrinfo function to use original later after mutating it
_original_getaddrinfo = socket.getaddrinfo


def _get_osm_filter(network_type):
    """
    Create a filter to query OSM for the specified network type.

    Parameters
    ----------
    network_type : string {"all_private", "all", "bike", "drive", "drive_service", "walk"}
        what type of street network to get

    Returns
    -------
    string
    """
    # define built-in queries to send to the API. specifying way["highway"]
    # means that all ways returned must have a highway tag. the filters then
    # remove ways by tag/value.
    filters = {}

    # driving: filter out un-drivable roads, service roads, private ways, and
    # anything specifying motor=no. also filter out any non-service roads that
    # are tagged as providing certain services
    filters["drive"] = (
        f'["highway"]["area"!~"yes"]{settings.default_access}'
        f'["highway"!~"abandoned|bridleway|bus_guideway|construction|corridor|cycleway|elevator|'
        f"escalator|footway|no|path|pedestrian|planned|platform|proposed|raceway|razed|service|"
        f'steps|track"]'
        f'["motor_vehicle"!~"no"]["motorcar"!~"no"]'
        f'["service"!~"alley|driveway|emergency_access|parking|parking_aisle|private"]'
    )

    # drive+service: allow ways tagged 'service' but filter out certain types
    filters["drive_service"] = (
        f'["highway"]["area"!~"yes"]{settings.default_access}'
        f'["highway"!~"abandoned|bridleway|bus_guideway|construction|corridor|cycleway|elevator|'
        f"escalator|footway|no|path|pedestrian|planned|platform|proposed|raceway|razed|steps|"
        f'track"]'
        f'["motor_vehicle"!~"no"]["motorcar"!~"no"]'
        f'["service"!~"emergency_access|parking|parking_aisle|private"]'
    )

    # walking: filter out cycle ways, motor ways, private ways, and anything
    # specifying foot=no. allow service roads, permitting things like parking
    # lot lanes, alleys, etc that you *can* walk on even if they're not
    # exactly pleasant walks. some cycleways may allow pedestrians, but this
    # filter ignores such cycleways.
    filters["walk"] = (
        f'["highway"]["area"!~"yes"]{settings.default_access}'
        f'["highway"!~"abandoned|bus_guideway|construction|cycleway|motor|no|planned|platform|'
        f'proposed|raceway|razed"]'
        f'["foot"!~"no"]["service"!~"private"]'
    )

    # biking: filter out foot ways, motor ways, private ways, and anything
    # specifying biking=no
    filters["bike"] = (
        f'["highway"]["area"!~"yes"]{settings.default_access}'
        f'["highway"!~"abandoned|bus_guideway|construction|corridor|elevator|escalator|footway|'
        f'motor|no|planned|platform|proposed|raceway|razed|steps"]'
        f'["bicycle"!~"no"]["service"!~"private"]'
    )

    # to download all ways, just filter out everything not currently in use or
    # that is private-access only
    filters["all"] = (
        f'["highway"]["area"!~"yes"]{settings.default_access}'
        f'["highway"!~"abandoned|construction|no|planned|platform|proposed|raceway|razed"]'
        f'["service"!~"private"]'
    )

    # to download all ways, including private-access ones, just filter out
    # everything not currently in use
    filters["all_private"] = (
        '["highway"]["area"!~"yes"]["highway"!~"abandoned|construction|no|planned|platform|'
        'proposed|raceway|razed"]'
    )

    if network_type in filters:
        osm_filter = filters[network_type]
    else:  # pragma: no cover
        msg = f"Unrecognized network_type {network_type!r}"
        raise ValueError(msg)

    return osm_filter


def _save_to_cache(url, response_json, ok):
    """
    Save a HTTP response JSON object to a file in the cache folder.

    Function calculates the checksum of url to generate the cache file's name.
    If the request was sent to server via POST instead of GET, then URL should
    be a GET-style representation of request. Response is only saved to a
    cache file if settings.use_cache is True, response_json is not None, and
    ok is True.

    Users should always pass OrderedDicts instead of dicts of parameters into
    request functions, so the parameters remain in the same order each time,
    producing the same URL string, and thus the same hash. Otherwise the cache
    will eventually contain multiple saved responses for the same request
    because the URL's parameters appeared in a different order each time.

    Parameters
    ----------
    url : string
        the URL of the request
    response_json : dict
        the JSON response
    ok : bool
        requests response.ok value

    Returns
    -------
    None
    """
    if settings.use_cache:
        if not ok:  # pragma: no cover
            utils.log("Did not save to cache because response status_code is not OK")

        elif response_json is None:  # pragma: no cover
            utils.log("Did not save to cache because response_json is None")

        else:
            # create the folder on the disk if it doesn't already exist
            cache_folder = Path(settings.cache_folder)
            cache_folder.mkdir(parents=True, exist_ok=True)

            # hash the url to make the filename succinct but unique
            # sha1 digest is 160 bits = 20 bytes = 40 hexadecimal characters
            filename = sha1(url.encode("utf-8")).hexdigest() + ".json"
            cache_filepath = cache_folder / filename

            # dump to json, and save to file
            cache_filepath.write_text(json.dumps(response_json), encoding="utf-8")
            utils.log(f"Saved response to cache file {str(cache_filepath)!r}")


def _url_in_cache(url):
    """
    Determine if a URL's response exists in the cache.

    Calculates the checksum of url to determine the cache file's name.

    Parameters
    ----------
    url : string
        the URL to look for in the cache

    Returns
    -------
    filepath : pathlib.Path
        path to cached response for url if it exists, otherwise None
    """
    # hash the url to generate the cache filename
    filename = sha1(url.encode("utf-8")).hexdigest() + ".json"
    filepath = Path(settings.cache_folder) / filename

    # if this file exists in the cache, return its full path
    return filepath if filepath.is_file() else None


def _retrieve_from_cache(url, check_remark=True):
    """
    Retrieve a HTTP response JSON object from the cache, if it exists.

    Parameters
    ----------
    url : string
        the URL of the request
    check_remark : string
        if True, only return filepath if cached response does not have a
        remark key indicating a server warning

    Returns
    -------
    response_json : dict
        cached response for url if it exists in the cache, otherwise None
    """
    # if the tool is configured to use the cache
    if settings.use_cache:
        # return cached response for this url if exists, otherwise return None
        cache_filepath = _url_in_cache(url)
        if cache_filepath is not None:
            response_json = json.loads(cache_filepath.read_text(encoding="utf-8"))

            # return None if check_remark is True and there is a server
            # remark in the cached response
            if check_remark and "remark" in response_json:  # pragma: no cover
                utils.log(
                    f"Ignoring cache file {str(cache_filepath)!r} because "
                    f"it contains a remark: {response_json['remark']!r}"
                )
                return None

            utils.log(f"Retrieved response from cache file {str(cache_filepath)!r}")
            return response_json
    return None


def _get_http_headers(user_agent=None, referer=None, accept_language=None):
    """
    Update the default requests HTTP headers with OSMnx info.

    Parameters
    ----------
    user_agent : string
        the user agent string, if None will set with OSMnx default
    referer : string
        the referer string, if None will set with OSMnx default
    accept_language : string
        make accept-language explicit e.g. for consistent nominatim result
        sorting

    Returns
    -------
    headers : dict
    """
    if user_agent is None:
        user_agent = settings.default_user_agent
    if referer is None:
        referer = settings.default_referer
    if accept_language is None:
        accept_language = settings.default_accept_language

    headers = requests.utils.default_headers()
    headers.update(
        {"User-Agent": user_agent, "referer": referer, "Accept-Language": accept_language}
    )
    return headers


def _resolve_host_via_doh(hostname):
    """
    Resolve hostname to IP address via Google's public DNS-over-HTTPS API.

    Necessary fallback as socket.gethostbyname will not always work when using
    a proxy. See https://developers.google.com/speed/public-dns/docs/doh/json
    If the user has set `settings.doh_url_template=None` or if resolution
    fails (e.g., due to local network blocking DNS-over-HTTPS) the hostname
    itself will be returned instead. Note that this means that server slot
    management may be violated: see `_config_dns` documentation for details.

    Parameters
    ----------
    hostname : string
        the hostname to consistently resolve the IP address of

    Returns
    -------
    ip_address : string
        resolved IP address of host, or hostname itself if resolution failed
    """
    if settings.doh_url_template is None:
        # if user has set the url template to None, return hostname itself
        utils.log("User set `doh_url_template=None`, requesting host by name", level=lg.WARNING)
        return hostname

    err_msg = f"Failed to resolve {hostname!r} IP via DoH, requesting host by name"
    try:
        url = settings.doh_url_template.format(hostname=hostname)
        response = requests.get(url, timeout=settings.timeout)
        data = response.json()
        if response.ok and data["Status"] == 0:
            # status 0 means NOERROR, so return the IP address
            return data["Answer"][0]["data"]
        else:  # pragma: no cover
            # if we cannot reach DoH server or cannot resolve host, return hostname itself
            utils.log(err_msg, level=lg.ERROR)
            return hostname

    # if we cannot reach DoH server or cannot resolve host, return hostname itself
    except requests.exceptions.RequestException:  # pragma: no cover
        utils.log(err_msg, level=lg.ERROR)
        return hostname


def _config_dns(url):
    """
    Force socket.getaddrinfo to use IP address instead of hostname.

    Resolves the URL's domain to an IP address so that we use the same server
    for both 1) checking the necessary pause duration and 2) sending the query
    itself even if there is round-robin redirecting among multiple server
    machines on the server-side. Mutates the getaddrinfo function so it uses
    the same IP address everytime it finds the hostname in the URL.

    For example, the server overpass-api.de just redirects to one of the other
    servers (currently gall.openstreetmap.de and lambert.openstreetmap.de). So
    if we check the status endpoint of overpass-api.de, we may see results for
    server gall, but when we submit the query itself it gets redirected to
    server lambert. This could result in violating server lambert's slot
    management timing.

    Parameters
    ----------
    url : string
        the URL to consistently resolve the IP address of

    Returns
    -------
    None
    """
    hostname = _hostname_from_url(url)
    try:
        ip = socket.gethostbyname(hostname)
    except socket.gaierror:  # pragma: no cover
        # may occur when using a proxy, so instead resolve IP address via DoH
        utils.log(
            f"Encountered gaierror while trying to resolve {hostname!r}, trying again via DoH...",
            level=lg.ERROR,
        )
        ip = _resolve_host_via_doh(hostname)

    # mutate socket.getaddrinfo to map hostname -> IP address
    def _getaddrinfo(*args, **kwargs):
        if args[0] == hostname:
            utils.log(f"Resolved {hostname!r} to {ip!r}")
            return _original_getaddrinfo(ip, *args[1:], **kwargs)
        else:
            return _original_getaddrinfo(*args, **kwargs)

    socket.getaddrinfo = _getaddrinfo


def _hostname_from_url(url):
    """
    Extract the hostname (domain) from a URL.

    Parameters
    ----------
    url : string
        the url from which to extract the hostname

    Returns
    -------
    hostname : string
        the extracted hostname (domain)
    """
    return urlparse(url).netloc.split(":")[0]


def _get_pause(base_endpoint, recursive_delay=5, default_duration=60):
    """
    Get a pause duration from the Overpass API status endpoint.

    Check the Overpass API status endpoint to determine how long to wait until
    the next slot is available. You can disable this via the `settings`
    module's `overpass_rate_limit` setting.

    Parameters
    ----------
    base_endpoint : string
        base Overpass API url (without "/status" at the end)
    recursive_delay : int
        how long to wait between recursive calls if the server is currently
        running a query
    default_duration : int
        if fatal error, fall back on returning this value

    Returns
    -------
    pause : int
    """
    if not settings.overpass_rate_limit:
        # if overpass rate limiting is False, then there is zero pause
        return 0

    sc = None

    try:
        url = base_endpoint.rstrip("/") + "/status"
        response = requests.get(
            url, headers=_get_http_headers(), timeout=settings.timeout, **settings.requests_kwargs
        )
        sc = response.status_code
        status = response.text.split("\n")[4]
        status_first_token = status.split(" ")[0]
    except ConnectionError:  # pragma: no cover
        # cannot reach status endpoint, log error and return default duration
        utils.log(f"Unable to query {url}, got status {sc}", level=lg.ERROR)
        return default_duration
    except (AttributeError, IndexError, ValueError):  # pragma: no cover
        # cannot parse output, log error and return default duration
        utils.log(f"Unable to parse {url} response: {response.text}", level=lg.ERROR)
        return default_duration

    try:
        # if first token is numeric, it's how many slots you have available,
        # no wait required
        _ = int(status_first_token)  # number of available slots
        pause = 0

    except ValueError:  # pragma: no cover
        # if first token is 'Slot', it tells you when your slot will be free
        if status_first_token == "Slot":
            utc_time_str = status.split(" ")[3]
            pattern = "%Y-%m-%dT%H:%M:%SZ,"
            utc_time = dt.datetime.strptime(utc_time_str, pattern).astimezone(dt.timezone.utc)
            utc_now = dt.datetime.now(tz=dt.timezone.utc)
            seconds = int(np.ceil((utc_time - utc_now).total_seconds()))
            pause = max(seconds, 1)

        # if first token is 'Currently', it is currently running a query so
        # check back in recursive_delay seconds
        elif status_first_token == "Currently":
            time.sleep(recursive_delay)
            pause = _get_pause(base_endpoint)

        # any other status is unrecognized: log error, return default duration
        else:
            utils.log(f"Unrecognized server status: {status!r}", level=lg.ERROR)
            return default_duration

    return pause


def _make_overpass_settings():
    """
    Make settings string to send in Overpass query.

    Returns
    -------
    string
    """
    if settings.memory is None:
        maxsize = ""
    else:
        maxsize = f"[maxsize:{settings.memory}]"
    return settings.overpass_settings.format(timeout=settings.timeout, maxsize=maxsize)


def _make_overpass_polygon_coord_strs(polygon):
    """
    Subdivide query polygon and return list of coordinate strings.

    Project to utm, divide polygon up into sub-polygons if area exceeds a
    max size (in meters), project back to lat-lng, then get a list of
    polygon(s) exterior coordinates

    Parameters
    ----------
    polygon : shapely.geometry.Polygon or shapely.geometry.MultiPolygon

    Returns
    -------
    polygon_coord_strs : list
        list of exterior coordinate strings for smaller sub-divided polygons
    """
    geometry_proj, crs_proj = projection.project_geometry(polygon)
    gpcs = utils_geo._consolidate_subdivide_geometry(geometry_proj)
    geometry, _ = projection.project_geometry(gpcs, crs=crs_proj, to_latlong=True)
    return utils_geo._get_polygons_coordinates(geometry)


def _create_overpass_query(polygon_coord_str, tags):
    """
    Create an overpass query string based on passed tags.

    Parameters
    ----------
    polygon_coord_str : list
        list of lat lng coordinates
    tags : dict
        dict of tags used for finding elements in the selected area

    Returns
    -------
    query : string
    """
    # create overpass settings string
    overpass_settings = _make_overpass_settings()

    # make sure every value in dict is bool, str, or list of str
    err_msg = "tags must be a dict with values of bool, str, or list of str"
    if not isinstance(tags, dict):  # pragma: no cover
        raise TypeError(err_msg)

    tags_dict = {}
    for key, value in tags.items():
        if isinstance(value, bool):
            tags_dict[key] = value

        elif isinstance(value, str):
            tags_dict[key] = [value]

        elif isinstance(value, list):
            if not all(isinstance(s, str) for s in value):  # pragma: no cover
                raise TypeError(err_msg)
            tags_dict[key] = value

        else:  # pragma: no cover
            raise TypeError(err_msg)

    # convert the tags dict into a list of {tag:value} dicts
    tags_list = []
    for key, value in tags_dict.items():
        if isinstance(value, bool):
            tags_list.append({key: value})
        else:
            for value_item in value:
                tags_list.append({key: value_item})

    # add node/way/relation query components one at a time
    components = []
    for d in tags_list:
        for key, value in d.items():
            if isinstance(value, bool):
                # if bool (ie, True) just pass the key, no value
                tag_str = f"[{key!r}](poly:{polygon_coord_str!r});(._;>;);"
            else:
                # otherwise, pass "key"="value"
                tag_str = f"[{key!r}={value!r}](poly:{polygon_coord_str!r});(._;>;);"

            for kind in ("node", "way", "relation"):
                components.append(f"({kind}{tag_str});")

    # finalize query and return
    components = "".join(components)
    return f"{overpass_settings};({components});out;"


def _osm_network_download(polygon, network_type, custom_filter):
    """
    Retrieve networked ways and nodes within boundary from the Overpass API.

    Parameters
    ----------
    polygon : shapely.geometry.Polygon or shapely.geometry.MultiPolygon
        boundary to fetch the network ways/nodes within
    network_type : string
        what type of street network to get if custom_filter is None
    custom_filter : string
        a custom ways filter to be used instead of the network_type presets

    Yields
    ------
    response_json : dict
        a generator of JSON responses from the Overpass server
    """
    # create a filter to exclude certain kinds of ways based on the requested
    # network_type, if provided, otherwise use custom_filter
    if custom_filter is not None:
        osm_filter = custom_filter
    else:
        osm_filter = _get_osm_filter(network_type)

    # create overpass settings string
    overpass_settings = _make_overpass_settings()

    # subdivide query polygon to get list of sub-divided polygon coord strings
    polygon_coord_strs = _make_overpass_polygon_coord_strs(polygon)
    utils.log(f"Requesting data from API in {len(polygon_coord_strs)} request(s)")

    # pass each polygon exterior coordinates in the list to the API, one at a
    # time. The '>' makes it recurse so we get ways and the ways' nodes.
    for polygon_coord_str in polygon_coord_strs:
        query_str = f"{overpass_settings};(way{osm_filter}(poly:{polygon_coord_str!r});>;);out;"
        yield _overpass_request(data={"data": query_str})


def _osm_features_download(polygon, tags):
    """
    Retrieve OSM features within boundary from the Overpass API.

    Parameters
    ----------
    polygon : shapely.geometry.Polygon
        boundaries to fetch elements within
    tags : dict
        dict of tags used for finding elements in the selected area

    Returns
    -------
    response_jsons : list
        list of JSON responses from the Overpass server
    """
    response_jsons = []

    # subdivide query polygon to get list of sub-divided polygon coord strings
    polygon_coord_strs = _make_overpass_polygon_coord_strs(polygon)
    utils.log(f"Requesting data from API in {len(polygon_coord_strs)} request(s)")

    # pass exterior coordinates of each polygon in list to API, one at a time
    for polygon_coord_str in polygon_coord_strs:
        query_str = _create_overpass_query(polygon_coord_str, tags)
        response_json = _overpass_request(data={"data": query_str})
        response_jsons.append(response_json)

    utils.log(
        f"Got all features data within polygon from API in {len(polygon_coord_strs)} request(s)"
    )

    return response_jsons


def _retrieve_osm_element(query, by_osmid=False, limit=1, polygon_geojson=1):
    """
    Retrieve an OSM element from the Nominatim API.

    Parameters
    ----------
    query : string or dict
        query string or structured query dict
    by_osmid : bool
        if True, treat query as an OSM ID lookup rather than text search
    limit : int
        max number of results to return
    polygon_geojson : int
        retrieve the place's geometry from the API, 0=no, 1=yes

    Returns
    -------
    response_json : dict
        JSON response from the Nominatim server
    """
    # define the parameters
    params = OrderedDict()
    params["format"] = "json"
    params["polygon_geojson"] = polygon_geojson

    if by_osmid:
        # if querying by OSM ID, use the lookup endpoint
        request_type = "lookup"
        params["osm_ids"] = query

    else:
        # if not querying by OSM ID, use the search endpoint
        request_type = "search"

        # prevent OSM from deduping so we get precise number of results
        params["dedupe"] = 0
        params["limit"] = limit

        if isinstance(query, str):
            params["q"] = query
        elif isinstance(query, dict):
            # add query keys in alphabetical order so URL is the same string
            # each time, for caching purposes
            for key in sorted(query):
                params[key] = query[key]
        else:  # pragma: no cover
            msg = "query must be a dict or a string"
            raise TypeError(msg)

    # request the URL, return the JSON
    return _nominatim_request(params=params, request_type=request_type)


def _parse_response(response):
    """
    Parse JSON from a requests response and log the details.

    Parameters
    ----------
    response : requests.response
        the response object

    Returns
    -------
    response_json : dict
    """
    # log the response size and domain
    domain = _hostname_from_url(response.url)
    size_kb = len(response.content) / 1000
    utils.log(f"Downloaded {size_kb:,.1f}kB from {domain!r} with code {response.status_code}")

    # parse the response to JSON and log/raise exceptions
    try:
        response_json = response.json()
    except JSONDecodeError as e:  # pragma: no cover
        msg = f"{domain!r} responded: {response.status_code} {response.reason} {response.text}"
        utils.log(msg, level=lg.ERROR)
        if response.ok:
            raise InsufficientResponseError(msg) from e
        raise ResponseStatusCodeError(msg) from e

    # log any remarks if they exist
    if "remark" in response_json:  # pragma: no cover
        utils.log(f'{domain!r} remarked: {response_json["remark"]!r}', level=lg.WARNING)

    return response_json


def _nominatim_request(params, request_type="search", pause=1, error_pause=60):
    """
    Send a HTTP GET request to the Nominatim API and return response.

    Parameters
    ----------
    params : OrderedDict
        key-value pairs of parameters
    request_type : string {"search", "reverse", "lookup"}
        which Nominatim API endpoint to query
    pause : float
        how long to pause before request, in seconds. per the nominatim usage
        policy: "an absolute maximum of 1 request per second" is allowed
    error_pause : float
        how long to pause in seconds before re-trying request if error

    Returns
    -------
    response_json : dict
    """
    if request_type not in {"search", "reverse", "lookup"}:  # pragma: no cover
        msg = 'Nominatim request_type must be "search", "reverse", or "lookup"'
        raise ValueError(msg)

    # prepare Nominatim API URL and see if request already exists in cache
    url = settings.nominatim_endpoint.rstrip("/") + "/" + request_type
    params["key"] = settings.nominatim_key
    prepared_url = requests.Request("GET", url, params=params).prepare().url
    cached_response_json = _retrieve_from_cache(prepared_url)
    if cached_response_json is not None:
        return cached_response_json

    # pause then request this URL
    domain = _hostname_from_url(url)
    utils.log(f"Pausing {pause} second(s) before making HTTP GET request to {domain!r}")
    time.sleep(pause)

    # transmit the HTTP GET request
    utils.log(f"Get {prepared_url} with timeout={settings.timeout}")
    response = requests.get(
        url,
        params=params,
        timeout=settings.timeout,
        headers=_get_http_headers(),
        **settings.requests_kwargs,
    )

    # handle 429 and 504 errors by pausing then recursively re-trying request
    if response.status_code in {429, 504}:  # pragma: no cover
        msg = (
            f"{domain!r} responded {response.status_code} {response.reason}: "
            f"we'll retry in {error_pause} secs"
        )
        utils.log(msg, level=lg.WARNING)
        time.sleep(error_pause)
        return _nominatim_request(params, request_type, pause, error_pause)

    response_json = _parse_response(response)
    _save_to_cache(prepared_url, response_json, response.status_code)
    return response_json


def _overpass_request(data, pause=None, error_pause=60):
    """
    Send a HTTP POST request to the Overpass API and return response.

    Parameters
    ----------
    data : OrderedDict
        key-value pairs of parameters
    pause : float
        how long to pause in seconds before request, if None, will query API
        status endpoint to find when next slot is available
    error_pause : float
        how long to pause in seconds (in addition to `pause`) before re-trying
        request if error

    Returns
    -------
    response_json : dict
    """
    # resolve url to same IP even if there is server round-robin redirecting
    _config_dns(settings.overpass_endpoint)

    # prepare the Overpass API URL and see if request already exists in cache
    url = settings.overpass_endpoint.rstrip("/") + "/interpreter"
    prepared_url = requests.Request("GET", url, params=data).prepare().url
    cached_response_json = _retrieve_from_cache(prepared_url)
    if cached_response_json is not None:
        return cached_response_json

    # pause then request this URL
    if pause is None:
        this_pause = _get_pause(settings.overpass_endpoint)
    domain = _hostname_from_url(url)
    utils.log(f"Pausing {this_pause} second(s) before making HTTP POST request to {domain!r}")
    time.sleep(this_pause)

    # transmit the HTTP POST request
    utils.log(f"Post {prepared_url} with timeout={settings.timeout}")
    response = requests.post(
        url,
        data=data,
        timeout=settings.timeout,
        headers=_get_http_headers(),
        **settings.requests_kwargs,
    )

    # handle 429 and 504 errors by pausing then recursively re-trying request
    if response.status_code in {429, 504}:  # pragma: no cover
        this_pause = error_pause + _get_pause(settings.overpass_endpoint)
        msg = (
            f"{domain!r} responded {response.status_code} {response.reason}: "
            f"we'll retry in {this_pause} secs"
        )
        utils.log(msg, level=lg.WARNING)
        time.sleep(this_pause)
        return _overpass_request(data, pause, error_pause)

    response_json = _parse_response(response)
    _save_to_cache(prepared_url, response_json, response.status_code)
    return response_json


def _google_request(url, pause):
    """
    Send a HTTP GET request to Google Maps Elevation API and return response.

    Parameters
    ----------
    url : string
        URL for API endpoint populated with request data
    pause : float
        how long to pause in seconds before request

    Returns
    -------
    response_json : dict
    """
    # check if request already exists in cache
    cached_response_json = _retrieve_from_cache(url)
    if cached_response_json is not None:
        return cached_response_json

    # pause then request this URL
    domain = _hostname_from_url(url)
    utils.log(f"Pausing {pause} second(s) before making HTTP GET request to {domain!r}")
    time.sleep(pause)

    # transmit the HTTP GET request
    utils.log(f"Get {url} with timeout={settings.timeout}")
    response = requests.get(
        url, timeout=settings.timeout, headers=_get_http_headers(), **settings.requests_kwargs
    )

    response_json = _parse_response(response)
    _save_to_cache(url, response_json, response.status_code)
    return response_json
