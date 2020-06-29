# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# pylint: disable=unused-import
"""Built-in metrics.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import types

import numpy as np
import six

from tensorflow.python.distribute import distribution_strategy_context as distribute_ctx
from tensorflow.python.eager import context
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_spec
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.engine import base_layer
from tensorflow.python.keras.engine import base_layer_utils
from tensorflow.python.keras.losses import binary_crossentropy
from tensorflow.python.keras.losses import categorical_crossentropy
from tensorflow.python.keras.losses import categorical_hinge
from tensorflow.python.keras.losses import hinge
from tensorflow.python.keras.losses import kullback_leibler_divergence
from tensorflow.python.keras.losses import logcosh
from tensorflow.python.keras.losses import mean_absolute_error
from tensorflow.python.keras.losses import mean_absolute_percentage_error
from tensorflow.python.keras.losses import mean_squared_error
from tensorflow.python.keras.losses import mean_squared_logarithmic_error
from tensorflow.python.keras.losses import poisson
from tensorflow.python.keras.losses import sparse_categorical_crossentropy
from tensorflow.python.keras.losses import squared_hinge
from tensorflow.python.keras.saving.saved_model import metric_serialization
from tensorflow.python.keras.utils import metrics_utils
from tensorflow.python.keras.utils.generic_utils import deserialize_keras_object
from tensorflow.python.keras.utils.generic_utils import serialize_keras_object
from tensorflow.python.keras.utils.generic_utils import to_list
from tensorflow.python.keras.utils.tf_utils import is_tensor_or_variable
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import confusion_matrix
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn
from tensorflow.python.ops import variables as tf_variables
from tensorflow.python.ops import weights_broadcast_ops
from tensorflow.python.ops.losses import util as tf_losses_utils
from tensorflow.python.training.tracking import base as trackable
from tensorflow.python.util import nest
from tensorflow.python.util import tf_inspect
from tensorflow.python.util.tf_export import keras_export
from tensorflow.tools.docs import doc_controls


@keras_export('keras.metrics.Metric')
@six.add_metaclass(abc.ABCMeta)
class Metric(base_layer.Layer):
  """Encapsulates metric logic and state.

  Usage:

  ```python
  m = SomeMetric(...)
  for input in ...:
    m.update_state(input)
  print('Final result: ', m.result().numpy())
  ```

  Usage with tf.keras API:

  ```python
  model = tf.keras.Sequential()
  model.add(tf.keras.layers.Dense(64, activation='relu'))
  model.add(tf.keras.layers.Dense(64, activation='relu'))
  model.add(tf.keras.layers.Dense(10, activation='softmax'))

  model.compile(optimizer=tf.keras.optimizers.RMSprop(0.01),
                loss=tf.keras.losses.CategoricalCrossentropy(),
                metrics=[tf.keras.metrics.CategoricalAccuracy()])

  data = np.random.random((1000, 32))
  labels = np.random.random((1000, 10))

  dataset = tf.data.Dataset.from_tensor_slices((data, labels))
  dataset = dataset.batch(32)

  model.fit(dataset, epochs=10)
  ```

  To be implemented by subclasses:
  * `__init__()`: All state variables should be created in this method by
    calling `self.add_weight()` like: `self.var = self.add_weight(...)`
  * `update_state()`: Has all updates to the state variables like:
    self.var.assign_add(...).
  * `result()`: Computes and returns a value for the metric
    from the state variables.

  Example subclass implementation:

  ```python
  class BinaryTruePositives(tf.keras.metrics.Metric):

    def __init__(self, name='binary_true_positives', **kwargs):
      super(BinaryTruePositives, self).__init__(name=name, **kwargs)
      self.true_positives = self.add_weight(name='tp', initializer='zeros')

    def update_state(self, y_true, y_pred, sample_weight=None):
      y_true = tf.cast(y_true, tf.bool)
      y_pred = tf.cast(y_pred, tf.bool)

      values = tf.logical_and(tf.equal(y_true, True), tf.equal(y_pred, True))
      values = tf.cast(values, self.dtype)
      if sample_weight is not None:
        sample_weight = tf.cast(sample_weight, self.dtype)
        sample_weight = tf.broadcast_weights(sample_weight, values)
        values = tf.multiply(values, sample_weight)
      self.true_positives.assign_add(tf.reduce_sum(values))

    def result(self):
      return self.true_positives
  ```
  """

  def __init__(self, name=None, dtype=None, **kwargs):
    super(Metric, self).__init__(name=name, dtype=dtype, **kwargs)
    self.stateful = True  # All metric layers are stateful.
    self.built = True
    if not base_layer_utils.v2_dtype_behavior_enabled():
      # We only do this when the V2 behavior is not enabled, as when it is
      # enabled, the dtype already defaults to floatx.
      self._dtype = K.floatx() if dtype is None else dtypes.as_dtype(dtype).name

  def __new__(cls, *args, **kwargs):
    obj = super(Metric, cls).__new__(cls)

    # If `update_state` is not in eager/tf.function and it is not from a
    # built-in metric, wrap it in `tf.function`. This is so that users writing
    # custom metrics in v1 need not worry about control dependencies and
    # return ops.
    if (base_layer_utils.is_in_eager_or_tf_function() or
        is_built_in(cls)):
      update_state_fn = obj.update_state
    else:
      if isinstance(obj.update_state, def_function.Function):
        update_state_fn = obj.update_state
      else:
        update_state_fn = def_function.function(obj.update_state)

    obj.update_state = types.MethodType(
        metrics_utils.update_state_wrapper(update_state_fn), obj)
    obj.result = types.MethodType(metrics_utils.result_wrapper(obj.result), obj)
    return obj

  def __call__(self, *args, **kwargs):
    """Accumulates statistics and then computes metric result value.

    Args:
      *args:
      **kwargs: A mini-batch of inputs to the Metric,
        passed on to `update_state()`.

    Returns:
      The metric value tensor.
    """

    def replica_local_fn(*args, **kwargs):
      """Updates the state of the metric in a replica-local context."""
      update_op = self.update_state(*args, **kwargs)  # pylint: disable=not-callable
      update_ops = []
      if update_op is not None:
        update_ops.append(update_op)
      with ops.control_dependencies(update_ops):
        result_t = self.result()  # pylint: disable=not-callable

        # We are adding the metric object as metadata on the result tensor.
        # This is required when we want to use a metric with `add_metric` API on
        # a Model/Layer in graph mode. This metric instance will later be used
        # to reset variable state after each epoch of training.
        # Example:
        #   model = Model()
        #   mean = Mean()
        #   model.add_metric(mean(values), name='mean')
        result_t._metric_obj = self  # pylint: disable=protected-access
        return result_t

    from tensorflow.python.keras.distribute import distributed_training_utils  # pylint:disable=g-import-not-at-top
    return distributed_training_utils.call_replica_local_fn(
        replica_local_fn, *args, **kwargs)

  @property
  def dtype(self):
    return self._dtype

  def get_config(self):
    """Returns the serializable config of the metric."""
    return {'name': self.name, 'dtype': self.dtype}

  def reset_states(self):
    """Resets all of the metric state variables.

    This function is called between epochs/steps,
    when a metric is evaluated during training.
    """
    K.batch_set_value([(v, 0) for v in self.variables])

  @abc.abstractmethod
  def update_state(self, *args, **kwargs):
    """Accumulates statistics for the metric.

    Note: This function is executed as a graph function in graph mode.
    This means:
      a) Operations on the same resource are executed in textual order.
         This should make it easier to do things like add the updated
         value of a variable to another, for example.
      b) You don't need to worry about collecting the update ops to execute.
         All update ops added to the graph by this function will be executed.
      As a result, code should generally work the same way with graph or
      eager execution.

    Args:
      *args:
      **kwargs: A mini-batch of inputs to the Metric.
    """
    raise NotImplementedError('Must be implemented in subclasses.')

  @abc.abstractmethod
  def result(self):
    """Computes and returns the metric value tensor.

    Result computation is an idempotent operation that simply calculates the
    metric value using the state variables.
    """
    raise NotImplementedError('Must be implemented in subclasses.')

  ### For use by subclasses ###
  @doc_controls.for_subclass_implementers
  def add_weight(self,
                 name,
                 shape=(),
                 aggregation=tf_variables.VariableAggregation.SUM,
                 synchronization=tf_variables.VariableSynchronization.ON_READ,
                 initializer=None,
                 dtype=None):
    """Adds state variable. Only for use by subclasses."""
    from tensorflow.python.keras.distribute import distributed_training_utils  # pylint:disable=g-import-not-at-top

    if distribute_ctx.has_strategy():
      strategy = distribute_ctx.get_strategy()
    else:
      strategy = None

    # TODO(b/120571621): Make `ON_READ` work with Keras metrics on TPU.
    if distributed_training_utils.is_tpu_strategy(strategy):
      synchronization = tf_variables.VariableSynchronization.ON_WRITE

    return super(Metric, self).add_weight(
        name=name,
        shape=shape,
        dtype=self._dtype if dtype is None else dtype,
        trainable=False,
        initializer=initializer,
        collections=[],
        synchronization=synchronization,
        aggregation=aggregation)

  ### End: For use by subclasses ###

  @property
  def _trackable_saved_model_saver(self):
    return metric_serialization.MetricSavedModelSaver(self)


class Reduce(Metric):
  """Encapsulates metrics that perform a reduce operation on the values."""

  def __init__(self, reduction, name, dtype=None):
    """Creates a `Reduce` instance.

    Args:
      reduction: a `tf.keras.metrics.Reduction` enum value.
      name: string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(Reduce, self).__init__(name=name, dtype=dtype)
    self.reduction = reduction
    with ops.init_scope():
      self.total = self.add_weight(
          'total', initializer=init_ops.zeros_initializer)
      if reduction in [metrics_utils.Reduction.SUM_OVER_BATCH_SIZE,
                       metrics_utils.Reduction.WEIGHTED_MEAN]:
        self.count = self.add_weight(
            'count', initializer=init_ops.zeros_initializer)

  def update_state(self, values, sample_weight=None):
    """Accumulates statistics for computing the reduction metric.

    For example, if `values` is [1, 3, 5, 7] and reduction=SUM_OVER_BATCH_SIZE,
    then the value of `result()` is 4. If the `sample_weight` is specified as
    [1, 1, 0, 0] then value of `result()` would be 2.

    Args:
      values: Per-example value.
      sample_weight: Optional weighting of each example. Defaults to 1.

    Returns:
      Update op.
    """
    [values], sample_weight = \
        metrics_utils.ragged_assert_compatible_and_get_flat_values(
            [values], sample_weight)
    values = math_ops.cast(values, self._dtype)
    if sample_weight is not None:
      sample_weight = math_ops.cast(sample_weight, self._dtype)
      # Update dimensions of weights to match with values if possible.
      values, _, sample_weight = tf_losses_utils.squeeze_or_expand_dimensions(
          values, sample_weight=sample_weight)
      try:
        # Broadcast weights if possible.
        sample_weight = weights_broadcast_ops.broadcast_weights(
            sample_weight, values)
      except ValueError:
        # Reduce values to same ndim as weight array
        ndim = K.ndim(values)
        weight_ndim = K.ndim(sample_weight)
        if self.reduction == metrics_utils.Reduction.SUM:
          values = math_ops.reduce_sum(
              values, axis=list(range(weight_ndim, ndim)))
        else:
          values = math_ops.reduce_mean(
              values, axis=list(range(weight_ndim, ndim)))
      values = math_ops.multiply(values, sample_weight)

    value_sum = math_ops.reduce_sum(values)
    with ops.control_dependencies([value_sum]):
      update_total_op = self.total.assign_add(value_sum)

    # Exit early if the reduction doesn't have a denominator.
    if self.reduction == metrics_utils.Reduction.SUM:
      return update_total_op

    # Update `count` for reductions that require a denominator.
    if self.reduction == metrics_utils.Reduction.SUM_OVER_BATCH_SIZE:
      num_values = math_ops.cast(array_ops.size(values), self._dtype)
    elif self.reduction == metrics_utils.Reduction.WEIGHTED_MEAN:
      if sample_weight is None:
        num_values = math_ops.cast(array_ops.size(values), self._dtype)
      else:
        num_values = math_ops.reduce_sum(sample_weight)
    else:
      raise NotImplementedError(
          'reduction [%s] not implemented' % self.reduction)

    with ops.control_dependencies([update_total_op]):
      return self.count.assign_add(num_values)

  def result(self):
    if self.reduction == metrics_utils.Reduction.SUM:
      return array_ops.identity(self.total)
    elif self.reduction in [
        metrics_utils.Reduction.WEIGHTED_MEAN,
        metrics_utils.Reduction.SUM_OVER_BATCH_SIZE
    ]:
      return math_ops.div_no_nan(self.total, self.count)
    else:
      raise NotImplementedError(
          'reduction [%s] not implemented' % self.reduction)


