import os
import pathlib
import string
import warnings

import numpy as np

try:
    import tensorstore as ts
    tensorstore_available = True
except ModuleNotFoundError:
    tensorstore_available = False
    have_warned = False
import zarr

from ome_zarr import io as omio
import dask.array as da


def _tensorstore_or_dask(zarr_path):
    if tensorstore_available:
        return open_zarr(zarr_path)
    else:
        return da.from_zarr(zarr_path)


def load_ome_zarr(path: pathlib.Path | str):
    """Read image and OME metadata from path."""
    path = pathlib.Path(path)  # just use the thing regardless of input
    metadata_dict = ome_metadata(path)
    layer_meta, layer_type = ome_to_napari(metadata_dict)
    ds = metadata_dict['multiscales'][0]['datasets']
    if layer_type == 'image':  # potentially, read multiscales
        nscales = len(ds)
        if nscales == 1:
            data = _tensorstore_or_dask(path / ds[0]['path'])
        else:
            data = [_tensorstore_or_dask(path / di['path']) for di in ds]
    else:  # 'labels'; we can't paint into multiscale labels so load high res
        data = open_zarr(path / ds[0]['path'])  # can't paint into dask either
    return [(data, layer_meta, layer_type)]


def get_napari_reader(path):
    if path.endswith('ome.zarr'):
        return load_ome_zarr
    return None


def ome_metadata(path: pathlib.Path) -> dict:
    """Load OME v0.4 metadata from a directory."""
    return omio.ZarrLocation(path).root_attrs


def is_ome_labels(ome_meta: dict) -> bool:
    return 'image-label' in ome_meta


def ome_to_napari(ome_meta: dict) -> tuple[dict, str]:
    """Convert an OME metadata dict to napari metadata dict and layer type."""
    layer_type = 'labels' if is_ome_labels(ome_meta) else 'image'
    if layer_type == 'image':
        meta = _ome_to_napari_meta_image(ome_meta)
    else:
        meta = _ome_to_napari_meta_labels(ome_meta)
    return meta, layer_type


def _get_scale(ome_meta):
    axes = ome_meta['multiscales'][0]['axes']
    non_channel_axes = [i for i, ax in enumerate(axes)
                        if ax['type'] != 'channel']
    full_ndim = len(axes)  # including channel — we'll subset later
    default_scale = np.ones(full_ndim)
    dataset_dict = ome_meta['multiscales'][0]['datasets'][0]
    if 'coordinateTransformations' in dataset_dict:
        scales = [d['scale']
                  for d in dataset_dict['coordinateTransformations']
                  if d['type'] == 'scale']
        if len(scales) > 0:
            scale = np.multiply.reduce(scales)
        else:
            scale = default_scale
    else:
        scale = default_scale
    return scale[non_channel_axes]


def _get_translate(ome_meta):
    axes = ome_meta['multiscales'][0]['axes']
    non_channel_axes = [i for i, ax in enumerate(axes)
                        if ax['type'] != 'channel']
    full_ndim = len(axes)  # including channel — we'll subset later
    default_translate = np.zeros(full_ndim)
    dataset_dict = ome_meta['multiscales'][0]['datasets'][0]
    if 'coordinateTransformations' in dataset_dict:
        translates = [d['translation']
                      for d in dataset_dict['coordinateTransformations']
                      if d['type'] == 'translation']
        if len(translates) > 0:
            translate = np.add.reduce(translates)
        else:
            translate = default_translate
    else:
        translate = default_translate
    return translate[non_channel_axes]


def _get_contrast(ome_meta):
    contrast_limits = None
    contrast_range = None
    if 'omero' in ome_meta:
        if 'channels' in (omero := ome_meta['omero']):
            channels = omero['channels']
            contrast_limits_dicts = [ch.get('window', None) for ch in channels]
            if 0 < len(contrast_limits_dicts) < len(channels):
                raise ValueError(
                        'Either all or no channels should have '
                        'window/contrast limits metadata'
                        )
            if len(contrast_limits_dicts) != 0:
                contrast_limits = [(d['start'], d['end'])
                                   for d in contrast_limits_dicts
                                   if 'start' in d and 'end' in d]
                contrast_range = [(d['min'], d['max'])
                                   for d in contrast_limits_dicts
                                   if 'min' in d and 'max' in d]
    return contrast_limits, contrast_range


