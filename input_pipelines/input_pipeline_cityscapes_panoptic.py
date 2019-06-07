import tensorflow as tf
import functools
import os
from utils.box_wrapper import BoxWrapper
from utils.box_utils import normalize_boxes, flip_normalized_boxes_left_right, convert_input_box_format


def from_0_1_to_m1_1(images):
  """
  Center images from [0, 1) to [-1, 1).

  Arguments:
    images: tf.float32, in [0, 1), of any dimensions

  Return:
    images linearly scaled to [-1, 1)
  """

  # shifting from [0, 1) to [-1, 1) is equivalent to assuming 0.5 mean
  mean = 0.5
  proimages = (images - mean) / mean

  return proimages

def _parse_and_decode(filename, dataset_directory):
  """

  Args:
    filename:
    dataset_directory:

  Returns:

  """
  filename_split = tf.unstack(tf.string_split([filename], "_").values[:-1], num=3)
  strip_filename = tf.string_join(filename_split, "_")

  im_dir = tf.cast(os.path.join(dataset_directory, 'images/'), tf.string)
  la_dir = tf.cast(os.path.join(dataset_directory, 'panoptic_proc/'), tf.string)
  im_ext = tf.cast('.png', tf.string)
  la_ext = tf.cast('_gtFine_instanceIds.png', tf.string)

  im_filename = tf.string_join([im_dir, filename, im_ext])
  la_filename = tf.string_join([la_dir, strip_filename, la_ext])

  im_dec = tf.image.decode_jpeg(tf.read_file(im_filename))

  # Check if the image is in greyscale and convert to RGB if so
  greyscale_cond = tf.equal(tf.shape(im_dec)[-1], 1)
  im_dec = tf.cond(greyscale_cond,
                   lambda: tf.image.grayscale_to_rgb(im_dec),
                   lambda: tf.identity(im_dec))

  im_dec = tf.image.convert_image_dtype(im_dec, dtype=tf.float32)
  im_dec = from_0_1_to_m1_1(im_dec)

  la_dec = tf.image.decode_png(tf.read_file(la_filename))

  orig_dims = tf.shape(im_dec)[0:2]
  boxes, classes, weights = _parse_and_store_boxes(filename, dataset_directory, orig_dims)

  return im_dec, la_dec, boxes, classes, weights

def _parse_and_decode_inference(filename, dataset_directory):
  im_dir = tf.cast(os.path.join(dataset_directory, 'images/'), tf.string)
  im_ext = tf.cast('.png', tf.string)

  im_filename = tf.string_join([im_dir, filename, im_ext])
  im_dec = tf.image.decode_jpeg(tf.read_file(im_filename))
  im_dec_raw = im_dec

  # Check if the image is in greyscale and convert to RGB if so
  greyscale_cond = tf.equal(tf.shape(im_dec)[-1], 1)
  im_dec = tf.cond(greyscale_cond,
                   lambda: tf.image.grayscale_to_rgb(im_dec),
                   lambda: tf.identity(im_dec))

  im_dec = tf.image.convert_image_dtype(im_dec, dtype=tf.float32)
  im_dec = from_0_1_to_m1_1(im_dec)

  return im_dec, im_filename, im_dec_raw

def _parse_and_store_boxes(filename, dataset_directory, orig_dims):
  filename_split = tf.unstack(tf.string_split([filename], "_").values[:-1], num=3)
  strip_filename = tf.string_join(filename_split, "_")
  txt_dir = tf.cast(os.path.join(dataset_directory, 'panoptic_txt_weights/'), tf.string)
  txt_ext = tf.cast('_gtFine_instanceIds.txt', tf.string)
  txt_filename = tf.string_join([txt_dir, strip_filename, txt_ext])

  la_in_txt = tf.read_file(txt_filename)
  la_in_txt = tf.string_split([la_in_txt], delimiter='\n').values
  la_in_txt = tf.string_split(la_in_txt, delimiter=' ').values
  la_in_int = tf.reshape(tf.string_to_number(la_in_txt, out_type=tf.int32), [-1, 7])

  # i_ids = la_in_int[:, 0]

  weights = la_in_int[:, 6]
  boxes_orig = la_in_int[:, 2:6]
  boxes_format = convert_input_box_format(boxes_orig)
  boxes_norm = normalize_boxes(boxes_format, orig_height=orig_dims[0], orig_width=orig_dims[1])
  classes = la_in_int[:, 1]

  return boxes_norm, classes, weights

def _preprocess_images(image, label, boxes, classes, weights, params):
  if params.random_flip:
    uniform_random = tf.random_uniform([], 0, 1.0, seed=params.random_seed)
    flip_cond = tf.greater(uniform_random, 0.5)

    def _flip_image_left_right(image):
      return tf.image.flip_left_right(image)

    def _flip_label_left_right(label):
      label = tf.expand_dims(label, -1)
      label = tf.image.flip_left_right(label)
      return tf.squeeze(label)

    image = tf.cond(flip_cond,
                    lambda: _flip_image_left_right(image),
                    lambda: image)

    label = tf.cond(flip_cond,
                    lambda: _flip_label_left_right(label),
                    lambda: label)

    boxes = tf.cond(flip_cond,
                    lambda: flip_normalized_boxes_left_right(boxes),
                    lambda: boxes)

    image.set_shape([params.height_input, params.width_input, 3])
    label.set_shape([params.height_input, params.width_input])

    classes = tf.reshape(classes, [-1, 1])
    weights = tf.reshape(weights, [-1, 1])

  return image, label, boxes, classes, weights

