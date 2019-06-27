# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Training script for Mask-RCNN.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import pprint
from absl import flags

import tensorflow as tf

from config import retinanet_config
from dataloader import input_reader
from dataloader import mode_keys as ModeKeys
from executor import tpu_executor
from modeling import model_builder
from utils import params_dict


flags.DEFINE_string(
    'tpu',
    default=None,
    help='The Cloud TPU to use for training. This should be either the name '
    'used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 '
    'url.')
flags.DEFINE_string(
    'gcp_project',
    default=None,
    help='Project name for the Cloud TPU-enabled project. If not specified, we '
    'will attempt to automatically detect the GCE project from metadata.')
flags.DEFINE_string(
    'tpu_zone',
    default=None,
    help='GCE zone where the Cloud TPU is located in. If not specified, we '
    'will attempt to automatically detect the GCE project from metadata.')
flags.DEFINE_integer(
    'num_cores', default=8, help='Number of TPU cores for training')
flags.DEFINE_string(
    'eval_master', default='',
    help='GRPC URL of the eval master. Set to an appropiate value when running '
    'on CPU/GPU')
flags.DEFINE_bool('use_tpu', True, 'Use TPUs rather than CPUs')
flags.DEFINE_string('mode', 'train',
                    'Mode to run: train or eval or train_and_eval '
                    '(default: train)')
flags.DEFINE_bool('eval_after_training', False,
                  'Run one eval after the training finishes.')
flags.DEFINE_string('model_dir', None, 'Location of model_dir')
flags.DEFINE_string(
    'config_file', default=None,
    help=('A YAML file which specifies overrides. Note that this file can be '
          'used as an override template to override the default parameters '
          'specified in Python. If the same parameter is specified in both '
          '`--config_file` and `--params_override`, the one in '
          '`--params_override` will be used finally.'))
flags.DEFINE_string(
    'params_override', default=None,
    help=('a YAML/JSON string or a YAML file which specifies additional '
          'overrides over the default parameters and those specified in '
          '`--config_file`. Note that this is supposed to be used only to '
          'override the model parameters, but not the parameters like TPU '
          'specific flags. One canonical use case of `--config_file` and '
          '`--params_override` is users first define a template config file '
          'using `--config_file`, then use `--params_override` to adjust the '
          'minimal set of tuning parameters, for example setting up different'
          ' `train_batch_size`. '
          'The final override order of parameters: default_model_params --> '
          'params from config_file --> params in params_override.'
          'See also the help message of `--config_file`.'))


FLAGS = flags.FLAGS


def save_config(params, model_dir):
  if model_dir:
    if not tf.gfile.Exists(model_dir):
      tf.gfile.MakeDirs(model_dir)
    params_dict.save_params_dict_to_yaml(params, model_dir + '/param.yaml')


def main(argv):
  del argv  # Unused.

  params = params_dict.ParamsDict(
      retinanet_config.RETINANET_CFG, retinanet_config.RETINANET_RESTRICTIONS)

  if FLAGS.config_file:
    params = params_dict.override_params_dict(
        params, FLAGS.config_file, is_strict=True)

  params = params_dict.override_params_dict(
      params, FLAGS.params_override, is_strict=True)
  params.override({
      'platform': {
          'eval_master': FLAGS.eval_master,
          'tpu': FLAGS.tpu,
          'tpu_zone': FLAGS.tpu_zone,
          'gcp_project': FLAGS.gcp_project,
      },
      'use_tpu': FLAGS.use_tpu,
      'model_dir': FLAGS.model_dir,
      'train': {
          'num_shards': FLAGS.num_cores,
      },
  }, is_strict=False)
  params.validate()
  params.lock()
  pp = pprint.PrettyPrinter()
  params_str = pp.pformat(params.as_dict())
  tf.logging.info('Model Parameters: {}'.format(params_str))

  # Builds detection model on TPUs.
  model_fn = model_builder.ModelFn(params)
  executor = tpu_executor.TpuExecutor(model_fn, params)

  # Prepares input functions for train and eval.
  train_input_fn = input_reader.InputFn(
      params.train.train_file_pattern, params, mode=ModeKeys.TRAIN)
  eval_input_fn = input_reader.InputFn(
      params.eval.eval_file_pattern, params, mode=ModeKeys.PREDICT_WITH_GT)

  # Runs the model.
  if FLAGS.mode == 'train':
    save_config(params, params.model_dir)
    executor.train(train_input_fn, params.train.total_steps)
    if FLAGS.eval_after_training:
      executor.evaluate(
          eval_input_fn,
          params.eval.eval_samples // params.predict.predict_batch_size,
          params.train.total_steps)

  elif FLAGS.mode == 'eval':
    def terminate_eval():
      tf.logging.info('Terminating eval after %d seconds of no checkpoints' %
                      params.eval.eval_timeout)
      return True
    # Runs evaluation when there's a new checkpoint.
    for ckpt in tf.contrib.training.checkpoints_iterator(
        params.model_dir,
        min_interval_secs=params.eval.min_eval_interval,
        timeout=params.eval.eval_timeout,
        timeout_fn=terminate_eval):
      # Terminates eval job when final checkpoint is reached.
      current_step = int(os.path.basename(ckpt).split('-')[1])

      tf.logging.info('Starting to evaluate.')
      try:
        executor.evaluate(
            eval_input_fn,
            params.eval.eval_samples // params.predict.predict_batch_size,
            current_step)

        if current_step >= params.train.total_steps:
          tf.logging.info('Evaluation finished after training step %d' %
                          current_step)
          break
      except tf.errors.NotFoundError:
        # Since the coordinator is on a different job than the TPU worker,
        # sometimes the TPU worker does not finish initializing until long after
        # the CPU job tells it to start evaluating. In this case, the checkpoint
        # file could have been deleted already.
        tf.logging.info('Checkpoint %s no longer exists, skipping checkpoint' %
                        ckpt)

  elif FLAGS.mode == 'train_and_eval':
    save_config(params, params.model_dir)
    num_cycles = int(params.train.total_steps / params.eval.num_steps_per_eval)
    for cycle in range(num_cycles):
      tf.logging.info('Start training cycle %d.' % cycle)
      current_step = (cycle + 1) * params.eval.num_steps_per_eval
      executor.train(train_input_fn, current_step)
      executor.evaluate(
          eval_input_fn,
          params.eval.eval_samples // params.predict.predict_batch_size,
          current_step)
  else:
    tf.logging.info('Mode not found.')


if __name__ == '__main__':
  tf.logging.set_verbosity(tf.logging.INFO)
  tf.app.run(main)