@keras_export('keras.metrics.Sum')
class Sum(Reduce):
  """Computes the (weighted) sum of the given values.

  For example, if values is [1, 3, 5, 7] then the sum is 16.
  If the weights were specified as [1, 1, 0, 0] then the sum would be 4.

  This metric creates one variable, `total`, that is used to compute the sum of
  `values`. This is ultimately returned as `sum`.

  If `sample_weight` is `None`, weights default to 1.  Use `sample_weight` of 0
  to mask values.

  Usage:

  >>> m = tf.keras.metrics.Sum()
  >>> _ = m.update_state([1, 3, 5, 7])
  >>> m.result().numpy()
  16.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.add_metric(tf.keras.metrics.Sum(name='sum_1')(outputs))
  model.compile('sgd', loss='mse')
  ```
  """

  def __init__(self, name='sum', dtype=None):
    """Creates a `Sum` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(Sum, self).__init__(reduction=metrics_utils.Reduction.SUM,
                              name=name, dtype=dtype)


@keras_export('keras.metrics.Mean')
class Mean(Reduce):
  """Computes the (weighted) mean of the given values.

  For example, if values is [1, 3, 5, 7] then the mean is 4.
  If the weights were specified as [1, 1, 0, 0] then the mean would be 2.

  This metric creates two variables, `total` and `count` that are used to
  compute the average of `values`. This average is ultimately returned as `mean`
  which is an idempotent operation that simply divides `total` by `count`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.Mean()
  >>> _ = m.update_state([1, 3, 5, 7])
  >>> m.result().numpy()
  4.0
  >>> m.reset_states()
  >>> _ = m.update_state([1, 3, 5, 7], sample_weight=[1, 1, 0, 0])
  >>> m.result().numpy()
  2.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.add_metric(tf.keras.metrics.Mean(name='mean_1')(outputs))
  model.compile('sgd', loss='mse')
  ```
  """

  def __init__(self, name='mean', dtype=None):
    """Creates a `Mean` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(Mean, self).__init__(
        reduction=metrics_utils.Reduction.WEIGHTED_MEAN, name=name, dtype=dtype)


@keras_export('keras.metrics.MeanRelativeError')
class MeanRelativeError(Mean):
  """Computes the mean relative error by normalizing with the given values.

  This metric creates two local variables, `total` and `count` that are used to
  compute the mean relative error. This is weighted by `sample_weight`, and
  it is ultimately returned as `mean_relative_error`:
  an idempotent operation that simply divides `total` by `count`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.MeanRelativeError(normalizer=[1, 3, 2, 3])
  >>> _ = m.update_state([1, 3, 2, 3], [2, 4, 6, 8])

  >>> # metric = mean(|y_pred - y_true| / normalizer)
  >>> #        = mean([1, 1, 4, 5] / [1, 3, 2, 3]) = mean([1, 1/3, 2, 5/3])
  >>> #        = 5/4 = 1.25
  >>> m.result().numpy()
  1.25

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    loss='mse',
    metrics=[tf.keras.metrics.MeanRelativeError(normalizer=[1, 3])])
  ```
  """

  def __init__(self, normalizer, name=None, dtype=None):
    """Creates a `MeanRelativeError` instance.

    Args:
      normalizer: The normalizer values with same shape as predictions.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(MeanRelativeError, self).__init__(name=name, dtype=dtype)
    normalizer = math_ops.cast(normalizer, self._dtype)
    self.normalizer = normalizer

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates metric statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    y_true = math_ops.cast(y_true, self._dtype)
    y_pred = math_ops.cast(y_pred, self._dtype)
    [y_pred, y_true], sample_weight = \
        metrics_utils.ragged_assert_compatible_and_get_flat_values(
            [y_pred, y_true], sample_weight)
    y_pred, y_true = tf_losses_utils.squeeze_or_expand_dimensions(
        y_pred, y_true)

    y_pred, self.normalizer = confusion_matrix.remove_squeezable_dimensions(
        y_pred, self.normalizer)
    y_pred.shape.assert_is_compatible_with(y_true.shape)
    relative_errors = math_ops.div_no_nan(
        math_ops.abs(y_true - y_pred), self.normalizer)

    return super(MeanRelativeError, self).update_state(
        relative_errors, sample_weight=sample_weight)

  def get_config(self):
    n = self.normalizer
    config = {'normalizer': K.eval(n) if is_tensor_or_variable(n) else n}
    base_config = super(MeanRelativeError, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


class MeanMetricWrapper(Mean):
  """Wraps a stateless metric function with the Mean metric."""

  def __init__(self, fn, name=None, dtype=None, **kwargs):
    """Creates a `MeanMetricWrapper` instance.

    Args:
      fn: The metric function to wrap, with signature
        `fn(y_true, y_pred, **kwargs)`.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      **kwargs: The keyword arguments that are passed on to `fn`.
    """
    super(MeanMetricWrapper, self).__init__(name=name, dtype=dtype)
    self._fn = fn
    self._fn_kwargs = kwargs

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates metric statistics.

    `y_true` and `y_pred` should have the same shape.

    Args:
      y_true: Ground truth values. shape = `[batch_size, d0, .. dN]`.
      y_pred: The predicted values. shape = `[batch_size, d0, .. dN]`.
      sample_weight: Optional `sample_weight` acts as a
        coefficient for the metric. If a scalar is provided, then the metric is
        simply scaled by the given value. If `sample_weight` is a tensor of size
        `[batch_size]`, then the metric for each sample of the batch is rescaled
        by the corresponding element in the `sample_weight` vector. If the shape
        of `sample_weight` is `[batch_size, d0, .. dN-1]` (or can be broadcasted
        to this shape), then each metric element of `y_pred` is scaled by the
        corresponding value of `sample_weight`. (Note on `dN-1`: all metric
        functions reduce by 1 dimension, usually the last axis (-1)).

    Returns:
      Update op.
    """
    y_true = math_ops.cast(y_true, self._dtype)
    y_pred = math_ops.cast(y_pred, self._dtype)
    [y_true, y_pred], sample_weight = \
        metrics_utils.ragged_assert_compatible_and_get_flat_values(
            [y_true, y_pred], sample_weight)
    y_pred, y_true = tf_losses_utils.squeeze_or_expand_dimensions(
        y_pred, y_true)

    matches = self._fn(y_true, y_pred, **self._fn_kwargs)
    return super(MeanMetricWrapper, self).update_state(
        matches, sample_weight=sample_weight)

  def get_config(self):
    config = {}

    if type(self) is MeanMetricWrapper:  # pylint: disable=unidiomatic-typecheck
      # Only include function argument when the object is a MeanMetricWrapper
      # and not a subclass.
      config['fn'] = self._fn

    for k, v in six.iteritems(self._fn_kwargs):
      config[k] = K.eval(v) if is_tensor_or_variable(v) else v
    base_config = super(MeanMetricWrapper, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  @classmethod
  def from_config(cls, config):
    # Note that while MeanMetricWrapper itself isn't public, objects of this
    # class may be created and added to the model by calling model.compile.
    if cls is MeanMetricWrapper:
      fn = get(config.pop('fn'))
      return cls(fn, **config)

    return super(MeanMetricWrapper, cls).from_config(config)


@keras_export('keras.metrics.Accuracy')
class Accuracy(MeanMetricWrapper):
  """Calculates how often predictions equals labels.

  This metric creates two local variables, `total` and `count` that are used to
  compute the frequency with which `y_pred` matches `y_true`. This frequency is
  ultimately returned as `binary accuracy`: an idempotent operation that simply
  divides `total` by `count`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.Accuracy()
  >>> _ = m.update_state([[1], [2], [3], [4]], [[0], [2], [3], [4]])
  >>> m.result().numpy()
  0.75

  >>> m.reset_states()
  >>> _ = m.update_state([[1], [2], [3], [4]], [[0], [2], [3], [4]],
  ...                    sample_weight=[1, 1, 0, 0])
  >>> m.result().numpy()
  0.5

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.Accuracy()])
  ```
  """

  def __init__(self, name='accuracy', dtype=None):
    super(Accuracy, self).__init__(accuracy, name, dtype=dtype)


@keras_export('keras.metrics.BinaryAccuracy')
class BinaryAccuracy(MeanMetricWrapper):
  """Calculates how often predictions matches binary labels.

  This metric creates two local variables, `total` and `count` that are used to
  compute the frequency with which `y_pred` matches `y_true`. This frequency is
  ultimately returned as `binary accuracy`: an idempotent operation that simply
  divides `total` by `count`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.BinaryAccuracy()
  >>> _ = m.update_state([[1], [1], [0], [0]], [[0.98], [1], [0], [0.6]])
  >>> m.result().numpy()
  0.75

  >>> m.reset_states()
  >>> _ = m.update_state([[1], [1], [0], [0]], [[0.98], [1], [0], [0.6]],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  0.5

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.BinaryAccuracy()])
  ```
  """

  def __init__(self, name='binary_accuracy', dtype=None, threshold=0.5):
    """Creates a `BinaryAccuracy` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      threshold: (Optional) Float representing the threshold for deciding
      whether prediction values are 1 or 0.
    """
    super(BinaryAccuracy, self).__init__(
        binary_accuracy, name, dtype=dtype, threshold=threshold)


@keras_export('keras.metrics.CategoricalAccuracy')
class CategoricalAccuracy(MeanMetricWrapper):
  """Calculates how often predictions matches one-hot labels.

  You can provide logits of classes as `y_pred`, since argmax of
  logits and probabilities are same.

  This metric creates two local variables, `total` and `count` that are used to
  compute the frequency with which `y_pred` matches `y_true`. This frequency is
  ultimately returned as `categorical accuracy`: an idempotent operation that
  simply divides `total` by `count`.

  `y_pred` and `y_true` should be passed in as vectors of probabilities, rather
  than as labels. If necessary, use `tf.one_hot` to expand `y_true` as a vector.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.CategoricalAccuracy()
  >>> _ = m.update_state([[0, 0, 1], [0, 1, 0]], [[0.1, 0.9, 0.8],
  ...                     [0.05, 0.95, 0]])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 0, 1], [0, 1, 0]], [[0.1, 0.9, 0.8],
  ...                     [0.05, 0.95, 0]],
  ...                    sample_weight=[0.7, 0.3])
  >>> m.result().numpy()
  0.3

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    loss='mse',
    metrics=[tf.keras.metrics.CategoricalAccuracy()])
  ```
  """

  def __init__(self, name='categorical_accuracy', dtype=None):
    """Creates a `CategoricalAccuracy` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(CategoricalAccuracy, self).__init__(
        categorical_accuracy, name, dtype=dtype)


@keras_export('keras.metrics.SparseCategoricalAccuracy')
class SparseCategoricalAccuracy(MeanMetricWrapper):
  """Calculates how often predictions matches integer labels.

  You can provide logits of classes as `y_pred`, since argmax of
  logits and probabilities are same.

  This metric creates two local variables, `total` and `count` that are used to
  compute the frequency with which `y_pred` matches `y_true`. This frequency is
  ultimately returned as `sparse categorical accuracy`: an idempotent operation
  that simply divides `total` by `count`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.SparseCategoricalAccuracy()
  >>> _ = m.update_state([[2], [1]], [[0.1, 0.9, 0.8], [0.05, 0.95, 0]])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([[2], [1]], [[0.1, 0.9, 0.8], [0.05, 0.95, 0]],
  ...                    sample_weight=[0.7, 0.3])
  >>> m.result().numpy()
  0.3

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.SparseCategoricalAccuracy()])
  ```
  """

  def __init__(self, name='sparse_categorical_accuracy', dtype=None):
    super(SparseCategoricalAccuracy, self).__init__(
        sparse_categorical_accuracy, name, dtype=dtype)


@keras_export('keras.metrics.TopKCategoricalAccuracy')
class TopKCategoricalAccuracy(MeanMetricWrapper):
  """Computes how often targets are in the top `K` predictions.

  Usage:

  >>> m = tf.keras.metrics.TopKCategoricalAccuracy(k=1)
  >>> _ = m.update_state([[0, 0, 1], [0, 1, 0]],
  ...                    [[0.1, 0.9, 0.8], [0.05, 0.95, 0]])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 0, 1], [0, 1, 0]],
  ...                    [[0.1, 0.9, 0.8], [0.05, 0.95, 0]],
  ...                    sample_weight=[0.7, 0.3])
  >>> m.result().numpy()
  0.3

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', metrics=[tf.keras.metrics.TopKCategoricalAccuracy()])
  ```
  """

  def __init__(self, k=5, name='top_k_categorical_accuracy', dtype=None):
    """Creates a `TopKCategoricalAccuracy` instance.

    Args:
      k: (Optional) Number of top elements to look at for computing accuracy.
        Defaults to 5.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(TopKCategoricalAccuracy, self).__init__(
        top_k_categorical_accuracy, name, dtype=dtype, k=k)


@keras_export('keras.metrics.SparseTopKCategoricalAccuracy')
class SparseTopKCategoricalAccuracy(MeanMetricWrapper):
  """Computes how often integer targets are in the top `K` predictions.

  Usage:

  >>> m = tf.keras.metrics.SparseTopKCategoricalAccuracy(k=1)
  >>> _ = m.update_state([2, 1], [[0.1, 0.9, 0.8], [0.05, 0.95, 0]])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([2, 1], [[0.1, 0.9, 0.8], [0.05, 0.95, 0]],
  ...                    sample_weight=[0.7, 0.3])
  >>> m.result().numpy()
  0.3

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    metrics=[tf.keras.metrics.SparseTopKCategoricalAccuracy()])
  ```
  """

  def __init__(self, k=5, name='sparse_top_k_categorical_accuracy', dtype=None):
    """Creates a `SparseTopKCategoricalAccuracy` instance.

    Args:
      k: (Optional) Number of top elements to look at for computing accuracy.
        Defaults to 5.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(SparseTopKCategoricalAccuracy, self).__init__(
        sparse_top_k_categorical_accuracy, name, dtype=dtype, k=k)


