import logging
log = logging.getLogger('boundaries.api.load_shapefiles')
from optparse import make_option
import os
import os.path
import sys

from zipfile import ZipFile
from tempfile import mkdtemp

from django.conf import settings
from django.contrib.gis.gdal import (CoordTransform, DataSource, OGRGeometry,
                                     OGRGeomType)
from django.core.management.base import BaseCommand
from django.db import connections, DEFAULT_DB_ALIAS, transaction

from boundaryservice.models import (BoundarySet, Boundary, NAMERS, PointSet,
                                    Point, Shapefile)

settings.DEFAULT_SHAPEFILES_DIR = getattr(settings, 'SHAPEFILES_DIR',
                                          'media/shapefiles')


class Command(BaseCommand):
    help = 'Import boundaries described by shapefiles.'
    option_list = BaseCommand.option_list + (
        make_option('-c', '--clear', action='store_true', dest='clear',
            help='Clear all jurisdictions in the DB.'),
        make_option('-d', '--data-dir', action='store', dest='data_dir',
            default=settings.DEFAULT_SHAPEFILES_DIR,
            help='Load shapefiles from this directory'),
        make_option('-e', '--except', action='store', dest='except',
                    default=False,
                    help='Don\'t load these kinds of Areas, comma-delimited.'),
        make_option('-o', '--only', action='store', dest='only',
                    default=False,
                    help='Only load these kinds of Areas, comma-delimited.'),
        make_option('-u', '--database', action='store', dest='database',
                    default=DEFAULT_DB_ALIAS,
                    help='Specify a database to load shape data into.'),
    )

    def get_version(self):
        return '0.1'

    def handle(self, *args, **options):
        # Load configuration
        sys.path.append(options['data_dir'])
        try:
            from definitions import SHAPEFILES
            shapefiles = SHAPEFILES
        except ImportError:
            shapefiles = {}

        for sf in Shapefile.objects.all():
            shapefiles[sf.name] = {
                'file': sf.file.name.replace(
                    '%s/' % settings.SHAPEFILES_SUBDIR, '', 1),
                'singular': sf.singular,
                'kind_first': sf.kind_first,
                'ider': NAMERS[sf.ider_namer]([n.strip() for n in
                                               sf.ider_fields.split(',')]),
                'namer': NAMERS[sf.name_namer]([n.strip() for n in
                                                sf.name_fields.split(',')]),
                'authority': sf.authority,
                'domain': sf.domain,
                'last_updated': sf.last_updated,
                'href': sf.href,
                'notes': sf.notes,
                'encoding': sf.encoding,
                'srid': sf.srid,
                'simplification': sf.simplification
            }

        if options['only']:
            only = options['only'].upper().split(',')
            # TODO: stripping whitespace here because optparse doesn't handle
            # it correctly
            sources = [s for s in shapefiles
                       if s.replace(' ', '').upper() in only]
        elif options['except']:
            exceptions = options['except'].upper().split(',')
            # See above
            sources = [s for s in shapefiles
                       if s.replace(' ', '').upper() not in exceptions]
        else:
            sources = [s for s in shapefiles]

        for kind, config in shapefiles.items():
            if kind not in sources:
                log.info('Skipping %s.' % kind)
                continue

            log.info('Processing %s.' % kind)

            self.load_set(kind, config, options)

    @transaction.commit_on_success
    def load_set(self, kind, config, options):
        log.info('Processing %s.' % kind)

        if options['clear']:
            dset = None

            try:
                dset = BoundarySet.objects.get(name=kind)

                if dset:
                    log.info('Clearing old %s.' % kind)
                    dset.boundaries.all().delete()
                    dset.delete()

                    log.info('Loading new %s.' % kind)
            except BoundarySet.DoesNotExist:
                log.info('No existing boundary set of kind [%s] so nothing to '
                         'delete' % kind)

            try:
                dset = PointSet.objects.get(name=kind)

                if dset:
                    log.info('Clearing old %s.' % kind)
                    dset.points.all().delete()
                    dset.delete()

                    log.info('Loading new %s.' % kind)
            except PointSet.DoesNotExist:
                log.info('No existing point set of kind [%s] so nothing to '
                         'delete' % kind)

        path = os.path.join(options['data_dir'], config['file'])
        datasources = create_datasources(path)

        layer = datasources[0][0]
        data_type = layer.geom_type.name

        # create dataset
        if data_type in ['Polygon', 'MultiPolygon']:
            dset = BoundarySet.objects.create(
                name=kind,
                singular=config['singular'],
                kind_first=config['kind_first'],
                authority=config['authority'],
                domain=config['domain'],
                last_updated=config['last_updated'],
                href=config['href'],
                notes=config['notes'],
                count=len(layer),
                metadata_fields=layer.fields)
        elif data_type in ['Point', 'MultiPoint']:
            dset = PointSet.objects.create(
                name=kind,
                singular=config['singular'],
                kind_first=config['kind_first'],
                authority=config['authority'],
                domain=config['domain'],
                last_updated=config['last_updated'],
                href=config['href'],
                notes=config['notes'],
                count=len(layer),
                metadata_fields=layer.fields)
        else:
            raise Exception(
                'This data set contains an unknown data type: %s' % data_type)

        for datasource in datasources:
            log.info("Loading %s from %s" % (kind, datasource.name))
            # Assume only a single-layer in shapefile
            if datasource.layer_count > 1:
                log.warn('%s shapefile [%s] has multiple layers, using first.'
                         % (datasource.name, kind))
            layer = datasource[0]

            if data_type in ['Polygon', 'MultiPolygon']:
                self.add_boundaries_for_layer(config, layer, dset,
                                              options['database'])
            elif data_type in ['Point', 'MultiPoint']:
                self.add_points_for_layer(config, layer, dset,
                                          options['database'])
        # sync this with reality
        if data_type in ['Polygon', 'MultiPolygon']:
            dset.count = Boundary.objects.filter(set=dset).count()
        elif data_type in ['Point', 'MultiPoint']:
            dset.count = Point.objects.filter(set=dset).count()
        dset.save()
        log.info('%s count: %i' % (kind, dset.count))

    def polygon_to_multipolygon(self, geom):
        """
        Convert polygons to multipolygons so all features are homogenous in the
        database.
        """
        if geom.__class__.__name__ == 'Polygon':
            g = OGRGeometry(OGRGeomType('MultiPolygon'))
            g.add(geom)
            return g
        elif geom.__class__.__name__ == 'MultiPolygon':
            return geom
        else:
            raise ValueError('Geom is neither Polygon nor MultiPolygon.')

    def add_boundaries_for_layer(self, config, layer, dset, database):
        # Get spatial reference system for the postgis geometry field
        geometry_field = Boundary._meta.get_field_by_name('shape')[0]
        SpatialRefSys = connections[database].ops.spatial_ref_sys()
        db_srs = SpatialRefSys.objects.using(database).get(
            srid=geometry_field.srid).srs

        if 'srid' in config and config['srid']:
            layer_srs = SpatialRefSys.objects.get(srid=config['srid']).srs
        else:
            layer_srs = layer.srs

        # Simplification can be configured but default is to create simplified
        # geometry field by collapsing points within 1/1000th of a degree.
        # For reference, Chicago is at approx. 42 degrees latitude this works
        # out to a margin of roughly 80 meters latitude and 112 meters
        # longitude for Chicago area.
        simplification = config.get('simplification', 0.0001)

        # Create a convertor to turn the source data into
        transformer = CoordTransform(layer_srs, db_srs)

        for feature in layer:
            # Transform the geometry to the correct SRS
            geometry = self.polygon_to_multipolygon(feature.geom)
            geometry.transform(transformer)

            # Preserve topology prevents a shape from ever crossing over
            # itself.
            simple_geometry = geometry.geos.simplify(simplification,
                                                     preserve_topology=True)

            # Conversion may force multipolygons back to being polygons
            simple_geometry = self.polygon_to_multipolygon(simple_geometry.ogr)

            # Extract metadata into a dictionary
            metadata = {}

            for field in layer.fields:

                # Decode string fields using encoding specified in definitions
                # config
                if config['encoding'] != '':
                    try:
                        metadata[field] = feature.get(field).decode(
                            config['encoding'])
                    # Only strings will be decoded, get value in normal way if
                    # int etc.
                    except AttributeError:
                        metadata[field] = feature.get(field)
                else:
                    metadata[field] = feature.get(field)

            external_id = config['ider'](feature)
            feature_name = config['namer'](feature)

            # If encoding is specified, decode id and feature name
            if config['encoding'] != '':
                external_id = external_id.decode(config['encoding'])
                feature_name = feature_name.decode(config['encoding'])

            if config['kind_first']:
                display_name = '%s %s' % (config['singular'], feature_name)
            else:
                display_name = '%s %s' % (feature_name, config['singular'])

            Boundary.objects.create(
                set=dset,
                kind=config['singular'],
                external_id=external_id,
                name=feature_name,
                display_name=display_name,
                metadata=metadata,
                shape=geometry.wkt,
                simple_shape=simple_geometry.wkt,
                centroid=geometry.geos.centroid)

    def point_to_multipoint(self, geom):
        """
        Convert points to multipoints so all features are homogenous in the
        database.
        """
        if geom.__class__.__name__ == 'Point':
            g = OGRGeometry(OGRGeomType('MultiPoint'))
            g.add(geom)
            return g
        elif geom.__class__.__name__ == 'MultiPoint':
            return geom
        else:
            raise ValueError('Geom is neither Point nor MultiPoint.')

    def add_points_for_layer(self, config, layer, dset, database):
        # Get spatial reference system for the postgis geometry field
        geometry_field = Point._meta.get_field_by_name('point')[0]
        SpatialRefSys = connections[database].ops.spatial_ref_sys()
        db_srs = SpatialRefSys.objects.using(database).get(
            srid=geometry_field.srid).srs

        if 'srid' in config and config['srid']:
            layer_srs = SpatialRefSys.objects.get(srid=config['srid']).srs
        else:
            layer_srs = layer.srs

        # Create a convertor to turn the source data into
        transformer = CoordTransform(layer_srs, db_srs)

        for feature in layer:
            # Transform the geometry to the correct SRS
            geometry = self.point_to_multipoint(feature.geom)
            geometry.transform(transformer)

            # Extract metadata into a dictionary
            metadata = {}

            for field in layer.fields:

                # Decode string fields using encoding specified in definitions
                # config
                if config['encoding'] != '':
                    try:
                        metadata[field] = feature.get(field).decode(
                            config['encoding'])
                    # Only strings will be decoded, get value in normal way if
                    # int etc.
                    except AttributeError:
                        metadata[field] = feature.get(field)
                else:
                    metadata[field] = feature.get(field)

            external_id = config['ider'](feature)
            feature_name = config['namer'](feature)

            # If encoding is specified, decode id and feature name
            if config['encoding'] != '':
                external_id = external_id.decode(config['encoding'])
                feature_name = feature_name.decode(config['encoding'])

            if config['kind_first']:
                display_name = '%s %s' % (config['singular'], feature_name)
            else:
                display_name = '%s %s' % (feature_name, config['singular'])

            Point.objects.create(
                set=dset,
                kind=config['singular'],
                external_id=external_id,
                name=feature_name,
                display_name=display_name,
                metadata=metadata,
                point=geometry.wkt)


