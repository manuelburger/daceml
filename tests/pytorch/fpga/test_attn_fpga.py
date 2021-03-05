import torch
import numpy as np
import pytest

from daceml.pytorch import DaceModule

from dace.transformation.dataflow import RedundantSecondArray
from daceml.transformation import ConstantFolding
import daceml.onnx as donnx
donnx.default_implementation = "pure"
from dace.transformation.interstate import FPGATransformSDFG, InlineSDFG
from dace.transformation.dataflow import PruneConnectors
from dace.transformation.dataflow import streaming_memory as sm
from dace import StorageType
from dace import SDFG
import argparse
###################################################################
# Transformer configurations to be used for MHA
# Note:
# - base and large, refer to original Bert model
# - tiny and small are just for testing
# - lu20, refers to the test configuration from "Hardware Accelerator for Multi-Head Attention and
#       Position-Wise Feed-Forward in the Transformer" by Lu et al. They use the original transformer base model

# Key:
# H = #Heads
# P = #projections
# N = # features (sometimes referred as d_model)
# SM, SN = input/output sequence length
# numb_emb= 4N (after MHA, sometimes referred as feed forward filter size or d_ff)
# Typically, N = P*H
configurations = {
    "tiny": {
        "H": 4,
        "P": 8,
        "N": 32,
        "SM": 16,
        "SN": 16
    },
    "small": {
        "H": 12,
        "P": 32,
        "N": 384,
        "SM": 32,
        "SN": 32
    },
    "base": {
        "H": 12,
        "P": 64,
        "N": 768,
        "SM": 128,
        "SN": 128
    },
    "large": {
        "H": 16,
        "P": 64,
        "N": 1024,
        "SM": 512,
        "SN": 512
    },
    "lu20": {
        "H": 8,
        "P": 64,
        "N": 512,
        "SM": 64,
        "SN": 64
    },
}


@pytest.mark.ort
def test_attn(batch_size, configuration_name, execute_cpu_dace=False):

    B = batch_size
    conf = configurations[configuration_name]
    H = conf["H"]
    P = conf["P"]
    N = conf["N"]
    SM = conf["SM"]
    SN = conf["SN"]

    print("******************************************************")
    print("Executing MHA with configuration: ", configuration_name)
    print("B: ",B, " H: ", H, " P: ", P, " N: ", N, " SM: ", SM, " SN:", SN)
    print("******************************************************")

    #############

    K, Q, V = [
        torch.randn([SM, B, N]),
        torch.randn([SN, B, N]),
        torch.randn([SM, B, N])
    ]
    ptmodel = torch.nn.MultiheadAttention(N, H, bias=False)

    donnx.ONNXCast.default_implementation = "onnxruntime"

    pt_outputs = ptmodel(Q, K, V)

    if execute_cpu_dace:
        dace_model = DaceModule(ptmodel, dummy_inputs=(Q, K, V))
        # dace_outputs_0 = dace_model(Q, K, V)

    else:
        dace_model = DaceModule(ptmodel, dummy_inputs=(Q, K, V))

    dace_model.sdfg.save('/tmp/out_pre.sdfg')

    ################################################
    # Apply transformations
    dace_model.dace_model.sdfg.apply_transformations_repeated(
        [ConstantFolding, RedundantSecondArray],
        validate_all=True,
        print_report=True)
    dace_model.sdfg.save('/tmp/out.sdfg')

    if execute_cpu_dace:
        dace_outputs_1 = dace_model(Q, K, V)
        assert np.allclose(pt_outputs[0].detach().numpy(),
                           dace_outputs_1[0],
                           atol=1e-06)
        assert np.allclose(pt_outputs[1].detach().numpy(),
                           dace_outputs_1[1],
                           atol=1e-06)

    # Get the SDFG
    sdfg = dace_model.sdfg

    ###################################################
    # Transform to FPGA

    donnx.ONNXMatMul.default_implementation = "fpga"
    donnx.ONNXReshape.default_implementation = "fpga"
    donnx.ONNXSoftmax.default_implementation = "fpga"
    donnx.ONNXReduceSum.default_implementation = "fpga"

    sdfg.apply_transformations([FPGATransformSDFG])
    sdfg.expand_library_nodes()
    sdfg.save('/tmp/out_fpga_pre_inlined.sdfg')

    sdfg.apply_transformations_repeated([InlineSDFG])
    sdfg.apply_transformations_repeated(PruneConnectors)
    sdfg.save('/tmp/out_fpga.sdfg')

    # Streaming composition (Prov. disabled)
    #sdfg.apply_transformations_repeated([InlineSDFG, sm.StreamingComposition], [{}, {"storage": StorageType.FPGA_Local}], print_report=True)
    sdfg.save('/tmp/out_fpga.sdfg')

    dace_output_fpga = dace_model(Q, K, V)

    diff0 = np.linalg.norm(pt_outputs[0].detach().numpy() -
                           dace_output_fpga[0]) / dace_output_fpga[0].size
    diff1 = np.linalg.norm(pt_outputs[1].detach().numpy() -
                           dace_output_fpga[1]) / dace_output_fpga[1].size

    assert np.allclose(pt_outputs[0].detach().numpy(),
                       dace_output_fpga[0],
                       atol=1e-06)
    assert np.allclose(pt_outputs[1].detach().numpy(),
                       dace_output_fpga[1],
                       atol=1e-06)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("B",
                        type=int,
                        nargs="?",
                        default=2,
                        help="Batch size")
    parser.add_argument("conf",
                        type=str,
                        nargs="?",
                        default="tiny",
                        help="Configuration")


    args = vars(parser.parse_args())
    B = args["B"]
    conf = args["conf"]
    test_attn(B, conf, False)