def _resize_images(image, label, boxes, classes, weights, height, width):
  """

  Args:
    image:
    label:
    height:
    width:

  Returns:

  """
  im_res = tf.image.resize_images(image, [height, width])
  la_res = tf.image.resize_images(label, [height, width],
                                  method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
  la_res = tf.squeeze(la_res, axis=2)

  im_res.set_shape([height, width, 3])
  la_res.set_shape([height, width])

  return im_res, la_res, boxes, classes, weights

def _resize_images_inference(image, im_filename, im_raw, height, width):
  """

  Args:
    image:
    label:
    height:
    width:

  Returns:

  """
  im_res = tf.image.resize_images(image, [height, width])
  im_res.set_shape([height, width, 3])

  return im_res, im_filename, im_raw

def _format_inputs(image, label, boxes, classes, weights):
  num_boxes = tf.shape(boxes)[0]

  boxes = tf.cond(tf.equal(num_boxes, 0),
                  lambda: _add_dummy_bbox(),
                  lambda: boxes)

  classes = tf.cond(tf.equal(num_boxes, 0),
                    lambda: tf.convert_to_tensor([-1]),
                    lambda: classes)

  weights = tf.cond(tf.equal(num_boxes, 0),
                    lambda: tf.convert_to_tensor([0]),
                    lambda: weights)

  num_boxes = tf.cond(tf.equal(num_boxes, 0),
                      lambda: tf.convert_to_tensor([1]),
                      lambda: num_boxes)

  classes = tf.reshape(classes, [-1, 1])
  weights = tf.reshape(weights, [-1, 1])
  num_boxes = tf.reshape(num_boxes, [-1])

  return image, label, boxes, classes, weights, num_boxes


def _add_dummy_bbox():
  return tf.convert_to_tensor([(0.0, 0.0, 0.1, 0.1)], dtype=tf.float32)


def train_input(params):
  """

  Args:
    params:

  Returns:

  """
  dataset_directory = params.dataset_directory
  filelist_filepath = params.filelist_filepath
  filenames_string = tf.cast(filelist_filepath, tf.string)

  dataset = tf.data.TextLineDataset(filenames=filenames_string)

  dataset = dataset.map(
    functools.partial(_parse_and_decode, dataset_directory=dataset_directory),
    num_parallel_calls=30)

  dataset = dataset.map(
    functools.partial(_resize_images, height=params.height_input, width=params.width_input))

  dataset = dataset.map(
    functools.partial(_preprocess_images, params=params))

  dataset = dataset.map(_format_inputs)

  # IMPORTANT: if Nb > 1, then shape of dataset elements must be the same
  # dataset = dataset.batch(params.Nb)
  # dataset = dataset.padded_batch(params.Nb, padded_shapes=(tf.TensorShape([params.height_input, params.width_input, 3]),
  #                                                          tf.TensorShape([params.height_input, params.width_input]),
  #                                                          tf.TensorShape([None, 4]),
  #                                                          tf.TensorShape([None, 1]),
  #                                                          tf.TensorShape([1])))

  dataset = dataset.apply(tf.contrib.data.padded_batch_and_drop_remainder(
    batch_size=tf.cast(params.Nb, tf.int64),
    padded_shapes=(tf.TensorShape([params.height_input, params.width_input, 3]),
                   tf.TensorShape([params.height_input, params.width_input]),
                   tf.TensorShape([None, 4]),
                   tf.TensorShape([None, 1]),
                   tf.TensorShape([None, 1]),
                   tf.TensorShape([1]))))
  dataset = dataset.shuffle(100)
  dataset = dataset.repeat(None)
  # dataset = dataset.prefetch(10)

  return dataset

def evaluate_input(params):
  """

  Args:
    params:

  Returns:

  """
  dataset_directory = params.dataset_directory
  filelist_filepath = params.filelist_filepath
  filenames_string = tf.cast(filelist_filepath, tf.string)

  dataset = tf.data.TextLineDataset(filenames=filenames_string)

  dataset = dataset.map(
    functools.partial(_parse_and_decode, dataset_directory=dataset_directory),
    num_parallel_calls=30)

  dataset = dataset.map(
    functools.partial(_resize_images, height=params.height_input, width=params.width_input))

  dataset = dataset.map(_format_inputs)

  # IMPORTANT: if Nb > 1, then shape of dataset elements must be the same
  # dataset = dataset.padded_batch(1, padded_shapes=([params.height_input, params.width_input, 3],
  #                                                  [params.height_input, params.width_input],
  #                                                  [None, 4],
  #                                                  [None, 1],
  #                                                  [1]))
  dataset = dataset.batch(1)

  return dataset

def inference_input(params):
  """

  Args:
    params:

  Returns:

  """
  dataset_directory = params.dataset_directory
  filelist_filepath = params.filelist_filepath
  filenames_string = tf.cast(filelist_filepath, tf.string)

  dataset = tf.data.TextLineDataset(filenames=filenames_string)

  dataset = dataset.map(
    functools.partial(_parse_and_decode_inference, dataset_directory=dataset_directory),
    num_parallel_calls=30)

  dataset = dataset.map(
    functools.partial(_resize_images_inference, height=params.height_input, width=params.width_input))

  # IMPORTANT: if Nb > 1, then shape of dataset elements must be the same
  dataset = dataset.batch(1)

  return dataset