def create_datasources(path):
    if path.endswith('.zip'):
        path = temp_shapefile_from_zip(path)

    if path.endswith('.shp'):
        return [DataSource(path)]

    # assume it's a directory...
    sources = []
    for fn in os.listdir(path):
        fn = os.path.join(path, fn)
        if fn.endswith('.zip'):
            fn = temp_shapefile_from_zip(fn)
        if fn.endswith('.shp'):
            sources.append(DataSource(fn))
    return sources


def temp_shapefile_from_zip(zip_path):
    """
    Given a path to a ZIP file, unpack it into a temp dir and return the path
    to the shapefile that was in there.  Doesn't clean up after itself unless
    there was an error.

    If you want to cleanup later, you can derive the temp dir from this path.
    """
    zf = ZipFile(zip_path)
    tempdir = mkdtemp()
    shape_path = None
    # Copy the zipped files to a temporary directory, preserving names.
    for name in zf.namelist():
        data = zf.read(name)
        outfile = os.path.join(tempdir, name)
        if name.endswith('.shp'):
            shape_path = outfile
        f = open(outfile, 'w')
        f.write(data)
        f.close()

    if shape_path is None:
        log.warn("No shapefile, cleaning up")
        # Clean up after ourselves.
        for file in os.listdir(tempdir):
            os.unlink(os.path.join(tempdir, file))
        os.rmdir(tempdir)
        raise ValueError("No shapefile found in zip")

    return shape_path