class _ConfusionMatrixConditionCount(Metric):
  """Calculates the number of the given confusion matrix condition."""

  def __init__(self,
               confusion_matrix_cond,
               thresholds=None,
               name=None,
               dtype=None):
    """Creates a `_ConfusionMatrixConditionCount` instance.

    Args:
      confusion_matrix_cond: One of `metrics_utils.ConfusionMatrix` conditions.
      thresholds: (Optional) Defaults to 0.5. A float value or a python
        list/tuple of float threshold values in [0, 1]. A threshold is compared
        with prediction values to determine the truth value of predictions
        (i.e., above the threshold is `true`, below is `false`). One metric
        value is generated for each threshold value.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(_ConfusionMatrixConditionCount, self).__init__(name=name, dtype=dtype)
    self._confusion_matrix_cond = confusion_matrix_cond
    self.init_thresholds = thresholds
    self.thresholds = metrics_utils.parse_init_thresholds(
        thresholds, default_threshold=0.5)
    self.accumulator = self.add_weight(
        'accumulator',
        shape=(len(self.thresholds),),
        initializer=init_ops.zeros_initializer)

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates the given confusion matrix condition statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    return metrics_utils.update_confusion_matrix_variables(
        {self._confusion_matrix_cond: self.accumulator},
        y_true,
        y_pred,
        thresholds=self.thresholds,
        sample_weight=sample_weight)

  def result(self):
    if len(self.thresholds) == 1:
      result = self.accumulator[0]
    else:
      result = self.accumulator
    return ops.convert_to_tensor_v2(result)

  def reset_states(self):
    num_thresholds = len(to_list(self.thresholds))
    K.batch_set_value(
        [(v, np.zeros((num_thresholds,))) for v in self.variables])

  def get_config(self):
    config = {'thresholds': self.init_thresholds}
    base_config = super(_ConfusionMatrixConditionCount, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.FalsePositives')
class FalsePositives(_ConfusionMatrixConditionCount):
  """Calculates the number of false positives.

  If `sample_weight` is given, calculates the sum of the weights of
  false positives. This metric creates one local variable, `accumulator`
  that is used to keep track of the number of false positives.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.FalsePositives()
  >>> _ = m.update_state([0, 1, 0, 0], [0, 0, 1, 1])
  >>> m.result().numpy()
  2.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 0, 0], [0, 0, 1, 1], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.FalsePositives()])
  ```
  """

  def __init__(self, thresholds=None, name=None, dtype=None):
    """Creates a `FalsePositives` instance.

    Args:
      thresholds: (Optional) Defaults to 0.5. A float value or a python
        list/tuple of float threshold values in [0, 1]. A threshold is compared
        with prediction values to determine the truth value of predictions
        (i.e., above the threshold is `true`, below is `false`). One metric
        value is generated for each threshold value.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(FalsePositives, self).__init__(
        confusion_matrix_cond=metrics_utils.ConfusionMatrix.FALSE_POSITIVES,
        thresholds=thresholds,
        name=name,
        dtype=dtype)


@keras_export('keras.metrics.FalseNegatives')
class FalseNegatives(_ConfusionMatrixConditionCount):
  """Calculates the number of false negatives.

  If `sample_weight` is given, calculates the sum of the weights of
  false negatives. This metric creates one local variable, `accumulator`
  that is used to keep track of the number of false negatives.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.FalseNegatives()
  >>> _ = m.update_state([0, 1, 1, 1], [0, 1, 0, 0])
  >>> m.result().numpy()
  2.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 1, 1], [0, 1, 0, 0], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.FalseNegatives()])
  ```
  """

  def __init__(self, thresholds=None, name=None, dtype=None):
    """Creates a `FalseNegatives` instance.

    Args:
      thresholds: (Optional) Defaults to 0.5. A float value or a python
        list/tuple of float threshold values in [0, 1]. A threshold is compared
        with prediction values to determine the truth value of predictions
        (i.e., above the threshold is `true`, below is `false`). One metric
        value is generated for each threshold value.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(FalseNegatives, self).__init__(
        confusion_matrix_cond=metrics_utils.ConfusionMatrix.FALSE_NEGATIVES,
        thresholds=thresholds,
        name=name,
        dtype=dtype)


