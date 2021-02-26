# Tests for matmul: many of these can be implemented by using einsum

# TODO:
# - some deadlock for small matrices, such as (2, 16, 8) (2, 8, 8), not clear why. I suspect some problem with draining conditions

from dace.transformation.interstate import FPGATransformSDFG, InlineSDFG

import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np

import daceml.onnx as donnx
from daceml.pytorch import DaceModule, dace_module
import copy
import dace
import argparse
from daceml.util import utils
from multiprocessing import Process, Queue


class Model(nn.Module):
    def __init__(self):
        super(Model, self).__init__()

    def forward(self, x,y):
        # equivalent to np.einsum('bik,bkj->bij', A, B)
        z = torch.matmul(x, y)
        return z


def run(x_shape: tuple, y_shape:tuple, vec_width = 1,
        queue=None):
    '''
    Evaluates the given configuration
    :param x_shape:
    :param y_shape:
    :param vec_width:
    :param execute_cpu_dace:
    :param queue:
    :return:
    '''

    import daceml.onnx as donnx
    donnx.default_implementation = "pure"

    ptmodel = Model()

    x = torch.rand(x_shape, dtype=torch.float32)
    y = torch.rand(y_shape, dtype=torch.float32)
    torch_output = ptmodel(x, y)

    dace_model = DaceModule(ptmodel)
    dace_output = dace_model(x, y)
    assert np.allclose(torch_output.detach().numpy(), dace_output, atol=1e-06)
    sdfg = dace_model.sdfg
    sdfg.save('/tmp/out.sdfg')
    ##################################
    # Vectorize output container and input B
    vec_type = dace.vector(dace.float32, vec_width)
    input_data_name = sdfg.states()[0].source_nodes()[1].data
    output_data_name = sdfg.states()[0].sink_nodes()[0].data
    utils.vectorize_array_and_memlet(sdfg, output_data_name, vec_type)
    utils.vectorize_array_and_memlet(sdfg, input_data_name, vec_type)
    sdfg.save('/tmp/out_vectorized.sdfg')
    # ##################################
    # Transform to FPGA
    #
    donnx.ONNXMatMul.default_implementation = "fpga"
    sdfg.apply_transformations([FPGATransformSDFG])



    ###################################################
    sdfg.expand_library_nodes()
    sdfg.apply_transformations_repeated([InlineSDFG])
    sdfg.save('/tmp/out_fpga_expanded.sdfg')
    dace_output_fpga = dace_model(x, y)
    dace_output_fpga_reshaped = dace_output_fpga.reshape(torch_output.detach().numpy().shape)
    diff = np.linalg.norm(torch_output.detach().numpy() - dace_output_fpga_reshaped) /  dace_output_fpga_reshaped.size
    print(
        "Difference: ", diff
        )

    if queue is not None:
        # we are testing
        queue.put(diff)
    else:
        if diff > 1e-6:
            import pdb
            pdb.set_trace()
            assert (False)

    del dace_model, ptmodel, x


def test():
    '''
    Evaluates multiple combination of Matmul/input size
    :return:
    '''
    print("----------- Testing Batched Matmul (3Dx3D tensor) ---------------")

    # Run FPGA tests in a different process to avoid issues with Intel OpenCL tools
    # (But not in parallel)

    # each position of this lists contains a test configuration
    vec_width = [1, 1, 1, 1, 2, 4]
    x_shapes = [(4,8,16), (8,16,32), (8,16,16), (8,16,8), (8,16,32),  (8,32,64)]
    y_shapes = [(4,16,4), (8,32,64), (8,16,8), (8,8,16),  (8,32,64), (8, 64, 16)]

    for i in range(0, len(vec_width)):
        print("##########################################################")
        print(f"# Configuration: vw={vec_width[i]}, x_shape={x_shapes[i]}, y_shape={y_shapes[i]}")
        print("##########################################################")
        queue = Queue()
        p = Process(target=run,
                    args=(x_shapes[i], y_shapes[i], vec_width[i], queue))
        p.start()
        p.join()
        assert (queue.get() < 1e-6)

    print("----------- Testing Matmul (3Dx2D tensor) ---------------")

    vec_width = [1, 1, 1, 2, 4]
    x_shapes = [(4, 8, 16), (8, 16, 32), (2, 16, 32), (16,2,32), (16,2,32), (16,2,32)]
    y_shapes = [(4, 16, 4), (32, 64), (32, 16), (32,32), (32,64), (32,16)]

    for i in range(0, len(vec_width)):
        print("##########################################################")
        print(f"# Configuration: vw={vec_width[i]}, x_shape={x_shapes[i]}, y_shape={y_shapes[i]}")
        print("##########################################################")
        queue = Queue()
        p = Process(target=run,
                    args=(x_shapes[i], y_shapes[i], vec_width[i], queue))
        p.start()
        p.join()
        assert (queue.get() < 1e-6)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("W",
                        type=int,
                        nargs="?",
                        default=1,
                        help="Vectorization width")
    parser.add_argument("-test",
                        action="store_true",
                        default=False,
                        help="Perform tests (USE ONLY WITH EMULATION)")

    args = vars(parser.parse_args())
    vec_width = args["W"]
    t = args["test"]

    #
    # vec_width = args["W"]
    if t:
        test()
    else:
        data_shape_1 = (8,32, 64)
        data_shape_2 = (8, 64,16)
        run(data_shape_1, data_shape_2, vec_width)

