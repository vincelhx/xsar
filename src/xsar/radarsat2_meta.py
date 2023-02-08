import copy

import cartopy.feature
import pandas as pd
import rasterio
import shapely
from rasterio.control import GroundControlPoint
from scipy.interpolate import RectBivariateSpline
from shapely.geometry import Polygon
from shapely.ops import unary_union

# from .raster_readers import available_rasters
from .utils import to_lon180, haversine, timing, class_or_instancemethod
from . import raster_readers
from xradarsat2 import rs2_reader
import os
import geopandas as gpd
import numpy as np


class RadarSat2Meta:
    """
        Handle dataset metadata.
        A `xsar.RadarSat2Meta` object can be used with `xsar.open_dataset`,
        but it can be used as itself: it contains usefull attributes and methods.

        Parameters
        ----------
        name: str
            path or gdal identifier like `'SENTINEL1_DS:%s:WV_001' % path`

        """
    # default mask feature (see self.set_mask_feature and cls.set_mask_feature)
    _mask_features_raw = {
        'land': cartopy.feature.NaturalEarthFeature('physical', 'land', '10m')
    }

    rasters = raster_readers.available_rasters.iloc[0:0].copy()

    # class attributes are needed to fetch instance attribute (ie self.name) with dask actors
    # ref http://distributed.dask.org/en/stable/actors.html#access-attributes
    # FIXME: not needed if @property, so it might be a good thing to have getter for those attributes
    multidataset = None
    name = None
    short_name = None
    path = None
    product = None
    manifest = None
    subdatasets = None
    dsid = None
    manifest_attrs = None
    dt = None
    safe = None
    _time_range = None

    @timing
    def __init__(self, name):
        self.dt = rs2_reader(name)
        if not name.startswith('RADARSAT2_DS:'):
            name = 'RADARSAT2_DS:%s:' % name
        self.name = name
        """Gdal dataset name"""
        name_parts = self.name.split(':')
        if len(name_parts) > 3:
            # windows might have semicolon in path ('c:\...')
            name_parts[1] = ':'.join(name_parts[1:-1])
            del name_parts[2:-1]
        name_parts[1] = os.path.basename(name_parts[1])
        self.short_name = ':'.join(name_parts)
        """Like name, but without path"""
        self.path = ':'.join(self.name.split(':')[1:-1])
        """Dataset path"""
        self.safe = os.path.basename(self.path)
        """Safe file name"""
        # there is no information on resolution 'F' 'H' or 'M' in the manifest, so we have to extract it from filename
        try:
            self.product = os.path.basename(self.path).split('_')[9]
        except:
            self.product = "XXX"
        """Product type, like 'GRDH', 'SLC', etc .."""

        # self.manifest = os.path.join(self.path, 'manifest.safe')

        self._safe_files = None
        self.multidataset = False
        """True if multi dataset"""
        self.subdatasets = gpd.GeoDataFrame(geometry=[], index=[])
        """Subdatasets as GeodataFrame (empty if single dataset)"""
        self.geoloc = self.dt['geolocationGrid'].to_dataset()

        self.orbit_and_attitude = self.dt['orbitAndAttitude'].ds
        self.doppler_centroid = self.dt['imageGenerationParameters']['doppler']['dopplerCentroid'].ds
        self.doppler_rate_values = self.dt['imageGenerationParameters']['doppler']['dopplerRateValues'].ds
        self.chirp = self.dt['imageGenerationParameters']['chirp'].ds
        self.radar_parameters = self.dt['radarParameters'].ds
        self.lut = self.dt['lut'].ds
        self.manifest_attrs = self._create_manifest_attrs()
        self._mask_features_raw = {}
        self._mask_features = {}
        self._mask_intersecting_geometries = {}
        self._mask_geometry = {}

        # get defaults masks from class attribute
        for name, feature in self.__class__._mask_features_raw.items():
            self.set_mask_feature(name, feature)
        self._geoloc = None
        self.rasters = self.__class__.rasters.copy()

    def _create_manifest_attrs(self):
        dic = dict()
        dic["swath_type"] = os.path.basename(self.path).split('_')[4]
        dic["polarizations"] = self.dt["radarParameters"]["pole"].values
        dic["product_type"] = self.product
        dic['satellite'] = self.dt.attrs['satellite']
        dic['start_date'] = self.start_date
        dic['stop_date'] = self.stop_date
        # compute attributes (footprint, coverage, pixel_size)
        footprint_dict = {}
        for ll in ['longitude', 'latitude']:
            footprint_dict[ll] = [
                self.geoloc[ll].isel(line=a, pixel=x).values for a, x in [(0, 0), (0, -1), (-1, -1), (-1, 0)]
            ]
        corners = list(zip(footprint_dict['longitude'], footprint_dict['latitude']))
        p = Polygon(corners)
        self.geoloc.attrs['footprint'] = p
        dic["footprints"] = p
        # compute acquisition size/resolution in meters
        # first vector is on sample
        acq_sample_meters, _ = haversine(*corners[0], *corners[1])
        # second vector is on line
        acq_line_meters, _ = haversine(*corners[1], *corners[2])
        dic['coverage'] = "%dkm * %dkm (line * sample )" % (
            acq_line_meters / 1000, acq_sample_meters / 1000)

        def _to_rio_gcp(pt_geoloc):
            # convert a point from self._geoloc grid to rasterio GroundControlPoint
            return GroundControlPoint(
                x=pt_geoloc.longitude.item(),
                y=pt_geoloc.latitude.item(),
                z=pt_geoloc.height.item(),
                col=pt_geoloc.line.item(),
                row=pt_geoloc.pixel.item()
            )

        gcps = [
            _to_rio_gcp(self.geoloc.sel(line=line, pixel=sample))
            for line in self.geoloc.line for sample in self.geoloc.pixel
        ]
        # approx transform, from all gcps (inaccurate)
        dic['approx_transform'] = rasterio.transform.from_gcps(gcps)
        return dic

    @property
    def cross_antemeridian(self):
        """True if footprint cross antemeridian"""
        return ((np.max(self.geoloc['longitude']) - np.min(
            self.geoloc['longitude'])) > 180).item()

    @property
    def dict(self):
        # return a minimal dictionary that can be used with Sentinel1Meta.from_dict() or pickle (see __reduce__)
        # to reconstruct another instance of self
        #
        minidict = {
            'name': self.name,
            '_mask_features_raw': self._mask_features_raw,
            '_mask_features': {},
            '_mask_intersecting_geometries': {},
            '_mask_geometry': {},
            'rasters': self.rasters
        }
        for name in minidict['_mask_features_raw'].keys():
            minidict['_mask_intersecting_geometries'][name] = None
            minidict['_mask_geometry'][name] = None
            minidict['_mask_features'][name] = None
        return minidict

    @classmethod
    def from_dict(cls, minidict):
        # like copy constructor, but take a dict from Sentinel1Meta.dict
        # https://github.com/umr-lops/xsar/issues/23
        for name in minidict['_mask_features_raw'].keys():
            assert minidict['_mask_geometry'][name] is None
            assert minidict['_mask_features'][name] is None
        minidict = copy.copy(minidict)
        new = cls(minidict['name'])
        new.__dict__.update(minidict)
        return new

    def to_dict(self, keys='minimal'):

        info_keys = {
            'minimal': [
                #'platform',
                'swath', 'product', 'pols']
        }
        info_keys['all'] = info_keys['minimal'] + ['name', 'start_date', 'stop_date',
                                                   'footprint',
                                                   'coverage',
                                                   'pixel_line_m', 'pixel_sample_m',
                                                   'approx_transform',

                                                   #'orbit_pass',
                                                   #'platform_heading'
                                                   ]

        if isinstance(keys, str):
            keys = info_keys[keys]

        res_dict = {}
        for k in keys:
            if hasattr(self, k):
                res_dict[k] = getattr(self, k)
            elif k in self.manifest_attrs.keys():
                res_dict[k] = self.manifest_attrs[k]
            else:
                raise KeyError('Unable to find key/attr "%s" in RadarSat2Meta' % k)
        return res_dict

    @property
    def swath(self):
        """string like 'EW', 'IW', 'WV', etc ..."""
        return self.manifest_attrs['swath_type']

    @property
    def pols(self):
        """polarisations strings, separated by spaces """
        return " ".join(self.manifest_attrs['polarizations'])

    @property
    def footprint(self):
        """footprint, as a shapely polygon or multi polygon"""
        return self.geoloc.attrs['footprint']

    @property
    def _dict_coords2ll(self):
        """
        dict with keys ['longitude', 'latitude'] with interpolation function (RectBivariateSpline) as values.

        Examples:
        ---------
            get longitude at line=100 and sample=200:
            ```
            >>> self._dict_coords2ll['longitude'].ev(100,200)
            array(-66.43947434)
            ```
        Notes:
        ------
            if self.cross_antemeridian is True, 'longitude' will be in range [0, 360]
        """
        resdict = {}
        geoloc = self.geoloc
        if self.cross_antemeridian:
            geoloc['longitude'] = geoloc['longitude'] % 360

        idx_sample = np.array(geoloc.pixel)
        idx_line = np.array(geoloc.line)

        for ll in ['longitude', 'latitude']:
            resdict[ll] = RectBivariateSpline(idx_line, idx_sample, np.asarray(geoloc[ll]), kx=1, ky=1)

        return resdict

    @property
    def pixel_line_m(self):
        """pixel line spacing, in meters (at sensor level)"""
        if self.multidataset:
            res = None  # not defined for multidataset
        else:
            res = self.geoloc.line.attrs['rasterAttributes_sampledLineSpacing_value']
        return res

    @property
    def pixel_sample_m(self):
        """pixel sample spacing, in meters (at sensor level)"""
        if self.multidataset:
            res = None  # not defined for multidataset
        else:
            res = self.geoloc.pixel.attrs['rasterAttributes_sampledPixelSpacing_value']
        return res

    @property
    def approx_transform(self):
        """
        Affine transfom from geoloc.

        This is an inaccurate transform, with errors up to 600 meters.
        But it's fast, and may fit some needs, because the error is stable localy.
        See `xsar.Sentinel1Meta.coords2ll` `xsar.RdarSat2Dataset.ll2coords` for accurate methods.

        Examples
        --------
            get `longitude` and `latitude` from tuple `(line, sample)`:

            >>> longitude, latitude = self.approx_transform * (line, sample)

            get `line` and `sample` from tuple `(longitude, latitude)`

            >>> line, sample = ~self.approx_transform * (longitude, latitude)

        See Also
        --------
        xsar.RadarSat2Dataset.coords2ll
        xsar.RadarSat2Dataset.ll2coords`

        """
        return self.manifest_attrs['approx_transform']

    def _coords2ll_shapely(self, shape, approx=False):
        if approx:
            (xoff, a, b, yoff, d, e) = self.approx_transform.to_gdal()
            return shapely.affinity.affine_transform(shape, (a, b, d, e, xoff, yoff))
        else:
            return shapely.ops.transform(self.coords2ll, shape)

    def coords2ll(self, *args, to_grid=False, approx=False):
        """
        convert `lines`, `samples` arrays to `longitude` and `latitude` arrays.
        or a shapely object in `lines`, `samples` coordinates to `longitude` and `latitude`.

        Parameters
        ----------
        *args: lines, samples  or a shapely geometry
            lines, samples are iterables or scalar

        to_grid: bool, default False
            If True, `lines` and `samples` must be 1D arrays. The results will be 2D array of shape (lines.size, samples.size).

        Returns
        -------
        tuple of np.array or tuple of float
            (longitude, latitude) , with shape depending on `to_grid` keyword.

        See Also
        --------
        xsar.RadarSat2Dataset.ll2coords
        xsar.RadarSat2Dataset.ll2coords

        """

        if isinstance(args[0], shapely.geometry.base.BaseGeometry):
            return self._coords2ll_shapely(args[0])

        lines, samples = args

        scalar = True
        if hasattr(lines, '__iter__'):
            scalar = False

        if approx:
            if to_grid:
                samples2D, lines2D = np.meshgrid(samples, lines)
                lon, lat = self.approx_transform * (lines2D, samples2D)
                pass
            else:
                lon, lat = self.approx_transform * (lines, samples)
        else:
            dict_coords2ll = self._dict_coords2ll
            if to_grid:
                lon = dict_coords2ll['longitude'](lines, samples)
                lat = dict_coords2ll['latitude'](lines, samples)
            else:
                lon = dict_coords2ll['longitude'].ev(lines, samples)
                lat = dict_coords2ll['latitude'].ev(lines, samples)

        if self.cross_antemeridian:
            lon = to_lon180(lon)

        if scalar and hasattr(lon, '__iter__'):
            lon = lon.item()
            lat = lat.item()

        if hasattr(lon, '__iter__') and type(lon) is not type(lines):
            lon = type(lines)(lon)
            lat = type(lines)(lat)

        return lon, lat

    def ll2coords(self, *args):
        """
        Get `(lines, samples)` from `(lon, lat)`,
        or convert a lon/lat shapely shapely object to line/sample coordinates.

        Parameters
        ----------
        *args: lon, lat or shapely object
            lon and lat might be iterables or scalars

        Returns
        -------
        tuple of np.array or tuple of float (lines, samples) , or a shapely object

        Examples
        --------
            get nearest (line,sample) from (lon,lat) = (84.81, 21.32) in ds, without bounds checks

            >>> (line, sample) = meta.ll2coords(84.81, 21.32) # (lon, lat)
            >>> (line, sample)
            (9752.766349989339, 17852.571322887554)

        See Also
        --------
        xsar.Sentinel1Meta.coords2ll
        xsar.Sentinel1Dataset.coords2ll

        """

        if isinstance(args[0], shapely.geometry.base.BaseGeometry):
            return self._ll2coords_shapely(args[0])

        lon, lat = args

        # approximation with global inaccurate transform
        line_approx, sample_approx = ~self.approx_transform * (np.asarray(lon), np.asarray(lat))

        # Theoretical identity. It should be the same, but the difference show the error.
        lon_identity, lat_identity = self.coords2ll(line_approx, sample_approx, to_grid=False)
        line_identity, sample_identity = ~self.approx_transform * (lon_identity, lat_identity)

        # we are now able to compute the error, and make a correction
        line_error = line_identity - line_approx
        sample_error = sample_identity - sample_approx

        line = line_approx - line_error
        sample = sample_approx - sample_error

        if hasattr(lon, '__iter__'):
            scalar = False
        else:
            scalar = True

        return line, sample

    def _ll2coords_shapely(self, shape, approx=False):
        if approx:
            (xoff, a, b, yoff, d, e) = (~self.approx_transform).to_gdal()
            return shapely.affinity.affine_transform(shape, (a, b, d, e, xoff, yoff))
        else:
            return shapely.ops.transform(self.ll2coords, shape)

    def coords2heading(self, lines, samples, to_grid=False, approx=True):
        """
        Get image heading (lines increasing direction) at coords `lines`, `samples`.

        Parameters
        ----------
        lines: np.array or scalar
        samples: np.array or scalar
        to_grid: bool
            If True, `lines` and `samples` must be 1D arrays. The results will be 2D array of shape (lines.size, samples.size).

        Returns
        -------
        np.array or float
            `heading` , with shape depending on `to_grid` keyword.

        """

        lon1, lat1 = self.coords2ll(lines - 1, samples, to_grid=to_grid, approx=approx)
        lon2, lat2 = self.coords2ll(lines + 1, samples, to_grid=to_grid, approx=approx)
        _, heading = haversine(lon1, lat1, lon2, lat2)
        return heading

    @class_or_instancemethod
    def set_mask_feature(self_or_cls, name, feature):
        """
        Set a named mask from a shapefile or a cartopy feature.

        Parameters
        ----------
        name: str
            mask name
        feature: str or cartopy.feature.Feature
            if str, feature is a path to a shapefile or whatever file readable with fiona.
            It is recommended to use str, as the serialization of cartopy feature might be big.

        Examples
        --------
            Add an 'ocean' mask at class level (ie as default mask):
            ```
            >>> xsar.RadarSat2Meta.set_mask_feature('ocean', cartopy.feature.OCEAN)
            ```

            Add an 'ocean' mask at instance level (ie only for this self Sentinel1Meta instance):
            ```
            >>> xsar.RadarSat2Meta.set_mask_feature('ocean', cartopy.feature.OCEAN)
            ```


            High resoltion shapefiles can be found from openstreetmap.
            It is recommended to use WGS84 with large polygons split from https://osmdata.openstreetmap.de/

        See Also
        --------
        xsar.Sentinel1Meta.get_mask
        """

        # see https://stackoverflow.com/a/28238047/5988771 for self_or_cls

        self_or_cls._mask_features_raw[name] = feature

        if not isinstance(self_or_cls, type):
            # self (instance, not class)
            self_or_cls._mask_intersecting_geometries[name] = None
            self_or_cls._mask_geometry[name] = None
            self_or_cls._mask_features[name] = None

    def _get_time_range(self):
        if self.multidataset:
            time_range = [self.manifest_attrs['start_date'], self.manifest_attrs['stop_date']]
        else:
            time_range = self.orbit_and_attitude.timeStamp
        return pd.Interval(left=pd.Timestamp(time_range.values[0]), right=pd.Timestamp(time_range.values[-1]), closed='both')

    @property
    def time_range(self):
        """time range as pd.Interval"""
        if self._time_range is None:
            self._time_range = self._get_time_range()
        return self._time_range

    @property
    def start_date(self):
        """start date, as datetime.datetime"""
        return '%s' % self.time_range.left

    @property
    def stop_date(self):
        """stort date, as datetime.datetime"""
        return '%s' % self.time_range.right

    @property
    def get_azitime(self):
        """
        Get time at low resolution

        Returns
        -------
        array[datetime64[ns]]
            times
        """
        return self.orbit_and_attitude.timeStamp.values



