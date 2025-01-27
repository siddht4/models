# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
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

"""YT8M model definition."""

import functools
from typing import Optional

import tensorflow as tf

from official.modeling import tf_utils
from official.projects.yt8m.configs import yt8m as yt8m_cfg
from official.projects.yt8m.modeling import nn_layers
from official.projects.yt8m.modeling import yt8m_model_utils as utils


layers = tf.keras.layers


class DbofModel(tf.keras.Model):
  """A YT8M model class builder.

  Creates a Deep Bag of Frames model.
  The model projects the features for each frame into a higher dimensional
  'clustering' space, pools across frames in that space, and then
  uses a configurable video-level model to classify the now aggregated features.
  The model will randomly sample either frames or sequences of frames during
  training to speed up convergence.
  """

  def __init__(
      self,
      params: yt8m_cfg.DbofModel,
      num_classes: int = 3862,
      input_specs: layers.InputSpec = layers.InputSpec(
          shape=[None, None, 1152]),
      activation: str = "relu",
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      l2_weight_decay: Optional[float] = None,
      **kwargs):
    """YT8M initialization function.

    Args:
      params: model configuration parameters
      num_classes: `int` number of classes in dataset.
      input_specs: `tf.keras.layers.InputSpec` specs of the input tensor.
        [batch_size x num_frames x num_features]
      activation: A `str` of name of the activation function.
      use_sync_bn: If True, use synchronized batch normalization.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      l2_weight_decay: An optional `float` of kernel regularizer weight decay.
      **kwargs: keyword arguments to be passed.
    """
    model_input, activation = self.get_dbof(
        params=params,
        num_classes=num_classes,
        input_specs=input_specs,
        activation=activation,
        use_sync_bn=use_sync_bn,
        norm_momentum=norm_momentum,
        norm_epsilon=norm_epsilon,
        l2_weight_decay=l2_weight_decay,
        **kwargs,
    )
    output = self.get_aggregation(model_input=activation, **kwargs)
    super().__init__(
        inputs=model_input, outputs=output.get("predictions"), **kwargs)

  def get_dbof(
      self,
      params: yt8m_cfg.DbofModel,
      num_classes: int = 3862,
      input_specs: layers.InputSpec = layers.InputSpec(
          shape=[None, None, 1152]),
      activation: str = "relu",
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      l2_weight_decay: Optional[float] = None,
      **kwargs):

    del kwargs  # Unused and reserved for future extension.
    self._self_setattr_tracking = False
    self._config_dict = {
        "input_specs": input_specs,
        "num_classes": num_classes,
        "params": params,
        "use_sync_bn": use_sync_bn,
        "activation": activation,
        "l2_weight_decay": l2_weight_decay,
        "norm_momentum": norm_momentum,
        "norm_epsilon": norm_epsilon,
    }
    self._num_classes = num_classes
    self._input_specs = input_specs
    self._params = params
    self._activation = activation
    self._l2_weight_decay = l2_weight_decay
    self._use_sync_bn = use_sync_bn
    self._norm_momentum = norm_momentum
    self._norm_epsilon = norm_epsilon
    self._act_fn = tf_utils.get_activation(activation)
    self._norm = functools.partial(
        layers.BatchNormalization, synchronized=use_sync_bn)

    # Divide weight decay by 2.0 to match the implementation of tf.nn.l2_loss.
    # (https://www.tensorflow.org/api_docs/python/tf/keras/regularizers/l2)
    # (https://www.tensorflow.org/api_docs/python/tf/nn/l2_loss)
    l2_regularizer = (
        tf.keras.regularizers.l2(l2_weight_decay / 2.0)
        if l2_weight_decay
        else None
    )

    bn_axis = -1
    # [batch_size x num_frames x num_features]
    feature_size = input_specs.shape[-1]
    # shape 'excluding' batch_size
    model_input = tf.keras.Input(shape=self._input_specs.shape[1:])
    # normalize input features
    input_data = tf.nn.l2_normalize(model_input, -1)
    tf.summary.histogram("input_hist", input_data)

    # configure model
    if params.add_batch_norm:
      input_data = self._norm(
          axis=bn_axis,
          momentum=norm_momentum,
          epsilon=norm_epsilon,
          name="input_bn")(
              input_data)

    # activation = reshaped input * cluster weights
    if params.cluster_size > 0:
      activation = layers.Dense(
          params.cluster_size,
          kernel_regularizer=l2_regularizer,
          kernel_initializer=tf.random_normal_initializer(
              stddev=1 / tf.sqrt(tf.cast(feature_size, tf.float32))))(
                  input_data)

    if params.add_batch_norm:
      activation = self._norm(
          axis=bn_axis,
          momentum=norm_momentum,
          epsilon=norm_epsilon,
          name="cluster_bn")(
              activation)
    else:
      cluster_biases = tf.Variable(
          tf.random_normal_initializer(stddev=1 / tf.math.sqrt(feature_size))(
              shape=[params.cluster_size]),
          name="cluster_biases")
      tf.summary.histogram("cluster_biases", cluster_biases)
      activation += cluster_biases

    activation = self._act_fn(activation)
    tf.summary.histogram("cluster_output", activation)

    if params.use_context_gate_cluster_layer:
      pooling_method = None
      norm_args = dict(
          axis=bn_axis,
          momentum=norm_momentum,
          epsilon=norm_epsilon,
          name="context_gate_bn")
      activation = utils.context_gate(
          activation,
          normalizer_fn=self._norm,
          normalizer_params=norm_args,
          pooling_method=pooling_method,
          hidden_layer_size=params.context_gate_cluster_bottleneck_size,
          kernel_regularizer=l2_regularizer)

    activation = utils.frame_pooling(activation, params.pooling_method)

    # activation = activation * hidden1_weights
    activation = layers.Dense(
        params.hidden_size,
        kernel_regularizer=l2_regularizer,
        kernel_initializer=tf.random_normal_initializer(
            stddev=1 / tf.sqrt(tf.cast(params.cluster_size, tf.float32))))(
                activation)

    if params.add_batch_norm:
      activation = self._norm(
          axis=bn_axis,
          momentum=norm_momentum,
          epsilon=norm_epsilon,
          name="hidden1_bn")(
              activation)

    else:
      hidden1_biases = tf.Variable(
          tf.random_normal_initializer(stddev=0.01)(shape=[params.hidden_size]),
          name="hidden1_biases")

      tf.summary.histogram("hidden1_biases", hidden1_biases)
      activation += hidden1_biases

    activation = self._act_fn(activation)
    tf.summary.histogram("hidden1_output", activation)

    return model_input, activation

  def get_aggregation(self, model_input, **kwargs):
    del kwargs  # Unused and reserved for future extension.
    normalizer_fn = functools.partial(
        layers.BatchNormalization, synchronized=self._use_sync_bn)
    normalizer_params = dict(
        axis=-1, momentum=self._norm_momentum, epsilon=self._norm_epsilon)
    aggregated_model = getattr(
        nn_layers, self._params.yt8m_agg_classifier_model)

    output = aggregated_model().create_model(
        model_input=model_input,
        vocab_size=self._num_classes,
        num_mixtures=self._params.agg_model.num_mixtures,
        normalizer_fn=normalizer_fn,
        normalizer_params=normalizer_params,
        vocab_as_last_dim=self._params.agg_model.vocab_as_last_dim,
        l2_penalty=self._params.agg_model.l2_penalty,
    )
    return output

  @property
  def checkpoint_items(self):
    """Returns a dictionary of items to be additionally checkpointed."""
    return dict()

  def get_config(self):
    return self._config_dict

  @classmethod
  def from_config(cls, config):
    return cls(**config)