def _validate_colormap(cmap_str):
    if (all(char in string.hexdigits for char in cmap_str)
            and not cmap_str.startswith('#')):
        result = '#' + cmap_str
    else:
        result = cmap_str  # could be colormap name; let napari validate
    return result


def _get_channel_info(ome_meta):
    names = []
    colormaps = []
    visibles = []
    if 'omero' in ome_meta:
        if 'channels' in (omero := ome_meta['omero']):
            channels = omero['channels']
            names_ = [ch['label'] for ch in channels if 'label' in ch]
            colormaps_ = [_validate_colormap(ch['color']) for ch in channels
                         if 'color' in ch]
            visibles_ = [ch['active'] for ch in channels if 'active' in ch]
            if 0 < len(names_) < len(channels):
                raise ValueError(
                        'Either all or no channels should have names metadata'
                        )
            if 0 < len(colormaps_) < len(channels):
                raise ValueError(
                        'Either all or no channels should have color metadata'
                        )
            if 0 < len(visibles_) < len(channels):
                raise ValueError(
                        'Either all or no channels should have visibility '
                        'metadata'
                        )
            if len(names_) != 0:
                names = names_
            if len(colormaps_) != 0:
                colormaps = colormaps_
            if len(visibles_) != 0:
                visibles = visibles_
    return names, colormaps, visibles


def _unwrap(arglist, channel_axis):
    """Return the first element of arglist if channel_axis is None."""
    if channel_axis is None and arglist is not None and len(arglist) > 0:
        return arglist[0]
    else:
        return arglist


def _ome_to_napari_meta_image(ome_meta: dict) -> dict:
    metadata = {'axes': ome_meta['multiscales'][0]['axes']}
    try:
        channel_axis = [i for i, ax in enumerate(metadata['axes'])
                        if ax['type'] == 'channel'][0]
    except IndexError:
        channel_axis = None
    scale = _get_scale(ome_meta)
    translate = _get_translate(ome_meta)
    contrast_limits, contrast_range = _get_contrast(ome_meta)
    names, colormaps, visibles = _get_channel_info(ome_meta)
    napari_meta_dict = {
            'channel_axis': channel_axis,
            'scale': scale,
            'translate': translate,
            'contrast_limits': _unwrap(contrast_limits, channel_axis),
            #'contrast_limits_range': contrast_range,  # not a thing
            'name': _unwrap(names, channel_axis),
            'colormap': _unwrap(colormaps, channel_axis),
            'visible': _unwrap(visibles, channel_axis),
            'metadata': metadata,
    }
    return napari_meta_dict


def _ome_to_napari_meta_labels(ome_meta: dict) -> dict:
    metadata = {'axes': ome_meta['multiscales'][0]['axes']}
    scale = _get_scale(ome_meta)
    translate = _get_translate(ome_meta)
    napari_meta_dict = {
        'scale': scale,
        'translate': translate,
        'metadata': metadata,
    }
    return napari_meta_dict


def open_zarr(labels_file: pathlib.Path, *, shape=None, chunks=None):
    """Open a zarr file, with tensorstore if available, with zarr otherwise.

    If the file doesn't exist, it is created.

    Parameters
    ----------
    labels_file : Path
        The output file name.
    shape : tuple of int
        The shape of the array.
    chunks : tuple of int
        The chunk size of the array.

    Returns
    -------
    data : ts.Array or zarr.Array
        The array loaded from file.
    """
    if not os.path.exists(labels_file):
        zarr.open(
                str(labels_file),
                mode='w',
                shape=shape,
                dtype=np.uint32,
                chunks=chunks,
                )
    # read some of the metadata for tensorstore driver from file
    labels_temp = zarr.open(str(labels_file), mode='a')
    metadata = {
            'dtype': labels_temp.dtype.str,
            'order': labels_temp.order,
            'shape': labels_temp.shape,
            }

    dir, name = os.path.split(labels_file)
    labels_ts_spec = {
            'driver': 'zarr',
            'kvstore': {'driver': 'file', 'path': dir},
            'path': name,
            'metadata': metadata,
            }
    if tensorstore_available:
        data = ts.open(labels_ts_spec, create=False, open=True).result()
    else:
        global have_warned
        if not have_warned:
            warnings.warn(
                    'tensorstore not available, falling back to zarr.\n'
                    'Drawing with tensorstore is *much faster*. We recommend '
                    'you install tensorstore with '
                    '`python -m pip install tensorstore`.'
                    )
            have_warned = True
        data = labels_temp
    return data


