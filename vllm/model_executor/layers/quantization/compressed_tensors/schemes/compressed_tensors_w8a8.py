from typing import Callable, List, Tuple, Union

import torch
from torch.nn import Parameter

from vllm import _custom_ops as ops
from vllm.model_executor.layers.quantization.compressed_tensors.schemes import (
    CompressedTensorsScheme)
from vllm.model_executor.layers.quantization.compressed_tensors.utils import (
    QuantizationStrategy)
from vllm.model_executor.utils import set_weight_attrs


class CompressedTensorsW8A8(CompressedTensorsScheme):

    def __init__(self, strategy: str, is_static_input_scheme: bool):
        self.strategy = strategy
        self.is_static_input_scheme = is_static_input_scheme

    # Cutlass kernels support only per-tensor and per-channel cases.
    # So if we have a fused module (QKV, MLP) with per tensor scales (thus N
    # scales being passed to the kernel), we convert to the per-channel case.
    def process_weights_after_loading(self, layer: torch.nn.Module) -> None:
        if (self.strategy == QuantizationStrategy.TENSOR
                and len(self.logical_widths) > 1):

            # Load the N per-tensor scales into the channelwise buffer.
            weight_scale_channel = torch.empty(
                (sum(self.logical_widths), 1),
                dtype=torch.float32,
                device=layer.weight_scale.device)
            start = 0
            for idx, logical_width in enumerate(self.logical_widths):
                end = start + logical_width
                weight_scale_channel[start:end, :] = layer.weight_scale[idx]
                start = end

            layer.weight_scale = Parameter(weight_scale_channel,
                                           requires_grad=False)

        # transpose weights for cutlass.
        weight = layer.weight
        layer.weight = Parameter(weight.t(), requires_grad=False)

    def create_weights(self, layer: torch.nn.Module,
                       output_partition_sizes: List[int],
                       input_size_per_partition: int,
                       params_dtype: torch.dtype, weight_loader: Callable,
                       **kwargs):
        self.logical_widths = output_partition_sizes

        # WEIGHT SCALE
        shape: Union[Tuple[int], Tuple[int, int]]
        if self.strategy == QuantizationStrategy.CHANNEL:
            shape = (sum(self.logical_widths), 1)
        else:
            shape = (len(self.logical_widths), )

        weight_scale = Parameter(torch.empty(*shape, dtype=torch.float32),
                                 requires_grad=False)
        layer.register_parameter("weight_scale", weight_scale)
        if self.strategy == QuantizationStrategy.CHANNEL:
            set_weight_attrs(weight_scale, {
                "weight_loader": weight_loader,
                "output_dim": 0,
            })
        else:
            set_weight_attrs(weight_scale, {
                "weight_loader": weight_loader,
                "needs_scalar_to_array": True,
            })

        # WEIGHT
        weight = Parameter(torch.empty(sum(output_partition_sizes),
                                       input_size_per_partition,
                                       dtype=torch.int8),
                           requires_grad=False)
        layer.register_parameter("weight", weight)
        set_weight_attrs(weight, {
            "input_dim": 1,
            "output_dim": 0,
            "weight_loader": weight_loader,
        })

        # INPUT SCALE
        # Static quantization:  load from disk.
        if self.is_static_input_scheme:
            input_scale = Parameter(torch.empty(1, dtype=torch.float32),
                                    requires_grad=False)
            layer.register_parameter("input_scale", input_scale)
            set_weight_attrs(input_scale, {
                "weight_loader": weight_loader,
                "ignore_warning": True,
            })
        # Dynamic quantization: set to None.
        else:
            layer.input_scale = None

    def apply_weights(self, layer: torch.nn.Module, x: torch.Tensor):
        # ops.scaled_int8_quant supports both dynamic and static quant.
        # * dynamic, layer.input_scale is None and x_scale computed from x.
        # * static, layer.input_scale is scalar and x_scale is input_scale.
        x_q, x_scale = ops.scaled_int8_quant(x, layer.input_scale)

        return ops.cutlass_scaled_mm(x_q,
                                     layer.weight,
                                     scale_a=x_scale,
                                     scale_b=layer.weight_scale,
                                     out_dtype=x.dtype)