@keras_export('keras.metrics.TrueNegatives')
class TrueNegatives(_ConfusionMatrixConditionCount):
  """Calculates the number of true negatives.

  If `sample_weight` is given, calculates the sum of the weights of
  true negatives. This metric creates one local variable, `accumulator`
  that is used to keep track of the number of true negatives.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.TrueNegatives()
  >>> _ = m.update_state([0, 1, 0, 0], [1, 1, 0, 0])
  >>> m.result().numpy()
  2.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 0, 0], [1, 1, 0, 0], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.TrueNegatives()])
  ```
  """

  def __init__(self, thresholds=None, name=None, dtype=None):
    """Creates a `TrueNegatives` instance.

    Args:
      thresholds: (Optional) Defaults to 0.5. A float value or a python
        list/tuple of float threshold values in [0, 1]. A threshold is compared
        with prediction values to determine the truth value of predictions
        (i.e., above the threshold is `true`, below is `false`). One metric
        value is generated for each threshold value.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(TrueNegatives, self).__init__(
        confusion_matrix_cond=metrics_utils.ConfusionMatrix.TRUE_NEGATIVES,
        thresholds=thresholds,
        name=name,
        dtype=dtype)


@keras_export('keras.metrics.TruePositives')
class TruePositives(_ConfusionMatrixConditionCount):
  """Calculates the number of true positives.

  If `sample_weight` is given, calculates the sum of the weights of
  true positives. This metric creates one local variable, `true_positives`
  that is used to keep track of the number of true positives.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.TruePositives()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1])
  >>> m.result().numpy()
  2.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.TruePositives()])
  ```
  """

  def __init__(self, thresholds=None, name=None, dtype=None):
    """Creates a `TruePositives` instance.

    Args:
      thresholds: (Optional) Defaults to 0.5. A float value or a python
        list/tuple of float threshold values in [0, 1]. A threshold is compared
        with prediction values to determine the truth value of predictions
        (i.e., above the threshold is `true`, below is `false`). One metric
        value is generated for each threshold value.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(TruePositives, self).__init__(
        confusion_matrix_cond=metrics_utils.ConfusionMatrix.TRUE_POSITIVES,
        thresholds=thresholds,
        name=name,
        dtype=dtype)


@keras_export('keras.metrics.Precision')
class Precision(Metric):
  """Computes the precision of the predictions with respect to the labels.

  The metric creates two local variables, `true_positives` and `false_positives`
  that are used to compute the precision. This value is ultimately returned as
  `precision`, an idempotent operation that simply divides `true_positives`
  by the sum of `true_positives` and `false_positives`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  If `top_k` is set, we'll calculate precision as how often on average a class
  among the top-k classes with the highest predicted values of a batch entry is
  correct and can be found in the label for that entry.

  If `class_id` is specified, we calculate precision by considering only the
  entries in the batch for which `class_id` is above the threshold and/or in the
  top-k highest predictions, and computing the fraction of them for which
  `class_id` is indeed a correct label.

  Usage:

  >>> m = tf.keras.metrics.Precision()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1])
  >>> m.result().numpy()
  0.6666667

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.Precision()])
  ```
  """

  def __init__(self,
               thresholds=None,
               top_k=None,
               class_id=None,
               name=None,
               dtype=None):
    """Creates a `Precision` instance.

    Args:
      thresholds: (Optional) A float value or a python list/tuple of float
        threshold values in [0, 1]. A threshold is compared with prediction
        values to determine the truth value of predictions (i.e., above the
        threshold is `true`, below is `false`). One metric value is generated
        for each threshold value. If neither thresholds nor top_k are set, the
        default is to calculate precision with `thresholds=0.5`.
      top_k: (Optional) Unset by default. An int value specifying the top-k
        predictions to consider when calculating precision.
      class_id: (Optional) Integer class ID for which we want binary metrics.
        This must be in the half-open interval `[0, num_classes)`, where
        `num_classes` is the last dimension of predictions.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(Precision, self).__init__(name=name, dtype=dtype)
    self.init_thresholds = thresholds
    self.top_k = top_k
    self.class_id = class_id

    default_threshold = 0.5 if top_k is None else metrics_utils.NEG_INF
    self.thresholds = metrics_utils.parse_init_thresholds(
        thresholds, default_threshold=default_threshold)
    self.true_positives = self.add_weight(
        'true_positives',
        shape=(len(self.thresholds),),
        initializer=init_ops.zeros_initializer)
    self.false_positives = self.add_weight(
        'false_positives',
        shape=(len(self.thresholds),),
        initializer=init_ops.zeros_initializer)

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates true positive and false positive statistics.

    Args:
      y_true: The ground truth values, with the same dimensions as `y_pred`.
        Will be cast to `bool`.
      y_pred: The predicted values. Each element must be in the range `[0, 1]`.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    return metrics_utils.update_confusion_matrix_variables(
        {
            metrics_utils.ConfusionMatrix.TRUE_POSITIVES: self.true_positives,
            metrics_utils.ConfusionMatrix.FALSE_POSITIVES: self.false_positives
        },
        y_true,
        y_pred,
        thresholds=self.thresholds,
        top_k=self.top_k,
        class_id=self.class_id,
        sample_weight=sample_weight)

  def result(self):
    result = math_ops.div_no_nan(self.true_positives,
                                 self.true_positives + self.false_positives)
    return result[0] if len(self.thresholds) == 1 else result

  def reset_states(self):
    num_thresholds = len(to_list(self.thresholds))
    K.batch_set_value(
        [(v, np.zeros((num_thresholds,))) for v in self.variables])

  def get_config(self):
    config = {
        'thresholds': self.init_thresholds,
        'top_k': self.top_k,
        'class_id': self.class_id
    }
    base_config = super(Precision, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.Recall')
class Recall(Metric):
  """Computes the recall of the predictions with respect to the labels.

  This metric creates two local variables, `true_positives` and
  `false_negatives`, that are used to compute the recall. This value is
  ultimately returned as `recall`, an idempotent operation that simply divides
  `true_positives` by the sum of `true_positives` and `false_negatives`.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  If `top_k` is set, recall will be computed as how often on average a class
  among the labels of a batch entry is in the top-k predictions.

  If `class_id` is specified, we calculate recall by considering only the
  entries in the batch for which `class_id` is in the label, and computing the
  fraction of them for which `class_id` is above the threshold and/or in the
  top-k predictions.

  Usage:

  >>> m = tf.keras.metrics.Recall()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1])
  >>> m.result().numpy()
  0.6666667

  >>> m.reset_states()
  >>> _ = m.update_state([0, 1, 1, 1], [1, 0, 1, 1], sample_weight=[0, 0, 1, 0])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.Recall()])
  ```
  """

  def __init__(self,
               thresholds=None,
               top_k=None,
               class_id=None,
               name=None,
               dtype=None):
    """Creates a `Recall` instance.

    Args:
      thresholds: (Optional) A float value or a python list/tuple of float
        threshold values in [0, 1]. A threshold is compared with prediction
        values to determine the truth value of predictions (i.e., above the
        threshold is `true`, below is `false`). One metric value is generated
        for each threshold value. If neither thresholds nor top_k are set, the
        default is to calculate recall with `thresholds=0.5`.
      top_k: (Optional) Unset by default. An int value specifying the top-k
        predictions to consider when calculating recall.
      class_id: (Optional) Integer class ID for which we want binary metrics.
        This must be in the half-open interval `[0, num_classes)`, where
        `num_classes` is the last dimension of predictions.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(Recall, self).__init__(name=name, dtype=dtype)
    self.init_thresholds = thresholds
    self.top_k = top_k
    self.class_id = class_id

    default_threshold = 0.5 if top_k is None else metrics_utils.NEG_INF
    self.thresholds = metrics_utils.parse_init_thresholds(
        thresholds, default_threshold=default_threshold)
    self.true_positives = self.add_weight(
        'true_positives',
        shape=(len(self.thresholds),),
        initializer=init_ops.zeros_initializer)
    self.false_negatives = self.add_weight(
        'false_negatives',
        shape=(len(self.thresholds),),
        initializer=init_ops.zeros_initializer)

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates true positive and false negative statistics.

    Args:
      y_true: The ground truth values, with the same dimensions as `y_pred`.
        Will be cast to `bool`.
      y_pred: The predicted values. Each element must be in the range `[0, 1]`.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    return metrics_utils.update_confusion_matrix_variables(
        {
            metrics_utils.ConfusionMatrix.TRUE_POSITIVES: self.true_positives,
            metrics_utils.ConfusionMatrix.FALSE_NEGATIVES: self.false_negatives
        },
        y_true,
        y_pred,
        thresholds=self.thresholds,
        top_k=self.top_k,
        class_id=self.class_id,
        sample_weight=sample_weight)

  def result(self):
    result = math_ops.div_no_nan(self.true_positives,
                                 self.true_positives + self.false_negatives)
    return result[0] if len(self.thresholds) == 1 else result

  def reset_states(self):
    num_thresholds = len(to_list(self.thresholds))
    K.batch_set_value(
        [(v, np.zeros((num_thresholds,))) for v in self.variables])

  def get_config(self):
    config = {
        'thresholds': self.init_thresholds,
        'top_k': self.top_k,
        'class_id': self.class_id
    }
    base_config = super(Recall, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@six.add_metaclass(abc.ABCMeta)
class SensitivitySpecificityBase(Metric):
  """Abstract base class for computing sensitivity and specificity.

  For additional information about specificity and sensitivity, see the
  following: https://en.wikipedia.org/wiki/Sensitivity_and_specificity
  """

  def __init__(self, value, num_thresholds=200, name=None, dtype=None):
    super(SensitivitySpecificityBase, self).__init__(name=name, dtype=dtype)
    if num_thresholds <= 0:
      raise ValueError('`num_thresholds` must be > 0.')
    self.value = value
    self.true_positives = self.add_weight(
        'true_positives',
        shape=(num_thresholds,),
        initializer=init_ops.zeros_initializer)
    self.true_negatives = self.add_weight(
        'true_negatives',
        shape=(num_thresholds,),
        initializer=init_ops.zeros_initializer)
    self.false_positives = self.add_weight(
        'false_positives',
        shape=(num_thresholds,),
        initializer=init_ops.zeros_initializer)
    self.false_negatives = self.add_weight(
        'false_negatives',
        shape=(num_thresholds,),
        initializer=init_ops.zeros_initializer)

    # Compute `num_thresholds` thresholds in [0, 1]
    if num_thresholds == 1:
      self.thresholds = [0.5]
    else:
      thresholds = [(i + 1) * 1.0 / (num_thresholds - 1)
                    for i in range(num_thresholds - 2)]
      self.thresholds = [0.0] + thresholds + [1.0]

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates confusion matrix statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    return metrics_utils.update_confusion_matrix_variables(
        {
            metrics_utils.ConfusionMatrix.TRUE_POSITIVES: self.true_positives,
            metrics_utils.ConfusionMatrix.TRUE_NEGATIVES: self.true_negatives,
            metrics_utils.ConfusionMatrix.FALSE_POSITIVES: self.false_positives,
            metrics_utils.ConfusionMatrix.FALSE_NEGATIVES: self.false_negatives,
        },
        y_true,
        y_pred,
        thresholds=self.thresholds,
        sample_weight=sample_weight)

  def reset_states(self):
    num_thresholds = len(self.thresholds)
    K.batch_set_value(
        [(v, np.zeros((num_thresholds,))) for v in self.variables])


@keras_export('keras.metrics.SensitivityAtSpecificity')
class SensitivityAtSpecificity(SensitivitySpecificityBase):
  """Computes the sensitivity at a given specificity.

  `Sensitivity` measures the proportion of actual positives that are correctly
  identified as such (tp / (tp + fn)).
  `Specificity` measures the proportion of actual negatives that are correctly
  identified as such (tn / (tn + fp)).

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the
  sensitivity at the given specificity. The threshold for the given specificity
  value is computed and used to evaluate the corresponding sensitivity.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  For additional information about specificity and sensitivity, see the
  following: https://en.wikipedia.org/wiki/Sensitivity_and_specificity

  Usage:

  >>> m = tf.keras.metrics.SensitivityAtSpecificity(0.4, num_thresholds=1)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.SensitivityAtSpecificity()])
  ```
  """

  def __init__(self, specificity, num_thresholds=200, name=None, dtype=None):
    """Creates a `SensitivityAtSpecificity` instance.

    Args:
      specificity: A scalar value in range `[0, 1]`.
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use for matching the given specificity.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    if specificity < 0 or specificity > 1:
      raise ValueError('`specificity` must be in the range [0, 1].')
    self.specificity = specificity
    self.num_thresholds = num_thresholds
    super(SensitivityAtSpecificity, self).__init__(
        specificity, num_thresholds=num_thresholds, name=name, dtype=dtype)

  def result(self):
    # Calculate specificities at all the thresholds.
    specificities = math_ops.div_no_nan(
        self.true_negatives, self.true_negatives + self.false_positives)

    # Find the index of the threshold where the specificity is closest to the
    # given specificity.
    min_index = math_ops.argmin(
        math_ops.abs(specificities - self.value), axis=0)
    min_index = math_ops.cast(min_index, dtypes.int32)

    # Compute sensitivity at that index.
    return math_ops.div_no_nan(
        self.true_positives[min_index],
        self.true_positives[min_index] + self.false_negatives[min_index])

  def get_config(self):
    config = {
        'num_thresholds': self.num_thresholds,
        'specificity': self.specificity
    }
    base_config = super(SensitivityAtSpecificity, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.SpecificityAtSensitivity')
class SpecificityAtSensitivity(SensitivitySpecificityBase):
  """Computes the specificity at a given sensitivity.

  `Sensitivity` measures the proportion of actual positives that are correctly
  identified as such (tp / (tp + fn)).
  `Specificity` measures the proportion of actual negatives that are correctly
  identified as such (tn / (tn + fp)).

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the
  specificity at the given sensitivity. The threshold for the given sensitivity
  value is computed and used to evaluate the corresponding specificity.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  For additional information about specificity and sensitivity, see the
  following: https://en.wikipedia.org/wiki/Sensitivity_and_specificity

  Usage:

  >>> m = tf.keras.metrics.SpecificityAtSensitivity(0.8, num_thresholds=1)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> m.result().numpy()
  1.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.SpecificityAtSensitivity()])
  ```
  """

  def __init__(self, sensitivity, num_thresholds=200, name=None, dtype=None):
    """Creates a `SpecificityAtSensitivity` instance.

    Args:
      sensitivity: A scalar value in range `[0, 1]`.
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use for matching the given sensitivity.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    if sensitivity < 0 or sensitivity > 1:
      raise ValueError('`sensitivity` must be in the range [0, 1].')
    self.sensitivity = sensitivity
    self.num_thresholds = num_thresholds
    super(SpecificityAtSensitivity, self).__init__(
        sensitivity, num_thresholds=num_thresholds, name=name, dtype=dtype)

  def result(self):
    # Calculate sensitivities at all the thresholds.
    sensitivities = math_ops.div_no_nan(
        self.true_positives, self.true_positives + self.false_negatives)

    # Find the index of the threshold where the sensitivity is closest to the
    # requested value.
    min_index = math_ops.argmin(
        math_ops.abs(sensitivities - self.value), axis=0)
    min_index = math_ops.cast(min_index, dtypes.int32)

    # Compute specificity at that index.
    return math_ops.div_no_nan(
        self.true_negatives[min_index],
        self.true_negatives[min_index] + self.false_positives[min_index])

  def get_config(self):
    config = {
        'num_thresholds': self.num_thresholds,
        'sensitivity': self.sensitivity
    }
    base_config = super(SpecificityAtSensitivity, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.PrecisionAtRecall')
class PrecisionAtRecall(SensitivitySpecificityBase):
  """Computes the precision at a given recall.

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the
  precision at the given recall. The threshold for the given recall
  value is computed and used to evaluate the corresponding precision.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.PrecisionAtRecall(0.8, num_thresholds=1)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> m.result().numpy()
  1.0

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.PrecisionAtRecall(recall=0.8)])
  ```
  """

  def __init__(self, recall, num_thresholds=200, name=None, dtype=None):
    """Creates a `PrecisionAtRecall` instance.

    Args:
      recall: A scalar value in range `[0, 1]`.
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use for matching the given recall.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    if recall < 0 or recall > 1:
      raise ValueError('`recall` must be in the range [0, 1].')
    self.recall = recall
    self.num_thresholds = num_thresholds
    super(PrecisionAtRecall, self).__init__(
        value=recall,
        num_thresholds=num_thresholds,
        name=name,
        dtype=dtype)

  def result(self):
    # Calculate recall at all the thresholds.
    recalls = math_ops.div_no_nan(
        self.true_positives, self.true_positives + self.false_negatives)

    # Find the index of the threshold where the recall is closest to the
    # requested value.
    min_index = math_ops.argmin(
        math_ops.abs(recalls - self.value), axis=0)
    min_index = math_ops.cast(min_index, dtypes.int32)

    # Compute precision at that index.
    return math_ops.div_no_nan(
        self.true_positives[min_index],
        self.true_positives[min_index] + self.false_positives[min_index])

  def get_config(self):
    config = {'num_thresholds': self.num_thresholds, 'recall': self.recall}
    base_config = super(PrecisionAtRecall, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.RecallAtPrecision')
class RecallAtPrecision(SensitivitySpecificityBase):
  """Computes the maximally achievable recall at a required precision.

  For a given score-label-distribution the required precision might not
  be achievable, in this case 0.0 is returned as recall.

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the
  recall at the given precision. The threshold for the given precision
  value is computed and used to evaluate the corresponding recall.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.RecallAtPrecision(0.8, num_thresholds=1)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.RecallAtPrecision(precision=0.8)])
  ```
  """

  def __init__(self, precision, num_thresholds=200, name=None, dtype=None):
    """Creates a `RecallAtPrecision` instance.

    Args:
      precision: A scalar value in range `[0, 1]`.
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use for matching the given precision.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    if precision < 0 or precision > 1:
      raise ValueError('`precision` must be in the range [0, 1].')
    self.precision = precision
    self.num_thresholds = num_thresholds
    super(RecallAtPrecision, self).__init__(
        value=precision,
        num_thresholds=num_thresholds,
        name=name,
        dtype=dtype)

  def result(self):
    # Calculate precision and recall at all the thresholds.
    # All recalls are computed, because they are not a monotoneous function of
    # precision and we want to search for the highest feasible recall.
    precisions = math_ops.div_no_nan(
        self.true_positives, self.true_positives + self.false_positives)
    recalls = math_ops.div_no_nan(
        self.true_positives, self.true_positives + self.false_negatives)
    # Find best recall where the precision is as good as required.
    feasible = array_ops.where(math_ops.greater_equal(precisions, self.value))
    feasible_exists = math_ops.greater(array_ops.size(feasible), 0)
    best_recall = control_flow_ops.cond(
        feasible_exists,
        lambda: math_ops.reduce_max(array_ops.gather(recalls, feasible)),
        lambda: 0.0)
    return best_recall

  def get_config(self):
    config = {'num_thresholds': self.num_thresholds,
              'precision': self.precision}
    base_config = super(RecallAtPrecision, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.AUC')
class AUC(Metric):
  """Computes the approximate AUC (Area under the curve) via a Riemann sum.

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the AUC.
  To discretize the AUC curve, a linearly spaced set of thresholds is used to
  compute pairs of recall and precision values. The area under the ROC-curve is
  therefore computed using the height of the recall values by the false positive
  rate, while the area under the PR-curve is the computed using the height of
  the precision values by the recall.

  This value is ultimately returned as `auc`, an idempotent operation that
  computes the area under a discretized curve of precision versus recall values
  (computed using the aforementioned variables). The `num_thresholds` variable
  controls the degree of discretization with larger numbers of thresholds more
  closely approximating the true AUC. The quality of the approximation may vary
  dramatically depending on `num_thresholds`. The `thresholds` parameter can be
  used to manually specify thresholds which split the predictions more evenly.

  For best results, `predictions` should be distributed approximately uniformly
  in the range [0, 1] and not peaked around 0 or 1. The quality of the AUC
  approximation may be poor if this is not the case. Setting `summation_method`
  to 'minoring' or 'majoring' can help quantify the error in the approximation
  by providing lower or upper bound estimate of the AUC.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.AUC(num_thresholds=3)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> # threshold values are [0 - 1e-7, 0.5, 1 + 1e-7]
  >>> # tp = [2, 1, 0], fp = [2, 0, 0], fn = [0, 1, 2], tn = [0, 2, 2]
  >>> # recall = [1, 0.5, 0], fp_rate = [1, 0, 0]
  >>> # auc = ((((1+0.5)/2)*(1-0))+ (((0.5+0)/2)*(0-0))) = 0.75
  >>> m.result().numpy()
  0.75

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.AUC()])
  ```
  """

  def __init__(self,
               num_thresholds=200,
               curve='ROC',
               summation_method='interpolation',
               name=None,
               dtype=None,
               thresholds=None,
               multi_label=False,
               label_weights=None):
    """Creates an `AUC` instance.

    Args:
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use when discretizing the roc curve. Values must be > 1.
      curve: (Optional) Specifies the name of the curve to be computed, 'ROC'
        [default] or 'PR' for the Precision-Recall-curve.
      summation_method: (Optional) Specifies the Riemann summation method used
        (https://en.wikipedia.org/wiki/Riemann_sum): 'interpolation' [default],
          applies mid-point summation scheme for `ROC`. For PR-AUC, interpolates
          (true/false) positives but not the ratio that is precision (see Davis
          & Goadrich 2006 for details); 'minoring' that applies left summation
          for increasing intervals and right summation for decreasing intervals;
          'majoring' that does the opposite.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      thresholds: (Optional) A list of floating point values to use as the
        thresholds for discretizing the curve. If set, the `num_thresholds`
        parameter is ignored. Values should be in [0, 1]. Endpoint thresholds
        equal to {-epsilon, 1+epsilon} for a small positive epsilon value will
        be automatically included with these to correctly handle predictions
        equal to exactly 0 or 1.
      multi_label: boolean indicating whether multilabel data should be
        treated as such, wherein AUC is computed separately for each label and
        then averaged across labels, or (when False) if the data should be
        flattened into a single label before AUC computation. In the latter
        case, when multilabel data is passed to AUC, each label-prediction pair
        is treated as an individual data point. Should be set to False for
        multi-class data.
      label_weights: (optional) list, array, or tensor of non-negative weights
        used to compute AUCs for multilabel data. When `multi_label` is True,
        the weights are applied to the individual label AUCs when they are
        averaged to produce the multi-label AUC. When it's False, they are used
        to weight the individual label predictions in computing the confusion
        matrix on the flattened data. Note that this is unlike class_weights in
        that class_weights weights the example depending on the value of its
        label, whereas label_weights depends only on the index of that label
        before flattening; therefore `label_weights` should not be used for
        multi-class data.
    """
    # Validate configurations.
    if isinstance(curve, metrics_utils.AUCCurve) and curve not in list(
        metrics_utils.AUCCurve):
      raise ValueError('Invalid curve: "{}". Valid options are: "{}"'.format(
          curve, list(metrics_utils.AUCCurve)))
    if isinstance(
        summation_method,
        metrics_utils.AUCSummationMethod) and summation_method not in list(
            metrics_utils.AUCSummationMethod):
      raise ValueError(
          'Invalid summation method: "{}". Valid options are: "{}"'.format(
              summation_method, list(metrics_utils.AUCSummationMethod)))

    # Update properties.
    if thresholds is not None:
      # If specified, use the supplied thresholds.
      self.num_thresholds = len(thresholds) + 2
      thresholds = sorted(thresholds)
    else:
      if num_thresholds <= 1:
        raise ValueError('`num_thresholds` must be > 1.')

      # Otherwise, linearly interpolate (num_thresholds - 2) thresholds in
      # (0, 1).
      self.num_thresholds = num_thresholds
      thresholds = [(i + 1) * 1.0 / (num_thresholds - 1)
                    for i in range(num_thresholds - 2)]

    # Add an endpoint "threshold" below zero and above one for either
    # threshold method to account for floating point imprecisions.
    self.thresholds = [0.0 - K.epsilon()] + thresholds + [1.0 + K.epsilon()]

    if isinstance(curve, metrics_utils.AUCCurve):
      self.curve = curve
    else:
      self.curve = metrics_utils.AUCCurve.from_str(curve)
    if isinstance(summation_method, metrics_utils.AUCSummationMethod):
      self.summation_method = summation_method
    else:
      self.summation_method = metrics_utils.AUCSummationMethod.from_str(
          summation_method)
    super(AUC, self).__init__(name=name, dtype=dtype)

    # Handle multilabel arguments.
    self.multi_label = multi_label
    if label_weights is not None:
      label_weights = constant_op.constant(label_weights, dtype=self.dtype)
      checks = [
          check_ops.assert_non_negative(
              label_weights,
              message='All values of `label_weights` must be non-negative.')
      ]
      self.label_weights = control_flow_ops.with_dependencies(
          checks, label_weights)

    else:
      self.label_weights = None

    self._built = False
    if self.multi_label:
      self._num_labels = None
    else:
      self._build(None)

  def _build(self, shape):
    """Initialize TP, FP, TN, and FN tensors, given the shape of the data."""
    if self.multi_label:
      if shape.ndims != 2:
        raise ValueError('`y_true` must have rank=2 when `multi_label` is '
                         'True. Found rank %s.' % shape.ndims)
      self._num_labels = shape[1]
      variable_shape = tensor_shape.TensorShape(
          [tensor_shape.Dimension(self.num_thresholds), self._num_labels])

    else:
      variable_shape = tensor_shape.TensorShape(
          [tensor_shape.Dimension(self.num_thresholds)])
    self._build_input_shape = shape
    # Create metric variables
    self.true_positives = self.add_weight(
        'true_positives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.true_negatives = self.add_weight(
        'true_negatives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.false_positives = self.add_weight(
        'false_positives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.false_negatives = self.add_weight(
        'false_negatives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)

    if self.multi_label:
      with ops.init_scope():
        # This should only be necessary for handling v1 behavior. In v2, AUC
        # should be initialized outside of any tf.functions, and therefore in
        # eager mode.
        if not context.executing_eagerly():
          K._initialize_variables(K._get_session())  # pylint: disable=protected-access

    self._built = True

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates confusion matrix statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    deps = []
    if not self._built:
      self._build(tensor_shape.TensorShape(y_pred.shape))

    if self.multi_label or (self.label_weights is not None):
      # y_true should have shape (number of examples, number of labels).
      shapes = [
          (y_true, ('N', 'L'))
      ]
      if self.multi_label:
        # TP, TN, FP, and FN should all have shape
        # (number of thresholds, number of labels).
        shapes.extend([(self.true_positives, ('T', 'L')),
                       (self.true_negatives, ('T', 'L')),
                       (self.false_positives, ('T', 'L')),
                       (self.false_negatives, ('T', 'L'))])
      if self.label_weights is not None:
        # label_weights should be of length equal to the number of labels.
        shapes.append((self.label_weights, ('L',)))
      deps = [
          check_ops.assert_shapes(
              shapes, message='Number of labels is not consistent.')
      ]

    # Only forward label_weights to update_confusion_matrix_variables when
    # multi_label is False. Otherwise the averaging of individual label AUCs is
    # handled in AUC.result
    label_weights = None if self.multi_label else self.label_weights
    with ops.control_dependencies(deps):
      return metrics_utils.update_confusion_matrix_variables(
          {
              metrics_utils.ConfusionMatrix.TRUE_POSITIVES:
                  self.true_positives,
              metrics_utils.ConfusionMatrix.TRUE_NEGATIVES:
                  self.true_negatives,
              metrics_utils.ConfusionMatrix.FALSE_POSITIVES:
                  self.false_positives,
              metrics_utils.ConfusionMatrix.FALSE_NEGATIVES:
                  self.false_negatives,
          },
          y_true,
          y_pred,
          self.thresholds,
          sample_weight=sample_weight,
          multi_label=self.multi_label,
          label_weights=label_weights)

  def interpolate_pr_auc(self):
    """Interpolation formula inspired by section 4 of Davis & Goadrich 2006.

    https://www.biostat.wisc.edu/~page/rocpr.pdf

    Note here we derive & use a closed formula not present in the paper
    as follows:

      Precision = TP / (TP + FP) = TP / P

    Modeling all of TP (true positive), FP (false positive) and their sum
    P = TP + FP (predicted positive) as varying linearly within each interval
    [A, B] between successive thresholds, we get

      Precision slope = dTP / dP
                      = (TP_B - TP_A) / (P_B - P_A)
                      = (TP - TP_A) / (P - P_A)
      Precision = (TP_A + slope * (P - P_A)) / P

    The area within the interval is (slope / total_pos_weight) times

      int_A^B{Precision.dP} = int_A^B{(TP_A + slope * (P - P_A)) * dP / P}
      int_A^B{Precision.dP} = int_A^B{slope * dP + intercept * dP / P}

    where intercept = TP_A - slope * P_A = TP_B - slope * P_B, resulting in

      int_A^B{Precision.dP} = TP_B - TP_A + intercept * log(P_B / P_A)

    Bringing back the factor (slope / total_pos_weight) we'd put aside, we get

      slope * [dTP + intercept *  log(P_B / P_A)] / total_pos_weight

    where dTP == TP_B - TP_A.

    Note that when P_A == 0 the above calculation simplifies into

      int_A^B{Precision.dTP} = int_A^B{slope * dTP} = slope * (TP_B - TP_A)

    which is really equivalent to imputing constant precision throughout the
    first bucket having >0 true positives.

    Returns:
      pr_auc: an approximation of the area under the P-R curve.
    """
    dtp = self.true_positives[:self.num_thresholds -
                              1] - self.true_positives[1:]
    p = self.true_positives + self.false_positives
    dp = p[:self.num_thresholds - 1] - p[1:]
    prec_slope = math_ops.div_no_nan(
        dtp, math_ops.maximum(dp, 0), name='prec_slope')
    intercept = self.true_positives[1:] - math_ops.multiply(prec_slope, p[1:])

    safe_p_ratio = array_ops.where(
        math_ops.logical_and(p[:self.num_thresholds - 1] > 0, p[1:] > 0),
        math_ops.div_no_nan(
            p[:self.num_thresholds - 1],
            math_ops.maximum(p[1:], 0),
            name='recall_relative_ratio'),
        array_ops.ones_like(p[1:]))

    pr_auc_increment = math_ops.div_no_nan(
        prec_slope * (dtp + intercept * math_ops.log(safe_p_ratio)),
        math_ops.maximum(self.true_positives[1:] + self.false_negatives[1:], 0),
        name='pr_auc_increment')

    if self.multi_label:
      by_label_auc = math_ops.reduce_sum(
          pr_auc_increment, name=self.name + '_by_label', axis=0)
      if self.label_weights is None:
        # Evenly weighted average of the label AUCs.
        return math_ops.reduce_mean(by_label_auc, name=self.name)
      else:
        # Weighted average of the label AUCs.
        return math_ops.div_no_nan(
            math_ops.reduce_sum(
                math_ops.multiply(by_label_auc, self.label_weights)),
            math_ops.reduce_sum(self.label_weights),
            name=self.name)
    else:
      return math_ops.reduce_sum(pr_auc_increment, name='interpolate_pr_auc')

  def result(self):
    if (self.curve == metrics_utils.AUCCurve.PR and
        self.summation_method == metrics_utils.AUCSummationMethod.INTERPOLATION
       ):
      # This use case is different and is handled separately.
      return self.interpolate_pr_auc()

    # Set `x` and `y` values for the curves based on `curve` config.
    recall = math_ops.div_no_nan(self.true_positives,
                                 self.true_positives + self.false_negatives)
    if self.curve == metrics_utils.AUCCurve.ROC:
      fp_rate = math_ops.div_no_nan(self.false_positives,
                                    self.false_positives + self.true_negatives)
      x = fp_rate
      y = recall
    else:  # curve == 'PR'.
      precision = math_ops.div_no_nan(
          self.true_positives, self.true_positives + self.false_positives)
      x = recall
      y = precision

    # Find the rectangle heights based on `summation_method`.
    if self.summation_method == metrics_utils.AUCSummationMethod.INTERPOLATION:
      # Note: the case ('PR', 'interpolation') has been handled above.
      heights = (y[:self.num_thresholds - 1] + y[1:]) / 2.
    elif self.summation_method == metrics_utils.AUCSummationMethod.MINORING:
      heights = math_ops.minimum(y[:self.num_thresholds - 1], y[1:])
    else:  # self.summation_method = metrics_utils.AUCSummationMethod.MAJORING:
      heights = math_ops.maximum(y[:self.num_thresholds - 1], y[1:])

    # Sum up the areas of all the rectangles.
    if self.multi_label:
      riemann_terms = math_ops.multiply(x[:self.num_thresholds - 1] - x[1:],
                                        heights)
      by_label_auc = math_ops.reduce_sum(
          riemann_terms, name=self.name + '_by_label', axis=0)

      if self.label_weights is None:
        # Unweighted average of the label AUCs.
        return math_ops.reduce_mean(by_label_auc, name=self.name)
      else:
        # Weighted average of the label AUCs.
        return math_ops.div_no_nan(
            math_ops.reduce_sum(
                math_ops.multiply(by_label_auc, self.label_weights)),
            math_ops.reduce_sum(self.label_weights),
            name=self.name)
    else:
      return math_ops.reduce_sum(
          math_ops.multiply(x[:self.num_thresholds - 1] - x[1:], heights),
          name=self.name)

  def reset_states(self):
    if self.multi_label:
      K.batch_set_value([(v, np.zeros((self.num_thresholds, self._num_labels)))
                         for v in self.variables])
    else:
      K.batch_set_value([
          (v, np.zeros((self.num_thresholds,))) for v in self.variables
      ])

  def get_config(self):
    if is_tensor_or_variable(self.label_weights):
      label_weights = K.eval(self.label_weights)
    else:
      label_weights = self.label_weights
    config = {
        'num_thresholds': self.num_thresholds,
        'curve': self.curve.value,
        'summation_method': self.summation_method.value,
        # We remove the endpoint thresholds as an inverse of how the thresholds
        # were initialized. This ensures that a metric initialized from this
        # config has the same thresholds.
        'thresholds': self.thresholds[1:-1],
        'multi_label': self.multi_label,
        'label_weights': label_weights
    }
    base_config = super(AUC, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.AUC0')
class AUC0(Metric):
  """Computes the approximate AUC0 (Area under the curve) via a Riemann sum.

  This metric creates four local variables, `true_positives`, `true_negatives`,
  `false_positives` and `false_negatives` that are used to compute the AUC0.
  To discretize the AUC0 curve, a linearly spaced set of thresholds is used to
  compute pairs of recall and precision values. The area under the ROC-curve is
  therefore computed using the height of the recall values by the false positive
  rate, while the area under the PR-curve is the computed using the height of
  the precision values by the recall.

  This value is ultimately returned as `auc0`, an idempotent operation that
  computes the area under a discretized curve of precision versus recall values
  (computed using the aforementioned variables). The `num_thresholds` variable
  controls the degree of discretization with larger numbers of thresholds more
  closely approximating the true AUC0. The quality of the approximation may vary
  dramatically depending on `num_thresholds`. The `thresholds` parameter can be
  used to manually specify thresholds which split the predictions more evenly.

  For best results, `predictions` should be distributed approximately uniformly
  in the range [0, 1] and not peaked around 0 or 1. The quality of the AUC0
  approximation may be poor if this is not the case. Setting `summation_method`
  to 'minoring' or 'majoring' can help quantify the error in the approximation
  by providing lower or upper bound estimate of the AUC0.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> m = tf.keras.metrics.AUC0(num_thresholds=3)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9])
  >>> # threshold values are [0 - 1e-7, 0.5, 1 + 1e-7]
  >>> # tp = [2, 1, 0], fp = [2, 0, 0], fn = [0, 1, 2], tn = [0, 2, 2]
  >>> # recall = [1, 0.5, 0], fp_rate = [1, 0, 0]
  >>> # auc0 = ((((1+0.5)/2)*(1-0))+ (((0.5+0)/2)*(0-0))) = 0.75
  >>> m.result().numpy()
  0.75

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 0.5, 0.3, 0.9],
  ...                    sample_weight=[1, 0, 0, 1])
  >>> m.result().numpy()
  1.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.AUC0()])
  ```
  """

  def __init__(self,
               num_thresholds=200,
               curve='ROC',
               summation_method='interpolation',
               name=None,
               dtype=None,
               thresholds=None,
               multi_label=False,
               label_weights=None):
    """Creates an `AUC0` instance.

    Args:
      num_thresholds: (Optional) Defaults to 200. The number of thresholds to
        use when discretizing the roc curve. Values must be > 1.
      curve: (Optional) Specifies the name of the curve to be computed, 'ROC'
        [default] or 'PR' for the Precision-Recall-curve.
      summation_method: (Optional) Specifies the Riemann summation method used
        (https://en.wikipedia.org/wiki/Riemann_sum): 'interpolation' [default],
          applies mid-point summation scheme for `ROC`. For PR-AUC0, interpolates
          (true/false) positives but not the ratio that is precision (see Davis
          & Goadrich 2006 for details); 'minoring' that applies left summation
          for increasing intervals and right summation for decreasing intervals;
          'majoring' that does the opposite.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      thresholds: (Optional) A list of floating point values to use as the
        thresholds for discretizing the curve. If set, the `num_thresholds`
        parameter is ignored. Values should be in [0, 1]. Endpoint thresholds
        equal to {-epsilon, 1+epsilon} for a small positive epsilon value will
        be automatically included with these to correctly handle predictions
        equal to exactly 0 or 1.
      multi_label: boolean indicating whether multilabel data should be
        treated as such, wherein AUC0 is computed separately for each label and
        then averaged across labels, or (when False) if the data should be
        flattened into a single label before AUC0 computation. In the latter
        case, when multilabel data is passed to AUC0, each label-prediction pair
        is treated as an individual data point. Should be set to False for
        multi-class data.
      label_weights: (optional) list, array, or tensor of non-negative weights
        used to compute AUCs for multilabel data. When `multi_label` is True,
        the weights are applied to the individual label AUCs when they are
        averaged to produce the multi-label AUC0. When it's False, they are used
        to weight the individual label predictions in computing the confusion
        matrix on the flattened data. Note that this is unlike class_weights in
        that class_weights weights the example depending on the value of its
        label, whereas label_weights depends only on the index of that label
        before flattening; therefore `label_weights` should not be used for
        multi-class data.
    """
    # Validate configurations.
    if isinstance(curve, metrics_utils.AUCCurve) and curve not in list(
            metrics_utils.AUCCurve):
      raise ValueError('Invalid curve: "{}". Valid options are: "{}"'.format(
          curve, list(metrics_utils.AUCCurve)))
    if isinstance(
        summation_method,
        metrics_utils.AUCSummationMethod) and summation_method not in list(
            metrics_utils.AUCSummationMethod):
      raise ValueError(
          'Invalid summation method: "{}". Valid options are: "{}"'.format(
              summation_method, list(metrics_utils.AUCSummationMethod)))

    # Update properties.
    if thresholds is not None:
      # If specified, use the supplied thresholds.
      self.num_thresholds = len(thresholds) + 2
      thresholds = sorted(thresholds)
    else:
      if num_thresholds <= 1:
        raise ValueError('`num_thresholds` must be > 1.')

      # Otherwise, linearly interpolate (num_thresholds - 2) thresholds in
      # (0, 1).
      self.num_thresholds = num_thresholds
      thresholds = [(i + 1) * 1.0 / (num_thresholds - 1)
                    for i in range(num_thresholds - 2)]

    # Add an endpoint "threshold" below zero and above one for either
    # threshold method to account for floating point imprecisions.
    self.thresholds = [0.0 - K.epsilon()] + thresholds + [1.0 + K.epsilon()]

    if isinstance(curve, metrics_utils.AUCCurve):
      self.curve = curve
    else:
      self.curve = metrics_utils.AUCCurve.from_str(curve)
    if isinstance(summation_method, metrics_utils.AUCSummationMethod):
      self.summation_method = summation_method
    else:
      self.summation_method = metrics_utils.AUCSummationMethod.from_str(
          summation_method)
    super(AUC0, self).__init__(name=name, dtype=dtype)

    # Handle multilabel arguments.
    self.multi_label = multi_label
    if label_weights is not None:
      label_weights = constant_op.constant(label_weights, dtype=self.dtype)
      checks = [
          check_ops.assert_non_negative(
              label_weights,
              message='All values of `label_weights` must be non-negative.')
      ]
      self.label_weights = control_flow_ops.with_dependencies(
          checks, label_weights)

    else:
      self.label_weights = None

    self._built = False
    if self.multi_label:
      self._num_labels = None
    else:
      self._build(None)

  def _build(self, shape):
    """Initialize TP, FP, TN, and FN tensors, given the shape of the data."""
    if self.multi_label:
      if shape.ndims != 2:
        raise ValueError('`y_true` must have rank=2 when `multi_label` is '
                         'True. Found rank %s.' % shape.ndims)
      self._num_labels = shape[1]
      variable_shape = tensor_shape.TensorShape(
          [tensor_shape.Dimension(self.num_thresholds), self._num_labels])

    else:
      variable_shape = tensor_shape.TensorShape(
          [tensor_shape.Dimension(self.num_thresholds)])
    self._build_input_shape = shape
    # Create metric variables
    self.true_positives = self.add_weight(
        'true_positives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.true_negatives = self.add_weight(
        'true_negatives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.false_positives = self.add_weight(
        'false_positives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)
    self.false_negatives = self.add_weight(
        'false_negatives',
        shape=variable_shape,
        initializer=init_ops.zeros_initializer)

    if self.multi_label:
      with ops.init_scope():
        # This should only be necessary for handling v1 behavior. In v2, AUC0
        # should be initialized outside of any tf.functions, and therefore in
        # eager mode.
        if not context.executing_eagerly():
          K._initialize_variables(
              K._get_session())  # pylint: disable=protected-access

    self._built = True

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates confusion matrix statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    deps = []
    if not self._built:
      self._build(tensor_shape.TensorShape(y_pred.shape))

    if self.multi_label or (self.label_weights is not None):
      # y_true should have shape (number of examples, number of labels).
      shapes = [
          (y_true, ('N', 'L'))
      ]
      if self.multi_label:
        # TP, TN, FP, and FN should all have shape
        # (number of thresholds, number of labels).
        shapes.extend([(self.true_positives, ('T', 'L')),
                       (self.true_negatives, ('T', 'L')),
                       (self.false_positives, ('T', 'L')),
                       (self.false_negatives, ('T', 'L'))])
      if self.label_weights is not None:
        # label_weights should be of length equal to the number of labels.
        shapes.append((self.label_weights, ('L',)))
      deps = [
          check_ops.assert_shapes(
              shapes, message='Number of labels is not consistent.')
      ]

    # Only forward label_weights to update_confusion_matrix_variables when
    # multi_label is False. Otherwise the averaging of individual label AUCs is
    # handled in AUC0.result
    label_weights = None if self.multi_label else self.label_weights
    with ops.control_dependencies(deps):
      return metrics_utils.update_confusion_matrix_variables(
          {
              metrics_utils.ConfusionMatrix.TRUE_POSITIVES:
                  self.true_positives,
              metrics_utils.ConfusionMatrix.TRUE_NEGATIVES:
                  self.true_negatives,
              metrics_utils.ConfusionMatrix.FALSE_POSITIVES:
                  self.false_positives,
              metrics_utils.ConfusionMatrix.FALSE_NEGATIVES:
                  self.false_negatives,
          },
          1-y_true,
          1-y_pred,
          self.thresholds,
          sample_weight=sample_weight,
          multi_label=self.multi_label,
          label_weights=label_weights)

  def interpolate_pr_auc(self):
    """Interpolation formula inspired by section 4 of Davis & Goadrich 2006.

    https://www.biostat.wisc.edu/~page/rocpr.pdf

    Note here we derive & use a closed formula not present in the paper
    as follows:

      Precision = TP / (TP + FP) = TP / P

    Modeling all of TP (true positive), FP (false positive) and their sum
    P = TP + FP (predicted positive) as varying linearly within each interval
    [A, B] between successive thresholds, we get

      Precision slope = dTP / dP
                      = (TP_B - TP_A) / (P_B - P_A)
                      = (TP - TP_A) / (P - P_A)
      Precision = (TP_A + slope * (P - P_A)) / P

    The area within the interval is (slope / total_pos_weight) times

      int_A^B{Precision.dP} = int_A^B{(TP_A + slope * (P - P_A)) * dP / P}
      int_A^B{Precision.dP} = int_A^B{slope * dP + intercept * dP / P}

    where intercept = TP_A - slope * P_A = TP_B - slope * P_B, resulting in

      int_A^B{Precision.dP} = TP_B - TP_A + intercept * log(P_B / P_A)

    Bringing back the factor (slope / total_pos_weight) we'd put aside, we get

      slope * [dTP + intercept *  log(P_B / P_A)] / total_pos_weight

    where dTP == TP_B - TP_A.

    Note that when P_A == 0 the above calculation simplifies into

      int_A^B{Precision.dTP} = int_A^B{slope * dTP} = slope * (TP_B - TP_A)

    which is really equivalent to imputing constant precision throughout the
    first bucket having >0 true positives.

    Returns:
      pr_auc: an approximation of the area under the P-R curve.
    """
    dtp = self.true_positives[:self.num_thresholds -
                              1] - self.true_positives[1:]
    p = self.true_positives + self.false_positives
    dp = p[:self.num_thresholds - 1] - p[1:]
    prec_slope = math_ops.div_no_nan(
        dtp, math_ops.maximum(dp, 0), name='prec_slope')
    intercept = self.true_positives[1:] - math_ops.multiply(prec_slope, p[1:])

    safe_p_ratio = array_ops.where(
        math_ops.logical_and(p[:self.num_thresholds - 1] > 0, p[1:] > 0),
        math_ops.div_no_nan(
            p[:self.num_thresholds - 1],
            math_ops.maximum(p[1:], 0),
            name='recall_relative_ratio'),
        array_ops.ones_like(p[1:]))

    pr_auc_increment = math_ops.div_no_nan(
        prec_slope * (dtp + intercept * math_ops.log(safe_p_ratio)),
        math_ops.maximum(
            self.true_positives[1:] + self.false_negatives[1:], 0),
        name='pr_auc_increment')

    if self.multi_label:
      by_label_auc = math_ops.reduce_sum(
          pr_auc_increment, name=self.name + '_by_label', axis=0)
      if self.label_weights is None:
        # Evenly weighted average of the label AUCs.
        return math_ops.reduce_mean(by_label_auc, name=self.name)
      else:
        # Weighted average of the label AUCs.
        return math_ops.div_no_nan(
            math_ops.reduce_sum(
                math_ops.multiply(by_label_auc, self.label_weights)),
            math_ops.reduce_sum(self.label_weights),
            name=self.name)
    else:
      return math_ops.reduce_sum(pr_auc_increment, name='interpolate_pr_auc')

  def result(self):
    if (self.curve == metrics_utils.AUCCurve.PR and
            self.summation_method == metrics_utils.AUCSummationMethod.INTERPOLATION
            ):
      # This use case is different and is handled separately.
      return self.interpolate_pr_auc()

    # Set `x` and `y` values for the curves based on `curve` config.
    recall = math_ops.div_no_nan(self.true_positives,
                                 self.true_positives + self.false_negatives)
    if self.curve == metrics_utils.AUCCurve.ROC:
      fp_rate = math_ops.div_no_nan(self.false_positives,
                                    self.false_positives + self.true_negatives)
      x = fp_rate
      y = recall
    else:  # curve == 'PR'.
      precision = math_ops.div_no_nan(
          self.true_positives, self.true_positives + self.false_positives)
      x = recall
      y = precision

    # Find the rectangle heights based on `summation_method`.
    if self.summation_method == metrics_utils.AUCSummationMethod.INTERPOLATION:
      # Note: the case ('PR', 'interpolation') has been handled above.
      heights = (y[:self.num_thresholds - 1] + y[1:]) / 2.
    elif self.summation_method == metrics_utils.AUCSummationMethod.MINORING:
      heights = math_ops.minimum(y[:self.num_thresholds - 1], y[1:])
    else:  # self.summation_method = metrics_utils.AUCSummationMethod.MAJORING:
      heights = math_ops.maximum(y[:self.num_thresholds - 1], y[1:])

    # Sum up the areas of all the rectangles.
    if self.multi_label:
      riemann_terms = math_ops.multiply(x[:self.num_thresholds - 1] - x[1:],
                                        heights)
      by_label_auc = math_ops.reduce_sum(
          riemann_terms, name=self.name + '_by_label', axis=0)

      if self.label_weights is None:
        # Unweighted average of the label AUCs.
        return math_ops.reduce_mean(by_label_auc, name=self.name)
      else:
        # Weighted average of the label AUCs.
        return math_ops.div_no_nan(
            math_ops.reduce_sum(
                math_ops.multiply(by_label_auc, self.label_weights)),
            math_ops.reduce_sum(self.label_weights),
            name=self.name)
    else:
      return math_ops.reduce_sum(
          math_ops.multiply(x[:self.num_thresholds - 1] - x[1:], heights),
          name=self.name)

  def reset_states(self):
    if self.multi_label:
      K.batch_set_value([(v, np.zeros((self.num_thresholds, self._num_labels)))
                         for v in self.variables])
    else:
      K.batch_set_value([
          (v, np.zeros((self.num_thresholds,))) for v in self.variables
      ])

  def get_config(self):
    if is_tensor_or_variable(self.label_weights):
      label_weights = K.eval(self.label_weights)
    else:
      label_weights = self.label_weights
    config = {
        'num_thresholds': self.num_thresholds,
        'curve': self.curve.value,
        'summation_method': self.summation_method.value,
        # We remove the endpoint thresholds as an inverse of how the thresholds
        # were initialized. This ensures that a metric initialized from this
        # config has the same thresholds.
        'thresholds': self.thresholds[1:-1],
        'multi_label': self.multi_label,
        'label_weights': label_weights
    }
    base_config = super(AUC0, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.CosineSimilarity')
class CosineSimilarity(MeanMetricWrapper):
  """Computes the cosine similarity between the labels and predictions.

  cosine similarity = (a . b) / ||a|| ||b||
  [Cosine Similarity](https://en.wikipedia.org/wiki/Cosine_similarity)

  This metric keeps the average cosine similarity between `predictions` and
  `labels` over a stream of data.

  Usage:

  >>> # l2_norm(y_true) = [[0., 1.], [1./1.414], 1./1.414]]]
  >>> # l2_norm(y_pred) = [[1., 0.], [1./1.414], 1./1.414]]]
  >>> # l2_norm(y_true) . l2_norm(y_pred) = [[0., 0.], [0.5, 0.5]]
  >>> # result = mean(sum(l2_norm(y_true) . l2_norm(y_pred), axis=1))
  >>> #        = ((0. + 0.) +  (0.5 + 0.5)) / 2
  >>> m = tf.keras.metrics.CosineSimilarity(axis=1)
  >>> _ = m.update_state([[0., 1.], [1., 1.]], [[1., 0.], [1., 1.]])
  >>> m.result().numpy()
  0.49999997

  >>> m.reset_states()
  >>> _ = m.update_state([[0., 1.], [1., 1.]], [[1., 0.], [1., 1.]],
  ...                    sample_weight=[0.3, 0.7])
  >>> m.result().numpy()
  0.6999999

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.CosineSimilarity(axis=1)])
  ```
  """

  def __init__(self, name='cosine_similarity', dtype=None, axis=-1):
    """Creates a `CosineSimilarity` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      axis: (Optional) Defaults to -1. The dimension along which the cosine
        similarity is computed.
    """
    super(CosineSimilarity, self).__init__(
        cosine_similarity, name, dtype=dtype, axis=axis)


@keras_export('keras.metrics.MeanAbsoluteError')
class MeanAbsoluteError(MeanMetricWrapper):
  """Computes the mean absolute error between the labels and predictions.

  Usage:

  >>> m = tf.keras.metrics.MeanAbsoluteError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.25

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.5

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd', loss='mse', metrics=[tf.keras.metrics.MeanAbsoluteError()])
  ```
  """

  def __init__(self, name='mean_absolute_error', dtype=None):
    super(MeanAbsoluteError, self).__init__(
        mean_absolute_error, name, dtype=dtype)


@keras_export('keras.metrics.MeanAbsolutePercentageError')
class MeanAbsolutePercentageError(MeanMetricWrapper):
  """Computes the mean absolute percentage error between `y_true` and `y_pred`.

  Usage:

  >>> m = tf.keras.metrics.MeanAbsolutePercentageError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  250000000.0

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  500000000.0

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.MeanAbsolutePercentageError()])
  ```
  """

  def __init__(self, name='mean_absolute_percentage_error', dtype=None):
    super(MeanAbsolutePercentageError, self).__init__(
        mean_absolute_percentage_error, name, dtype=dtype)


@keras_export('keras.metrics.MeanSquaredError')
class MeanSquaredError(MeanMetricWrapper):
  """Computes the mean squared error between `y_true` and `y_pred`.

  Usage:

  >>> m = tf.keras.metrics.MeanSquaredError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.25

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.5

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd', loss='mse', metrics=[tf.keras.metrics.MeanSquaredError()])
  ```
  """

  def __init__(self, name='mean_squared_error', dtype=None):
    super(MeanSquaredError, self).__init__(
        mean_squared_error, name, dtype=dtype)


@keras_export('keras.metrics.MeanSquaredLogarithmicError')
class MeanSquaredLogarithmicError(MeanMetricWrapper):
  """Computes the mean squared logarithmic error between `y_true` and `y_pred`.

  Usage:

  >>> m = tf.keras.metrics.MeanSquaredLogarithmicError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.12011322

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.24022643

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.MeanSquaredLogarithmicError()])
  ```
  """

  def __init__(self, name='mean_squared_logarithmic_error', dtype=None):
    super(MeanSquaredLogarithmicError, self).__init__(
        mean_squared_logarithmic_error, name, dtype=dtype)


@keras_export('keras.metrics.Hinge')
class Hinge(MeanMetricWrapper):
  """Computes the hinge metric between `y_true` and `y_pred`.

  `y_true` values are expected to be -1 or 1. If binary (0 or 1) labels are
  provided we will convert them to -1 or 1.

  Usage:

  >>> m = tf.keras.metrics.Hinge()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]])
  >>> m.result().numpy()
  1.3

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  1.1

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.Hinge()])
  ```
  """

  def __init__(self, name='hinge', dtype=None):
    super(Hinge, self).__init__(hinge, name, dtype=dtype)


@keras_export('keras.metrics.SquaredHinge')
class SquaredHinge(MeanMetricWrapper):
  """Computes the squared hinge metric between `y_true` and `y_pred`.

  `y_true` values are expected to be -1 or 1. If binary (0 or 1) labels are
  provided we will convert them to -1 or 1.

  Usage:

  >>> m = tf.keras.metrics.SquaredHinge()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]])
  >>> m.result().numpy()
  1.86

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  1.46

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.SquaredHinge()])
  ```
  """

  def __init__(self, name='squared_hinge', dtype=None):
    super(SquaredHinge, self).__init__(squared_hinge, name, dtype=dtype)


@keras_export('keras.metrics.CategoricalHinge')
class CategoricalHinge(MeanMetricWrapper):
  """Computes the categorical hinge metric between `y_true` and `y_pred`.

  Usage:

  >>> m = tf.keras.metrics.CategoricalHinge()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]])
  >>> m.result().numpy()
  1.4000001

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  1.2

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.CategoricalHinge()])
  ```
  """

  def __init__(self, name='categorical_hinge', dtype=None):
    super(CategoricalHinge, self).__init__(categorical_hinge, name, dtype=dtype)


@keras_export('keras.metrics.RootMeanSquaredError')
class RootMeanSquaredError(Mean):
  """Computes root mean squared error metric between `y_true` and `y_pred`.

  Usage:

  >>> m = tf.keras.metrics.RootMeanSquaredError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.5

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.70710677

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.RootMeanSquaredError()])
  ```
  """

  def __init__(self, name='root_mean_squared_error', dtype=None):
    super(RootMeanSquaredError, self).__init__(name, dtype=dtype)

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates root mean squared error statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """
    y_true = math_ops.cast(y_true, self._dtype)
    y_pred = math_ops.cast(y_pred, self._dtype)
    y_pred, y_true = tf_losses_utils.squeeze_or_expand_dimensions(
        y_pred, y_true)
    error_sq = math_ops.squared_difference(y_pred, y_true)
    return super(RootMeanSquaredError, self).update_state(
        error_sq, sample_weight=sample_weight)

  def result(self):
    return math_ops.sqrt(math_ops.div_no_nan(self.total, self.count))


