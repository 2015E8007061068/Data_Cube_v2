# Copyright 2016 United States Government as represented by the Administrator
# of the National Aeronautics and Space Administration. All Rights Reserved.
#
# Portion of this code is Copyright Geoscience Australia, Licensed under the
# Apache License, Version 2.0 (the "License"); you may not use this file
# except in compliance with the License. You may obtain a copy of the License
# at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# The CEOS 2 platform is licensed under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Django specific
from celery.decorators import task
from celery.signals import worker_process_init, worker_process_shutdown
from .models import Query, Result, ResultType, Metadata

import numpy as np
import math
import xarray as xr
import collections
import gdal
import shutil
import sys
import osr
import os
import datetime
from collections import OrderedDict
from dateutil.tz import tzutc

from utils.data_access_api import DataAccessApi
from utils.dc_mosaic import create_mosaic_iterative, create_median_mosaic, create_max_ndvi_mosaic, create_min_ndvi_mosaic
from utils.dc_utilities import get_spatial_ref, save_to_geotiff, create_rgb_png_from_tiff, create_cfmask_clean_mask, split_task

from .utils import update_model_bounds_with_dataset

"""
Class for handling loading celery workers to perform tasks asynchronously.
"""

# Author: AHDS
# Creation date: 2016-06-23
# Modified by:
# Last modified date:

# constants up top for easy access/modification
base_result_path = '/ui_results/custom_mosaic/'
base_temp_path = '/ui_results_temp/'

# Datacube instance to be initialized.
# A seperate DC instance is created for each worker.
dc = None

#default measurements. leaves out all qa bands.
measurements = ['blue', 'green', 'red', 'nir', 'swir1', 'swir2', 'cf_mask']

"""
functions used to combine time sliced data after being combined geographically.
Fill nodata uses the first timeslice as a base, then uses subsequent slices to
fill in indices with nodata values.
this should be used for recent/leastrecent + anything that is done in a single time chunk (median pixel?)
things like max/min ndvi should be able to compound max/min ops between ddifferent timeslices so this will be
different for that.
"""
def fill_nodata(dataset, dataset_intermediate):
    if dataset_intermediate is None:
        return dataset.copy(deep=True)
    dataset_out = dataset_intermediate.copy(deep=True)
    for key in list(dataset_out.data_vars):
        # Get raw data for current variable and mask the data
        dataset_out[key].values[dataset_out[key].values==-9999] = dataset[key].values[dataset_out[key].values==-9999]
    return dataset_out

def max_value(dataset, dataset_intermediate):
    if dataset_intermediate is None:
        return dataset.copy(deep=True)
    dataset_out = dataset_intermediate.copy(deep=True)
    for key in list(dataset_out.data_vars):
        # Get raw data for current variable and mask the data
        dataset_out[key].values[dataset.ndvi.values > dataset_out.ndvi.values] = dataset[key].values[dataset.ndvi.values > dataset_out.ndvi.values]
    return dataset_out

def min_value(dataset, dataset_intermediate):
    if dataset_intermediate is None:
        return dataset.copy(deep=True)
    dataset_out = dataset_intermediate.copy(deep=True)
    for key in list(dataset_out.data_vars):
        # Get raw data for current variable and mask the data
        dataset_out[key].values[dataset.ndvi.values < dataset_out.ndvi.values] = dataset[key].values[dataset.ndvi.values < dataset_out.ndvi.values]
    return dataset_out

#holds the different compositing algorithms. Most/least recent, max/min ndvi, median, etc.
# all options are required. setting None to a option will have the algo/task splitting
# process disregard it.
#experimentally optimized geo/time/slices_per_iter
processing_algorithms = {
    'most_recent': {
        'geo_chunk_size': 0.5,
        'time_chunks': 5,
        'time_slices_per_iteration': 5,
        'reverse_time': True,
        'chunk_combination_method': fill_nodata,
        'processing_method': create_mosaic_iterative
    },
    'least_recent': {
        'geo_chunk_size': 0.5,
        'time_chunks': 5,
        'time_slices_per_iteration': 1,
        'reverse_time': False,
        'chunk_combination_method': fill_nodata,
        'processing_method': create_mosaic_iterative
    },
    'median_pixel': {
        'geo_chunk_size': 0.01,
        'time_chunks': None,
        'time_slices_per_iteration': None,
        'reverse_time': False,
        'chunk_combination_method': fill_nodata,
        'processing_method': create_median_mosaic
    },
    'max_ndvi': {
        'geo_chunk_size': 0.5,
        'time_chunks': 5,
        'time_slices_per_iteration': 5,
        'reverse_time': False,
        'chunk_combination_method': max_value,
        'processing_method': create_max_ndvi_mosaic
    },
    'min_ndvi': {
        'geo_chunk_size': 0.5,
        'time_chunks': 5,
        'time_slices_per_iteration': 5,
        'reverse_time': False,
        'chunk_combination_method': min_value,
        'processing_method': create_min_ndvi_mosaic
    }
}

