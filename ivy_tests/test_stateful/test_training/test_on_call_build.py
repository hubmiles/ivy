"""
Collection of tests for training neural network layers with "on call" building
"""

# global
import pytest
import numpy as np

# local
import ivy
import ivy_tests.helpers as helpers


# Weight Conditioned Network #
# ---------------------------#

class FC(ivy.Module):

    def __init__(self, output_size=1, num_layers=2, layer_dim=64, dev=None, v=None):
        self._output_size = output_size
        self._num_layers = num_layers
        self._layer_dim = layer_dim
        super(FC, self).__init__(dev=dev, v=v,  build_mode='on_call')

    # noinspection PyUnusedLocal
    def _build(self, x, *args, **kwargs):
        input_size = x.shape[-1]
        self._layers = [ivy.Linear(input_size, self._layer_dim, dev=self._dev)]
        self._layers += [ivy.Linear(self._layer_dim, self._layer_dim, dev=self._dev)
                         for _ in range(self._num_layers - 2)]
        self._layers.append(ivy.Linear(self._layer_dim, self._output_size, dev=self._dev))

    def _forward(self, x):
        for layer in self._layers:
            x = ivy.leaky_relu(layer(x))
        return x


class WeConLayerFC(ivy.Module):

    def __init__(self, num_layers=2, layer_dim=64, dev=None, v=None):
        self._num_layers = num_layers
        self._layer_dim = layer_dim
        super(WeConLayerFC, self).__init__(dev=dev, v=v,  build_mode='on_call')

    # noinspection PyUnusedLocal
    def _build(self, implicit_weights, *args, **kwargs):
        implicit_shapes = implicit_weights.shapes
        self._layers = list()
        for i in range(self._num_layers):
            if i == 0:
                self._layers.append(implicit_shapes.map(
                    lambda shp, kc: ivy.Linear(int(np.prod(shp[1:])), self._layer_dim, dev=self._dev)))
            else:
                self._layers.append(implicit_shapes.map(
                    lambda shp, kc: ivy.Linear(self._layer_dim, self._layer_dim, dev=self._dev)))

    def _forward(self, implicit_weights):
        xs = implicit_weights
        for layer in self._layers:
            xs = ivy.Container.multi_map(lambda args, _: ivy.leaky_relu(args[0](args[1])), [layer, xs])
        return xs


class WeConFC(ivy.Module):

    def __init__(self, dev=None, v=None):
        self._layer_specific_fc = WeConLayerFC(dev=dev)
        self._fc = FC(dev=dev)
        super(WeConFC, self).__init__(dev=dev, v=v)

    # noinspection PyUnusedLocal
    def _build(self, *args, **kwargs):
        self._layer_specific_fc.build()
        self._fc.build()
        return self._layer_specific_fc.built and self._fc.built

    def _forward(self, implicit_weights):
        batch_shape = [i for i in implicit_weights.shape if i]
        total_batch_size = np.prod(batch_shape)
        reshaped_weights = implicit_weights.reshape(pre_shape=[total_batch_size], post_shape=[-1])
        xs = self._layer_specific_fc(reshaped_weights)
        x = ivy.concatenate([v for k, v in xs.to_iterator()], -1)
        ret_flat = self._fc(x)
        return ivy.reshape(ret_flat, batch_shape + [-1])


# WeConFC
@pytest.mark.parametrize(
    "batch_shape", [[1, 2]])
@pytest.mark.parametrize(
    "dtype", ['float32'])
@pytest.mark.parametrize(
    "tensor_fn", [ivy.array, helpers.var_fn])
def test_weight_conditioned_network_training(batch_shape, dtype, tensor_fn, dev, call):

    # smoke test
    if call is helpers.np_call:
        # NumPy does not support gradients
        pytest.skip()
    x = ivy.Container({'layer0': {'w': ivy.random_uniform(shape=batch_shape+[64, 3], dev=dev),
                                  'b': ivy.random_uniform(shape=batch_shape+[64], dev=dev)},
                       'layer1': {'w': ivy.random_uniform(shape=batch_shape+[1, 64], dev=dev),
                                  'b': ivy.random_uniform(shape=batch_shape+[1], dev=dev)}})
    we_con_net = WeConFC(dev=dev)

    def loss_fn(v_=None):
        out = we_con_net(x, v=v_)
        return ivy.reduce_mean(out)[0]

    # train
    loss_tm1 = 1e12
    loss = None
    grads = None
    loss_fn()  # build on_call layers
    for i in range(10):
        loss, grads = ivy.execute_with_gradients(loss_fn, we_con_net.v)
        we_con_net.v = ivy.gradient_descent_update(we_con_net.v, grads, 1e-3)
        assert loss < loss_tm1
        loss_tm1 = loss

    # type test
    assert ivy.is_array(loss)
    assert isinstance(grads, ivy.Container)
    # cardinality test
    if call is helpers.mx_call:
        # mxnet slicing cannot reduce dimension to zero
        assert loss.shape == (1,)
    else:
        assert loss.shape == ()
    # value test
    assert (abs(grads).reduce_max() > 0).all_true()
    # compilation test
    if call is helpers.torch_call:
        # pytest scripting does not **kwargs
        return
    if not ivy.array_mode():
        helpers.assert_compilable(loss_fn)