@keras_export('keras.metrics.LogCoshError')
class LogCoshError(MeanMetricWrapper):
  """Computes the logarithm of the hyperbolic cosine of the prediction error.

  `logcosh = log((exp(x) + exp(-x))/2)`, where x is the error (y_pred - y_true)

  Usage:

  >>> m = tf.keras.metrics.LogCoshError()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.10844523

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.21689045

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.LogCoshError()])
  ```
  """

  def __init__(self, name='logcosh', dtype=None):
    super(LogCoshError, self).__init__(logcosh, name, dtype=dtype)


@keras_export('keras.metrics.Poisson')
class Poisson(MeanMetricWrapper):
  """Computes the Poisson metric between `y_true` and `y_pred`.

  `metric = y_pred - y_true * log(y_pred)`

  Usage:

  >>> m = tf.keras.metrics.Poisson()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]])
  >>> m.result().numpy()
  0.49999997

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[1, 1], [0, 0]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.99999994

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.Poisson()])
  ```
  """

  def __init__(self, name='poisson', dtype=None):
    super(Poisson, self).__init__(poisson, name, dtype=dtype)


@keras_export('keras.metrics.KLDivergence')
class KLDivergence(MeanMetricWrapper):
  """Computes Kullback-Leibler divergence metric between `y_true` and `y_pred`.

  `metric = y_true * log(y_true / y_pred)`

  Usage:

  >>> m = tf.keras.metrics.KLDivergence()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]])
  >>> m.result().numpy()
  0.45814306

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.9162892

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile('sgd', loss='mse', metrics=[tf.keras.metrics.KLDivergence()])
  ```
  """

  def __init__(self, name='kullback_leibler_divergence', dtype=None):
    super(KLDivergence, self).__init__(
        kullback_leibler_divergence, name, dtype=dtype)


