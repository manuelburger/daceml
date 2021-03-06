import collections
import logging
import os
import tempfile
from functools import wraps
from typing import Optional, Tuple, Callable, OrderedDict

import dace
import onnx
import torch
import torch.nn as nn
from torch.onnx import TrainingMode

from daceml.autodiff.pytorch import make_backward_function
from daceml.onnx import ONNXModel
from daceml.onnx.shape_inference import infer_shapes
from daceml.util import utils


class DaceModule(nn.Module):
    """ A wrapper that converts a PyTorch ``nn.Module`` to a PyTorch compatible data-centric ``nn.Module``.

        :param module: the model to wrap.
        :param dummy_inputs: a tuple of tensors to use as input when tracing ``model``.
        :param cuda: if ``True``, the module will execute using CUDA.
        :param train: whether to use train mode when tracing ``model``.
        :param backward: whether to enable the backward pass.
        :param apply_strict: whether to apply strict transforms after conversion (this generally improves performance,
                             but can be slow).
        :param sdfg_name: the name to give to the sdfg (defaults to ``dace_model``).
        :param auto_optimize: whether to apply automatic optimizations.

        :Example:

            >>> from daceml.pytorch import DaceModule
            >>> class MyModule(nn.Module):
            ...     def forward(self, x):
            ...        x = torch.log(x)
            ...        x = torch.sqrt(x)
            ...        return x
            >>> module = MyModule()
            >>> module(torch.ones(2))
            tensor([0., 0.])
            >>> dace_module = DaceModule(module)
            >>> dace_module(torch.ones(2))
            Automatically expanded library node "ONNX_Log_0" with implementation "onnxruntime".
            Automatically expanded library node "ONNX_Sqrt_1" with implementation "onnxruntime".
            tensor([0., 0.])
    """
    def __init__(self,
                 module: nn.Module,
                 dummy_inputs: Optional[Tuple[torch.Tensor]] = None,
                 cuda: bool = False,
                 train: bool = False,
                 backward=False,
                 apply_strict: bool = True,
                 auto_optimize: bool = True,
                 sdfg_name: Optional[str] = None):
        super(DaceModule, self).__init__()

        self.backward = backward
        self.model = module
        self.dace_model: Optional[ONNXModel] = None
        self.train = train
        self.sdfg: Optional[dace.SDFG] = None
        self.cuda = cuda
        self.sdfg_name = sdfg_name or "dace_model"

        self.function = None

        #: hooks that are executed after onnx graph is imported to an SDFG
        self.post_onnx_hooks: OrderedDict[str, Callable[
            [ONNXModel], None]] = collections.OrderedDict()

        #: hooks that are executed after the backpropagation sdfg has been created
        self.post_autodiff_hooks: OrderedDict[str, Callable[
            [dace.SDFG, dace.SDFG], None]] = collections.OrderedDict()

        # setup optimization hooks
        if auto_optimize:
            if self.backward:

                def auto_optimize_backward(fwd_sdfg, bwd_sdfg):
                    utils.auto_optimize(fwd_sdfg,
                                        self.cuda,
                                        apply_strict=apply_strict)
                    utils.auto_optimize(bwd_sdfg,
                                        self.cuda,
                                        apply_strict=apply_strict)

                self.post_autodiff_hooks[
                    "auto_optimize"] = auto_optimize_backward
            else:
                self.post_onnx_hooks["auto_optimize"] = \
                    lambda onnx_model: utils.auto_optimize(onnx_model.sdfg,
                                                           self.cuda,
                                                           apply_strict=apply_strict)
        elif apply_strict:
            if self.backward:

                def apply_strict(fwd_sdfg, bwd_sdfg):
                    fwd_sdfg.apply_strict_transformations()
                    bwd_sdfg.apply_strict_transformations()

                self.post_autodiff_hooks["apply_strict"] = apply_strict
            else:
                self.post_onnx_hooks["apply_strict"] = \
                    lambda onnx_model: onnx_model.sdfg.apply_strict_transformations()

        if dummy_inputs is not None:
            self.function = self._initialize_sdfg(dummy_inputs)

    def reset_sdfg(self):
        """ Clear the sdfg so that optimizations are reapplied. """
        self.function = None

    def prepend_post_onnx_hook(self, name: str, func: Callable[[ONNXModel],
                                                               None]):
        self.post_onnx_hooks[name] = func
        self.post_onnx_hooks.move_to_end(name, last=False)

    def append_post_onnx_hook(self, name: str, func: Callable[[ONNXModel],
                                                              None]):
        self.post_onnx_hooks[name] = func

    def prepend_post_autodiff_hook(self, name: str,
                                   func: Callable[[dace.SDFG, dace.SDFG],
                                                  None]):
        self.post_autodiff_hooks[name] = func
        self.post_autodiff_hooks.move_to_end(name, last=False)

    def append_post_autodiff_hook(self, name: str,
                                  func: Callable[[dace.SDFG, dace.SDFG],
                                                 None]):
        self.post_autodiff_hooks[name] = func

    def _initialize_sdfg(self, dummy_inputs):
        # TODO change to StringIO if not too big
        with tempfile.TemporaryDirectory() as dir_name:
            export_name = os.path.join(dir_name, "export.onnx")

            torch.onnx.export(
                self.model,
                dummy_inputs,
                export_name,
                verbose=logging.root.level <= logging.DEBUG,
                training=(TrainingMode.TRAINING
                          if self.train else TrainingMode.EVAL),
                opset_version=12,
                strip_doc_string=False,
                export_params=not self.backward,
                # pytorch constant folding will add new unnamed inputs to the graph and remove some of the
                # named parameters of the model: this means that we can't match with the state dict
                # anymore, so we disable this. Our CF is more flexible.
                do_constant_folding=False)

            onnx_model = infer_shapes(onnx.load(export_name))
            self.onnx_model = onnx_model

            dace_model = ONNXModel(self.sdfg_name,
                                   onnx_model,
                                   infer_shapes=False,
                                   cuda=self.cuda,
                                   parent_pytorch_module=self.model)
            self.sdfg = dace_model.sdfg
            self.dace_model = dace_model

            self.sdfg.validate()

            for _, hook in self.post_onnx_hooks.items():
                hook(self.dace_model)

            if self.backward:
                function = make_backward_function(dace_model)

                for _, hook in self.post_autodiff_hooks.items():
                    hook(function._forward_model.sdfg, function._backward_sdfg)

                def forward(*args):
                    args_and_params = list(args)
                    args_and_params.extend(self.parameters())
                    return function.apply(*args_and_params)

                return forward
            else:

                return dace_model

    def forward(self, *actual_inputs):
        """ Execute the forward pass using the traced ``module``."""
        if self.function is None:
            self.function = self._initialize_sdfg(actual_inputs)

        return self.function(*actual_inputs)