# HyperNetwork #
# -------------#

class HyperNet(ivy.Module):

    def __init__(self, num_layers=3, layer_dim=64, latent_size=256, dev=None, v=None):
        self._num_layers = num_layers
        self._layer_dim = layer_dim
        self._latent_size = latent_size
        super(HyperNet, self).__init__(dev=dev, v=v,  build_mode='on_call')

    def _create_variables(self, dev):
        return {'latent': ivy.variable(ivy.random_uniform(shape=(self._latent_size,), dev=dev))}

    # noinspection PyUnusedLocal
    def _build(self, hypo_shapes, *args, **kwargs):
        self._layers = list()
        for i in range(self._num_layers):
            if i == 0:
                self._layers.append(ivy.Linear(self._latent_size, self._layer_dim, dev=self._dev))
            if i < self._num_layers - 1:
                self._layers.append(ivy.Linear(self._layer_dim, self._layer_dim, dev=self._dev))
            else:
                self._layers.append(hypo_shapes.map(
                    lambda shp, kc: ivy.Linear(self._layer_dim, int(np.prod(shp)), dev=self._dev)))

    def _forward(self, hypo_shapes):
        x = self.v.latent
        for layer in self._layers[:-1]:
            x = ivy.leaky_relu(layer(x))
        weights_flat = self._layers[-1].map(lambda lyr, _: ivy.leaky_relu(lyr(x)))
        return weights_flat.reshape_like(hypo_shapes)


class HypoNet(ivy.Module):

    def __init__(self, input_size=1, output_size=1, num_layers=2, layer_dim=64, dev=None, v=None):
        self._input_size = input_size
        self._output_size = output_size
        self._num_layers = num_layers
        self._layer_dim = layer_dim
        super(HypoNet, self).__init__(dev=dev, v=v, store_vars=False)

    # noinspection PyUnusedLocal
    def _build(self, *args, **kwargs):
        self._layers = [ivy.Linear(self._input_size, self._layer_dim, dev=self._dev)]
        self._layers += [ivy.Linear(self._layer_dim, self._layer_dim, dev=self._dev)
                         for _ in range(self._num_layers - 2)]
        self._layers.append(ivy.Linear(self._layer_dim, self._output_size, dev=self._dev))

    def _forward(self, x):
        for layer in self._layers:
            x = ivy.leaky_relu(layer(x))
        return x


class HyperHypoNet(ivy.Module):

    def __init__(self, dev=None, v=None):
        self._hypernet = HyperNet(dev=dev)
        self._hyponet = HypoNet(dev=dev)
        super(HyperHypoNet, self).__init__(dev=dev, v=v)

    # noinspection PyUnusedLocal
    def _build(self, *args, **kwargs):
        self._hypernet.build()
        hypo_v = self._hyponet.build()
        self._hypo_shapes = hypo_v.shapes
        return self._hypernet.built and self._hyponet.built

    def _forward(self, hyponet_input):
        return self._hyponet(hyponet_input, v=self._hypernet(self._hypo_shapes))


# HyperHypoNet
@pytest.mark.parametrize(
    "batch_shape", [[1, 2]])
@pytest.mark.parametrize(
    "dtype", ['float32'])
@pytest.mark.parametrize(
    "tensor_fn", [ivy.array, helpers.var_fn])
def test_hyper_hypo_network_training(batch_shape, dtype, tensor_fn, dev, call):

    # smoke test
    if call is helpers.np_call:
        # NumPy does not support gradients
        pytest.skip()
    x = ivy.random_uniform(shape=batch_shape + [1], dev=dev)
    hyper_hypo_net = HyperHypoNet(dev=dev)

    def loss_fn(v_=None):
        out = hyper_hypo_net(x, v=v_)
        return ivy.reduce_mean(out)[0]

    # train
    loss_tm1 = 1e12
    loss = None
    grads = None
    loss_fn()  # build on_call layers
    for i in range(10):
        loss, grads = ivy.execute_with_gradients(loss_fn, hyper_hypo_net.v)
        hyper_hypo_net.v = ivy.gradient_descent_update(hyper_hypo_net.v, grads, 1e-3)
        assert loss < loss_tm1
        loss_tm1 = loss

    # type test
    assert ivy.is_array(loss)
    assert isinstance(grads, ivy.Container)
    # cardinality test
    if call is helpers.mx_call:
        # mxnet slicing cannot reduce dimension to zero
        assert loss.shape == (1,)
    else:
        assert loss.shape == ()
    # value test
    assert (abs(grads).reduce_max() > 0).all_true()
    # compilation test
    if call is helpers.torch_call:
        # pytest scripting does not **kwargs
        return
    if not ivy.array_mode():
        helpers.assert_compilable(loss_fn)