@keras_export('keras.metrics.MeanIoU')
class MeanIoU(Metric):
  """Computes the mean Intersection-Over-Union metric.

  Mean Intersection-Over-Union is a common evaluation metric for semantic image
  segmentation, which first computes the IOU for each semantic class and then
  computes the average over classes. IOU is defined as follows:
    IOU = true_positive / (true_positive + false_positive + false_negative).
  The predictions are accumulated in a confusion matrix, weighted by
  `sample_weight` and the metric is then calculated from it.

  If `sample_weight` is `None`, weights default to 1.
  Use `sample_weight` of 0 to mask values.

  Usage:

  >>> # cm = [[1, 1],
  >>> #        [1, 1]]
  >>> # sum_row = [2, 2], sum_col = [2, 2], true_positives = [1, 1]
  >>> # iou = true_positives / (sum_row + sum_col - true_positives))
  >>> # result = (1 / (2 + 2 - 1) + 1 / (2 + 2 - 1)) / 2 = 0.33
  >>> m = tf.keras.metrics.MeanIoU(num_classes=2)
  >>> _ = m.update_state([0, 0, 1, 1], [0, 1, 0, 1])
  >>> m.result().numpy()
  0.33333334

  >>> m.reset_states()
  >>> _ = m.update_state([0, 0, 1, 1], [0, 1, 0, 1],
  ...                    sample_weight=[0.3, 0.3, 0.3, 0.1])
  >>> m.result().numpy()
  0.23809525

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    loss='mse',
    metrics=[tf.keras.metrics.MeanIoU(num_classes=2)])
  ```
  """

  def __init__(self, num_classes, name=None, dtype=None):
    """Creates a `MeanIoU` instance.

    Args:
      num_classes: The possible number of labels the prediction task can have.
        This value must be provided, since a confusion matrix of dimension =
        [num_classes, num_classes] will be allocated.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(MeanIoU, self).__init__(name=name, dtype=dtype)
    self.num_classes = num_classes

    # Variable to accumulate the predictions in the confusion matrix. Setting
    # the type to be `float64` as required by confusion_matrix_ops.
    self.total_cm = self.add_weight(
        'total_confusion_matrix',
        shape=(num_classes, num_classes),
        initializer=init_ops.zeros_initializer,
        dtype=dtypes.float64)

  def update_state(self, y_true, y_pred, sample_weight=None):
    """Accumulates the confusion matrix statistics.

    Args:
      y_true: The ground truth values.
      y_pred: The predicted values.
      sample_weight: Optional weighting of each example. Defaults to 1. Can be a
        `Tensor` whose rank is either 0, or the same rank as `y_true`, and must
        be broadcastable to `y_true`.

    Returns:
      Update op.
    """

    y_true = math_ops.cast(y_true, self._dtype)
    y_pred = math_ops.cast(y_pred, self._dtype)

    # Flatten the input if its rank > 1.
    if y_pred.shape.ndims > 1:
      y_pred = array_ops.reshape(y_pred, [-1])

    if y_true.shape.ndims > 1:
      y_true = array_ops.reshape(y_true, [-1])

    if sample_weight is not None:
      sample_weight = math_ops.cast(sample_weight, self._dtype)
      if sample_weight.shape.ndims > 1:
        sample_weight = array_ops.reshape(sample_weight, [-1])

    # Accumulate the prediction to current confusion matrix.
    current_cm = confusion_matrix.confusion_matrix(
        y_true,
        y_pred,
        self.num_classes,
        weights=sample_weight,
        dtype=dtypes.float64)
    return self.total_cm.assign_add(current_cm)

  def result(self):
    """Compute the mean intersection-over-union via the confusion matrix."""
    sum_over_row = math_ops.cast(
        math_ops.reduce_sum(self.total_cm, axis=0), dtype=self._dtype)
    sum_over_col = math_ops.cast(
        math_ops.reduce_sum(self.total_cm, axis=1), dtype=self._dtype)
    true_positives = math_ops.cast(
        array_ops.diag_part(self.total_cm), dtype=self._dtype)

    # sum_over_row + sum_over_col =
    #     2 * true_positives + false_positives + false_negatives.
    denominator = sum_over_row + sum_over_col - true_positives

    # The mean is only computed over classes that appear in the
    # label or prediction tensor. If the denominator is 0, we need to
    # ignore the class.
    num_valid_entries = math_ops.reduce_sum(
        math_ops.cast(math_ops.not_equal(denominator, 0), dtype=self._dtype))

    iou = math_ops.div_no_nan(true_positives, denominator)

    return math_ops.div_no_nan(
        math_ops.reduce_sum(iou, name='mean_iou'), num_valid_entries)

  def reset_states(self):
    K.set_value(self.total_cm, np.zeros((self.num_classes, self.num_classes)))

  def get_config(self):
    config = {'num_classes': self.num_classes}
    base_config = super(MeanIoU, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


@keras_export('keras.metrics.MeanTensor')
class MeanTensor(Metric):
  """Computes the element-wise (weighted) mean of the given tensors.

  `MeanTensor` returns a tensor with the same shape of the input tensors. The
  mean value is updated by keeping local variables `total` and `count`. The
  `total` tracks the sum of the weighted values, and `count` stores the sum of
  the weighted counts.

  Usage:

  >>> m = tf.keras.metrics.MeanTensor()
  >>> _ = m.update_state([0, 1, 2, 3])
  >>> _ = m.update_state([4, 5, 6, 7])
  >>> m.result().numpy()
  array([2., 3., 4., 5.], dtype=float32)

  >>> _ = m.update_state([12, 10, 8, 6], sample_weight= [0, 0.2, 0.5, 1])
  >>> m.result().numpy()
  array([2.       , 3.6363635, 4.8      , 5.3333335], dtype=float32)
  """

  def __init__(self, name='mean_tensor', dtype=None):
    """Creates a `MeanTensor` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
    """
    super(MeanTensor, self).__init__(name=name, dtype=dtype)
    self._shape = None
    self._total = None
    self._count = None
    self._built = False

  def _build(self, shape):
    self._shape = tensor_shape.TensorShape(shape)
    self._build_input_shape = self._shape
    # Create new state variables
    self._total = self.add_weight(
        'total', shape=shape, initializer=init_ops.zeros_initializer)
    self._count = self.add_weight(
        'count', shape=shape, initializer=init_ops.zeros_initializer)
    with ops.init_scope():
      if not context.executing_eagerly():
        K._initialize_variables(K._get_session())  # pylint: disable=protected-access
    self._built = True

  @property
  def total(self):
    return self._total if self._built else None

  @property
  def count(self):
    return self._count if self._built else None

  def update_state(self, values, sample_weight=None):
    """Accumulates statistics for computing the element-wise mean.

    Args:
      values: Per-example value.
      sample_weight: Optional weighting of each example. Defaults to 1.

    Returns:
      Update op.
    """
    values = math_ops.cast(values, self._dtype)
    if not self._built:
      self._build(values.shape)
    elif values.shape != self._shape:
      raise ValueError('MeanTensor input values must always have the same '
                       'shape. Expected shape (set during the first call): {}. '
                       'Got: {}'.format(self._shape, values.shape))

    num_values = array_ops.ones_like(values)
    if sample_weight is not None:
      sample_weight = math_ops.cast(sample_weight, self._dtype)

      # Update dimensions of weights to match with values if possible.
      values, _, sample_weight = tf_losses_utils.squeeze_or_expand_dimensions(
          values, sample_weight=sample_weight)
      try:
        # Broadcast weights if possible.
        sample_weight = weights_broadcast_ops.broadcast_weights(
            sample_weight, values)
      except ValueError:
        # Reduce values to same ndim as weight array
        ndim = K.ndim(values)
        weight_ndim = K.ndim(sample_weight)
        values = math_ops.reduce_mean(
            values, axis=list(range(weight_ndim, ndim)))

      num_values = math_ops.multiply(num_values, sample_weight)
      values = math_ops.multiply(values, sample_weight)

    update_total_op = self._total.assign_add(values)
    with ops.control_dependencies([update_total_op]):
      return self._count.assign_add(num_values)

  def result(self):
    if not self._built:
      raise ValueError(
          'MeanTensor does not have any result yet. Please call the MeanTensor '
          'instance or use `.update_state(value)` before retrieving the result.'
          )
    return math_ops.div_no_nan(self.total, self.count)

  def reset_states(self):
    if self._built:
      K.batch_set_value(
          [(v, np.zeros(self._shape.as_list())) for v in self.variables])


@keras_export('keras.metrics.BinaryCrossentropy')
class BinaryCrossentropy(MeanMetricWrapper):
  """Computes the crossentropy metric between the labels and predictions.

  This is the crossentropy metric class to be used when there are only two
  label classes (0 and 1).

  Usage:

  >>> m = tf.keras.metrics.BinaryCrossentropy()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]])
  >>> m.result().numpy()
  0.81492424

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1], [0, 0]], [[0.6, 0.4], [0.4, 0.6]],
  ...                    sample_weight=[1, 0])
  >>> m.result().numpy()
  0.9162905

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
      'sgd',
      loss='mse',
      metrics=[tf.keras.metrics.BinaryCrossentropy()])
  ```
  """

  def __init__(self,
               name='binary_crossentropy',
               dtype=None,
               from_logits=False,
               label_smoothing=0):
    """Creates a `BinaryCrossentropy` instance.

    Args:
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      from_logits: (Optional )Whether output is expected to be a logits tensor.
        By default, we consider that output encodes a probability distribution.
      label_smoothing: (Optional) Float in [0, 1]. When > 0, label values are
        smoothed, meaning the confidence on label values are relaxed.
        e.g. `label_smoothing=0.2` means that we will use a value of `0.1` for
        label `0` and `0.9` for label `1`"
    """

    super(BinaryCrossentropy, self).__init__(
        binary_crossentropy,
        name,
        dtype=dtype,
        from_logits=from_logits,
        label_smoothing=label_smoothing)


@keras_export('keras.metrics.CategoricalCrossentropy')
class CategoricalCrossentropy(MeanMetricWrapper):
  """Computes the crossentropy metric between the labels and predictions.

  This is the crossentropy metric class to be used when there are multiple
  label classes (2 or more). Here we assume that labels are given as a `one_hot`
  representation. eg., When labels values are [2, 0, 1],
   `y_true` = [[0, 0, 1], [1, 0, 0], [0, 1, 0]].

  Usage:

  >>> # EPSILON = 1e-7, y = y_true, y` = y_pred
  >>> # y` = clip_ops.clip_by_value(output, EPSILON, 1. - EPSILON)
  >>> # y` = [[0.05, 0.95, EPSILON], [0.1, 0.8, 0.1]]
  >>> # xent = -sum(y * log(y'), axis = -1)
  >>> #      = -((log 0.95), (log 0.1))
  >>> #      = [0.051, 2.302]
  >>> # Reduced xent = (0.051 + 2.302) / 2
  >>> m = tf.keras.metrics.CategoricalCrossentropy()
  >>> _ = m.update_state([[0, 1, 0], [0, 0, 1]],
  ...                    [[0.05, 0.95, 0], [0.1, 0.8, 0.1]])
  >>> m.result().numpy()
  1.1769392

  >>> m.reset_states()
  >>> _ = m.update_state([[0, 1, 0], [0, 0, 1]],
  ...                    [[0.05, 0.95, 0], [0.1, 0.8, 0.1]],
  ...                    sample_weight=tf.constant([0.3, 0.7]))
  >>> m.result().numpy()
  1.6271976

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    loss='mse',
    metrics=[tf.keras.metrics.CategoricalCrossentropy()])
  ```

  Args:
    name: (Optional) string name of the metric instance.
    dtype: (Optional) data type of the metric result.
    from_logits: (Optional ) Whether `y_pred` is expected to be a logits tensor.
      By default, we assume that `y_pred` encodes a probability distribution.
    label_smoothing: Float in [0, 1]. When > 0, label values are smoothed,
      meaning the confidence on label values are relaxed. e.g.
      `label_smoothing=0.2` means that we will use a value of `0.1` for label
      `0` and `0.9` for label `1`"
  """

  def __init__(self,
               name='categorical_crossentropy',
               dtype=None,
               from_logits=False,
               label_smoothing=0):

    super(CategoricalCrossentropy, self).__init__(
        categorical_crossentropy,
        name,
        dtype=dtype,
        from_logits=from_logits,
        label_smoothing=label_smoothing)


@keras_export('keras.metrics.SparseCategoricalCrossentropy')
class SparseCategoricalCrossentropy(MeanMetricWrapper):
  """Computes the crossentropy metric between the labels and predictions.

  Use this crossentropy metric when there are two or more label classes.
  We expect labels to be provided as integers. If you want to provide labels
  using `one-hot` representation, please use `CategoricalCrossentropy` metric.
  There should be `# classes` floating point values per feature for `y_pred`
  and a single floating point value per feature for `y_true`.

  In the snippet below, there is a single floating point value per example for
  `y_true` and `# classes` floating pointing values per example for `y_pred`.
  The shape of `y_true` is `[batch_size]` and the shape of `y_pred` is
  `[batch_size, num_classes]`.

  Usage:

  >>> # y_true = one_hot(y_true) = [[0, 1, 0], [0, 0, 1]]
  >>> # logits = log(y_pred)
  >>> # softmax = exp(logits) / sum(exp(logits), axis=-1)
  >>> # softmax = [[0.05, 0.95, EPSILON], [0.1, 0.8, 0.1]]
  >>> # xent = -sum(y * log(softmax), 1)
  >>> # log(softmax) = [[-2.9957, -0.0513, -16.1181],
  >>> #                [-2.3026, -0.2231, -2.3026]]
  >>> # y_true * log(softmax) = [[0, -0.0513, 0], [0, 0, -2.3026]]
  >>> # xent = [0.0513, 2.3026]
  >>> # Reduced xent = (0.0513 + 2.3026) / 2
  >>> m = tf.keras.metrics.SparseCategoricalCrossentropy()
  >>> _ = m.update_state([1, 2],
  ...                    [[0.05, 0.95, 0], [0.1, 0.8, 0.1]])
  >>> m.result().numpy()
  1.1769392

  >>> m.reset_states()
  >>> _ = m.update_state([1, 2],
  ...                    [[0.05, 0.95, 0], [0.1, 0.8, 0.1]],
  ...                    sample_weight=tf.constant([0.3, 0.7]))
  >>> m.result().numpy()
  1.6271976

  Usage with tf.keras API:

  ```python
  model = tf.keras.Model(inputs, outputs)
  model.compile(
    'sgd',
    loss='mse',
    metrics=[tf.keras.metrics.SparseCategoricalCrossentropy()])
  ```

  Args:
    name: (Optional) string name of the metric instance.
    dtype: (Optional) data type of the metric result.
    from_logits: (Optional ) Whether `y_pred` is expected to be a logits tensor.
      By default, we assume that `y_pred` encodes a probability distribution.
    axis: (Optional) Defaults to -1. The dimension along which the metric is
      computed.
  """

  def __init__(self,
               name='sparse_categorical_crossentropy',
               dtype=None,
               from_logits=False,
               axis=-1):

    super(SparseCategoricalCrossentropy, self).__init__(
        sparse_categorical_crossentropy,
        name,
        dtype=dtype,
        from_logits=from_logits,
        axis=axis)


class SumOverBatchSize(Reduce):
  """Computes the weighted sum over batch size of the given values.

  For example, if values is [1, 3, 5, 7] then the metric value is 4.
  If the weights were specified as [1, 1, 0, 0] then the value would be 1.

  This metric creates two variables, `total` and `count` that are used to
  compute the average of `values`. This average is ultimately returned as sum
  over batch size which is an idempotent operation that simply divides `total`
  by `count`.

  If `sample_weight` is `None`, weights default to 1.  Use `sample_weight` of 0
  to mask values.
  """

  def __init__(self, name='sum_over_batch_size', dtype=None):
    super(SumOverBatchSize, self).__init__(
        reduction=metrics_utils.Reduction.SUM_OVER_BATCH_SIZE,
        name=name,
        dtype=dtype)


class SumOverBatchSizeMetricWrapper(SumOverBatchSize):
  """Wraps a function with the `SumOverBatchSizeMetricWrapper` metric."""

  def __init__(self, fn, name=None, dtype=None, **kwargs):
    """Creates a `SumOverBatchSizeMetricWrapper` instance.

    Args:
      fn: The metric function to wrap, with signature `fn(y_true, y_pred,
        **kwargs)`.
      name: (Optional) string name of the metric instance.
      dtype: (Optional) data type of the metric result.
      **kwargs: The keyword arguments that are passed on to `fn`.
    """
    super(SumOverBatchSizeMetricWrapper, self).__init__(name=name, dtype=dtype)
    self._fn = fn
    self._fn_kwargs = kwargs

  def update_state(self, y_true, y_pred, sample_weight=None):
    y_true = math_ops.cast(y_true, self._dtype)
    y_pred = math_ops.cast(y_pred, self._dtype)
    y_pred, y_true = tf_losses_utils.squeeze_or_expand_dimensions(
        y_pred, y_true)

    matches = self._fn(y_true, y_pred, **self._fn_kwargs)
    return super(SumOverBatchSizeMetricWrapper, self).update_state(
        matches, sample_weight=sample_weight)

  def get_config(self):
    config = {}
    for k, v in six.iteritems(self._fn_kwargs):
      config[k] = K.eval(v) if is_tensor_or_variable(v) else v
    base_config = super(SumOverBatchSizeMetricWrapper, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


def accuracy(y_true, y_pred):
  [y_pred, y_true], _ = \
      metrics_utils.ragged_assert_compatible_and_get_flat_values(
          [y_pred, y_true])
  y_pred.shape.assert_is_compatible_with(y_true.shape)
  if y_true.dtype != y_pred.dtype:
    y_pred = math_ops.cast(y_pred, y_true.dtype)
  return math_ops.cast(math_ops.equal(y_true, y_pred), K.floatx())


@keras_export('keras.metrics.binary_accuracy')
def binary_accuracy(y_true, y_pred, threshold=0.5):
  """Calculates how often predictions matches binary labels.

  Args:
    y_true: Ground truth values. shape = `[batch_size, d0, .. dN]`.
    y_pred: The predicted values. shape = `[batch_size, d0, .. dN]`.
    threshold: (Optional) Float representing the threshold for deciding whether
      prediction values are 1 or 0.

  Returns:
    Binary accuracy values. shape = `[batch_size, d0, .. dN-1]`
  """
  threshold = math_ops.cast(threshold, y_pred.dtype)
  y_pred = math_ops.cast(y_pred > threshold, y_pred.dtype)
  return K.mean(math_ops.equal(y_true, y_pred), axis=-1)


@keras_export('keras.metrics.categorical_accuracy')
def categorical_accuracy(y_true, y_pred):
  """Calculates how often predictions matches one-hot labels.

  You can provide logits of classes as `y_pred`, since argmax of
  logits and probabilities are same.

  Args:
    y_true: One-hot ground truth values.
    y_pred: The prediction values.

  Returns:
    Categorical accuracy values.
  """
  return math_ops.cast(
      math_ops.equal(
          math_ops.argmax(y_true, axis=-1), math_ops.argmax(y_pred, axis=-1)),
      K.floatx())


@keras_export('keras.metrics.sparse_categorical_accuracy')
def sparse_categorical_accuracy(y_true, y_pred):
  """Calculates how often predictions matches integer labels.

  You can provide logits of classes as `y_pred`, since argmax of
  logits and probabilities are same.

  Args:
    y_true: Integer ground truth values.
    y_pred: The prediction values.

  Returns:
    Sparse categorical accuracy values.
  """
  y_pred_rank = ops.convert_to_tensor_v2(y_pred).shape.ndims
  y_true_rank = ops.convert_to_tensor_v2(y_true).shape.ndims
  # If the shape of y_true is (num_samples, 1), squeeze to (num_samples,)
  if (y_true_rank is not None) and (y_pred_rank is not None) and (len(
      K.int_shape(y_true)) == len(K.int_shape(y_pred))):
    y_true = array_ops.squeeze(y_true, [-1])
  y_pred = math_ops.argmax(y_pred, axis=-1)

  # If the predicted output and actual output types don't match, force cast them
  # to match.
  if K.dtype(y_pred) != K.dtype(y_true):
    y_pred = math_ops.cast(y_pred, K.dtype(y_true))

  return math_ops.cast(math_ops.equal(y_true, y_pred), K.floatx())


@keras_export('keras.metrics.top_k_categorical_accuracy')
def top_k_categorical_accuracy(y_true, y_pred, k=5):
  """Computes how often targets are in the top `K` predictions.

  Args:
    y_true: The ground truth values.
    y_pred: The prediction values.
    k: (Optional) Number of top elements to look at for computing accuracy.
      Defaults to 5.

  Returns:
    Top K categorical accuracy value.
  """
  return math_ops.cast(
      nn.in_top_k(y_pred, math_ops.argmax(y_true, axis=-1), k), K.floatx())


@keras_export('keras.metrics.sparse_top_k_categorical_accuracy')
def sparse_top_k_categorical_accuracy(y_true, y_pred, k=5):
  """Computes how often integer targets are in the top `K` predictions.

  Args:
    y_true: tensor of true targets.
    y_pred: tensor of predicted targets.
    k: (Optional) Number of top elements to look at for computing accuracy.
      Defaults to 5.

  Returns:
    Sparse top K categorical accuracy value.
  """
  y_pred_rank = ops.convert_to_tensor_v2(y_pred).shape.ndims
  y_true_rank = ops.convert_to_tensor_v2(y_true).shape.ndims
  # Flatten y_pred to (batch_size, num_samples) and y_true to (num_samples,)
  if (y_true_rank is not None) and (y_pred_rank is not None):
    if y_pred_rank > 2:
      y_pred = array_ops.reshape(y_pred, [-1, y_pred.shape[-1]])
    if y_true_rank > 1:
      y_true = array_ops.reshape(y_true, [-1])

  return math_ops.cast(
      nn.in_top_k(y_pred, math_ops.cast(y_true, 'int32'), k), K.floatx())


def cosine_proximity(y_true, y_pred, axis=-1):
  """Computes the cosine similarity between labels and predictions.

  Args:
    y_true: The ground truth values.
    y_pred: The prediction values.
    axis: (Optional) Defaults to -1. The dimension along which the cosine
      similarity is computed.

  Returns:
    Cosine similarity value.
  """
  y_true = nn.l2_normalize(y_true, axis=axis)
  y_pred = nn.l2_normalize(y_pred, axis=axis)
  return math_ops.reduce_sum(y_true * y_pred, axis=axis)

# Aliases

acc = ACC = accuracy
bce = BCE = binary_crossentropy
mse = MSE = mean_squared_error
mae = MAE = mean_absolute_error
mape = MAPE = mean_absolute_percentage_error
msle = MSLE = mean_squared_logarithmic_error
cosine_similarity = cosine_proximity


def clone_metric(metric):
  """Returns a clone of the metric if stateful, otherwise returns it as is."""
  if isinstance(metric, Metric):
    with ops.init_scope():
      return metric.__class__.from_config(metric.get_config())
  return metric


def clone_metrics(metrics):
  """Clones the given metric list/dict."""
  return nest.map_structure(clone_metric, metrics)


@keras_export('keras.metrics.serialize')
def serialize(metric):
  return serialize_keras_object(metric)


@keras_export('keras.metrics.deserialize')
def deserialize(config, custom_objects=None):
  return deserialize_keras_object(
      config,
      module_objects=globals(),
      custom_objects=custom_objects,
      printable_module_name='metric function')


@keras_export('keras.metrics.get')
def get(identifier):
  """Return a metric given its identifer."""
  if isinstance(identifier, dict):
    return deserialize(identifier)
  elif isinstance(identifier, six.string_types):
    return deserialize(str(identifier))
  elif callable(identifier):
    return identifier
  else:
    error_msg = 'Could not interpret metric function identifier: {}'.format(
        identifier)
    raise ValueError(error_msg)


def is_built_in(cls):
  return cls.__module__ == Metric.__module__