@dace.dtypes.paramdec
def dace_module(moduleclass,
                dummy_inputs: Optional[Tuple[torch.Tensor]] = None,
                cuda: bool = False,
                train: bool = False,
                backward=False,
                apply_strict: bool = True,
                auto_optimize: bool = True,
                sdfg_name: Optional[str] = None):
    """ Decorator to apply on a definition of a ``torch.nn.Module`` to
        convert it to a data-centric module upon construction.

        :Example:

            >>> from daceml.pytorch import dace_module
            >>> @dace_module
            ... class MyModule(nn.Module):
            ...     def forward(self, x):
            ...        x = torch.log(x)
            ...        x = torch.sqrt(x)
            ...        return x
            >>> module = MyModule()
            >>> module(torch.ones(2))
            Automatically expanded library node "ONNX_Log_0" with implementation "onnxruntime".
            Automatically expanded library node "ONNX_Sqrt_1" with implementation "onnxruntime".
            tensor([0., 0.])

        :param moduleclass: the model to wrap.
        :param dummy_inputs: a tuple of tensors to use as input when tracing ``model``.
        :param cuda: if ``True``, the module will execute using CUDA.
        :param train: whether to use train mode when tracing ``model``.
        :param backward: whether to enable the backward pass.
        :param apply_strict: whether to apply strict transforms after conversion (this generally improves performance,
                             but can be slow).
        :param auto_optimize: whether to apply automatic optimizations.
        :param sdfg_name: the name to give to the sdfg (defaults to ``dace_model``).
    """
    @wraps(moduleclass)
    def _create(*args, **kwargs):
        return DaceModule(moduleclass(*args, **kwargs),
                          dummy_inputs=dummy_inputs,
                          cuda=cuda,
                          train=train,
                          backward=backward,
                          apply_strict=apply_strict,
                          auto_optimize=auto_optimize,
                          sdfg_name=sdfg_name)

    return _create