@task(name="get_data_task")
def create_cloudfree_mosaic(query_id, user_id):
    """
    Creates metadata and result objects from a query id. gets the query, computes metadata for the
    parameters and saves the model. Uses the metadata to query the datacube for relevant data and
    creates the result. Results computed in single time slices for memory efficiency, pushed into a
    single numpy array containing the total result. this is then used to create png/tifs to populate
    a result model. Result model is constantly updated with progress and checked for task
    cancellation.

    Args:
        query_id (int): The ID of the query that will be created.
        user_id (string): The ID of the user that requested the query be made.

    Returns:
        Doesn't return as the method is ran asynchronously.
    """

    print("Starting for query:" + query_id)
    # its fair to assume that the query_id will exist at this point, as if it wasn't it wouldn't
    # start the task.
    queries = Query.objects.filter(query_id=query_id, user_id=user_id)
    # if there is a matching query other than the one we're using now then do nothing.
    # the ui section has already grabbed the result from the db.
    if queries.count() > 1:
        print("Repeat query, client will receive cached result.")
        if Result.objects.filter(query_id=query_id).count() > 0:
            queries.update(complete=True)
        return
    query = queries[0]
    print("Got the query, creating metadata.")

    result_type = ResultType.objects.get(satellite_id=query.platform, result_id=query.query_type)

    # creates the empty result.
    result = query.generate_result()

    product_details = dc.dc.list_products()[dc.dc.list_products().name == query.product]

    # do metadata before actually submitting the task.
    metadata = dc.get_scene_metadata(query.platform, query.product, time=(query.time_start, query.time_end), longitude=(
        query.longitude_min, query.longitude_max), latitude=(query.latitude_min, query.latitude_max))
    if not metadata:
        error_with_message(result, "There was an exception when handling this query.")
        return

    meta = query.generate_metadata(scene_count=metadata['scene_count'], pixel_count=metadata['pixel_count'])

    # wrapping this in a try/catch, as it will throw a few different errors
    # having to do with memory etc.
    try:
        # lists all acquisition dates for use in single tmeslice queries.
        acquisitions = dc.list_acquisition_dates(query.platform, query.product, time=(query.time_start, query.time_end), longitude=(
            query.longitude_min, query.longitude_max), latitude=(query.latitude_min, query.latitude_max))

        if len(acquisitions) < 1:
            error_with_message(result, "There were no acquisitions for this parameter set.")
            return

        processing_options = processing_algorithms[query.compositor]

        # Reversed time = True will make it so most recent = First, oldest = Last.
        #default is in order from oldest -> newwest.
        lat_ranges, lon_ranges, time_ranges = split_task(resolution=product_details.resolution.values[0][1], latitude=(query.latitude_min, query.latitude_max), longitude=(
            query.longitude_min, query.longitude_max), acquisitions=acquisitions, geo_chunk_size=processing_options['geo_chunk_size'], time_chunks=processing_options['time_chunks'], reverse_time=processing_options['reverse_time'])

        result.total_scenes = len(time_ranges) * len(lat_ranges)
        # Iterates through the acquisition dates with the step in acquisitions_per_iteration.
        # Uses a time range computed with the index and index+acquisitions_per_iteration.
        # ensures that the start and end are both valid.
        print("Getting data and creating mosaic")
        # create a temp folder that isn't on the nfs server so we can quickly
        # access/delete.
        if not os.path.exists(base_temp_path + query.query_id):
            os.mkdir(base_temp_path + query.query_id)
            os.chmod(base_temp_path + query.query_id, 0o777)

        time_chunk_tasks = []
        # iterate over the time chunks.
        print("Time chunks: " + str(len(time_ranges)))
        print("Geo chunks: " + str(len(lat_ranges)))
        for time_range_index in range(len(time_ranges)):
            # iterate over the geographic chunks.
            geo_chunk_tasks = []
            for geographic_chunk_index in range(len(lat_ranges)):
                geo_chunk_tasks.append(generate_mosaic_chunk.delay(time_range_index, geographic_chunk_index, processing_options=processing_options, query=query, acquisition_list=time_ranges[
                                       time_range_index], lat_range=lat_ranges[geographic_chunk_index], lon_range=lon_ranges[geographic_chunk_index], measurements=measurements))
            time_chunk_tasks.append(geo_chunk_tasks)

        # holds some acquisition based metadata. dict of objs keyed by date
        dataset_out = None
        acquisition_metadata = {}
        for geographic_group in time_chunk_tasks:
            full_dataset = None
            tiles = []
            for t in geographic_group:
                tile = t.get()
                # tile is [path, metadata]. Append tiles to list of tiles for concat, compile metadata.
                if tile == "CANCEL":
                    print("Cancelled task.")
                    shutil.rmtree(base_temp_path + query.query_id)
                    query.delete()
                    meta.delete()
                    result.delete()
                    return
                if tile[0] is not None:
                    tiles.append(tile)
                result.scenes_processed += 1
                result.save()
            print("Got results for a time slice, computing intermediate product..")
            xr_tiles = []
            for tile in tiles:
                tile_metadata = tile[1]
                for acquisition_date in tile_metadata:
                    if acquisition_date in acquisition_metadata:
                        acquisition_metadata[acquisition_date]['clean_pixels'] += tile_metadata[acquisition_date]['clean_pixels']
                    else:
                        acquisition_metadata[acquisition_date] = {'clean_pixels': tile_metadata[acquisition_date]['clean_pixels']}
                xr_tiles.append(xr.open_dataset(tile[0]))
            full_dataset = xr.concat(reversed(xr_tiles), dim='latitude')
            dataset = full_dataset.load()
            dataset_out = processing_options['chunk_combination_method'](dataset, dataset_out)

        latitude = dataset_out.latitude
        longitude = dataset_out.longitude

        # grabs the resolution.
        geotransform = [longitude.values[0], product_details.resolution.values[0][1],
                        0.0, latitude.values[0], 0.0, product_details.resolution.values[0][0]]
        #hardcoded crs for now. This is not ideal. Should maybe store this in the db with product type?
        crs = str("EPSG:4326")

        # remove intermediates
        shutil.rmtree(base_temp_path + query.query_id)

        # populate metadata values.
        dates = list(acquisition_metadata.keys())
        dates.sort()
        for date in reversed(dates):
            meta.acquisition_list += date.strftime("%m/%d/%Y") + ","
            meta.clean_pixels_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels']) + ","
            meta.clean_pixel_percentages_per_acquisition += str(
                acquisition_metadata[date]['clean_pixels'] * 100 / meta.pixel_count) + ","

        # Count clean pixels and correct for the number of measurements.
        clean_pixels = np.sum(dataset_out[measurements[0]].values != -9999)
        meta.clean_pixel_count = clean_pixels
        meta.percentage_clean_pixels = (meta.clean_pixel_count / meta.pixel_count) * 100
        meta.save()

        # generate all the results
        file_path = base_result_path + query_id
        tif_path = file_path + '.tif'
        netcdf_path = file_path + '.nc'
        png_path = file_path + '.png'
        png_filled_path = file_path + "_filled.png"

        print("Creating query results.")
        save_to_geotiff(tif_path, gdal.GDT_Int16, dataset_out, geotransform, get_spatial_ref(crs),
                        x_pixels=dataset_out.dims['longitude'], y_pixels=dataset_out.dims['latitude'],
                        band_order=['blue', 'green', 'red', 'nir', 'swir1', 'swir2'])
        dataset_out.to_netcdf(netcdf_path)

        # we've got the tif, now do the png.
        bands = [measurements.index(result_type.red)+1, measurements.index(result_type.green)+1, measurements.index(result_type.blue)+1]
        create_rgb_png_from_tiff(tif_path, png_path, png_filled_path=png_filled_path, fill_color=result_type.fill, bands=bands)

        # update the results and finish up.
        update_model_bounds_with_dataset([result, meta, query], dataset_out)
        result.result_path = png_path
        result.data_path = tif_path
        result.data_netcdf_path = netcdf_path
        result.result_filled_path = png_filled_path
        result.status = "OK"
        result.total_scenes = len(acquisitions)
        result.save()
        print("Finished processing results")
        # all data has been processed, create results and finish up.
        query.complete = True
        query.query_end = datetime.datetime.now()
        query.save()

    except:
        error_with_message(
            result, "There was an exception when handling this query.")
        raise
    # end error wrapping.
    return

@task(name="generate_TOOL_chunk")
def generate_TOOL_chunk(time_num, chunk_num, processing_options=None, query=None, acquisition_list=None, lat_range=None, lon_range=None, measurements=None):
    """
    responsible for generating a piece of a custom mosaic product. This grabs the x/y area specified in the lat/lon ranges, gets all data
    from acquisition_list, which is a list of acquisition dates, and creates the custom mosaic using the function named in processing_options.
    saves the result to disk using time/chunk num, and returns the path and the acquisition date keyed metadata.
    """
    time_index = 0
    iteration_data = None
    acquisition_metadata = {}
    print("Starting chunk: " + str(time_num) + " " + str(chunk_num))
    # holds some acquisition based metadata.
    while time_index < len(acquisition_list):
        # check if the task has been cancelled. if the result obj doesn't exist anymore then return.
        try:
            result = Result.objects.get(query_id=query.query_id)
        except:
            print("Cancelled task as result does not exist")
            return
        if result.status == "CANCEL":
            print("Cancelling...")
            return "CANCEL"

        # time ranges set based on if the acquisition_list has been reversed or not. If it has, then the 'start' index is the later date, and must be handled appropriately.
        start = acquisition_list[time_index] + datetime.timedelta(seconds=1) if processing_options['reverse_time'] else acquisition_list[time_index]
        if processing_options['time_slices_per_iteration'] is not None and (time_index + processing_options['time_slices_per_iteration'] - 1) < len(acquisition_list):
            end = acquisition_list[time_index + processing_options['time_slices_per_iteration'] - 1]
        else:
            end = acquisition_list[-1] if processing_options['reverse_time'] else acquisition_list[-1] + datetime.timedelta(seconds=1)
        time_range = (end, start) if processing_options['reverse_time'] else (start, end)

        raw_data = dc.get_dataset_by_extent(query.product, product_type=None, platform=query.platform, time=time_range, longitude=lon_range, latitude=lat_range, measurements=measurements)

        if "cf_mask" not in raw_data:
            time_index = time_index + (processing_options['time_slices_per_iteration'] if processing_options['time_slices_per_iteration'] is not None else 10000)
            continue
        clear_mask = create_cfmask_clean_mask(raw_data.cf_mask)

        # update metadata. # here the clear mask has all the clean
        # pixels for each acquisition.
        for timeslice in range(clear_mask.shape[0]):
            time = acquisition_list[time_index + timeslice]
            clean_pixels = np.sum(
                clear_mask[timeslice, :, :] == True)
            if time not in acquisition_metadata:
                acquisition_metadata[time] = {}
                acquisition_metadata[time]['clean_pixels'] = 0
            acquisition_metadata[time][
                'clean_pixels'] += clean_pixels

        # Removes the cf mask variable from the dataset after the clear mask has been created.
        # prevents the cf mask from being put through the mosaicing function as it doesn't fit
        # the correct format w/ nodata values for mosaicing.
        raw_data = raw_data.drop('cf_mask')

        iteration_data = processing_options['processing_method'](
            raw_data, clean_mask=clear_mask, intermediate_product=iteration_data)
        time_index = time_index + (processing_options['time_slices_per_iteration'] if processing_options['time_slices_per_iteration'] is not None else 10000)

    # Save this geographic chunk to disk.
    geo_path = base_temp_path + query.query_id + "/geo_chunk_" + \
        str(time_num) + "_" + str(chunk_num) + ".nc"
    # if this is an empty chunk, just return an empty dataset.
    if iteration_data is None:
        return [None, None]
    iteration_data.to_netcdf(geo_path)
    print("Done with chunk: " + str(time_num) + " " + str(chunk_num))
    return [geo_path, acquisition_metadata]

def error_with_message(result, message):
    """
    Errors out under specific circumstances, used to pass error msgs to user. Uses the result path as
    a message container: TODO? Change this.

    Args:
        result (Result): The current result of the query being ran.
        message (string): The message to be stored in the result object.

    Returns:
        Nothing is returned as the method is ran asynchronously.
    """
    if os.path.exists(base_temp_path + result.query_id):
        shutil.rmtree(base_temp_path + result.query_id)
    result.status = "ERROR"
    result.result_path = message
    result.save()
    print(message)
    return

# Init/shutdown functions for handling dc instances.
# this is done to prevent synchronization/conflicts between workers when
# accessing DC resources.
@worker_process_init.connect
def init_worker(**kwargs):
    """
    Creates an instance of the DataAccessApi worker.
    """

    print("Creating DC instance for worker.")
    global dc
    dc = DataAccessApi()


@worker_process_shutdown.connect
def shutdown_worker(**kwargs):
    """
    Deletes the instance of the DataAccessApi worker.
    """

    print('Closing DC instance for worker.')
    global dc
    dc = None
